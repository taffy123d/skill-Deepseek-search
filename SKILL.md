---
name: deepseek-search
description: 使用 DeepSeek 搜索引擎实时搜索网页或抓取网页正文。当用户需要联网查最新资讯、实时信息、验证事实、搜索文档/代码，或需要读取某个网页内容时使用。
---

# DeepSeek 搜索工具

通过 DeepSeek 自研搜索引擎进行实时网页搜索，或抓取单个网页转为 Markdown。

## 何时使用

- 用户询问时效性强的信息（新闻、发布动态、最新文档）
- 需要验证事实、查找来源、搜索代码/文档
- 需要读取某个具体网页的正文内容

## 用法

脚本：`scripts/deepseek-search.sh`（依赖 `bash`、`curl`、`jq`）。
完整参数与搜索语法速查：`bash scripts/deepseek-search.sh --help`

### 搜索

```bash
bash scripts/deepseek-search.sh search "DeepSeek V4 发布" --count 5 --need-content
bash scripts/deepseek-search.sh search "模型" --site github.com --site huggingface.co --after 2025
```

搜索词中也可直接写 `site:` / `lang:` / `after:` 等操作符；脚本同样提供 `--site`、`--exclude-site`、`--exclude`、`--phrase`、`--lang`、`--after`、`--before` 等 flag 便于拼装。

### 抓取网页

```bash
bash scripts/deepseek-search.sh fetch "https://example.com/article"
```

加 `--json` 可输出原始 JSON（默认输出便于阅读的格式化文本）。

## 结果解读

- `rerank_score ≥ 1.5` 为优质结果；`authority ≥ 3` 为极权威来源。
- `summary` 适合给模型阅读；`snippet` 是短摘要；`content` 仅在 `--need-content` 时有值。

## 错误处理

脚本自动处理：408/429/5xx 最多 3 次退避重试；402 提示充值；其余 4xx 直接报错。Fetch 的 403/404 表示被站点策略拦截或无可用正文。

## 计费
Deepseek按成功请求次数计费，不按 token 或返回结果条数计费。
请尽可能用更少的请求次数完成任务，不要拆分多次调用，以此节约调用成本。
(脚本的 --help 不会调用API请求，需要时放心用)