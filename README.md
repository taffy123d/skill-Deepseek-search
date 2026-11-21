# deepseek-search

基于 [DeepSeek 网页搜索与抓取 API](https://api-docs.deepseek.com/zh-cn/guides/search) 的 Claude / pi skill。提供实时网页搜索与网页正文抓取能力，并内置「先摘要、按需精读」的检索召回流程。

## 文件结构

```
.
├── SKILL.md                    # skill 入口：工作流 / 命令 / 边界规则（agent 自动发现）
├── scripts/
│   ├── deepseek_search.py      # 主体实现（Python 3 标准库，无第三方依赖）
│   ├── deepseek-search.sh      # 兼容包装器（旧调用方式仍可用）
│   └── selftest.py             # 离线自测（不访问网络）
├── DeepSeek_API_Key            # API 密钥（本地文件，已被 .gitignore 忽略）
└── README.md                   # 本文件
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

过滤后为空会自动回退为不过滤（stderr 告警），不会给你一个空列表。

### fetch 相关

| flag | 作用 |
| --- | --- |
| `--max-chars N` | 单篇正文上限，默认 10000；超出追加 `[…已截断，原文 N 字符…]` |
| `--keywords "a,b"` | 段落级关键词摘录（保留命中段 + 前后各 `--keyword-pad` 段，默认 1） |
| `--delay 0.2` | 多 URL 之间的请求间隔（Fetch 有账号 25 QPS + 域名级 QPS 限制） |
| `--fail-fast` | 任一 URL 失败立即退出（默认继续，最后 exit 3） |
| `--no-cache` / `--cache-ttl SECONDS` / `--cache-dir DIR` | 缓存控制：默认按 url 缓存 1 天，`0` 表示永不过期 |

### 输出模式

- 默认：面向模型的精简文本（标题 / URL / 来源·日期·score·authority / Summary），不含 `site_icon` 等噪音。
- `--json`：处理后的结构化 JSON（含 `stats` 或 `failures`）。
- `--raw`：接口原始响应。
- `--dry-run`：只打印将要发送的 endpoint + payload，不发请求。

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

把整个目录复制到 skills 目录即可；脚本按相对位置查找 `DeepSeek_API_Key`（`scripts/` 的上一级）。

## 自测

```bash
python3 scripts/selftest.py     # 26 个离线用例：去重/过滤/排序/截断/摘录/缓存/重试
```

## 说明

- Search 与 Fetch 均为只读请求；408/429/5xx 自动退避重试（遵守 `Retry-After`）。
- `rerank_score ≥ 1.5` 为优质结果，`authority ≥ 3` 为极权威来源。
- 计费按「成功请求次数」（Search / Fetch 各 5 元 / 1000 次），与 `count` 无关：一次多拿结果更省钱。
- 字段/语法/错误码/QPS 以[官方文档](https://api-docs.deepseek.com/zh-cn/guides/search)为准（代码里不做静态副本，避免过期）。
