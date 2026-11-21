#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deepseek_search.py — DeepSeek Search / Fetch CLI（面向 LLM Agent）

设计目标（详见 改进建议.md / SKILL.md）：

1. 默认只取摘要（need_content=false），本地做 URL 去重 + 低质过滤 + 规则重排，
   再把精简结果喂给模型，避免一次性拉全文造成上下文爆炸；
2. 摘要不足时再按需 fetch 原文，一次调用可抓多个 URL，自动截断长正文并缓存，
   避免重复计费、避免触发 Fetch 的 25 QPS 限制；
3. Search 与 Fetch 分开调用、分开计费（5 元 / 1000 次成功请求），尽量少调用。

只依赖 Python 3.7+ 标准库（不再需要 curl / jq）。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

VERSION = "2.0.0"
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1/tools").rstrip("/")

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_API = 2
EXIT_PARTIAL = 3

MAX_QUERY_CHARS = 128
MAX_ORI_QUESTION_CHARS = 128
MAX_URL_CHARS = 8192

GOOD_SCORE = 1.5      # rerank_score >= 1.5 为优质结果（官方定义）
AUTHORITY_STRONG = 3  # authority >= 3 为极权威结果（官方定义）

# 单篇正文被截断时追加的标记
TRUNC_MARK = "\n\n[…已截断，原文 {total} 字符（--max-chars 可放宽 / --keywords 可摘录）]"


# --------------------------------------------------------------------------- #
# 错误类型
# --------------------------------------------------------------------------- #
class UsageError(Exception):
    """参数 / 用法错误 -> exit 1"""


class ConfigError(Exception):
    """缺少密钥等配置问题 -> exit 1"""


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
# HTTP
# --------------------------------------------------------------------------- #
def error_message(body):
    try:
        obj = json.loads(body)
    except Exception:
        return (body or "").strip()[:300]
    if isinstance(obj, dict):
        e = obj.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e.get("code") or "").strip()
        if isinstance(e, str):
            return e.strip()
    return ""


def describe_error(status, message, kind="api"):
    if status == 400:
        head = "请求参数不合法"
    elif status == 401:
        head = "API Key 缺失或无效（检查 DeepSeek_API_Key）"
    elif status == 402:
        head = "账号余额不足，请先充值"
    elif status == 403:
        head = "Fetch 被安全策略拒绝" if kind == "fetch" else "无权限"
    elif status == 404:
        head = (
            "Fetch 未取到可用正文（URL 失效 / 被站点拦截 / 正文不可提取）"
            if kind == "fetch"
            else "未找到"
        )
    elif status == 408:
        head = "读取请求体超时（已重试 3 次）"
    elif status == 413:
        head = "请求体超过大小限制"
    elif status == 429:
        head = "触发 QPS 限流（已重试 3 次，稍后再试或减小并发）"
    elif 500 <= status <= 599:
        head = "服务端错误 HTTP %s（已重试 3 次）" % status
    else:
        head = "HTTP %s" % status
    return "%s：%s" % (head, message) if message else head


def _retry_after(headers):
    raw = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.isdigit():
        return max(1, min(int(raw), 20))
    return None


def http_post(url, payload, timeout, api_key):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "deepseek-search-skill/" + VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.getcode(), body, dict(resp.headers or {})
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        return exc.code, body, dict(exc.headers or {})
    except urllib.error.URLError as exc:
        raise NetworkError(str(getattr(exc, "reason", exc)))
    except OSError as exc:
        raise NetworkError(str(exc))


def api_post(kind, payload, timeout, api_key):
    """调用 /search 或 /fetch，408/429/5xx 最多重试 3 次（遵守 Retry-After）。"""
    url = BASE_URL + "/" + kind
    last_status, last_message = 0, ""
    for attempt in (1, 2, 3):
        try:
            status, body, headers = http_post(url, payload, timeout, api_key)
        except NetworkError as exc:
            last_status, last_message = 0, str(exc)
            if attempt == 3:
                raise NetworkError("网络请求失败（已重试 3 次）：%s" % exc)
            time.sleep(min(2 * attempt, 10))
            continue

        if status == 200:
            try:
                return json.loads(body)
            except ValueError:
                raise ApiError(200, "服务器返回的不是合法 JSON：%s" % (body or "")[:200])

        last_status, last_message = status, error_message(body)
        if status in (408, 429) or 500 <= status <= 599:
            if attempt == 3:
                break
            delay = _retry_after(headers) or min(2 * attempt, 10)
            time.sleep(delay)
            continue
        raise ApiError(status, last_message)
    raise ApiError(last_status, last_message)


# --------------------------------------------------------------------------- #
# 本地后处理（纯规则：去重 / 过滤 / 重排 / 截断 / 摘录）
# --------------------------------------------------------------------------- #
_TRACKING_RE = re.compile(
    r"^(utm_[^=&]*|ref|ref_src|ref_url|fbclid|gclid|yclid|msclkid|mc_cid|mc_eid|spm|"
    r"from|source|share_token|share_source)$",
    re.I,
)
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_PARA_RE = re.compile(r"\n\s*\n")


def num(value, default=0.0):
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_url(url):
    """去重用的 URL 归一化：去掉 fragment、尾斜杠与常见跟踪参数。"""
    if not url:
        return ""
    u = str(url).strip().split("#", 1)[0]
    if "?" in u:
        base, _, query = u.partition("?")
        kept = [
            part
            for part in query.split("&")
            if part and not _TRACKING_RE.match(part.split("=", 1)[0])
        ]
        u = base + "?" + "&".join(kept) if kept else base
    if "://" in u:
        scheme, rest = u.split("://", 1)
        u = scheme + "://" + rest.rstrip("/")
    return u


def date_num(value):
    """把 RFC3339 日期压成 YYYYMMDD 整数用于排序，未知返回 0。"""
    if not isinstance(value, str):
        return 0
    m = _DATE_RE.match(value)
    if not m:
        return 0
    return int(m.group(1) + m.group(2) + m.group(3))


def dedupe_results(items):
    seen = set()
    out = []
    for item in items:
        key = normalize_url(item.get("url", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def sort_results(items, mode):
    if mode == "score":
        return sorted(items, key=lambda r: -num(r.get("rerank_score"), 0))
    if mode == "date":
        return sorted(items, key=lambda r: -date_num(r.get("date")))
    if mode == "authority":
        # 权威性 > rerank_score > 发布日期（新在前）
        return sorted(
            items,
            key=lambda r: (
                -num(r.get("authority"), -1),
                -num(r.get("rerank_score"), 0),
                -date_num(r.get("date")),
            ),
        )
    return list(items)  # rank: 保留搜索引擎原始顺序


def process_results(results, min_score=0.0, min_authority=-2, sort_mode="rank", limit=0, do_dedupe=True):
    stats = {"raw": len(results), "low_score": 0, "low_authority": 0, "duplicate": 0}
    kept = []
    for item in results:
        if min_score > 0 and num(item.get("rerank_score"), 0) < min_score:
            stats["low_score"] += 1
            continue
        if min_authority > -2 and num(item.get("authority"), -1) < min_authority:
            stats["low_authority"] += 1
            continue
        kept.append(item)
    if do_dedupe:
        before = len(kept)
        kept = dedupe_results(kept)
        stats["duplicate"] = before - len(kept)
    kept = sort_results(kept, sort_mode)
    if limit and limit > 0:
        kept = kept[:limit]
    stats["shown"] = len(kept)
    return kept, stats


def truncate(text, limit):
    if not text:
        return ""
    if not limit or limit <= 0:
        return text
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + TRUNC_MARK.format(total=len(text))


def strip_leading_title(content, title):
    """去掉正文开头与页面标题重复的一级标题，省 token。"""
    if not content or not title:
        return content
    lines = content.lstrip("\n").split("\n")
    if not lines:
        return content
    first = lines[0].strip()
    if first.startswith("#") and first.lstrip("#").strip() == (title or "").strip():
        return "\n".join(lines[1:]).lstrip("\n")
    return content


def split_keywords(values):
    out = []
    for chunk in values or []:
        for part in re.split(r"[,，]", chunk or ""):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def excerpt(text, keywords, pad=1):
    """纯文本规则摘录：命中关键词的段落 + 前后 pad 段，保留开头段落作上下文。"""
    if not text or not keywords:
        return text
    kws = [k.lower() for k in keywords]
    paras = [p.strip() for p in _PARA_RE.split(text) if p.strip()]
    if not paras:
        return text
    hits = [i for i, p in enumerate(paras) if any(k in p.lower() for k in kws)]
    if not hits:
        return text
    keep = set([0])
    for i in hits:
        for j in range(max(0, i - pad), min(len(paras) - 1, i + pad) + 1):
            keep.add(j)
    out = []
    prev = None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            out.append("…")
        out.append(paras[i])
        prev = i
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
# 输出格式化
# --------------------------------------------------------------------------- #
def short_date(value):
    if not isinstance(value, str) or len(value) < 10:
        return "unknown"
    return value[:10]


def format_search_text(items, max_summary, max_content):
    lines = []
    for idx, item in enumerate(items, 1):
        title = (item.get("title") or "(无标题)").strip()
        summary = (item.get("summary") or "").strip() or (item.get("snippet") or "").strip()
        if max_summary and max_summary > 0 and len(summary) > max_summary:
            summary = summary[:max_summary].rstrip() + "…"
        score = item.get("rerank_score")
        authority = item.get("authority")
        lines.append("[%d] %s" % (idx, title))
        lines.append("    URL:       %s" % (item.get("url") or ""))
        lines.append(
            "    Source:    %s  |  date: %s  |  score: %s  |  authority: %s"
            % (
                item.get("site_name") or "unknown",
                short_date(item.get("date")),
                "-" if score is None else ("%.2f" % num(score, 0)),
                "-" if authority is None else authority,
            )
        )
        lines.append("    Summary:   %s" % (summary or "(空)"))
        content = (item.get("content") or "").strip()
        if content and max_content and max_content > 0:
            body = truncate(content, max_content)
            lines.append("    Content:   " + body.replace("\n", "\n               "))
    return "\n".join(lines)


def format_fetch_text(doc, index=None, total=None):
    title = (doc.get("title") or "").strip() or "(无标题)"
    head = "# %s" % title
    if total and total > 1:
        head = "## [%d/%d] %s" % (index, total, doc.get("title") or "(无标题)")
    lines = [head, "URL: %s" % doc.get("url", "")]
    if doc.get("cached"):
        lines.append("Source: cache")
    lines.append("")
    lines.append(doc.get("content") or "")
    return "\n".join(lines)


def dump_json(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# 缓存（key = url）
# --------------------------------------------------------------------------- #
def cache_root(cli_dir=None):
    return (
        cli_dir
        or os.environ.get("DEEPSEEK_SEARCH_CACHE")
        or os.path.join(tempfile.gettempdir(), "deepseek-search-cache")
    )


def cache_path(directory, url):
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    return os.path.join(directory, digest + ".json")


def cache_read(path, ttl):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or not obj.get("content"):
        return None
    if ttl and ttl > 0:
        try:
            if time.time() - float(obj.get("fetched_at") or 0) > ttl:
                return None
        except (TypeError, ValueError):
            return None
    return obj


def cache_write(path, url, title, content):
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"fetched_at": int(time.time()), "url": url, "title": title, "content": content},
                fh,
                ensure_ascii=False,
            )
        os.replace(tmp, path)
    except OSError as exc:
        warn("缓存写入失败（不影响本次结果）：%s" % exc)


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
def build_query(args):
    parts = [args.query.strip()]
    sites = [s.strip() for s in (args.site or []) if s.strip()]
    if sites:
        parts.append(" OR ".join("site:" + s for s in sites))
    for s in args.exclude_site or []:
        if s.strip():
            parts.append("-site:" + s.strip())
    for s in args.exclude or []:
        if s.strip():
            parts.append("-" + s.strip())
    for s in args.exclude_phrase or []:
        if s.strip():
            parts.append('-"' + s.strip().replace('"', "") + '"')
    for s in args.phrase or []:
        if s.strip():
            parts.append('"' + s.strip().replace('"', "") + '"')
    if args.lang and args.lang.strip():
        parts.append("lang:" + args.lang.strip())
    if args.after and args.after.strip():
        parts.append("after:" + args.after.strip())
    if args.before and args.before.strip():
        parts.append("before:" + args.before.strip())
    return " ".join(p for p in parts if p)


def _range_int(name, value, lo, hi):
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise UsageError("%s 必须是整数（收到 %r）" % (name, value))
    if v < lo or v > hi:
        raise UsageError("%s 必须在 %d~%d 之间（收到 %d）" % (name, lo, hi, v))
    return v


def _range_float(name, value, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise UsageError("%s 必须是数字（收到 %r）" % (name, value))
    if v < lo or v > hi:
        raise UsageError("%s 必须在 %s~%s 之间（收到 %s）" % (name, lo, hi, v))
    return v


def run_search(args):
    query = build_query(args)
    if not query.strip():
        raise UsageError("query 不能为空")
    if len(query) > MAX_QUERY_CHARS:
        raise UsageError(
            "最终 query 超过 %d 个字符（当前 %d）：%s" % (MAX_QUERY_CHARS, len(query), query)
        )

    count = _range_int("--count", args.count, 1, 50)
    min_score = _range_float("--min-score", args.min_score, 0, 3)
    min_authority = _range_int("--min-authority", args.min_authority, -2, 4)
    limit = _range_int("--limit", args.limit, 0, 50)
    max_summary = _range_int("--max-summary", args.max_summary, 0, 100000)
    max_content = _range_int("--max-content", args.max_content, 0, 1000000)
    if args.ori_question and len(args.ori_question) > MAX_ORI_QUESTION_CHARS:
        raise UsageError("--ori-question 超过 %d 个字符" % MAX_ORI_QUESTION_CHARS)

    payload = {"query": query, "count": count, "need_content": bool(args.need_content)}
    if args.ori_question:
        payload["ori_question"] = args.ori_question
    if args.localization:
        payload["localization"] = args.localization

    if args.dry_run:
        dump_json({"endpoint": BASE_URL + "/search", "payload": payload})
        return EXIT_OK

    api_key = resolve_key(args.key_file)
    data = api_post("search", payload, args.timeout or 30, api_key)
    results = data.get("results") or []
    if not isinstance(results, list):
        raise ApiError(200, "响应缺少 results 数组")

    items, stats = process_results(
        results, min_score, min_authority, args.sort, limit, not args.no_dedupe
    )
    if not items and results:
        warn("所有结果都被过滤条件排除，已回退为不过滤（可显式传 --min-score 0）")
        items, stats = process_results(
            results, 0, -2, args.sort, limit, not args.no_dedupe
        )
        stats["fallback"] = True

    if args.raw:
        dump_json(data)
    elif args.json:
        dump_json({"query": query, "count": len(items), "results": items, "stats": stats})
    elif not items:
        print("No matching results.")
    else:
        print(format_search_text(items, max_summary, max_content))
        if stats["shown"] != stats["raw"]:
            reasons = []
            if stats["duplicate"]:
                reasons.append("去重 %d" % stats["duplicate"])
            if stats["low_score"]:
                reasons.append("低分 %d" % stats["low_score"])
            if stats["low_authority"]:
                reasons.append("低权威 %d" % stats["low_authority"])
            if not reasons and limit:
                reasons.append("受 --limit %d 限制" % limit)
            print(
                "\n-- 显示 %d / %d 条%s，排序：%s%s"
                % (
                    stats["shown"],
                    stats["raw"],
                    "（%s）" % "，".join(reasons) if reasons else "",
                    args.sort,
                    "（已回退为不过滤）" if stats.get("fallback") else "",
                )
            )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def validate_url(url):
    if len(url) > MAX_URL_CHARS:
        raise UsageError("URL 过长（上限 %d）：%s" % (MAX_URL_CHARS, url[:80]))
    if any(ch.isspace() for ch in url):
        raise UsageError("URL 不能包含空白字符：%s" % url)
    if not re.match(r"^https?://", url, re.I):
        raise UsageError("URL 必须以 http:// 或 https:// 开头：%s" % url)
    authority = url.split("://", 1)[1].split("/", 1)[0]
    if not authority:
        raise UsageError("URL 必须包含 host：%s" % url)
    if "@" in authority:
        raise UsageError("URL 不能携带用户名/密码：%s" % url)


def run_fetch(args):
    urls = []
    for raw in args.urls or []:
        url = raw.strip()
        if not url:
            continue
        validate_url(url)
        if url not in urls:
            urls.append(url)
    if not urls:
        raise UsageError("至少需要一个 URL")

    max_chars = _range_int("--max-chars", args.max_chars, 0, 10000000)
    delay = _range_float("--delay", args.delay, 0, 60)
    ttl = _range_int("--cache-ttl", args.cache_ttl, 0, 10 ** 9)
    pad = _range_int("--keyword-pad", args.keyword_pad, 0, 10)
    keywords = split_keywords(args.keywords)

    if args.dry_run:
        dump_json(
            {
                "endpoint": BASE_URL + "/fetch",
                "payloads": [{"url": u} for u in urls],
                "max_chars": max_chars,
                "keywords": keywords,
            }
        )
        return EXIT_OK

    api_key = resolve_key(args.key_file)
    directory = cache_root(args.cache_dir)

    docs = []
    failures = []
    for index, url in enumerate(urls, 1):
        title = ""
        content = ""
        cached = False
        if not args.no_cache:
            hit = cache_read(cache_path(directory, url), ttl)
            if hit:
                title = (hit.get("title") or "").strip()
                content = hit.get("content") or ""
                cached = True

        if not cached:
            if index > 1 and delay > 0:
                time.sleep(delay)
            try:
                data = api_post("fetch", {"url": url}, args.timeout or 60, api_key)
            except (ApiError, NetworkError) as exc:
                if len(urls) == 1 or args.fail_fast:
                    raise
                status = getattr(exc, "status", 0)
                message = getattr(exc, "message", "") or str(exc)
                reason = describe_error(status, message, "fetch") if status else "网络请求失败：%s" % message
                failures.append((url, reason))
                note("[跳过] %s —— %s" % (url, reason))
                continue

            title = (data.get("title") or "").strip()
            content = data.get("content") or ""
            if not content.strip():
                reason = describe_error(404, "", "fetch")
                if len(urls) == 1 or args.fail_fast:
                    raise ApiError(404, "返回正文为空")
                failures.append((url, reason))
                note("[跳过] %s —— %s" % (url, reason))
                continue
            if not args.no_cache:
                cache_write(cache_path(directory, url), url, title, content)

        if cached:
            note("[缓存命中] %s" % url)

        body = strip_leading_title(content, title)
        if keywords:
            body = excerpt(body, keywords, pad)
        body = truncate(body, max_chars)
        docs.append(
            {
                "url": url,
                "title": title,
                "content": body,
                "content_raw": content,
                "raw_length": len(content),
                "cached": cached,
            }
        )

    if args.raw:
        dump_json({"results": [{"url": d["url"], "title": d["title"], "content": d["content_raw"]} for d in docs]})
    elif args.json:
        dump_json(
            {
                "count": len(docs),
                "results": [
                    {
                        "url": d["url"],
                        "title": d["title"],
                        "content": d["content"],
                        "cached": d["cached"],
                        "original_length": d["raw_length"],
                        "truncated": len(d["content"]) < len(d["content_raw"]) or d["raw_length"] > len(d["content"]),
                    }
                    for d in docs
                ],
                "failures": [{"url": u, "error": m} for u, m in failures],
            }
        )
    else:
        blocks = [format_fetch_text(d, i, len(docs)) for i, d in enumerate(docs, 1)]
        if blocks:
            print("\n\n---\n\n".join(blocks))

    if failures:
        err(
            "部分 URL 抓取失败（%d/%d）：%s"
            % (len(failures), len(urls), "; ".join(u for u, _ in failures))
        )
        return EXIT_PARTIAL
    return EXIT_OK


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
EPILOG = """\
推荐工作流（详见 SKILL.md）：
  1) 先粗搜摘要，默认不取正文：
     deepseek-search.sh search "关键词" --ori-question "用户原问题" --count 8 \\
         --min-score 1.5 --sort authority
  2) 摘要足够 -> 直接回答；不够 -> 挑最多 3 条 URL 一次抓原文：
     deepseek-search.sh fetch "URL1" "URL2" --max-chars 8000 --keywords "关键词1,关键词2"

退出码：0 成功 / 1 参数或配置错误 / 2 接口或网络错误 / 3 部分 URL 失败（其余成功）

计费：Search 与 Fetch 分开计费，均按“成功请求次数”收费（5 元 / 1000 次），
      无结果或失败不计费；count 大小不影响单价 -> 尽量一次多拿结果、少发请求。
"""


def add_common(p):
    g = p.add_argument_group("通用选项")
    g.add_argument("--json", action="store_true", help="输出处理后的 JSON")
    g.add_argument("--raw", action="store_true", help="输出接口原始响应 JSON")
    g.add_argument("--dry-run", action="store_true", help="只打印将要发送的请求，不真正调用 API")
    g.add_argument("--timeout", type=float, default=None, help="单次请求超时秒数（search 默认 30，fetch 默认 60）")
    g.add_argument("--key-file", default=None, help="API Key 文件路径（默认 skill 根目录/DeepSeek_API_Key）")


def add_cache(p):
    g = p.add_argument_group("fetch 缓存")
    g.add_argument("--no-cache", action="store_true", help="禁用读取缓存（仍然写入）")
    g.add_argument("--cache-ttl", default=86400, help="缓存有效期秒数，0 表示永不过期（默认 86400）")
    g.add_argument("--cache-dir", default=None, help="缓存目录（默认 $TMPDIR/deepseek-search-cache）")


class _Parser(argparse.ArgumentParser):
    """让 argparse 的用法错误统一走 exit 1，而不是默认的 exit 2（2 留给了接口错误）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        raise UsageError(message)


def build_parser():
    parser = _Parser(
        prog="deepseek-search",
        description="DeepSeek 网页搜索 / 网页抓取 CLI（面向 LLM Agent：先摘要、按需精读、少调用省 token）",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="deepseek-search " + VERSION)
    sub = parser.add_subparsers(dest="cmd", metavar="{search,fetch}")

    # ---- search ----
    sp = sub.add_parser(
        "search",
        help="搜索网页（返回精简候选摘要）",
        description="搜索网页，默认只返回摘要（need_content=false），并在本地去重/过滤/重排。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例：
  search "DeepSeek V4 发布" --count 5 --ori-question "DeepSeek V4 什么时候发布" \\
      --min-score 1.5 --sort authority
  search "模型" --site github.com --site huggingface.co --after 2025
  search "定价" --phrase "API 定价" --exclude-phrase "招聘" --lang zh,en
""",
    )
    sp.add_argument("query", help="搜索词（1~128 字符，可直接写 site:/lang:/after: 等语法）")
    sp.add_argument("--count", default=10, help="返回条数 1~50（默认 10；只改条数不改价格）")
    sp.add_argument("--ori-question", default=None, help="用户原始问题（1~128 字符），帮助理解搜索意图")
    sp.add_argument("--localization", default=None, help="用于本地化的 IP 地址（一般不用）")
    sp.add_argument("--need-content", action="store_true", help="在结果里返回网页正文（默认关闭，费 token）")
    sp.add_argument("--site", action="append", metavar="DOMAIN", help="限定站点，可重复（多个用 OR 连接）")
    sp.add_argument("--exclude-site", action="append", metavar="DOMAIN", help="排除站点，可重复")
    sp.add_argument("--exclude", action="append", metavar="KEYWORD", help="排除关键词，可重复")
    sp.add_argument("--phrase", action="append", metavar="STR", help='精确匹配短语 "...", 可重复')
    sp.add_argument("--exclude-phrase", action="append", metavar="STR", help='排除短语 -"...", 可重复')
    sp.add_argument("--lang", default=None, metavar="LANGS", help="限定语言，如 zh 或 zh,en")
    sp.add_argument("--after", default=None, metavar="DATE", help="发布日期下界，如 2025 或 2025-01-01")
    sp.add_argument("--before", default=None, metavar="DATE", help="发布日期上界")
    post = sp.add_argument_group("本地后处理（对应改进建议 步骤2）")
    post.add_argument("--min-score", default=0, help="丢弃 rerank_score 低于该值的结果（推荐 1.5，默认 0=不过滤）")
    post.add_argument("--min-authority", default=-2, help="只保留 authority >= N 的结果（-2=不过滤，3=极权威）")
    post.add_argument("--sort", default="rank", choices=["rank", "score", "authority", "date"],
                      help="排序：rank=原始顺序（默认）；authority=权威>分数>日期；score；date")
    post.add_argument("--limit", default=0, help="后处理后最多保留条数（0=不限）")
    post.add_argument("--no-dedupe", action="store_true", help="关闭 URL 去重")
    post.add_argument("--max-summary", default=800, help="每条摘要最多字符数（默认 800；0=不限，建议保留全文）")
    post.add_argument("--max-content", default=1500, help="--need-content 时每条正文最多字符数（默认 1500；0=不显示）")
    add_common(sp)

    # ---- fetch ----
    fp = sub.add_parser(
        "fetch",
        help="抓取网页正文（可一次多个 URL）",
        description="抓取网页并转成 Markdown。一次可传多个 URL；自动截断长正文并缓存，避免重复计费。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例：
  fetch "https://example.com/article"
  fetch "https://a.com/x" "https://b.com/y" --max-chars 6000
  fetch "https://example.com/very-long" --keywords "价格,限制" --keyword-pad 1
""",
    )
    fp.add_argument("urls", nargs="+", help="一个或多个 http(s) URL")
    fp.add_argument("--max-chars", default=10000, help="单篇正文最多保留字符数（默认 10000；0=不截断）")
    fp.add_argument("--keywords", action="append", metavar="KW[,KW...]",
                    help="只摘录命中关键词的段落（含前后文），可重复/逗号分隔")
    fp.add_argument("--keyword-pad", default=1, help="关键词摘录时保留的前后段落数（默认 1）")
    fp.add_argument("--delay", default=0.2, help="多个 URL 之间的间隔秒数（默认 0.2，避免触发域名 QPS 限制）")
    fp.add_argument("--fail-fast", action="store_true", help="任一 URL 失败立即退出（默认继续，最后 exit 3）")
    add_cache(fp)
    add_common(fp)
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
        if not getattr(args, "cmd", None):
            parser.print_help()
            return EXIT_USAGE
        if args.cmd == "search":
            return run_search(args)
        return run_fetch(args)
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
        err(describe_error(exc.status, exc.message, getattr(args, "cmd", "api")))
        return EXIT_API
    except KeyboardInterrupt:
        note("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
