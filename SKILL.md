---
name: deepseek-search
description: 使用 DeepSeek 搜索引擎做实时网页搜索、把网页抓成 Markdown，或做多步深度思考搜索（服务端自主搜索/打开网页/页内查找并带引用作答）。当用户需要联网查最新资讯/时效信息、核实事实、找一手来源、精读某个网页正文，或需要多跳查证、交叉验证、综述对比时使用。
---

# DeepSeek 搜索 / 抓取 / 深度思考搜索

三条命令，一个原则：**先最省，证据不够再升级**。

## 选哪个命令（路由由你判断，脚本只执行）

有两层决策，别混：

- **路由层**：调哪个脚本，由**你自己**（本地 agent 的模型）读本文后判断。**没有决策脚本、没有规则引擎、没有 `decide.py`**。
- **执行层**：`search`/`fetch` 的 query 与 URL 由你在调用前写死，代码照做；`deep-think` 一旦调用，**内部搜什么、打开哪些页由 DeepSeek 服务端模型自决**（服务端工具，`tool_choice` 无法强制）。

| 路由 | 判定条件 | 动作 |
| --- | --- | --- |
| `no_search` | 无事实性需求、不涉时效、答案已在上下文（问候、改写、纯分析） | 直接回答，不联网 |
| `only_search` | 单点短事实 / 封闭式问题；**请求前就能写出最终 query**；答案大概率在摘要里 | `search`（`need_content=false`），取 top 3 summary 直接答 |
| `search_and_fetch_once` | 摘要相关但不完整、需原文原话/定义/参数表/代码；候选源少（2–3 个权威站）；或用户直接给了 URL | `search` 筛高分 → `fetch` top 1–3 页；给了 URL 就直接 `fetch` |
| `deep_think` | **无法在请求前可靠写出最终 query**，或一次搜索后大概率要追问/验证 | `deepseek-deep-think.sh` |

升级路径：先 `only_search`，摘要不够 → `search_and_fetch_once`，仍不足 → `deep_think`。
**不要一上来就 deep_think**；也**不要**把一轮 `fetch` 当成深度搜索——深度搜索的核心是读完材料后**生成新的检索意图再搜**，这个循环在服务端。

`deep_think` 的触发信号（命中任一）：

1. 多跳问题（A→B→C 串联，路径提问时不可预判）；2. 来源冲突需多 query 交叉验证；
3. 探索性/综述/对比调研（无唯一正确网页）；4. 追踪演变与历史版本；
5. 时效 + 新鲜度判断；6. 第一轮质量差（`rerank_score` 普遍偏低、无权威源）；
7. 复杂推理与事实混合；8. 用户显式要求「深度研究/仔细核实/查官网对比」；
9. 需要图片搜索（只有服务端联网工具支持）；10. 愿以延迟换质量。

`rerank_score ≥ 1.5` 为优质结果的参考信号（不是硬规则）：优质结果 ≥2 且一致 → `only_search`；0 个 → 倾向 `deep_think`；1 个但要原文 → `search_and_fetch_once`；≥2 但矛盾 → `deep_think`。

## 命令一览

```bash
bash scripts/deepseek-search.sh search "<关键词>" [选项]      # 只拿摘要
bash scripts/deepseek-search.sh fetch "<URL>" [<URL>…] [选项]  # 抓正文
bash scripts/deepseek-deep-think.sh "<问题>" [选项]            # 深度思考搜索（方案 A）
# 等价写法：bash scripts/deepseek-search.sh deep-think "<问题>" [选项]
```

完整参数：加 `--help`（不调用 API、不花钱）。`--dry-run` 打印将要发送的请求体，同样免费。
两个入口的 `--help` 各自独立：`deepseek-search.sh --help` 只讲 search/fetch，deep-think 用 `deepseek-deep-think.sh --help`（含四层路由提示）。

---

## search：粗搜摘要（默认不取正文）

```bash
bash scripts/deepseek-search.sh search "<关键词>" \
  --ori-question "<用户原问题>" --count 8 --min-score 1.5 --sort authority
```

- **关键词化**：把长问题压成 2~6 个检索词（query ≤128 字符）。
- 站点/时效/精确匹配：`--site`、`--exclude-site`、`--phrase`、`--exclude-phrase`、`--exclude`、`--lang zh,en`、`--after`、`--before`（也可直接写 `site:`/`lang:`/`after:` 语法）。
- 脚本内置后处理：URL 归一化去重 → `rerank_score < 1.5` 过滤 → 权威优先重排；全被过滤时自动回退并告警。
- 上下文紧张用总预算：`--budget 4000`（摘要合计封顶，被压的条目保留标题/URL/元信息并标注）。
- 读结果：`[1] 标题 (站点 · 日期 · 分数 · authN)` + URL + 摘要。`score ≥ 1.5` 优质，`authN ≥ 3` 极权威。
- **Summary 已写明答案 → 直接回答，不要 fetch。**

## fetch：按需精读（一次抓多个）

```bash
bash scripts/deepseek-search.sh fetch "<URL1>" "<URL2>" --max-chars 8000 --keywords "<词1,词2>"
```

- 一轮最多 3 个 URL（默认硬拦）；只抓最可能含细节的链接。
- `--max-chars` 单篇上限（默认 10000），超长追加 `[…已截断…]`；`--keywords` 只摘录命中段落（含前后各 1 段）；`--total-max-chars` 给多篇设总预算。
- 缓存：`<skill>/.cache/fetch/`（key=url，默认 1 天），命中不发请求、不计费；`--cache-ttl 0` 永不过期。
- 多 URL 时单篇失败不中断（stderr 提示，末尾 exit 3）；要失败即停用 `--fail-fast`。

## deep-think：深度思考搜索（方案 A）

把整个多步检索外包给 DeepSeek 服务端：模型自己生成检索词、打开网页、页内查找、边推理边求证，最后带引用作答。**客户端不执行任何工具，只发一次对话请求。**

```bash
bash scripts/deepseek-deep-think.sh "<一个可独立表述的检索目标>"
bash scripts/deepseek-deep-think.sh "<问题>" --summary     # 多一行检索轨迹
bash scripts/deepseek-deep-think.sh "<问题>" --json        # {"answer","citations"}
bash scripts/deepseek-deep-think.sh "<问题>" --stream      # SSE 流式（默认非流式，最稳）
bash scripts/deepseek-deep-think.sh "<追问>" \
  --history .cache/deep-think/history.json \
  --save-history .cache/deep-think/history.json            # 多轮
```

常用参数：`--protocol chat|messages|responses`（默认 `chat`）、`--model`（默认 `deepseek-flash`）、`--max-tokens`、`--max-citations`（默认 8）、`--max-answer-chars`、`--thinking-chars`、`--log`、`--dump-raw`、`--timeout`、`--dry-run`。

### 返回什么（默认视图只有两样东西）

```json
{"answer": "...", "citations": [{"url": "...", "title": "..."}]}
```

- `answer` 与 `citations` 是**你唯一需要消费的内容**；`citations` 只有 url + title，**不带正文**，要原文再 `fetch` 那个 URL。
- 思考文本、工具调用轨迹、`signature`、`cited_text`、`snippet`、`usage` 都**不在默认返回值里**。
- `--summary` 追加一行 `trace_summary`（如「搜索3次，打开2页，页内查找1次」），只用来判断检索量是否异常，**不要在下轮复述**。
- `--verbose` 是诊断视图（加 `thinking_digest` 与 `tool_trace`）：**不要把它回传进后续上下文**，需要留档就自己落盘。
- `--brief` 只输出回答正文。
- `usage`（token 与工具计数）只写 stderr，供你判断成本，不是返回值。

调用粒度：**一次调用只解决一个可独立表述的检索目标**；别把一个大问题机械切碎，也别在 `deep-think` 之后指望还有更高级的检索路径（它已是顶层）。

### 多轮

`--history` / `--save-history` 指向**同一个 skill 私有状态文件**（默认 `.cache/deep-think/history.json`），脚本负责原样回传上一轮的协议字段（`server_tool_calls` / `kind` / `signature`）。**你不需要、也不应该**把这些字段搬进上下文。

- 上一轮用过联网工具的回复**不能续写**，中断就重发原始请求。
- 状态文件与协议绑定：`chat` 写的状态不能拿去跑 `--protocol responses`（脚本直接报错拒绝，不会发起请求白花 token）。
- 没有有效 `signature` 时，之前读过的网页不会拼回上下文（回答正文仍在）。
- 只想问一个新问题，就别带 `--history`。

### 边界（照做）

- `tool_choice` 不支持强制联网；本脚本从不发送，也不要自己拼请求去试。
- `deepseek_web_search` 不能与其他服务端工具（含 `web_search`、`custom`、`namespace`）同时声明；`allowed_domains` / `blocked_domains` / `user_location` 不支持。
- 服务端最多约 **20 次**工具调用后作答；问题越模糊步数越多、越贵。
- 图片搜索只能走服务端联网工具（在问题里说明要看图即可），`search` 接口不支持。
- 生成中失败（HTTP 200 但带 `error` / `status: failed`）脚本会打印错误 + usage 并 exit 2；`content_filter` / `length` 不会伪装成功。

---

## 边界情况

| 情况 | 处理 |
| --- | --- |
| `search` 结果为空 | 说明没有相关网页资料，不 fetch；可换关键词再搜 1 次 |
| 过滤后为空 | 脚本已自动回退；仍不理想就换 query |
| fetch 404 / 403 | 该链接读不到，跳过并说明，不要反复重试 |
| fetch 429 | 已自动重试；仍失败则减少 URL 数、稍后再试 |
| 402 | 余额不足：告知用户充值，**不重试**（deep-think 同样） |
| 401 | 密钥问题：检查 `DeepSeek_API_Key` 或 `DEEPSEEK_API_KEY` |
| deep-think 400 | 看提示：工具重复声明 / 与其他工具同声明 / 强制 `tool_choice` / `allowed_domains` / `Content Exists Risk` / 上下文超限 / 上一轮用过联网工具不能续写 |
| deep-think 步数暴涨 | 问题太模糊：缩小范围重问，或用 `--max-tokens` 限输出 |
| 多数页面读不到 | 如实说明检索受限，给摘要级结论与已知来源 |

退出码：`0` 成功 / `1` 参数或密钥问题 / `2` 接口或网络错误 / `3` 部分 URL 失败（fetch 多 URL，其余可用）。

## 两本账（省钱硬规则）

| 命令 | 计费 |
| --- | --- |
| `search` / `fetch` | 按**成功请求次数**，各 ¥5/1000 次；`count` 不影响单价 |
| `deep-think` | 服务端搜索/抓取**不计费**，只计模型 token（`thinking_tokens` 也算输出） |

省 `search`/`fetch`：默认不开 `--need-content`；一次 `--count 8` 优于 4 次 `--count 2`；同一 URL 靠缓存；一轮 fetch ≤3 URL。

省 `deep-think`：一次问清、缩小范围；`--max-tokens` 限输出；多轮只带必要历史；`--dry-run` 先看请求体；每次看 stderr 的 `input/cache_read/output/thinking` 与 `server_tool_use` 计数，步数暴涨是最早的预警。

| 场景 | 最省做法 |
| --- | --- |
| 已知一个 URL，要摘要/原文 | `fetch`（一次 ¥0.005） |
| 找候选链接、快速摸底 | `search`（`need_content=false`） |
| 多步交叉验证、边想边查 | `deep-think`（服务端搜索免费，只付推理 token） |
| 离线采集/自定义排序 | `search` + `fetch`（缓存复用） |

## 产物位置

运行期文件**只写本 skill 目录内部**：`.cache/fetch/`（fetch 缓存）、`.cache/deep-think/`（`history.json` 状态、`deep-think.log.jsonl` 日志、`raw/` 原始响应 dump）。目录不可写时直接报错，不回退到系统临时目录。清理：`rm -rf .cache/`。

## 需要原始细节时

- 联网工具（服务端搜索/深度思考搜索）：https://api-docs.deepseek.com/zh-cn/guides/web_tools
- 网页搜索与抓取：https://api-docs.deepseek.com/zh-cn/guides/search
