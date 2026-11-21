# deepseek-search

基于 [DeepSeek 网页搜索与抓取 API](https://api-docs.deepseek.com/zh-cn/guides/search) 与 [联网工具（服务端深度思考搜索）](https://api-docs.deepseek.com/zh-cn/guides/web_tools) 的 Claude / pi skill。提供三件事：实时网页搜索、网页正文抓取、多步深度思考搜索（服务端自主检索并带引用作答）。内置「先摘要、按需精读、证据不足再升级」的检索流程。

## 文件结构

```
.
├── SKILL.md                      # skill 入口：路由 / 命令 / 边界规则（agent 自动发现）
├── scripts/
│   ├── deepseek_search.py        # search / fetch 主体实现（Python 3 标准库）
│   ├── deepseek_deep_think.py    # 深度思考搜索（方案 A，独立实现，不依赖上一个）
│   ├── deepseek-search.sh        # 兼容包装器（首参 deep-think 时转发）
│   ├── deepseek-deep-think.sh    # 深度思考搜索专用包装器
│   ├── selftest.py               # search / fetch 离线自测
│   └── selftest_deep_think.py    # deep-think 离线自测
├── .cache/fetch/                 # fetch 缓存（自动生成，已 gitignore）
├── .cache/deep-think/            # 历史状态 / 日志 / 原始响应 dump（已 gitignore）
├── DeepSeek_API_Key              # API 密钥（本地文件，已被 .gitignore 忽略）
└── README.md                     # 本文件
```

## 依赖

- Python 3.7+（macOS 可用系统自带 `python3`；无需 curl / jq）

## 用法

```bash
# 搜索：默认只返回摘要，本地去重/过滤/按权威重排
bash scripts/deepseek-search.sh search "DeepSeek V4 发布" --count 5 --min-score 1.5 --sort authority

# 带上用户原问题，让搜索服务更懂意图
bash scripts/deepseek-search.sh search "模型 定价" --ori-question "DeepSeek API 现在多少钱" --count 8

# 抓取网页正文（可一次多个，自动截断 + 缓存）
bash scripts/deepseek-search.sh fetch "https://example.com/a" "https://example.com/b" --max-chars 8000

# 只摘录命中关键词的段落（含前后各 1 段）
bash scripts/deepseek-search.sh fetch "https://example.com/very-long" --keywords "价格,限制"

# 查看完整参数
bash scripts/deepseek-search.sh --help
```

深度思考搜索（服务端多步检索，默认只返回 `answer` + `citations`）：

```bash
# 无法在请求前写出最终 query 的问题，交给服务端模型自己搜
bash scripts/deepseek-deep-think.sh "对比埃菲尔铁塔和东京塔哪个更高，查官网确认"

# 等价写法
bash scripts/deepseek-search.sh deep-think "同一问题"

# 结构化输出 / 多一行检索轨迹 / 诊断视图
bash scripts/deepseek-deep-think.sh "问题" --json
bash scripts/deepseek-deep-think.sh "问题" --summary
bash scripts/deepseek-deep-think.sh "问题" --verbose

# 多轮（同一个 skill 私有状态文件）
bash scripts/deepseek-deep-think.sh "追问" \
  --history .cache/deep-think/history.json \
  --save-history .cache/deep-think/history.json

# 免费自检：只打印请求体，不调用 API
bash scripts/deepseek-deep-think.sh "问题" --dry-run
```

也支持 `python3 scripts/deepseek_search.py ...` 直接调用。

### 搜索语法 flag

| flag | 作用 |
| --- | --- |
| `--site DOMAIN` | 限定站点，可重复（多个自动 `OR` 连接） |
| `--exclude-site DOMAIN` | 排除站点 |
| `--exclude KEYWORD` | 排除关键词 |
| `--phrase "文本"` | 精确匹配短语 |
| `--exclude-phrase "文本"` | 排除短语 |
| `--lang zh,en` | 限定语言 |
| `--after` / `--before` | 限定发布日期 |

也可以直接在 query 里写 `site:`、`lang:`、`after:` 等原生操作符。

### 本地后处理（改进建议 步骤2）

| flag | 作用 |
| --- | --- |
| `--min-score 1.5` | 丢弃 `rerank_score < 1.5` 的低质结果（默认 0 = 不过滤） |
| `--min-authority 3` | 只保留 `authority >= 3` 的极权威来源（默认 -2 = 不过滤） |
| `--sort rank\|score\|authority\|date` | 排序方式；`authority` = 权威 > 分数 > 日期（新在前） |
| `--limit N` | 后处理后最多保留 N 条 |
| `--no-dedupe` | 关闭 URL 去重（默认去重：忽略 fragment、尾斜杠、`utm_*` 等跟踪参数） |
| `--max-summary N` | 每条摘要字符上限（默认 800，覆盖官方摘要长度，不再截到 400） |
| `--max-content N` | `--need-content` 时正文上限（默认 1500；0 = 不显示） |
| `--budget N` | 本次结果摘要的**字符总预算**（默认 0=不限）：按排名递减分配，份额不足 120 字符的条目只留标题+URL |
| `--full-meta` | 输出四行完整标签（默认精简：标题行带 `(站点 · 日期 · 分数 · authN)`） |

过滤后为空会自动回退为不过滤（stderr 告警），不会给你一个空列表。

### fetch 相关

| flag | 作用 |
| --- | --- |
| `--max-chars N` | 单篇正文上限，默认 10000；超出追加 `[…已截断，原文 N 字符…]` |
| `--keywords "a,b"` | 段落级关键词摘录（保留命中段 + 前后各 `--keyword-pad` 段，默认 1） |
| `--delay 0.2` | 多 URL 之间的请求间隔（Fetch 有账号 25 QPS + 域名级 QPS 限制） |
| `--max-urls N` | 一次最多抓几个 URL（默认 3，0 = 不限）：防止一口气拉全文爆上下文 |
| `--total-max-chars N` | 本次所有 URL 正文的**字符总预算**（默认 0=不限）：按剩余篇数均分、余额回流，份额不足 120 字符则省略该篇正文 |
| `--fail-fast` | 任一 URL 失败立即退出（默认继续，最后 exit 3） |
| `--no-cache` / `--cache-ttl SECONDS` / `--cache-dir DIR` | 缓存控制：默认按 url 缓存 1 天，`0` 表示永不过期（`--no-cache` 只禁读、仍写入） |

### 上下文预算（--budget / --total-max-chars）

`--max-summary` 是**逐条**上限，总量仍不可预测（`--count 10 --max-summary 800` 最坏 8000+ 字符）。加总预算就能把上下文占用变成确定值：

```bash
# 6~10 条结果，摘要总字符封顶 4000；超出按排名递减分配
bash scripts/deepseek-search.sh search "DeepSeek V4" --count 10 --budget 4000

# 3 个 URL，正文合计封顶 8000（先按剩余篇数均分，某篇用不完的额度回流给后面的）
bash scripts/deepseek-search.sh fetch "URL1" "URL2" "URL3" --total-max-chars 8000
```

行为约定（避免误解）：

| 情况 | 输出 |
| --- | --- |
| 额度够 | 摘要/正文原样显示，无任何标记 |
| 额度不够但 ≥120 字符 | 截到额度长度 + `…[原文 N 字]` |
| 额度 <120 字符 | 只省略摘要，保留标题/URL/元信息，标注 `[摘要按 --budget 省略，原 N 字]` 或 `[正文按 --total-max-chars 预算省略，原文 N 字符]` |
| 不限（默认） | `--budget 0` / `--total-max-chars 0`，行为与之前一致 |

末尾会打印 `-- 预算 1904/2000 字符（截断 5 条、省略 1 条摘要）`，方便判断要不要补抓。

### 输出模式

- 默认（精简，面向模型）：
  ```
  [1] 标题  (站点 · 2026-04-24 · 2.57 · auth4)
      https://example.com/article

  摘要正文（已去噪）
  ```
  相比四行标签格式约省 **6~8%** 字符：去掉标签列、`site_icon`、`unknown` 占位；
  摘要会清理 HTML 标签/Markdown 强调/与标题重复的 H1/新闻页样板行（`发布于：…`、`来源：…`、`责任编辑：…`），
  但保留表格行、省略号与长句；URL 会去掉 `utm_*`/`spm`/`scm`/空值参数等跟踪尾巴。
- `--full-meta`：回到四行定宽标签格式（人读/调试更直观）。
- `--json`：处理后的结构化 JSON（含 `stats` 或 `failures`）。
- `--raw`：接口原始响应。
- `--dry-run`：只打印将要发送的 endpoint + payload，不发请求。

## 深度思考搜索（deep-think）

方案 A：把整个多步检索外包给 DeepSeek 服务端。客户端**不执行任何工具**，只发一次对话请求、解析响应、输出精简视图。

```bash
bash scripts/deepseek-deep-think.sh "<一个可独立表述的检索目标>" [选项]
```

| 参数 | 作用 |
| --- | --- |
| `--protocol chat\|messages\|responses` | 对话协议，默认 `chat`（`/chat/completions`） |
| `--model` | 服务端模型，默认 `deepseek-flash` |
| `--stream` | SSE 流式（默认非流式，最稳） |
| `--brief` / `--summary` / `--verbose` | 只留正文 / 加一行检索轨迹 / 诊断视图（互斥） |
| `--json` | 结构化输出，默认 `{"answer","citations"}` |
| `--max-citations N` | 引用上限，默认 8（0=不限） |
| `--max-answer-chars N` | 回答正文上限（0=不限） |
| `--thinking-chars N` | `--verbose` 时每步思考保留字符数，默认 120 |
| `--max-tokens N` | 输出上限（`responses` 映射为 `max_output_tokens`；0=不传） |
| `--history FILE` / `--save-history FILE` | 读取 / 写回 skill 私有状态（默认 `.cache/deep-think/history.json`） |
| `--log [FILE]` | 追加 JSONL 日志（默认 `.cache/deep-think/deep-think.log.jsonl`） |
| `--dump-raw [FILE]` | 保存接口原始响应 / SSE 事件到 `.cache/deep-think/raw/` |
| `--dry-run` | 只打印请求体，不调用 API、不计费、不写文件 |
| `--timeout SEC` | 请求超时，默认 180 |

### 三种视图

| 视图 | 触发 | 返回 |
| --- | --- | --- |
| 默认 | 常规调用 | `answer` + `citations`（url + title，去重、标题截断、默认 ≤8 条） |
| 带摘要 | `--summary` | 再加 `trace_summary`（如「搜索3次，打开2页」） |
| 诊断 | `--verbose` | 再加 `thinking_digest`（每步结论句）与 `tool_trace`（action + query/url/pattern） |

不返回：`signature`、`encrypted_content`、`cited_text`、`encrypted_index`、`snippet`、`site_*`、`page.*`、`published_at`、`server_tool_calls` 原文。`usage` 只写 stderr。

### 请求约束（脚本已固化）

- 模型默认 `deepseek-flash`；开启思考（`thinking.type=enabled` / `reasoning.effort=high`）即「深度思考搜索」。
- `tools` 只声明一个 `deepseek_web_search`（Messages 协议带 `name`）。
- **不发送** `tool_choice`、`allowed_domains`、`blocked_domains`、`user_location`、`web_search_options`、`response_format`、`logprobs`。
- 服务端最多约 20 次工具调用（`search` / `open_page` / `find_in_page`）。

### 计费

服务端搜索与抓取**不计费**，只计模型 token（`input_tokens`、`cache_read_input_tokens`、`output_tokens`，其中 `thinking_tokens` 也算输出）。脚本从不调用 `/v1/tools/search`、`/v1/tools/fetch`，因此不产生按次搜索费。`usage.server_tool_use` 只是计数，不是账单项。

### 产物

全部写在 `<skill>/.cache/deep-think/`：`history.json`（多轮状态，原样保留 `kind`/`signature`/`server_tool_calls`）、`deep-think.log.jsonl`（每次一行 usage 与耗时）、`raw/`（`--dump-raw` 的原始响应）。目录不可写直接报错，不回退到系统临时目录。

## 缓存

- 位置：`<skill>/.cache/fetch/`，文件名 = `sha256(url)[:20].json`，内容 `{fetched_at, url, title, content}`
- 覆盖顺序：`--cache-dir` > 环境变量 `DEEPSEEK_SEARCH_CACHE` > `<skill>/.cache/fetch`
- 有效期：默认 1 天（`--cache-ttl 0` 永不过期）；命中时 stderr 打印 `[缓存命中]`，**不发请求、不计费**
- 项目目录只读时自动回退到 `$TMPDIR/deepseek-search-cache` 并告警；显式 `--cache-dir` 不可写则直接报错
- 清理：`rm -rf .cache/fetch`（脚本只判断 TTL，不会自动删文件）

## 退出码

| code | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 参数 / 用法 / 密钥配置错误 |
| 2 | 接口或网络错误（401/402/403/404/429/5xx/超时） |
| 3 | 部分 URL 失败（`fetch` 多 URL 场景，其余结果可用） |

## 密钥

优先级：`--key-file` > 环境变量 `DEEPSEEK_API_KEY` > skill 根目录 `DeepSeek_API_Key`。
文件缺失时报「没有密钥配置」。**不要把密钥明文输出到对话或提交进 git。**
建议 `chmod 600 DeepSeek_API_Key`；仓库内的 `DeepSeek_API_Key_copy` 只是占位示例。

## 安装

本仓库就是 skill 目录：直接放在 skills 目录下即可，脚本按相对位置查找 `DeepSeek_API_Key`（`scripts/` 的上一级）。运行期产物只写本目录内的 `.cache/`。

## 自测

```bash
python3 scripts/selftest.py             # 95 个离线用例：去重/过滤/排序/截断/摘录/缓存/重试
python3 scripts/selftest_deep_think.py  # 65 个离线用例：三协议解析/流式累积/视图/请求体/错误映射/状态往返
```

## 说明

- Search 与 Fetch 均为只读请求；408/429/5xx 自动退避重试（遵守 `Retry-After`）。`deep-think` 只在建连阶段重试，已开始收流后不重试（避免重复生成与重复计费）。
- `rerank_score ≥ 1.5` 为优质结果，`authority ≥ 3` 为极权威来源。
- 计费按「成功请求次数」（Search / Fetch 各 5 元 / 1000 次），与 `count` 无关：一次多拿结果更省钱。
- `deep-think` 的服务端搜索/抓取不计费，只付模型 token；`server_tool_use` 计数仅作观测。
- 字段/语法/错误码/QPS 以[官方文档](https://api-docs.deepseek.com/zh-cn/guides/search)与[联网工具文档](https://api-docs.deepseek.com/zh-cn/guides/web_tools)为准（代码里不做静态副本，避免过期）。
