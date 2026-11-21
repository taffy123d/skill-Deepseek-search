#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_peak.py — deepseek_peak.py 的离线自测（不联网、不依赖当前时间）

运行：python3 scripts/selftest_peak.py
所有用例都用 --at 固定时刻，结果与机器时区/当前时间无关。
"""

import contextlib
import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deepseek_peak as pk  # noqa: E402

CST = timezone(timedelta(hours=8))
NO_HOLIDAYS = set()


def at(text):
    return pk.parse_at(text)


def period(text, holidays=NO_HOLIDAYS):
    return pk.period_name(at(text), holidays)


class TestPeriod(unittest.TestCase):
    def test_weekday_morning_window(self):
        self.assertEqual(period("2026-11-23 09:00"), "peak")  # 周一 09:00 起
        self.assertEqual(period("2026-11-23 11:59"), "peak")

    def test_lunch_break_is_off_peak(self):
        self.assertEqual(period("2026-11-23 12:00"), "off-peak")
        self.assertEqual(period("2026-11-23 13:59"), "off-peak")

    def test_weekday_afternoon_window(self):
        self.assertEqual(period("2026-11-23 14:00"), "peak")
        self.assertEqual(period("2026-11-23 17:59"), "peak")
        self.assertEqual(period("2026-11-23 18:00"), "off-peak")

    def test_night_is_off_peak(self):
        self.assertEqual(period("2026-11-23 08:59"), "off-peak")
        self.assertEqual(period("2026-11-23 21:40"), "off-peak")

    def test_weekend_is_off_peak_all_day(self):
        for stamp in ("2026-11-21 10:00", "2026-11-21 15:00", "2026-11-22 10:00"):
            self.assertEqual(period(stamp), "off-peak", msg=stamp)

    def test_holiday_override(self):
        holidays = {datetime(2026, 11, 23).date()}
        self.assertEqual(period("2026-11-23 10:00"), "peak")
        self.assertEqual(period("2026-11-23 10:00", holidays), "off-peak")


class TestNextSwitch(unittest.TestCase):
    def test_weekend_night_goes_to_monday_morning(self):
        now = at("2026-11-21 21:40")  # 周六晚
        switches = pk.next_switches(now, NO_HOLIDAYS, 2)
        self.assertEqual(switches[0][0], at("2026-11-23 09:00"))
        self.assertEqual(switches[0][1], "peak")
        self.assertEqual(switches[1][0], at("2026-11-23 12:00"))
        self.assertEqual(switches[1][1], "off-peak")

    def test_peak_to_lunch_break(self):
        now = at("2026-11-23 10:30")
        switches = pk.next_switches(now, NO_HOLIDAYS, 1)
        self.assertEqual(switches[0][0], at("2026-11-23 12:00"))

    def test_lunch_to_afternoon_peak(self):
        now = at("2026-11-23 12:30")
        switches = pk.next_switches(now, NO_HOLIDAYS, 1)
        self.assertEqual(switches[0][0], at("2026-11-23 14:00"))
        self.assertEqual(switches[0][1], "peak")

    def test_friday_evening_skips_to_monday(self):
        now = at("2026-11-20 18:30")  # 周五晚
        switches = pk.next_switches(now, NO_HOLIDAYS, 1)
        self.assertEqual(switches[0][0], at("2026-11-23 09:00"))

    def test_holiday_makes_monday_off_peak(self):
        holidays = {datetime(2026, 11, 23).date(), datetime(2026, 11, 24).date()}
        now = at("2026-11-21 21:40")
        switches = pk.next_switches(now, holidays, 1)
        self.assertEqual(switches[0][0], at("2026-11-25 09:00"))


class TestReport(unittest.TestCase):
    def test_multiplier_and_prices(self):
        off = pk.build_report(at("2026-11-21 21:40"), NO_HOLIDAYS, 1)
        self.assertEqual(off["period"], "off-peak")
        self.assertEqual(off["multiplier"], 1)
        self.assertEqual(off["prices"]["output"], 4.0)
        peak = pk.build_report(at("2026-11-23 10:00"), NO_HOLIDAYS, 1)
        self.assertEqual(peak["multiplier"], 2)
        self.assertEqual(peak["prices"]["output"], 8.0)

    def test_seconds_until_next(self):
        report = pk.build_report(at("2026-11-21 21:40"), NO_HOLIDAYS, 1)
        self.assertEqual(report["seconds_until_next"], int((at("2026-11-23 09:00") - at("2026-11-21 21:40")).total_seconds()))

    def test_weekday_label(self):
        self.assertEqual(pk.build_report(at("2026-11-21 21:40"), NO_HOLIDAYS, 1)["weekday"], "周六")
        self.assertEqual(pk.build_report(at("2026-11-23 10:00"), NO_HOLIDAYS, 1)["weekday"], "周一")


class TestCli(unittest.TestCase):
    def run_cli(self, argv):
        out, errbuf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errbuf):
            code = pk.main(argv)
        return code, out.getvalue(), errbuf.getvalue()

    def test_json_shape(self):
        code, out, _ = self.run_cli(["--json", "--at", "2026-11-21 21:40"])
        self.assertEqual(code, pk.EXIT_OK)
        data = json.loads(out)
        for key in ("now", "local", "weekday", "period", "multiplier", "prices", "next", "seconds_until_next", "note"):
            self.assertIn(key, data)
        self.assertEqual(data["period"], "off-peak")
        self.assertEqual(data["next"][0]["period"], "peak")

    def test_human_one_line(self):
        code, out, _ = self.run_cli(["--at", "2026-11-23 10:30"])
        self.assertEqual(code, pk.EXIT_OK)
        lines = out.strip().split("\n")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("高峰 ×2 | 2026-11-23 10:30 CST 周一"))
        self.assertIn("下次切换 2026-11-23 12:00 CST 周一", lines[0])

    def test_next_lists_extra_lines(self):
        code, out, _ = self.run_cli(["--at", "2026-11-21 21:40", "--next", "2"])
        self.assertEqual(code, pk.EXIT_OK)
        self.assertEqual(len(out.strip().split("\n")), 2)

    def test_check_exit_codes(self):
        self.assertEqual(self.run_cli(["--check", "--at", "2026-11-21 21:40"])[0], pk.EXIT_OK)
        self.assertEqual(self.run_cli(["--check", "--at", "2026-11-23 10:00"])[0], pk.EXIT_PEAK)

    def test_holidays_flag_and_env(self):
        code, out, _ = self.run_cli(["--json", "--at", "2026-11-23 10:00", "--holidays", "2026-11-23"])
        self.assertEqual(json.loads(out)["period"], "off-peak")
        os.environ["DEEPSEEK_PEAK_HOLIDAYS"] = "2026-11-23"
        try:
            _, out2, _ = self.run_cli(["--json", "--at", "2026-11-23 10:00"])
            self.assertEqual(json.loads(out2)["period"], "off-peak")
        finally:
            os.environ.pop("DEEPSEEK_PEAK_HOLIDAYS", None)

    def test_bad_inputs(self):
        self.assertEqual(self.run_cli(["--at", "不是时间"])[0], pk.EXIT_USAGE)
        self.assertEqual(self.run_cli(["--next", "9"])[0], pk.EXIT_USAGE)
        self.assertEqual(self.run_cli(["--holidays", "2026-13-01"])[0], pk.EXIT_USAGE)

    def test_timezone_aware_input_converted(self):
        # UTC 02:00 == 北京 10:00（周一）-> peak
        code, out, _ = self.run_cli(["--json", "--at", "2026-11-23T02:00:00+00:00"])
        self.assertEqual(code, pk.EXIT_OK)
        self.assertEqual(json.loads(out)["period"], "peak")

    def test_verbose_adds_prices(self):
        _, out, _ = self.run_cli(["--at", "2026-11-21 21:40", "--verbose"])
        self.assertIn("单价", out)
        self.assertIn("节假日", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
