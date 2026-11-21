#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deepseek_deep_think.py — DeepSeek 深度思考搜索 CLI（方案 A）

方案 A 的含义：把整个「多步检索」外包给 DeepSeek 服务端。

  * 客户端**不执行任何工具**：模型在服务端自己生成检索词、打开网页、页内查找，
    边推理边求证，最后带引用作答；
  * 本地脚本只做三件事：发一次对话请求 -> 解析响应 -> 输出精简视图；
  * 服务端搜索/抓取**不按次计费**，只有模型 token 计费（含思考 token）。

因此本脚本只调用对话端点（chat / messages / responses），
**从不调用** `/v1/tools/search` 或 `/v1/tools/fetch`。

输出契约（默认视图只有两样东西，见 SKILL.md §16）：

    {"answer": "...", "citations": [{"url": "...", "title": "..."}]}

思考文本、工具轨迹默认丢弃；需要时用 `--summary` / `--verbose` 显式索取，
且 `--verbose` 的结果**不要回传到后续上下文**。

只依赖 Python 3.7+ 标准库。本文件不 import deepseek_search.py，
自带密钥读取 / HTTP / 错误映射，可单独运行。

运行期产生的文件（历史状态、日志、原始响应 dump）一律落在
`<skill>/.cache/deep-think/` 下，不写到 skill 目录之外。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")

# 协议 -> (端点路径, 鉴权方式)
PROTOCOLS = {
    "chat": "/chat/completions",
    "messages": "/anthropic/v1/messages",
    "responses": "/responses",
}

DEFAULT_MODEL = "deepseek-flash"
WEB_SEARCH_TOOL = "deepseek_web_search"

# 思考强度：官方映射 low/high/max（默认 high）。low 更早收敛、更省 token，
# 代价是复杂问题的推理深度下降。三协议传参位置不同，见 build_payload()。
DEFAULT_EFFORT = "high"
EFFORTS = ("low", "high", "max")

# 可选的软收敛提示（--converge）：只是提示词，不限制步数（步数不可参数化）
CONVERGE_PROMPT = (
    "一旦已有足够信息回答问题，立即停止检索并作答；"
    "若连续几步检索没有产生新的有效信息，直接基于现有证据给出结论，并注明不确定之处。"
    "回答保持简洁。"
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_API = 2

# 运行期产物目录（只写 skill 目录内部）
ARTIFACT_DIRNAME = os.path.join(".cache", "deep-think")
DEFAULT_HISTORY = os.path.join(ARTIFACT_DIRNAME, "history.json")
DEFAULT_LOG = os.path.join(ARTIFACT_DIRNAME, "deep-think.log.jsonl")
DEFAULT_RAW_DIR = os.path.join(ARTIFACT_DIRNAME, "raw")

# 单次深度思考搜索最多约 20 次工具调用；给足超时，但别无限等
DEFAULT_TIMEOUT = 180.0
MAX_TITLE_CHARS = 80
DEFAULT_MAX_CITATIONS = 8
DEFAULT_THINKING_CHARS = 120

# 官方文档中列出的 400 错误文案 -> 可读提示
ERROR_HINTS = (
    ("can only be declared once", "`deepseek_web_search` 被重复声明了：tools 里只能出现一次"),
    (
        "cannot be used together with",
        "`deepseek_web_search` 不能与其他服务端工具（含 web_search / custom / namespace 等）同时声明",
    ),
    (
        "allowed_domains",
        "`allowed_domains` / `blocked_domains` / `user_location` 不被 `deepseek_web_search` 支持",
    ),
    (
        "cannot be forced",
        "`tool_choice` 不能强制指定联网工具：是否搜索由模型判断（本脚本从不发送 tool_choice）",
    ),
    (
        "cannot be continued",
        "上一轮 assistant 用过联网工具，不能续写：请重发原始请求，而不是续写",
    ),
    (
        "Content Exists Risk",
        "输入命中内容审核（Content Exists Risk）：换个问法或去掉敏感内容",
    ),
)


# --------------------------------------------------------------------------- #
# 错误类型与输出
# --------------------------------------------------------------------------- #
class UsageError(Exception):
    """参数 / 用法错误 -> exit 1"""


class ConfigError(Exception):
    """缺少密钥、产物目录不可写等配置问题 -> exit 1"""


class NetworkError(Exception):
    """网络层失败 -> exit 2"""


class ApiError(Exception):
    """接口返回非 200 -> exit 2"""

    def __init__(self, status, message=""):
        super(ApiError, self).__init__(message or ("HTTP %s" % status))
        self.status = status
        self.message = message or ""


def err(msg):
    sys.stderr.write("ERROR: %s\n" % msg)


def warn(msg):
    sys.stderr.write("WARN: %s\n" % msg)


def note(msg):
    sys.stderr.write("%s\n" % msg)


def clip(text, limit):
    """按字符数截断，超出时补省略号。"""
    if not text:
        return ""
    if not limit or limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def dump_json(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# 密钥
# --------------------------------------------------------------------------- #
def _read_key_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    except OSError as exc:
        raise ConfigError("无法读取密钥文件 %s：%s" % (path, exc))
    if not key:
        raise ConfigError("密钥文件为空：%s" % path)
    return key


def resolve_key(cli_path=None):
    """优先级：--key-file > 环境变量 DEEPSEEK_API_KEY > skill 根目录 DeepSeek_API_Key。"""
    if cli_path:
        return _read_key_file(cli_path)
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return env
    default = os.environ.get("DEEPSEEK_API_KEY_FILE") or os.path.join(SKILL_ROOT, "DeepSeek_API_Key")
    if not os.path.isfile(default):
        raise ConfigError(
            "没有密钥配置：未找到 %s。请在该路径放置 API Key 文件，"
            "或设置环境变量 DEEPSEEK_API_KEY / 使用 --key-file" % default
        )
    return _read_key_file(default)


# --------------------------------------------------------------------------- #
# 运行期产物路径（只写 skill 目录内部）
# --------------------------------------------------------------------------- #
def artifact_path(path=None, default=None):
    """把相对路径解析到 skill 根目录下；默认值即 ARTIFACT_DIRNAME 内部文件。"""
    target = path or default
    if not target:
        raise UsageError("缺少产物路径")
    if not os.path.isabs(target):
        target = os.path.join(SKILL_ROOT, target)
    target = os.path.abspath(target)
    root = os.path.abspath(SKILL_ROOT)
    try:
        inside = os.path.commonpath([root, target]) == root
    except ValueError:  # 不同盘符（Windows）
        inside = False
    if not inside:
        warn("产物路径不在 skill 目录内：%s（默认应写 %s）" % (target, ARTIFACT_DIRNAME))
    return target


def ensure_parent(path):
    parent = os.path.dirname(path)
    if not parent:
        return
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise ConfigError("无法创建目录 %s：%s" % (parent, exc))


def write_atomic(path, text):
    """同目录临时文件 + os.replace，避免半截文件。"""
    ensure_parent(path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ConfigError("无法写入 %s：%s" % (path, exc))


def append_log(path, record):
    ensure_parent(path)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        warn("日志写入失败（忽略）：%s" % exc)


# --------------------------------------------------------------------------- #
# 请求构造
# --------------------------------------------------------------------------- #
def build_payload(protocol, model, items, stream=False, max_tokens=0, effort=DEFAULT_EFFORT, converge=False):
    """构造请求体。只放必要字段：model / 思考开关与强度 / 单个联网工具 / 消息 / 流式开关。

    硬约束（官方文档）：
      * 不传 tool_choice（联网工具不能被强制指定）；
      * 不传 allowed_domains / blocked_domains / user_location；
      * 不传 web_search_options / response_format / logprobs / top_logprobs；
      * 不传 temperature / presence_penalty / frequency_penalty（思考模式下无效）；
      * 只声明一个 deepseek_web_search。

    思考强度参数位置（官方 guides/thinking_mode）：
      chat      -> 顶层 reasoning_effort
      messages  -> output_config.effort
      responses -> reasoning.effort
    """
    messages = list(items or [])
    if protocol == "chat":
        payload = {
            "model": model,
            "thinking": {"type": "enabled"},
            "tools": [{"type": WEB_SEARCH_TOOL}],
            "messages": messages,
        }
        if effort:
            payload["reasoning_effort"] = effort
        if converge:
            payload["messages"] = [{"role": "system", "content": CONVERGE_PROMPT}] + messages
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
    elif protocol == "messages":
        payload = {
            "model": model,
            "thinking": {"type": "enabled"},
            "tools": [{"type": WEB_SEARCH_TOOL, "name": WEB_SEARCH_TOOL}],
            "messages": messages,
        }
        if effort:
            payload["output_config"] = {"effort": effort}
        if converge:
            payload["system"] = CONVERGE_PROMPT
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
    elif protocol == "responses":
        payload = {
            "model": model,
            "reasoning": {"effort": effort or DEFAULT_EFFORT},
            "tools": [{"type": WEB_SEARCH_TOOL}],
            "input": messages,
        }
        if converge:
            payload["instructions"] = CONVERGE_PROMPT
        if max_tokens:
            payload["max_output_tokens"] = int(max_tokens)
    else:
        raise UsageError("未知协议：%s" % protocol)
    if stream:
        payload["stream"] = True
    return payload


def build_headers(protocol, api_key, stream=False):
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": "deepseek-deep-think-skill/" + VERSION,
    }
    if protocol == "messages":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = "Bearer " + api_key
    return headers


def masked_headers(headers):
    out = {}
    for key, value in headers.items():
        if key.lower() in ("authorization", "x-api-key"):
            out[key] = "***"
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def error_message(body):
    try:
        obj = json.loads((body or "").lstrip())
    except Exception:
        return (body or "").strip()[:300]
    if isinstance(obj, dict):
        e = obj.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e.get("code") or "").strip()
        if isinstance(e, str):
            return e.strip()
    return ""


def _retry_after(headers):
    raw = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.isdigit():
        return max(1, min(int(raw), 20))
    return None


def _open(url, payload, headers, timeout):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _read_http_error(exc):
    body = ""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        pass
    return exc.code, body, dict(exc.headers or {})


def post_json(url, payload, headers, timeout):
    """非流式 POST，返回解析后的 JSON。408/429/5xx 退避重试，402 与其他 4xx 不重试。"""
    last_status, last_message = 0, ""
    for attempt in (1, 2, 3):
        try:
            with _open(url, payload, headers, timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                status = resp.getcode()
        except urllib.error.HTTPError as exc:
            status, body, hdrs = _read_http_error(exc)
            last_status, last_message = status, error_message(body)
            if status in (408, 429) or 500 <= status <= 599:
                if attempt == 3:
                    break
                time.sleep(_retry_after(hdrs) or min(2 * attempt, 10))
                continue
            raise ApiError(status, last_message)
        except urllib.error.URLError as exc:
            last_status, last_message = 0, str(getattr(exc, "reason", exc))
            if attempt == 3:
                raise NetworkError("网络请求失败（已重试 3 次）：%s" % last_message)
            time.sleep(min(2 * attempt, 10))
            continue
        except OSError as exc:
            last_status, last_message = 0, str(exc)
            if attempt == 3:
                raise NetworkError("网络请求失败（已重试 3 次）：%s" % last_message)
            time.sleep(min(2 * attempt, 10))
            continue

        if status == 200:
            try:
                # 非流式请求可能在 JSON 之前收到换行符（官方文档：限速与隔离）
                return json.loads(body.lstrip())
            except ValueError:
                raise ApiError(200, "服务器返回的不是合法 JSON：%s" % (body or "")[:200])
        last_status, last_message = status, error_message(body)
        if status in (408, 429) or 500 <= status <= 599:
            if attempt == 3:
                break
            time.sleep(min(2 * attempt, 10))
            continue
        raise ApiError(status, last_message)
    raise ApiError(last_status, last_message)


def iter_sse(url, payload, headers, timeout):
    """流式 POST，逐条 yield (event, data_dict)。

    连接阶段的 408/429/5xx 才重试；已经开始收事件后不再重试（避免重复生成与重复计费）。
    """
    for attempt in (1, 2, 3):
        try:
            resp = _open(url, payload, headers, timeout)
        except urllib.error.HTTPError as exc:
            status, body, hdrs = _read_http_error(exc)
            if (status in (408, 429) or 500 <= status <= 599) and attempt < 3:
                time.sleep(_retry_after(hdrs) or min(2 * attempt, 10))
                continue
            raise ApiError(status, error_message(body))
        except urllib.error.URLError as exc:
            message = str(getattr(exc, "reason", exc))
            if attempt < 3:
                time.sleep(min(2 * attempt, 10))
                continue
            raise NetworkError("网络请求失败（已重试 3 次）：%s" % message)
        except OSError as exc:
            if attempt < 3:
                time.sleep(min(2 * attempt, 10))
                continue
            raise NetworkError("网络请求失败（已重试 3 次）：%s" % exc)
        break

    try:
        event = None
        data_lines = []
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        yield event, _sse_json(data_lines)
                        event, data_lines = None, []
                    continue
                if line.startswith(":"):
                    continue  # keep-alive 注释
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                # 容错：没有 event 前缀的裸 data 行
                data_lines.append(line)
            if data_lines:
                yield event, _sse_json(data_lines)
    except OSError as exc:
        raise NetworkError("流式读取中断：%s" % exc)


def _sse_json(data_lines):
    raw = "\n".join(data_lines).strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# usage 归一化（三协议字段名不同，统一成一套）
# --------------------------------------------------------------------------- #
def _dig(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _first(obj, *paths):
    for path in paths:
        value = _dig(obj, path)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def normalize_usage(usage):
    usage = usage or {}
    stu = usage.get("server_tool_use") or {}
    return {
        "input": _first(usage, "input_tokens", "prompt_tokens"),
        "cache_read": _first(
            usage,
            "cache_read_input_tokens",
            "input_tokens_details.cached_tokens",
            "prompt_tokens_details.cached_tokens",
        ),
        "cache_creation": _first(usage, "cache_creation_input_tokens"),
        "output": _first(usage, "output_tokens", "completion_tokens"),
        "thinking": _first(
            usage,
            "output_tokens_details.thinking_tokens",
            "output_tokens_details.reasoning_tokens",
            "completion_tokens_details.thinking_tokens",
            "completion_tokens_details.reasoning_tokens",
        ),
        "tool_search": int(stu.get("tool_search") or 0),
        "tool_fetch": int(stu.get("tool_fetch") or 0),
        "tool_image_search": int(stu.get("tool_image_search") or 0),
        "web_search_requests": int(stu.get("web_search_requests") or 0),
        "web_fetch_requests": int(stu.get("web_fetch_requests") or 0),
    }


def usage_line(usage):
    if not usage:
        return "usage: （接口未返回）"
    return (
        "usage: input=%d cache_read=%d output=%d thinking=%d | "
        "server_tool_use: tool_search=%d tool_fetch=%d tool_image_search=%d（服务端工具不按次计费）"
        % (
            usage.get("input", 0),
            usage.get("cache_read", 0),
            usage.get("output", 0),
            usage.get("thinking", 0),
            usage.get("tool_search", 0),
            usage.get("tool_fetch", 0),
            usage.get("tool_image_search", 0),
        )
    )


# --------------------------------------------------------------------------- #
# 解析：统一结果结构
# --------------------------------------------------------------------------- #
class Result(object):
    """一次深度思考搜索的解析结果（原始形态，未做视图裁剪）。"""

    def __init__(self):
        self.answer = ""
        self.citations = []      # [{url, title, cited_text}]
        self.steps = []          # [{action, query, url, pattern, status, result_count, kind}]
        self.thinking = []       # 每步思考（原始文本）
        self.usage = {}
        self.finish_reason = ""
        self.error = ""
        self.raw_assistant = None  # 回传用：chat=message, messages={role,content[]}, responses=[items]


def _step(action, kind, status="completed", result_count=0, **extra):
    item = {
        "action": action or "",
        "kind": kind or "",
        "status": status or "",
        "result_count": result_count,
    }
    item.update({k: v for k, v in extra.items() if v not in (None, "", [])})
    return item


def _count_sources(container):
    if isinstance(container, list):
        return len(container)
    return 0


def parse_chat(obj):
    """Chat Completions 响应 -> Result。"""
    res = Result()
    if not isinstance(obj, dict):
        res.error = "响应不是 JSON 对象"
        return res
    res.usage = normalize_usage(obj.get("usage"))

    body_error = obj.get("error")
    choices = obj.get("choices") or []
    if body_error:
        res.error = error_message(json.dumps(obj)) or "生成失败（响应体含 error）"
        return res
    if not choices:
        res.error = "响应缺少 choices（可能生成失败）"
        return res

    choice = choices[0] or {}
    message = choice.get("message") or {}
    res.answer = message.get("content") or ""
    res.finish_reason = choice.get("finish_reason") or ""

    for ann in message.get("annotations") or []:
        citation = ann.get("url_citation") if isinstance(ann, dict) else None
        if isinstance(citation, dict):
            res.citations.append(
                {
                    "url": citation.get("url") or "",
                    "title": citation.get("title") or "",
                    "cited_text": "",
                }
            )

    for step_block in message.get("server_tool_calls") or []:
        reasoning = step_block.get("reasoning_content")
        if reasoning:
            res.thinking.append(reasoning)
        for call in step_block.get("calls") or []:
            res.steps.append(_chat_step(call))
    if message.get("reasoning_content"):
        res.thinking.append(message["reasoning_content"])

    # 回传用原文：保留 server_tool_calls 里的 kind / signature
    res.raw_assistant = {
        "role": "assistant",
        "content": res.answer,
    }
    if message.get("reasoning_content") is not None:
        res.raw_assistant["reasoning_content"] = message.get("reasoning_content")
    if message.get("server_tool_calls"):
        res.raw_assistant["server_tool_calls"] = message["server_tool_calls"]
    if message.get("annotations"):
        res.raw_assistant["annotations"] = message["annotations"]
    return res


def _chat_step(call):
    if not isinstance(call, dict):
        return _step("", "")
    action = call.get("action") or {}
    action_type = action.get("type") or call.get("kind") or ""
    queries = action.get("queries") or []
    sources = call.get("sources")
    result_count = _count_sources(sources)
    page = call.get("page") or {}
    if action_type == "search" and not result_count:
        result_count = 0
    return _step(
        action_type,
        call.get("kind"),
        status=call.get("status"),
        result_count=result_count,
        query=" / ".join([q for q in queries if isinstance(q, str)]),
        url=action.get("url") or page.get("url"),
        pattern=action.get("pattern"),
        error=(call.get("error") or {}).get("code") if isinstance(call.get("error"), dict) else None,
    )


def parse_messages(obj):
    """Messages API 响应 -> Result。"""
    res = Result()
    if not isinstance(obj, dict):
        res.error = "响应不是 JSON 对象"
        return res
    res.usage = normalize_usage(obj.get("usage"))
    if obj.get("type") == "error":
        res.error = error_message(json.dumps(obj)) or "生成失败（响应体 type=error）"
        return res

    blocks = obj.get("content") or []
    if not isinstance(blocks, list):
        res.error = "响应缺少 content 数组"
        return res
    res.finish_reason = obj.get("stop_reason") or ""

    calls = {}  # tool_use_id -> step dict
    texts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            thinking = block.get("thinking")
            if thinking:
                res.thinking.append(thinking)
        elif btype == "server_tool_use":
            action = block.get("input") or {}
            step = _step(
                action.get("action") or "",
                block.get("kind"),
                status="completed",
                query=" / ".join([q for q in (action.get("queries") or []) if isinstance(q, str)]),
                url=action.get("url"),
                pattern=action.get("pattern"),
            )
            res.steps.append(step)
            if block.get("id"):
                calls[block["id"]] = step
        elif btype in ("web_search_tool_result", "web_fetch_tool_result"):
            step = calls.get(block.get("tool_use_id"))
            content = block.get("content")
            if isinstance(content, list):
                count = len([c for c in content if isinstance(c, dict) and c.get("type") == "web_search_result"])
                failed = any(
                    isinstance(c, dict) and str(c.get("type", "")).endswith("_error") for c in content
                )
            elif isinstance(content, dict):
                failed = str(content.get("type", "")).endswith("_error")
                count = 0 if failed else 1
            else:
                count, failed = 0, False
            if step is not None:
                step["result_count"] = count
                if failed:
                    step["status"] = "failed"
        elif btype == "text":
            texts.append(block.get("text") or "")
            for citation in block.get("citations") or []:
                if isinstance(citation, dict):
                    res.citations.append(
                        {
                            "url": citation.get("url") or "",
                            "title": citation.get("title") or "",
                            "cited_text": citation.get("cited_text") or "",
                        }
                    )
    res.answer = "".join(texts)
    res.raw_assistant = {"role": "assistant", "content": blocks}
    return res


def _reasoning_text(item):
    parts = []
    direct = item.get("reasoning_text")
    if isinstance(direct, str) and direct:
        parts.append(direct)
    for chunk in item.get("content") or []:
        if isinstance(chunk, dict):
            text = chunk.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


def parse_responses(obj):
    """Responses API 响应 -> Result。"""
    res = Result()
    if not isinstance(obj, dict):
        res.error = "响应不是 JSON 对象"
        return res
    res.usage = normalize_usage(obj.get("usage"))

    status = obj.get("status") or ""
    if status == "failed":
        error = obj.get("error") or {}
        res.error = (error.get("message") if isinstance(error, dict) else str(error)) or "生成失败（status=failed）"
        return res

    texts = []
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "reasoning":
            text = _reasoning_text(item)
            if text:
                res.thinking.append(text)
        elif itype == "web_search_call":
            action = item.get("action") or {}
            action_type = action.get("type") or ""
            page = item.get("page") or {}
            error = item.get("error") or {}
            res.steps.append(
                _step(
                    action_type,
                    item.get("kind"),
                    status=item.get("status"),
                    result_count=_count_sources(action.get("sources")),
                    query=" / ".join([q for q in (action.get("queries") or []) if isinstance(q, str)]),
                    url=action.get("url") or page.get("url"),
                    pattern=action.get("pattern"),
                    error=error.get("code") if isinstance(error, dict) else None,
                )
            )
        elif itype == "message":
            for chunk in item.get("content") or []:
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("type") == "output_text":
                    texts.append(chunk.get("text") or "")
                    for ann in chunk.get("annotations") or []:
                        if isinstance(ann, dict):
                            res.citations.append(
                                {
                                    "url": ann.get("url") or "",
                                    "title": ann.get("title") or "",
                                    "cited_text": "",
                                }
                            )
    res.answer = "".join(texts)

    if status == "incomplete":
        reason = (obj.get("incomplete_details") or {}).get("reason") or ""
        res.finish_reason = reason or "incomplete"
    else:
        res.finish_reason = status or "completed"

    res.raw_assistant = list(obj.get("output") or [])
    return res


PARSERS = {"chat": parse_chat, "messages": parse_messages, "responses": parse_responses}


# --------------------------------------------------------------------------- #
# 流式累积：把事件流还原成非流式形态，再复用上面的解析器
# --------------------------------------------------------------------------- #
def collect_chat_stream(events):
    """Chat Completions SSE -> 伪非流式响应对象。"""
    content_parts = []
    reasoning_parts = []
    thinking_by_step = {}
    events_calls = {}   # (step, index) -> call dict
    final_calls = None
    final_annotations = None
    finish_reason = ""
    usage = {}
    stream_error = ""

    for _event, data in events:
        if not data:
            continue
        if data.get("error"):
            stream_error = error_message(json.dumps(data)) or "生成失败（流式响应含 error）"
            usage = data.get("usage") or usage
            break
        if data.get("usage"):
            usage = data["usage"]
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("server_tool_calls"):
                final_calls = delta["server_tool_calls"]
            if delta.get("annotations"):
                final_annotations = delta["annotations"]

            tool_event = choice.get("server_tool_event") or delta.get("server_tool_event")
            if isinstance(tool_event, dict):
                etype = tool_event.get("type")
                step_index = tool_event.get("step")
                if etype == "step_reasoning_done":
                    text = "".join(reasoning_parts)
                    reasoning_parts = []
                    if isinstance(step_index, int):
                        thinking_by_step[step_index] = text
                elif etype in ("call_started", "call_completed"):
                    call = tool_event.get("call") or {}
                    key = (step_index, call.get("index", 0))
                    if etype == "call_started":
                        events_calls[key] = dict(call)
                    else:
                        merged = events_calls.get(key, {})
                        merged.update(call)
                        events_calls[key] = merged

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    if stream_error:
        return {"error": {"message": stream_error}, "usage": usage}

    thinking = [thinking_by_step[i] for i in sorted(thinking_by_step) if thinking_by_step[i]]
    if reasoning_parts:
        thinking.append("".join(reasoning_parts))

    if final_calls is None:
        # 最后一个 chunk 没给完整调用时，用 call_started/call_completed 拼一个近似结构
        grouped = {}
        for (step_index, _call_index), call in events_calls.items():
            key = step_index if isinstance(step_index, int) else 0
            grouped.setdefault(key, []).append(call)
        final_calls = [{"index": idx, "calls": calls} for idx, calls in sorted(grouped.items())]

    message = {"role": "assistant", "content": "".join(content_parts)}
    trailing = "".join(reasoning_parts)
    if trailing:
        # 最后一个 step_reasoning_done 之后输出的思考就是最后一步的思考
        message["reasoning_content"] = trailing
    if final_calls:
        message["server_tool_calls"] = final_calls
        # 把事件里累积的各步思考补回各步（解析器只认 server_tool_calls[i].reasoning_content）
        for position, block in enumerate(final_calls):
            if not isinstance(block, dict) or block.get("reasoning_content"):
                continue
            step_index = block.get("index")
            if not isinstance(step_index, int):
                step_index = position
            if step_index in thinking_by_step:
                block["reasoning_content"] = thinking_by_step[step_index]
    if final_annotations:
        message["annotations"] = final_annotations
    return {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


def collect_messages_stream(events):
    """Messages SSE -> 伪非流式响应对象。"""
    blocks = {}          # index -> block dict
    order = []
    stop_reason = ""
    usage = {}
    stream_error = ""

    for event, data in events:
        if not data:
            continue
        etype = data.get("type") or event or ""
        if etype == "error":
            stream_error = error_message(json.dumps(data)) or "生成失败（流式 error 事件）"
            usage = data.get("usage") or usage
            break
        if etype == "message_start":
            message = data.get("message") or {}
            usage = message.get("usage") or usage
        elif etype == "content_block_start":
            index = data.get("index", len(order))
            block = dict(data.get("content_block") or {})
            block.setdefault("type", "text")
            blocks[index] = block
            order.append(index)
        elif etype == "content_block_delta":
            index = data.get("index", 0)
            block = blocks.setdefault(index, {"type": "text"})
            if index not in order:
                order.append(index)
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "thinking_delta":
                block["thinking"] = (block.get("thinking") or "") + (delta.get("thinking") or "")
            elif dtype == "signature_delta":
                block["signature"] = (block.get("signature") or "") + (delta.get("signature") or "")
            elif dtype == "text_delta":
                block["text"] = (block.get("text") or "") + (delta.get("text") or "")
            elif dtype == "citations_delta":
                citation = delta.get("citation") or {}
                block.setdefault("citations", []).append(citation)
            elif delta.get("text"):
                block["text"] = (block.get("text") or "") + delta["text"]
        elif etype == "message_delta":
            delta = data.get("delta") or {}
            stop_reason = delta.get("stop_reason") or stop_reason
            if data.get("usage"):
                usage = data["usage"]
        elif etype == "message_stop":
            continue

    if stream_error:
        return {"type": "error", "error": {"message": stream_error}, "usage": usage}

    content = [blocks[i] for i in order]
    return {"type": "message", "role": "assistant", "content": content, "stop_reason": stop_reason, "usage": usage}


def collect_responses_stream(events):
    """Responses SSE -> 伪非流式响应对象。"""
    items = {}       # output_index -> item
    texts = {}       # output_index -> str
    annotations = {}  # output_index -> [annotation]
    final = {}
    stream_error = ""

    for event, data in events:
        if not data:
            continue
        etype = data.get("type") or event or ""
        if etype == "response.output_item.added":
            index = data.get("output_index", 0)
            items[index] = dict(data.get("item") or {})
        elif etype == "response.output_item.done":
            index = data.get("output_index", 0)
            item = dict(data.get("item") or {})
            items.setdefault(index, {}).update(item)
        elif etype in ("response.web_search_call.in_progress", "response.web_search_call.searching"):
            index = data.get("output_index", 0)
            item = items.setdefault(index, {"type": "web_search_call"})
            item.setdefault("status", "in_progress")
        elif etype == "response.web_search_call.completed":
            index = data.get("output_index", 0)
            item = items.setdefault(index, {"type": "web_search_call"})
            item["status"] = "completed"
            if data.get("action"):
                item.setdefault("action", {}).update(data["action"])
        elif etype == "response.output_text.delta":
            index = data.get("output_index", 0)
            texts[index] = texts.get(index, "") + (data.get("delta") or "")
        elif etype == "response.output_text.annotation.added":
            index = data.get("output_index", 0)
            annotation = data.get("annotation") or {}
            annotations.setdefault(index, []).append(annotation)
        elif etype in ("response.completed", "response.incomplete", "response.failed"):
            final = data.get("response") or {}
        elif etype == "error":
            stream_error = error_message(json.dumps(data)) or "生成失败（流式 error 事件）"
            break

    if stream_error:
        return {"status": "failed", "error": {"message": stream_error}, "usage": final.get("usage") or {}}

    output = []
    for index in sorted(items):
        item = items[index]
        itype = item.get("type")
        if itype == "message":
            content = []
            for chunk in item.get("content") or []:
                chunk = dict(chunk) if isinstance(chunk, dict) else {}
                if chunk.get("type") == "output_text":
                    if texts.get(index):
                        chunk["text"] = texts[index]
                    if annotations.get(index):
                        chunk["annotations"] = annotations[index]
                content.append(chunk)
            if not content and texts.get(index):
                content = [
                    {
                        "type": "output_text",
                        "text": texts[index],
                        "annotations": annotations.get(index) or [],
                    }
                ]
            item["content"] = content
        output.append(item)

    return {
        "status": final.get("status") or ("failed" if stream_error else "completed"),
        "output": output,
        "usage": final.get("usage") or {},
        "error": final.get("error") or {},
        "incomplete_details": final.get("incomplete_details") or {},
    }


STREAM_COLLECTORS = {
    "chat": collect_chat_stream,
    "messages": collect_messages_stream,
    "responses": collect_responses_stream,
}


# --------------------------------------------------------------------------- #
# 视图归约（默认视图只留 answer + citations）
# --------------------------------------------------------------------------- #
def normalize_citations(citations, max_citations):
    seen = set()
    out = []
    for citation in citations or []:
        url = (citation.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = (citation.get("title") or "").strip() or url
        out.append({"url": url, "title": clip(title, MAX_TITLE_CHARS)})
        if max_citations and len(out) >= max_citations:
            break
    return out


def trace_summary(steps):
    counts = {"search": 0, "open_page": 0, "find_in_page": 0, "read_link": 0}
    for step in steps or []:
        action = step.get("action")
        if action in counts:
            counts[action] += 1
    parts = []
    if counts["search"]:
        parts.append("搜索%d次" % counts["search"])
    if counts["open_page"]:
        parts.append("打开%d页" % counts["open_page"])
    if counts["find_in_page"]:
        parts.append("页内查找%d次" % counts["find_in_page"])
    if counts["read_link"]:
        parts.append("阅读链接%d个" % counts["read_link"])
    return "，".join(parts) if parts else "未使用检索工具"


def _last_sentence(text):
    """只留最后一句结论（丢弃过程复述与过渡性填充）。"""
    text = (text or "").strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    tail = lines[-1]
    bounds = []
    for index, char in enumerate(tail):
        if char in "。！？!?":
            bounds.append(index)
        elif char == "." and (index + 1 == len(tail) or tail[index + 1] in " \t"):
            # 英文句号只在「后面是空白或结尾」时算句末，避免切断 3.5 / example.com
            bounds.append(index)
    if bounds:
        start = bounds[-2] + 1 if len(bounds) >= 2 else 0
        tail = tail[start : bounds[-1] + 1]
    return tail.strip()


def thinking_digest(thinking, limit):
    out = []
    for step in thinking or []:
        sentence = _last_sentence(step)
        if sentence:
            out.append(clip(sentence, limit))
    return out


def tool_trace(steps):
    out = []
    for step in steps or []:
        entry = {"action": step.get("action") or ""}
        if step.get("query"):
            entry["query"] = step["query"]
        if step.get("url"):
            entry["url"] = step["url"]
        if step.get("pattern"):
            entry["pattern"] = step["pattern"]
        if step.get("action") == "search":
            entry["result_count"] = step.get("result_count", 0)
        if step.get("status") and step.get("status") != "completed":
            entry["status"] = step["status"]
        out.append(entry)
    return out


def build_view(result, args):
    """按视图层级产出对外结构（默认只有 answer + citations）。"""
    answer = clip(result.answer, args.max_answer_chars)
    view = {"answer": answer}
    if not args.brief:
        view["citations"] = normalize_citations(result.citations, args.max_citations)
    if args.summary or args.verbose:
        view["trace_summary"] = trace_summary(result.steps)
    if args.verbose:
        view["thinking_digest"] = thinking_digest(result.thinking, args.thinking_chars)
        view["tool_trace"] = tool_trace(result.steps)
    return view


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def print_human(view, result, args):
    if not args.brief:
        sys.stdout.write("[deep-think] %s · %s · %s\n\n" % (args.protocol, args.model, result.finish_reason or "-"))
    sys.stdout.write(view.get("answer") or "（模型未返回正文）")
    sys.stdout.write("\n")

    citations = view.get("citations") or []
    if citations:
        sys.stdout.write("\n引用 (%d):\n" % len(citations))
        for index, citation in enumerate(citations, 1):
            sys.stdout.write("[%d] %s — %s\n" % (index, citation["title"], citation["url"]))

    if view.get("trace_summary"):
        sys.stdout.write("\n检索轨迹：%s\n" % view["trace_summary"])
    if view.get("thinking_digest"):
        sys.stdout.write("\n思考摘要：\n")
        for line in view["thinking_digest"]:
            sys.stdout.write("- %s\n" % line)
    if view.get("tool_trace"):
        sys.stdout.write("\n工具轨迹：\n")
        for index, step in enumerate(view["tool_trace"], 1):
            detail = step.get("query") or step.get("url") or step.get("pattern") or ""
            if step.get("pattern") and step.get("url"):
                detail = "%s @ %s" % (step["pattern"], step["url"])
            suffix = ""
            if step.get("result_count"):
                suffix = " (%d 条结果)" % step["result_count"]
            if step.get("status"):
                suffix += " [%s]" % step["status"]
            sys.stdout.write("%d. %s %s%s\n" % (index, step["action"] or "?", detail, suffix))


# --------------------------------------------------------------------------- #
# 历史状态（skill 私有，不是给 agent 的返回值）
# --------------------------------------------------------------------------- #
def load_history(path):
    """读历史状态，返回 (items, meta)。

    兼容三种形态：纯数组、{"items": [...]}、{"messages": [...]}；
    旧版本可能写出嵌套数组，这里顺便展平一层并丢弃非对象元素。
    """
    if not path or not os.path.isfile(path):
        return [], {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError("无法读取历史文件 %s：%s" % (path, exc))
    meta = {}
    if isinstance(data, dict):
        meta = {"protocol": data.get("protocol"), "model": data.get("model")}
        data = data.get("items") or data.get("messages") or []
    if not isinstance(data, list):
        raise ConfigError("历史文件格式不对（应为消息数组或 {\"items\": [...]}）：%s" % path)
    items = []
    for item in data:
        if isinstance(item, list):
            items.extend([sub for sub in item if isinstance(sub, dict)])
        elif isinstance(item, dict):
            items.append(item)
    return items, meta


def save_history(path, protocol, model, items):
    payload = {"version": 1, "protocol": protocol, "model": model, "items": items}
    write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def build_items(history, question, protocol):
    items = list(history or [])
    if protocol == "responses":
        items.append({"role": "user", "content": question})
    else:
        items.append({"role": "user", "content": question})
    return items


# --------------------------------------------------------------------------- #
# 错误描述
# --------------------------------------------------------------------------- #
def describe_api_error(status, message, usage=None):
    detail = (message or "").strip()
    lowered = detail.lower()
    for needle, hint in ERROR_HINTS:
        if needle.lower() in lowered:
            return "%s（%s）" % (hint, detail)
    if status == 400:
        head = "请求参数不合法"
        if "context" in lowered or "too long" in lowered or "maximum" in lowered:
            head = "上下文超过模型上限"
    elif status == 401:
        head = "API Key 缺失或无效（检查 DeepSeek_API_Key / DEEPSEEK_API_KEY）"
    elif status == 402:
        head = "账号余额不足，请先充值（不重试）"
    elif status == 403:
        head = "无权限（可能被安全策略拒绝）"
    elif status == 404:
        head = "端点不存在（检查 --protocol 与 DEEPSEEK_API_BASE）"
    elif status == 408:
        head = "请求超时（已重试 3 次）"
    elif status == 413:
        head = "请求体超过大小限制"
    elif status == 429:
        head = "触发限流（已重试 3 次，稍后再试）"
    elif 500 <= status <= 599:
        head = "服务端错误 HTTP %s（已重试 3 次）" % status
    else:
        head = "HTTP %s" % status
    text = "%s：%s" % (head, detail) if detail else head
    if usage:
        text += "\n" + usage_line(usage)
    return text


FINISH_MESSAGES = {
    "content_filter": "内容审核拒答（content_filter），回答不完整",
    "length": "输出达到上限被截断（length / max_tokens）",
    "max_tokens": "输出达到上限被截断（max_tokens）",
    "refusal": "模型拒答（refusal）",
    "max_output_tokens": "输出达到上限被截断（max_output_tokens）",
    "incomplete": "回答未完成（incomplete）",
}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(args):
    question = (args.question or "").strip()
    if not question:
        raise UsageError("问题不能为空")

    history_path = artifact_path(args.history, DEFAULT_HISTORY) if args.history else None
    save_path = None
    if args.save_history:
        save_path = artifact_path(args.save_history, DEFAULT_HISTORY)
    log_path = artifact_path(args.log, DEFAULT_LOG) if args.log else None
    raw_path = None
    if args.dump_raw:
        raw_path = artifact_path(args.dump_raw, os.path.join(DEFAULT_RAW_DIR, time.strftime("%Y%m%d-%H%M%S") + ".json"))

    history, history_meta = load_history(history_path) if history_path else ([], {})
    if history_meta.get("protocol") and history_meta["protocol"] != args.protocol:
        # 跨协议回传在服务端必然 422（如 messages 的 thinking 块），直接拒绝以免白花 token
        raise UsageError(
            "历史状态是 %s 协议写的，当前 --protocol 是 %s：状态文件与协议绑定，"
            "请用同一协议继续（或换一个新的状态文件）" % (history_meta["protocol"], args.protocol)
        )
    items = build_items(history, question, args.protocol)
    payload = build_payload(
        args.protocol, args.model, items, args.stream, args.max_tokens, args.effort, args.converge
    )
    url = API_BASE + PROTOCOLS[args.protocol]

    if args.dry_run:
        dump_json(
            {
                "endpoint": url,
                "protocol": args.protocol,
                "headers": masked_headers(build_headers(args.protocol, "***", args.stream)),
                "payload": payload,
            }
        )
        return EXIT_OK

    api_key = resolve_key(args.key_file)
    headers = build_headers(args.protocol, api_key, args.stream)
    started = time.time()

    if args.stream:
        events = list(iter_sse(url, payload, headers, args.timeout))
        if raw_path:
            write_atomic(raw_path, json.dumps([{"event": e, "data": d} for e, d in events], ensure_ascii=False, indent=2) + "\n")
        response = STREAM_COLLECTORS[args.protocol](events)
    else:
        response = post_json(url, payload, headers, args.timeout)
        if raw_path:
            write_atomic(raw_path, json.dumps(response, ensure_ascii=False, indent=2) + "\n")

    elapsed_ms = int((time.time() - started) * 1000)
    result = PARSERS[args.protocol](response)

    if log_path:
        append_log(
            log_path,
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "protocol": args.protocol,
                "model": args.model,
                "stream": bool(args.stream),
                "elapsed_ms": elapsed_ms,
                "finish_reason": result.finish_reason,
                "usage": result.usage,
                "error": result.error,
            },
        )

    if result.usage:
        note(usage_line(result.usage))

    if result.error:
        err(result.error)
        return EXIT_API

    view = build_view(result, args)
    if args.json:
        dump_json(view)
    else:
        print_human(view, result, args)

    if save_path:
        saved = list(items)
        raw = result.raw_assistant
        if isinstance(raw, list):
            # responses 协议的 output items 要逐个放回 input，不能塞成一个嵌套数组
            saved.extend([item for item in raw if isinstance(item, dict)])
        elif raw:
            saved.append(raw)
        save_history(save_path, args.protocol, args.model, saved)

    finish = (result.finish_reason or "").lower()
    if finish in FINISH_MESSAGES:
        warn(FINISH_MESSAGES[finish])
        return EXIT_API
    return EXIT_OK


EPILOG = """\
选哪个命令（路由由本地 agent 的模型判断，本脚本只执行、不做决策）：
  no_search             不用联网 -> 直接回答
  only_search           请求前就能写出最终 query 的单点事实 -> deepseek-search.sh search
  search_and_fetch_once 摘要不够但候选源少 / 用户给了 URL -> search + fetch（或直接 fetch）
  deep_think            无法在请求前固化 query、需多步交叉验证 -> 本脚本

本脚本 = 方案 A：检索规划权在 DeepSeek 服务端模型，客户端不执行任何工具。

示例：
  deepseek-deep-think.sh "对比埃菲尔铁塔和东京塔哪个更高，查官网确认"
  deepseek-deep-think.sh "问题" --summary                 # 多一行检索轨迹
  deepseek-deep-think.sh "问题" --verbose                 # 诊断视图（不要回传上下文）
  deepseek-deep-think.sh "问题" --json                    # {"answer","citations"}
  deepseek-deep-think.sh "追问" --history .cache/deep-think/history.json \\
      --save-history .cache/deep-think/history.json       # 多轮（skill 私有状态）

产物位置：历史/日志/原始 dump 一律写在 <skill>/.cache/deep-think/ 下。

计费：服务端搜索与抓取不计费，只计模型 token（含思考 token）。
      步数由模型自决（上限约 20），**没有**限步参数；能调的只有：
        --effort low        思考链更短、更早收敛（默认 high，实测同题省 ~36%）
        --converge          加软收敛提示（够用即停）
        --max-tokens N      限输出长度（--brief / --max-answer-chars 不省钱）
      另外：模型 token 分峰谷，高峰是空闲的 2 倍；
        先跑 deepseek-peak.sh 看时段，不急的活排到空闲时段。
      问题越模糊步数越多、越贵；先用 --dry-run 看请求体。

退出码：0 成功 / 1 参数或配置错误 / 2 接口或网络错误
"""


class _Parser(argparse.ArgumentParser):
    """argparse 的用法错误统一 exit 1（2 留给接口错误）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        raise UsageError(message)


def build_parser():
    parser = _Parser(
        prog="deepseek-deep-think",
        description="DeepSeek 深度思考搜索（方案 A）：多步检索在服务端完成，本地只发一次对话请求。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="deepseek-deep-think " + VERSION)
    parser.add_argument("question", help="要研究/核实的问题（一个可独立表述的检索目标）")

    g = parser.add_argument_group("协议")
    g.add_argument("--protocol", default="chat", choices=sorted(PROTOCOLS), help="默认 chat")
    g.add_argument("--model", default=DEFAULT_MODEL, help="服务端模型（默认 %s）" % DEFAULT_MODEL)
    g.add_argument("--stream", action="store_true", help="使用 SSE 流式响应（默认非流式，最稳）")
    g.add_argument(
        "--effort",
        default=DEFAULT_EFFORT,
        choices=list(EFFORTS),
        help="思考强度（默认 %s）：low 更早收敛、更省 token，复杂问题质量可能下降" % DEFAULT_EFFORT,
    )
    g.add_argument("--converge", action="store_true", help="加一段软收敛提示（够用即停、连续无新信息就收尾）")
    g.add_argument("--max-tokens", type=int, default=0, help="输出上限（responses 协议映射为 max_output_tokens；0=不传）")

    v = parser.add_argument_group("输出视图")
    group = v.add_mutually_exclusive_group()
    group.add_argument("--brief", action="store_true", help="只输出回答正文（不要引用）")
    group.add_argument("--summary", action="store_true", help="默认视图 + 一行检索轨迹")
    group.add_argument("--verbose", action="store_true", help="诊断视图：再加思考摘要与工具轨迹（不要回传上下文）")
    v.add_argument("--json", action="store_true", help="输出结构化 JSON（默认只有 answer + citations）")
    v.add_argument("--max-citations", type=int, default=DEFAULT_MAX_CITATIONS, help="引用上限（默认 8，0=不限）")
    v.add_argument("--max-answer-chars", type=int, default=0, help="回答正文上限（0=不限）")
    v.add_argument("--thinking-chars", type=int, default=DEFAULT_THINKING_CHARS, help="--verbose 时每步思考保留的字符数")

    s = parser.add_argument_group("多轮状态（skill 私有，不是给 agent 的返回值）")
    s.add_argument(
        "--history",
        default=None,
        help="读取历史状态文件（需显式路径，建议与 --save-history 用同一文件，如 %s）" % DEFAULT_HISTORY,
    )
    s.add_argument(
        "--save-history",
        default=None,
        help="成功后写回历史状态（需显式路径，建议与 --history 用同一文件，如 %s）" % DEFAULT_HISTORY,
    )

    d = parser.add_argument_group("产物与调试（默认都写在 <skill>/.cache/deep-think/ 下）")
    d.add_argument("--log", nargs="?", const=DEFAULT_LOG, default=None, help="追加 JSONL 日志（默认 %s）" % DEFAULT_LOG)
    d.add_argument("--dump-raw", nargs="?", const="", default=None, help="保存接口原始响应/SSE 事件到 .cache/deep-think/raw/")
    d.add_argument("--dry-run", action="store_true", help="只打印将要发送的请求，不调用 API、不计费、不写文件")
    d.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="请求超时秒数（默认 180）")
    d.add_argument("--key-file", default=None, help="API Key 文件路径（默认 skill 根目录/DeepSeek_API_Key）")
    return parser


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = build_parser()
    args = None
    try:
        args = parser.parse_args(argv)
        if args.dump_raw == "":
            args.dump_raw = os.path.join(DEFAULT_RAW_DIR, time.strftime("%Y%m%d-%H%M%S") + ".json")
        if args.max_citations < 0:
            raise UsageError("--max-citations 不能为负数")
        if args.max_answer_chars < 0:
            raise UsageError("--max-answer-chars 不能为负数")
        if args.thinking_chars < 0:
            raise UsageError("--thinking-chars 不能为负数")
        if args.max_tokens < 0:
            raise UsageError("--max-tokens 不能为负数")
        return run(args)
    except UsageError as exc:
        err(str(exc))
        return EXIT_USAGE
    except ConfigError as exc:
        err(str(exc))
        return EXIT_USAGE
    except NetworkError as exc:
        err(str(exc))
        return EXIT_API
    except ApiError as exc:
        err(describe_api_error(exc.status, exc.message))
        return EXIT_API
    except KeyboardInterrupt:
        note("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
