# deepseek-search

基于 [DeepSeek 网页搜索与抓取 API](https://api-docs.deepseek.com/zh-cn/guides/web_search) 的 Claude/pi skill。提供实时网页搜索与网页正文抓取能力。

## 文件结构

```
.
├── SKILL.md                    # skill 入口（含 frontmatter，供 agent 自动发现）
├── scripts/deepseek-search.sh  # 命令行工具
├── DeepSeek_API_Key            # API 密钥（保持在此位置，勿外泄）
└── README.md                   # 本文件
```

## 依赖

- bash 3.2+
- curl
- jq（`brew install jq`）

## 用法

```bash
# 搜索（默认输出格式化文本）
bash scripts/deepseek-search.sh search "DeepSeek V4 发布" --count 5 --need-content

# 搜索（原始 JSON 输出）
bash scripts/deepseek-search.sh search "模型" --site github.com --after 2025 --json

# 抓取网页正文
bash scripts/deepseek-search.sh fetch "https://example.com/article"

# 查看完整参数与搜索语法
bash scripts/deepseek-search.sh --help
```

### 常用搜索语法 flag

| flag | 作用 |
| --- | --- |
| `--site DOMAIN` | 限定站点，可重复（多个自动 `OR` 连接） |
| `--exclude-site DOMAIN` | 排除站点 |
| `--exclude KEYWORD` | 排除关键词 |
| `--phrase "文本"` | 精确匹配短语 |
| `--exclude-phrase "文本"` | 排除短语 |
| `--lang zh,en` | 限定语言 |
| `--after` / `--before` | 限定发布日期 |

也支持直接在搜索词中写 `site:`、`lang:` 等原生操作符。

## 密钥

脚本固定读取上级目录的 `DeepSeek_API_Key` 文件；文件缺失时报错「没有密钥配置」。不要把密钥明文输出到对话中。

## 安装

把整个目录复制到 skills 目录即可：

复制后确认 `DeepSeek_API_Key` 在 skill 根目录、`scripts/` 在其下，脚本按此相对关系读取密钥。

## 说明

- Search 与 Fetch 均为只读请求；408/429/5xx 自动重试，402 提示充值。
- `rerank_score ≥ 1.5` 为优质结果，`authority ≥ 3` 为极权威来源。
- 完整请求字段、响应字段、搜索语法与错误码见 `deepseek-api-reference.md`。
