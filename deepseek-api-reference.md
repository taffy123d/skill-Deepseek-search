# DeepSeek Search / Fetch API 速查

官方文档：
- 指南：https://api-docs.deepseek.com/zh-cn/guides/search
- Search 接口：https://api-docs.deepseek.com/zh-cn/api/search
- Fetch 接口：https://api-docs.deepseek.com/zh-cn/api/fetch

## 端点与鉴权

| API | Endpoint | 说明 |
| --- | --- | --- |
| Search | `POST https://api.deepseek.com/v1/tools/search` | 按 query 搜索网页，返回结构化结果 |
| Fetch | `POST https://api.deepseek.com/v1/tools/fetch` | 抓取单个网页，返回标题 + Markdown 正文 |

请求头：`Authorization: Bearer <API Key>`、`Content-Type: application/json`。
两者都是只读请求；未知请求字段会被忽略。

## Search 请求字段

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `query` | string | 是 | — | 1~128 个 Unicode 字符，不能全为空白；支持搜索语法 |
| `count` | integer | 否 | 10 | 1~50；只影响条数，不影响价格 |
| `ori_question` | string | 否 | — | 触发搜索的用户原始问题（1~128 字符），帮助理解意图 |
| `localization` | string | 否 | `""` | 优先返回本地化结果的 IP 地址 |
| `need_content` | boolean | 否 | false | 是否在 `content` 返回网页正文；false 时恒为空串 |

## Search 响应字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `rerank_score` | number | 0~3，综合相关性/权威性/时效性；**≥1.5 为优质** |
| `title` | string | 标题 |
| `url` | string | 链接 |
| `snippet` | string | 短摘要，100~200 字符，适合前端展示 |
| `summary` | string | 长摘要，600~800 字符，**适合给模型阅读** |
| `content` | string | 正文；`need_content=false` 或正文缺失时为空串 |
| `site_name` | string\|null | 站点名 |
| `site_icon` | string\|null | 站点图标 URL（对模型无用，脚本默认不输出） |
| `date` | string\|null | 发布时间，UTC RFC 3339 |
| `authority` | integer | 网页权威性 0~4，`-1` 表示未知；**≥3 为极权威** |

无匹配结果时返回 `{"results": []}`。

## 搜索语法（写进 `query`）

| 操作符 | 示例 | 说明 |
| --- | --- | --- |
| `site:` | `DeepSeek site:github.com` | 限定站点；多个用空格分隔并由 `OR` 连接：`site:github.com OR site:huggingface.co` |
| `-site:` | `DeepSeek -site:example.com` | 排除站点；多个用空格分隔 |
| `-` | `DeepSeek -广告` | 排除关键词 |
| `-"..."` | `DeepSeek -"release notes"` | 排除短语（短语内可含空格） |
| `"..."` | `DeepSeek "精确短语"` | 精确匹配短语 |
| `lang:` | `DeepSeek lang:zh,en` | 限定语言，多语言用逗号 |
| `after:` | `DeepSeek after:2025` / `after:2025-01-01` | 发布日期下界 |
| `before:` | `DeepSeek before:2026` / `before:2026-06-01` | 发布日期上界 |

## Fetch 请求 / 响应

- 请求：`{"url": "https://..."}`。URL ≤ 8192 字符，必须是含 host 的 http/https 地址，且不能携带用户名/密码。
- 响应：`{"title": "...", "content": "<Markdown 正文>"}`。
- Fetch 的结果受目标站点可用性与访问策略影响；一次只能传一个 URL（脚本内部循环实现多 URL）。

## 错误码

统一 OpenAI 兼容格式：`{"error": {"message": "...", "type": "...", "param": null, "code": "..."}}`

| HTTP | 含义 | 是否重试 |
| --- | --- | --- |
| 400 | 请求头/请求体不合法（JSON 错误或字段值无效） | 否，改了再试 |
| 401 | API Key 缺失或无效 | 否 |
| 402 | 账号余额不足 | 否，先充值 |
| 403 | Fetch 被安全策略拒绝 | 否 |
| 404 | Fetch 未取到可用正文（URL 失效/被站点拦截/正文不可提取/内容类型不支持） | 否 |
| 408 | 读取请求体超时 | 是 |
| 413 | 请求体超过大小限制 | 否 |
| 429 | 超过 QPS 限制 | 是，等待后重试 |
| 500 / 504 | 服务端内部错误 / 下游超时 | 是 |

脚本行为：408/429/5xx 自动退避重试最多 3 次（优先遵守 `Retry-After`），其余 4xx 直接报错退出。

## 限速

| API | QPS（账号级） |
| --- | --- |
| Search | 100 |
| Fetch | 25 |

- 账号级限速，与使用哪个 API Key 无关，与模型并发限制相互独立。
- Fetch 还额外对**每个目标域名**限速，超限同样返回 429。
- 因此批量抓取时脚本默认每个 URL 间隔 0.2s（`--delay` 可调）。

## 计费

| API | 单价 | 计费单位 |
| --- | --- | --- |
| Search | 5 元 / 1000 次（国际站约 $0.75 / 1K） | 成功请求次数 |
| Fetch | 5 元 / 1000 次（国际站约 $0.75 / 1K） | 成功请求次数 |

- **按请求次数计费，不按 token、不按返回条数** → `count` 调大不涨价。
- 只有 HTTP 200 且产生结果才计费（Search 的 `results` 必须非空，Fetch 必须返回正文）；失败、无结果、超时不计费。
- 模型通过服务端工具（联网工具 / Responses API `web_search` / Claude Code Web Search 等）触发的搜索不计费，只算 token；本脚本属于**客户端直接调用**，按上表计费。
- 结论：**一次 `count=10` 远优于 10 次 `count=1`；能复用摘要就不要 fetch。**

## 便宜的检索召回套路（脚本已内置）

1. `search`（`need_content=false`，`count` 5~10）→ 拿到 `summary`/`snippet` + 打分字段；
2. 本地规则：URL 去重 → `rerank_score<1.5` 过滤（可选 `authority<3`）→ 权威/分数/日期重排 → 只保留精简字段；
3. 摘要够用就回答；不够则挑 ≤3 个 URL `fetch`，长正文截断 + 关键词摘录；
4. fetch 结果缓存（key=url），避免重复计费与触发限流。
