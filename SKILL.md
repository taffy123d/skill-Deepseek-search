---
name: deepseek-search
description: 使用 DeepSeek 搜索引擎做实时网页搜索、把网页抓成 Markdown，或做多步深度思考搜索（服务端自主搜索/打开网页/页内查找并带引用作答）。当用户需要联网查最新资讯/时效信息、核实事实、找一手来源、精读某个网页正文，或需要多跳查证、交叉验证、综述对比时使用。
---

# DeepSeek 搜索 / 抓取 / 深度思考搜索

三条命令，一个原则：**先最省，证据不够再升级**。

## 选哪个命令（路由与成本一起判，脚本只执行）

两层决策，别混：

- **路由层**：调哪个脚本，由**你自己**（本地 agent 的模型）读本文后判断。**没有决策脚本、没有规则引擎、没有 `decide.py`**。
- **执行层**：`search`/`fetch` 的 query 与 URL 由你在调用前写死；`deep-think` 一旦调用，**内部搜什么、打开哪些页由服务端模型自决**（`tool_choice` 无法强制）。

**成本已固化在「动作」列里——照表执行即可，不要为省钱额外权衡**（两者差一个量级以内，选错路线的代价远大于省下的钱）：

| 路由 | 判定条件 | 动作（含省钱默认值） |
| --- | --- | --- |
| `no_search` | 无事实性需求、不涉时效、答案已在上下文（问候、改写、纯分析） | 直接回答，**不联网**（唯一省 100% 的选项） |
| `only_search` | 单点短事实 / 封闭式问题；**请求前就能写出最终 query**；答案大概率在摘要里 | `search --count 8 --min-score 1.5`；摘要够就答，不 fetch |
| `search_and_fetch_once` | 摘要不够但有明确缺口（原文/定义/参数表/代码）；候选源少；或用户直接给了 URL | `search` 筛高分 → `fetch` ≤3 页；给了 URL 就直接 `fetch` |
| `deep_think` | **无法在请求前可靠写出最终 query**，或一次搜索后大概率要追问/验证 | `deep-think`；**核对类加 `--effort low`**，多跳/综述用默认 `high` |

成本硬规则（已固化，不必另算）：

1. **时段**：模型 token 高峰（工作日 09:00–12:00、14:00–18:00 北京时间）是空闲的 2 倍 → 不急的批量活先 `bash scripts/deepseek-search.sh peak` 确认，排到空闲时段；用户在等就别等。
2. **两本账**：`search`/`fetch` ¥0.005/次（与 `count` 无关，一次多拿）；`deep-think` 搜索免费只计 token，单搜索 ≈¥0.004，**不比 search 贵**——别为省 ¥0.001 纠结选型。
3. **真正省钱的开关**：不调工具 > 排空闲（−50%）> `--effort low`（−36%）> `--max-tokens` > 多轮用 `chat`（`responses` 贵 3–7 倍）。`--brief` / `--max-answer-chars` / 视图裁剪**只省你的上下文、不省钱**。

升级路径：`only_search` → 摘要不够 `search_and_fetch_once` → 仍不足 `deep_think`。**不要一上来就 deep_think**；一轮 `fetch` ≠ 深度搜索（深度搜索的核心是读完材料后生成新检索意图再搜，该循环在服务端）。

`deep_think` 触发信号（命中任一）：多跳串联、来源冲突需交叉验证、综述/对比调研、追踪版本演变、时效+新鲜度判断、第一轮质量差、推理与事实混合、用户显式要求深挖、需要图片搜索、愿以延迟换质量。

`rerank_score ≥ 1.5` 为优质（参考信号，非硬规则）：优质 ≥2 且一致 → `only_search`；0 个 → `deep_think`；1 个但要原文 → `search_and_fetch_once`；≥2 但矛盾 → `deep_think`。

失败不重试 `deep-think`（步数不可参数化，上限约 20）：`answer` 明确说「未找到可靠信息」且问题本可固化成 query 时，**降级调一次 `search`**（¥0.005）。

## 命令一览

```bash
bash scripts/deepseek-search.sh search "<关键词>" [选项]      # 只拿摘要
bash scripts/deepseek-search.sh fetch "<URL>" [<URL>…] [选项]  # 抓正文
bash scripts/deepseek-deep-think.sh "<问题>" [选项]            # 深度思考搜索（方案 A）
bash scripts/deepseek-peak.sh [选项]                          # 当前是高峰还是空闲（读系统时间）
# 等价写法：bash scripts/deepseek-search.sh deep-think "<问题>" / bash scripts/deepseek-search.sh peak
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

常用参数：`--protocol chat|messages|responses`（默认 `chat`）、`--model`（默认 `deepseek-flash`）、`--effort low|high|max`（默认 `high`）、`--converge`（软收敛提示，默认关）、`--max-tokens`、`--max-citations`（默认 8）、`--max-answer-chars`、`--thinking-chars`、`--log`、`--dump-raw`、`--timeout`、`--dry-run`。

**`--effort` 怎么选**（默认 `high`）：单点核对/只要结论 → `low`（实测同题省 ~36%）；多跳、来源冲突、综述 → 用默认 `high`（推理深度不够会答错）。`--converge` 加一句「够用即停」，只是提示词、不保证生效，默认关。

### 返回什么（默认视图只有两样东西）

```json
{"answer": "...", "citations": [{"url": "...", "title": "..."}]}
```

- `answer` 与 `citations` 是**你唯一需要消费的内容**；`citations` 只有 url + title，**不带正文**，要原文再 `fetch` 那个 URL。
- 默认**只返回** `answer` + `citations`：没有状态行、没有 `usage`、没有思考/轨迹/`signature`/`cited_text`/`snippet`。
- `usage` 仅在 `--summary` / `--verbose` 时打到 stderr，或 `--log` 落盘；`--brief` 只输出正文。
- `--summary` 追加一行 `trace_summary`（如「搜索3次，打开2页」），只用来判断检索量是否异常，**不要在下轮复述**。
- `--verbose` 是诊断视图：**不要回传进后续上下文**，要留档自己落盘。

调用粒度：**一次调用只解决一个可独立表述的检索目标**；别把一个大问题机械切碎，也别在 `deep-think` 之后指望还有更高级的检索路径（它已是顶层）。

### 多轮

`--history` / `--save-history` 指向**同一个 skill 私有状态文件**（默认 `.cache/deep-think/history.json`），脚本负责原样回传上一轮的协议字段（`server_tool_calls` / `kind` / `signature`）。**你不需要、也不应该**把这些字段搬进上下文。

- 上一轮用过联网工具的回复**不能续写**，中断就重发原始请求。
- 状态文件与协议绑定：`chat` 写的状态不能拿去跑 `--protocol responses`（直接报错，不发请求）。
- **多轮优先用 `chat` 协议**：`responses` 每轮要把 output items 全塞回 `input`，贵 3–7 倍。
- 没有有效 `signature` 时，之前读过的网页不会拼回上下文（回答正文仍在）。
- 只想问一个新问题，就别带 `--history`。

### 边界（照做）

- `tool_choice` 不支持强制联网；本脚本从不发送，也不要自己拼请求去试。
- `deepseek_web_search` 不能与其他服务端工具（含 `web_search`、`custom`、`namespace`）同时声明；`allowed_domains` / `blocked_domains` / `user_location` 不支持。
- 服务端最多约 **20 次**工具调用后作答；**没有限步参数**（20 是硬顶，不是可调项），能调的只有 `--effort` / `--converge` / `--max-tokens`。
- 不要期望用「继续搜」的追加调用延长检索，也不要对答案不满意就自动重试 deep-think。
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
| deep-think 400 | 看提示：重复声明 / 与其他工具冲突 / 强制 `tool_choice` / `Content Exists Risk` / 上下文超限 / 用过联网工具不能续写 |
| deep-think 步数暴涨 | 问题太模糊：缩小范围重问，或用 `--effort low` / `--max-tokens` 限成本 |
| 多数页面读不到 | 如实说明检索受限，给摘要级结论与已知来源 |

退出码：`0` 成功 / `1` 参数或密钥问题 / `2` 接口或网络错误 / `3` 部分 URL 失败（fetch 多 URL，其余可用）。

## 产物位置

运行期文件**只写本 skill 目录内部**：`.cache/fetch/`（缓存）、`.cache/deep-think/`（`history.json`、`deep-think.log.jsonl`、`raw/`）。目录不可写直接报错，不回退 `$TMPDIR`。清理：`rm -rf .cache/`。`deepseek-peak.sh` 不写文件、不联网。

## 需要原始细节时

- 联网工具（服务端搜索/深度思考搜索）：https://api-docs.deepseek.com/zh-cn/guides/web_tools
- 网页搜索与抓取：https://api-docs.deepseek.com/zh-cn/guides/search

