#!/usr/bin/env bash
# 深度思考搜索（方案 A）专用入口：真正实现在 scripts/deepseek_deep_think.py。
# 检索规划权在 DeepSeek 服务端模型，客户端不执行任何工具。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  printf 'ERROR: 需要 python3（macOS: xcode-select --install；Linux: apt install python3）\n' >&2
  exit 2
fi
exec python3 "$DIR/deepseek_deep_think.py" "$@"
