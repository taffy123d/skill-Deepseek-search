#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deepseek_peak.py — 判断 DeepSeek API 当前处于「高峰」还是「空闲」计费时段

官方规则（api-docs.deepseek.com/zh-cn/quick_start/pricing）：
  北京时间 周一至周五（不含中国法定节假日）
  09:00–12:00、14:00–18:00 为高峰时段；其余时间（含周末、节假日全天）为空闲时段。
  空闲时段单价是高峰时段的一半（deepseek-flash：输入未命中 1 vs 2 元/M，
  输入命中 0.02 vs 0.04，输出 4 vs 8）。

用途：本地 agent 在决定「现在就查 / 等空闲时段再批量查」时读一下即可。

实现约定：
  * 只用标准库；时区用固定 UTC+8（中国不实行夏令时，等价于 Asia/Shanghai），
    因此 macOS / Linux / Windows 行为一致，不依赖 tzdata。
  * 法定节假日与调休**不内置**（每年变动，写死容易错）：默认按工作日处理，
    对高峰的判断偏保守（把假日算成高峰 → 高估成本，不会低估）。
    需要精确判断时用 --holidays 或环境变量 DEEPSEEK_PEAK_HOLIDAYS 传入日期。
  * 不写任何文件、不发网络请求。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

VERSION = "1.0.0"

CST = timezone(timedelta(hours=8))  # 中国标准时间，无夏令时

# 高峰窗口（左闭右开，小时）
PEAK_WINDOWS = ((9, 12), (14, 18))

# deepseek-flash 单价（元 / 百万 tokens）
PRICES = {
    "peak": {"input_miss": 2.0, "input_hit": 0.04, "output": 8.0},
    "off-peak": {"input_miss": 1.0, "input_hit": 0.02, "output": 4.0},
}
MULTIPLIER = {"peak": 2, "off-peak": 1}
LABELS = {"peak": "高峰", "off-peak": "空闲"}

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PEAK = 2  # 仅 --check 时使用：0=空闲，2=高峰

NOTE = (
    "法定节假日与调休未内置：默认按工作日处理，高峰判断偏保守；"
    "需要精确判断请用 --holidays 或环境变量 DEEPSEEK_PEAK_HOLIDAYS"
)


class UsageError(Exception):
    pass


def err(msg):
    sys.stderr.write("ERROR: %s\n" % msg)


# --------------------------------------------------------------------------- #
# 时间与判定
# --------------------------------------------------------------------------- #
def parse_at(text):
    """解析 --at：支持 'YYYY-MM-DD HH:MM[:SS]'、'YYYY-MM-DD'、ISO 8601。

    不带时区的一律按北京时间理解；带时区的先转成北京时间。
    """
    raw = (text or "").strip()
    if not raw:
        raise UsageError("--at 不能为空")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=CST)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise UsageError("无法解析 --at：%s（示例：2026-11-23 09:30）" % raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def parse_holidays(values):
    """--holidays '2026-10-01,2026-10-02' 或重复传入；也读环境变量。"""
    raw = []
    for value in values or []:
        raw.extend(str(value).replace(" ", ",").split(","))
    env = os.environ.get("DEEPSEEK_PEAK_HOLIDAYS", "")
    if env:
        raw.extend(env.replace(" ", ",").split(","))
    days = set()
    for item in raw:
        item = item.strip()
        if not item:
            continue
        try:
            days.add(datetime.strptime(item, "%Y-%m-%d").date())
        except ValueError:
            raise UsageError("--holidays 里的日期格式不对：%s（应为 YYYY-MM-DD）" % item)
    return days


def is_peak(moment, holidays):
    if moment.weekday() >= 5:  # 周六 / 周日
        return False
    if moment.date() in holidays:
        return False
    for start_hour, end_hour in PEAK_WINDOWS:
        if start_hour <= moment.hour < end_hour:
            return True
    return False


def period_name(moment, holidays):
    return "peak" if is_peak(moment, holidays) else "off-peak"


def _candidates(day):
    """一天内可能发生峰谷切换的时刻（含 00:00，用于跨日/节假日切换）。"""
    base = datetime(day.year, day.month, day.day, tzinfo=CST)
    return [base + timedelta(hours=hour) for hour in (0, 9, 12, 14, 18)]


def next_switches(now, holidays, count=1):
    """返回接下来的若干次峰谷切换 [(时刻, 目标时段), ...]。"""
    current = period_name(now, holidays)
    out = []
    for offset in range(0, 21):
        for candidate in _candidates((now + timedelta(days=offset)).date()):
            if candidate <= now:
                continue
            target = period_name(candidate, holidays)
            if target != current:
                out.append((candidate, target))
                current = target
                if len(out) >= max(1, count):
                    return out
    return out


def fmt_local(moment):
    return moment.strftime("%Y-%m-%d %H:%M") + " CST"


def fmt_full(moment):
    return "%s %s" % (fmt_local(moment), WEEKDAYS[moment.weekday()])


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def build_report(now, holidays, next_count):
    period = period_name(now, holidays)
    switches = next_switches(now, holidays, next_count)
    seconds = int((switches[0][0] - now).total_seconds()) if switches else None
    return {
        "now": now.isoformat(),
        "local": fmt_local(now),
        "weekday": WEEKDAYS[now.weekday()],
        "period": period,
        "multiplier": MULTIPLIER[period],
        "prices": PRICES[period],
        "next": [
            {
                "at": moment.isoformat(),
                "local": fmt_local(moment),
                "weekday": WEEKDAYS[moment.weekday()],
                "period": target,
                "multiplier": MULTIPLIER[target],
            }
            for moment, target in switches
        ],
        "seconds_until_next": seconds,
        "holidays_used": sorted(day.isoformat() for day in holidays),
        "note": NOTE,
    }


def print_human(report, next_count, verbose):
    period = report["period"]
    head = "%s ×%d | %s %s" % (
        LABELS[period],
        report["multiplier"],
        report["local"],
        report["weekday"],
    )
    if report["next"]:
        nxt = report["next"][0]
        head += " | 下次切换 %s %s（%s → %s）" % (
            nxt["local"],
            nxt["weekday"],
            LABELS[period],
            LABELS[nxt["period"]],
        )
    sys.stdout.write(head + "\n")
    for extra in report["next"][1:]:
        sys.stdout.write(
            "  后续 %s %s → %s ×%d\n"
            % (extra["local"], extra["weekday"], LABELS[extra["period"]], extra["multiplier"])
        )
    if verbose:
        prices = report["prices"]
        sys.stdout.write(
            "  单价（元/百万 tokens）：输入未命中 %g / 命中 %g / 输出 %g\n"
            % (prices["input_miss"], prices["input_hit"], prices["output"])
        )
        if report["holidays_used"]:
            sys.stdout.write("  已按节假日处理：%s\n" % ", ".join(report["holidays_used"]))
        else:
            sys.stdout.write("  提示：%s\n" % report["note"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
EPILOG = """\
示例：
  deepseek-peak.sh                                  # 当前时段（一行）
  deepseek-peak.sh --json                           # 结构化输出，给程序消费
  deepseek-peak.sh --next 3                         # 顺便看接下来 3 次切换
  deepseek-peak.sh --at "2026-11-23 10:30"          # 假设时刻（便于排期）
  deepseek-peak.sh --check                          # 0=空闲 / 2=高峰，便于 shell 分支
  deepseek-peak.sh --holidays 2026-10-01,2026-10-02 # 排除法定节假日

规则（官方 pricing.md）：北京时间 周一至周五 09:00–12:00、14:00–18:00 为高峰，
其余时间（含周末、节假日全天）为空闲；空闲单价 = 高峰的一半。

退出码：0 正常 / 1 参数错误 / 2 仅 --check 表示「当前是高峰」
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        raise UsageError(message)


def build_parser():
    parser = _Parser(
        prog="deepseek-peak",
        description="判断 DeepSeek API 当前处于高峰还是空闲计费时段（只读系统时间，不联网、不写文件）",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="deepseek-peak " + VERSION)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--at", default=None, metavar="TIME", help='假设时刻，如 "2026-11-23 10:30"（默认取当前时间）')
    parser.add_argument("--next", type=int, default=1, metavar="N", help="列出接下来 N 次峰谷切换（默认 1，最多 8）")
    parser.add_argument("--holidays", action="append", metavar="DATES", help="法定节假日，逗号分隔 YYYY-MM-DD，可重复")
    parser.add_argument("--check", action="store_true", help="只判定：退出 0=空闲，2=高峰")
    parser.add_argument("--verbose", action="store_true", help="人类可读模式追加单价与节假日提示")
    return parser


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.next < 1 or args.next > 8:
            raise UsageError("--next 取值 1~8")
        holidays = parse_holidays(args.holidays)
        now = parse_at(args.at) if args.at else datetime.now(CST)
        report = build_report(now, holidays, args.next)
        if args.check:
            return EXIT_PEAK if report["period"] == "peak" else EXIT_OK
        if args.json:
            sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        else:
            print_human(report, args.next, args.verbose)
        return EXIT_OK
    except UsageError as exc:
        err(str(exc))
        return EXIT_USAGE
    except KeyboardInterrupt:
        sys.stderr.write("已中断\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
