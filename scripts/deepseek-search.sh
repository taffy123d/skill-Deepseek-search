#!/usr/bin/env bash
# 兼容包装器：真正实现在 scripts/deepseek_search.py（Python 3，无外部依赖）。
# 保留该脚本是为了老用法 `bash scripts/deepseek-search.sh ...` 继续可用。
# 首参为 `deep-think` / `peak` 时转发到对应脚本。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  printf 'ERROR: 需要 python3（macOS: xcode-select --install；Linux: apt install python3）\n' >&2
  exit 2
fi
if [ "${1:-}" = "deep-think" ]; then
  shift
  exec python3 "$DIR/deepseek_deep_think.py" "$@"
fi
if [ "${1:-}" = "peak" ]; then
  shift
  exec python3 "$DIR/deepseek_peak.py" "$@"
fi
exec python3 "$DIR/deepseek_search.py" "$@"
