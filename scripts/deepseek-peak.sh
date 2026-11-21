#!/usr/bin/env bash
# 峰谷时段判断（读系统时间，不联网、不写文件）。实现在 scripts/deepseek_peak.py。
# Windows 下请直接用：python scripts\deepseek_peak.py
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  printf 'ERROR: 需要 python3（macOS: xcode-select --install；Linux: apt install python3）\n' >&2
  exit 2
fi
exec python3 "$DIR/deepseek_peak.py" "$@"
