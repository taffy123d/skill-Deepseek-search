---
name: deepseek-search
description: 使用 DeepSeek 搜索引擎做实时网页搜索，或把网页抓成 Markdown。当用户需要联网查最新资讯/时效信息、核实事实、找一手来源，或需要精读某个网页正文时使用。
---

# DeepSeek 搜索 / 抓取

基于 DeepSeek 自研搜索引擎的实时网页搜索与网页正文抓取（转 Markdown）。脚本只依赖 Python 3 标准库，无需 curl/jq。

## 核心原则（照做，别绕）

1. **先摘要，后原文**：`search` 默认 `need_content=false`，只拿 `summary`/`snippet`/`rerank_score`/`authority` 等元信息；摘要不足以回答时才 `fetch` 原文。
2. **少调用**：Search 与 Fetch 分开按「成功请求次数」计费（5 元 / 1000 次），与 `count` 无关 → 一次多拿几条，别拆成多次小请求；一轮 fetch 不超过 3 个 URL。
3. **不重复花钱**：fetch 结果默认写缓存（key=url，默认 1 天），同一 URL 再读不再计费。
4. **不编造**：结论只能来自本轮结果；读不到就明说读不到。

## 命令

入口：`scripts/deepseek-search.sh`（包装 Python 入口 `scripts/deepseek_search.py`）。
完整参数：`bash scripts/deepseek-search.sh --help`（不调用 API、不花钱）。

### 步骤 1：粗搜（只拿摘要）

```bash
bash scripts/deepseek-search.sh search "<关键词>" \
  --ori-question "<用户原问题>" --count 8 --min-score 1.5 --sort authority
```

- **关键词化**：把长问题压成 2~6 个检索词（query 上限 128 字符），别把整句话塞进去。
- 有站点/时效/精确匹配要求时加 flag（也可直接写在 query 里）：`--site`、`--exclude-site`、`--phrase`、`--exclude-phrase`、`--exclude`、`--lang zh,en`、`--after 2025`、`--before 2025-12-31`。
- 脚本已内置本地后处理：URL 归一化去重（去 fragment/尾斜杠/utm 等跟踪参数）→ `rerank_score < 1.5` 过滤 → 权威优先重排（authority > score > 日期）。若全被过滤掉会自动回退为不过滤并告警。
- 官方文档/政策类问题更吃来源质量；若 `--sort authority` 把自媒体顶到前面，改 `--sort score` 或加 `--min-authority 3`。

### 步骤 2：读结果（不花钱）

每条含：标题、URL、来源/日期、`score`（rerank_score）、`authority`、`Summary`（长摘要，专门给模型读，默认不截断）。

判断规则：
- `score ≥ 1.5` 为优质；`authority ≥ 3` 为极权威（政府/官媒/官方文档）。
- 时效性问题优先看 `date`；日期旧不等于不可用。
- **Summary 已经写明答案 → 直接回答，不要 fetch。**

### 步骤 3：按需精读（只在必要时，一次抓多个）

```bash
bash scripts/deepseek-search.sh fetch "<URL1>" "<URL2>" --max-chars 8000 --keywords "<关键词1,关键词2>"
```

- 一轮最多 3 个 URL，挑最可能含细节的链接；禁止抓无关链接。
- `--max-chars` 为单篇上限（默认 10000），超长会追加 `[…已截断，原文 N 字符…]` 标记；上下文紧张时用 `--keywords` 只摘录命中段落（含前后各 1 段）。
- 多 URL 时单篇失败不中断（stderr 提示，末尾 exit 3）；需要失败即停用 `--fail-fast`。

### 步骤 4：作答

- 用 Summary（+ 必要时原文片段）回答，**标注来源链接**；多来源冲突时说明分歧与各自出处。
- 只引用真正读到的内容；被截断或抓取失败的链接要说明「未能读取全文」。

## 边界情况

| 情况 | 处理 |
| --- | --- |
| `results` 为空 | 说明没有相关网页资料，不要 fetch；可换关键词/放宽语法再搜 1 次 |
| 过滤后为空 | 脚本已自动回退；仍不理想就换 query |
| fetch 404 / 403 | 该链接读不到，跳过并在回答中标注，不要反复重试 |
| fetch 429 | 已自动重试；仍失败则减少 URL 数、稍后再试（Fetch 有账号 25 QPS + 域名级 QPS 限制） |
| 402 | 余额不足：直接告知用户充值，不要重试 |
| 401 | 密钥问题：检查 `DeepSeek_API_Key` 文件或 `DEEPSEEK_API_KEY` 环境变量 |
| 多数页面读不到 | 如实说明检索受限，给出摘要级结论与已知来源 |

退出码：`0` 成功 / `1` 参数或密钥问题 / `2` 接口或网络错误 / `3` 部分 URL 失败（其余成功，可继续使用）。

## 省钱的硬规则

- 默认不开 `--need-content`；只有「必须一次拿全文」的离线采集才开。
- 同一问题不要重复搜索：先看已有结果里有没有答案。
- 一次 `--count 8` 优于 4 次 `--count 2`。
- 同一 URL 不要重复 fetch（缓存默认 1 天；`--cache-ttl 0` 表示永不过期）。
- `--json` / `--raw` 只给程序化处理用，不要为了「看看结构」而多调用；`--dry-run` 可离线检查将要发送的请求。

## 细节文档

请求/响应字段、搜索语法表、错误码、QPS、计费细则：`deepseek-api-reference.md`（需要时再读）。
