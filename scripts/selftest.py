#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测（不访问网络）：python3 scripts/selftest.py

覆盖 改进建议.md 里的纯规则环节：URL 去重、过滤、重排、截断、关键词摘录、缓存、重试。
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deepseek_search as ds  # noqa: E402


def make_item(url, score=2.0, authority=3, date="2025-06-01T00:00:00+00:00", **extra):
    item = {
        "title": "t:" + url,
        "url": url,
        "snippet": "snippet",
        "summary": "summary",
        "content": "",
        "site_name": "site",
        "site_icon": None,
        "date": date,
        "authority": authority,
        "rerank_score": score,
    }
    item.update(extra)
    return item


class TestUrlNormalization(unittest.TestCase):
    def test_strips_fragment_and_trailing_slash(self):
        self.assertEqual(ds.normalize_url("https://a.com/x/#frag"), "https://a.com/x")

    def test_strips_tracking_params_keeps_real_ones(self):
        self.assertEqual(
            ds.normalize_url("https://a.com/p?utm_source=tg&id=3"),
            "https://a.com/p?id=3",
        )
        self.assertEqual(ds.normalize_url("https://a.com/p?utm_source=tg&fbclid=9"), "https://a.com/p")

    def test_keeps_distinct_query_pages(self):
        a = ds.normalize_url("https://a.com/p?id=1")
        b = ds.normalize_url("https://a.com/p?id=2")
        self.assertNotEqual(a, b)

    def test_locale_variants_collapse(self):
        self.assertEqual(ds.normalize_url("https://a.com/zh-cn/x"), "https://a.com/x")
        self.assertEqual(
            ds.normalize_url("https://a.com/zh-cn/x"), ds.normalize_url("https://a.com/en/x")
        )
        self.assertEqual(ds.normalize_url("https://a.com/zh-cn"), "https://a.com")

    def test_locale_only_in_middle_path_not_touched(self):
        self.assertEqual(ds.normalize_url("https://a.com/docs/en/x"), "https://a.com/docs/en/x")

    def test_dedupe_merges_locale_variants(self):
        items = [make_item("https://a.com/zh-cn/x"), make_item("https://a.com/en/x")]
        self.assertEqual(len(ds.dedupe_results(items)), 1)

    def test_display_url_drops_empty_and_tracking_params(self):
        self.assertEqual(
            ds.display_url("https://a.com/x?referFrom=&wid=1&upstream_biz=tt&id=9"),
            "https://a.com/x?wid=1&id=9",
        )

    def test_display_url_keeps_locale(self):
        self.assertEqual(
            ds.display_url("https://a.com/zh-cn/x/?utm_source=1&id=2#f"),
            "https://a.com/zh-cn/x?id=2",
        )
        self.assertEqual(ds.display_url(""), "")

    def test_empty(self):
        self.assertEqual(ds.normalize_url(""), "")


class TestPostProcessing(unittest.TestCase):
    def test_dedupe_keeps_first_and_order(self):
        items = [
            make_item("https://a.com/1", score=2.5),
            make_item("https://a.com/1/#x", score=1.0),
            make_item("https://a.com/2", score=2.0),
        ]
        out = ds.dedupe_results(items)
        self.assertEqual([i["url"] for i in out], ["https://a.com/1", "https://a.com/2"])

    def test_min_score_filter(self):
        items = [make_item("https://a.com/1", score=2.0), make_item("https://a.com/2", score=0.9)]
        kept, stats = ds.process_results(items, min_score=1.5)
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats["low_score"], 1)

    def test_min_authority_filter(self):
        items = [make_item("https://a.com/1", authority=3), make_item("https://a.com/2", authority=1)]
        kept, stats = ds.process_results(items, min_authority=3)
        self.assertEqual([i["url"] for i in kept], ["https://a.com/1"])
        self.assertEqual(stats["low_authority"], 1)

    def test_sort_authority_then_score_then_date(self):
        items = [
            make_item("https://a.com/low", score=2.9, authority=1, date="2025-09-09T00:00:00Z"),
            make_item("https://a.com/old", score=2.0, authority=4, date="2020-01-01T00:00:00Z"),
            make_item("https://a.com/new", score=2.0, authority=4, date="2025-01-01T00:00:00Z"),
            make_item("https://a.com/top", score=2.8, authority=4, date="2019-01-01T00:00:00Z"),
        ]
        kept, _ = ds.process_results(items, sort_mode="authority")
        self.assertEqual(
            [i["url"] for i in kept],
            ["https://a.com/top", "https://a.com/new", "https://a.com/old", "https://a.com/low"],
        )

    def test_sort_rank_keeps_api_order(self):
        items = [make_item("https://a.com/1", score=1.0), make_item("https://a.com/2", score=3.0)]
        kept, _ = ds.process_results(items, sort_mode="rank")
        self.assertEqual([i["url"] for i in kept], ["https://a.com/1", "https://a.com/2"])

    def test_limit_after_sort(self):
        items = [make_item("https://a.com/%d" % i, score=float(i)) for i in range(1, 6)]
        kept, stats = ds.process_results(items, sort_mode="score", limit=2)
        self.assertEqual([i["url"] for i in kept], ["https://a.com/5", "https://a.com/4"])
        self.assertEqual(stats["shown"], 2)

    def test_no_dedupe_flag(self):
        items = [make_item("https://a.com/1"), make_item("https://a.com/1/#x")]
        kept, stats = ds.process_results(items, do_dedupe=False)
        self.assertEqual(len(kept), 2)
        self.assertEqual(stats["duplicate"], 0)


class TestTextProcessing(unittest.TestCase):
    def test_truncate_adds_marker(self):
        out = ds.truncate("x" * 50, 10)
        self.assertTrue(out.startswith("x" * 10))
        self.assertIn("已截断", out)
        self.assertIn("50", out)

    def test_truncate_noop_when_short_or_disabled(self):
        self.assertEqual(ds.truncate("short", 100), "short")
        self.assertEqual(ds.truncate("x" * 100, 0), "x" * 100)

    def test_excerpt_keeps_hits_with_padding(self):
        text = "intro about nothing\n\npara1\n\n目标段落含关键词\n\npara4\n\npara5"
        out = ds.excerpt(text, ["关键词"], pad=1)
        self.assertIn("intro about nothing", out)  # 开头保留
        self.assertIn("目标段落含关键词", out)
        self.assertIn("para4", out)  # 后一段
        self.assertNotIn("para5", out)

    def test_excerpt_falls_back_when_no_hit(self):
        text = "a\n\nb"
        self.assertEqual(ds.excerpt(text, ["未出现"]), text)

    def test_split_keywords(self):
        self.assertEqual(ds.split_keywords(["a, b", "c,d", "a"]), ["a", "b", "c", "d"])


class TestQueryBuilding(unittest.TestCase):
    def test_operators(self):
        args = argparse.Namespace(
            query="模型", site=["github.com", "huggingface.co"], exclude_site=["spam.com"],
            exclude=["广告"], phrase=["release notes"], exclude_phrase=["招聘 广告"],
            lang="zh,en", after="2025", before="2026-01-01",
        )
        q = ds.build_query(args)
        self.assertIn("site:github.com OR site:huggingface.co", q)
        self.assertIn("-site:spam.com", q)
        self.assertIn("-广告", q)
        self.assertIn('"release notes"', q)
        self.assertIn('-"招聘 广告"', q)
        self.assertTrue(q.endswith("lang:zh,en after:2025 before:2026-01-01"))


class TestCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ds-selftest-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_then_read(self):
        path = ds.cache_path(self.dir, "https://a.com/1")
        ds.cache_write(path, "https://a.com/1", "标题", "正文")
        hit = ds.cache_read(path, 86400)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["title"], "标题")
        self.assertEqual(hit["content"], "正文")

    def test_expired_cache_miss(self):
        path = ds.cache_path(self.dir, "https://a.com/2")
        ds.cache_write(path, "https://a.com/2", "t", "c")
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        obj["fetched_at"] = 1  # 很旧
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        self.assertIsNone(ds.cache_read(path, 60))
        self.assertIsNotNone(ds.cache_read(path, 0))  # ttl=0 永不过期

    def test_missing_and_broken_files(self):
        self.assertIsNone(ds.cache_read(os.path.join(self.dir, "nope.json"), 60))
        broken = os.path.join(self.dir, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(ds.cache_read(broken, 60))


class TestBudgetAllocation(unittest.TestCase):
    def test_no_budget_returns_desired(self):
        self.assertEqual(ds.allocate_budget([300, 200], 0), [300, 200])

    def test_within_budget_unchanged(self):
        self.assertEqual(ds.allocate_budget([300, 200], 1000), [300, 200])

    def test_total_never_exceeds_budget(self):
        allocs = ds.allocate_budget([800] * 10, 4000)
        self.assertLessEqual(sum(allocs), 4000)
        self.assertGreater(sum(allocs), 4000 * 0.95)  # 取整损失 < n 字符

    def test_tail_omitted_with_tight_budget(self):
        allocs = ds.allocate_budget([800] * 10, 900)
        self.assertEqual(allocs[-1], 0)
        self.assertTrue(all(a == 0 or a >= ds.MIN_SUMMARY_CHARS for a in allocs))

    def test_earlier_items_get_more(self):
        allocs = ds.allocate_budget([800] * 5, 2000)
        self.assertEqual(allocs, sorted(allocs, reverse=True))
        self.assertGreater(allocs[0], allocs[-1])

    def test_tail_small_share_is_omitted_not_sliced(self):
        allocs = ds.allocate_budget([800] * 10, 1500)
        # 份额不足 120 字符时宁可省略，也不给无意义的碎片
        self.assertTrue(all(a == 0 or a >= ds.MIN_SUMMARY_CHARS for a in allocs))

    def test_short_item_leaves_room_for_others(self):
        allocs = ds.allocate_budget([50, 800, 800], 900)
        self.assertEqual(allocs[0], 50)          # 短条目拿满即可
        self.assertGreater(allocs[1], 300)       # 余额回流给后面
        self.assertLessEqual(sum(allocs), 900)

    def test_empty_list(self):
        self.assertEqual(ds.allocate_budget([], 1000), [])


class TestBudgetFormatting(unittest.TestCase):
    def test_markers_and_stats(self):
        items = [make_item("https://a.com/%d" % i, summary="字" * 400) for i in range(1, 4)]
        stats = {}
        text = ds.format_search_text(items, 800, 0, budget=200, budget_stats=stats)
        self.assertIn("[摘要按 --budget 省略", text)
        self.assertIn("…[原文 400 字]", text)
        self.assertEqual(stats["budget"], 200)
        self.assertLessEqual(stats["budget_used"], 200)
        self.assertEqual(stats["truncated"] + stats["omitted"], 3)

    def test_no_marker_when_within_budget(self):
        items = [make_item("https://a.com/1", summary="短摘要")]
        stats = {}
        text = ds.format_search_text(items, 800, 0, budget=1000, budget_stats=stats)
        self.assertNotIn("--budget", text)
        self.assertEqual(stats["truncated"], 0)
        self.assertEqual(stats["omitted"], 0)

    def test_items_kept_even_when_summary_omitted(self):
        items = [make_item("https://a.com/%d" % i, summary="字" * 300) for i in range(1, 6)]
        text = ds.format_search_text(items, 800, 0, budget=150)
        for i in range(1, 6):
            self.assertIn("[%d] t:https://a.com/%d" % (i, i), text)
            self.assertIn("https://a.com/%d" % i, text)


class TestTitleCleaning(unittest.TestCase):
    def test_collapses_self_duplicated_title(self):
        self.assertEqual(ds.clean_title("A - A"), "A")
        self.assertEqual(ds.clean_title("A | B | A"), "A | B | A")
        self.assertEqual(ds.clean_title("DeepSeek V4 - DeepSeek V4"), "DeepSeek V4")

    def test_keeps_normal_titles(self):
        self.assertEqual(ds.clean_title("标题 - 站点"), "标题 - 站点")
        self.assertEqual(ds.clean_title("  多个   空格  "), "多个 空格")
        self.assertEqual(ds.clean_title(None), "")


class TestSummaryCleaning(unittest.TestCase):
    def test_strips_html_tags_and_br_to_newline(self):
        out = ds.clean_summary("<strong>Agent</strong> 能力<br>提升")
        self.assertEqual(out, "Agent 能力\n提升")

    def test_strips_leading_heading_duplicating_title(self):
        self.assertEqual(ds.clean_summary("# 模型 & 价格\n\n正文", "模型 & 价格"), "正文")

    def test_strips_markdown_emphasis_and_collapses_blank_lines(self):
        self.assertEqual(ds.clean_summary("**重点**\n\n\n\n后续"), "重点\n\n后续")

    def test_keeps_tables_and_ellipsis(self):
        text = "模型 | 价格\n...\n- 另一行"
        self.assertEqual(ds.clean_summary(text), text)

    def test_drops_short_boilerplate_lines_only(self):
        text = "2026-04-24 12:23 发布于：上海市\n来源：凤凰网\n正文第一段\n责任编辑：张三"
        self.assertEqual(ds.clean_summary(text), "正文第一段")
        # 长句虽以“来源：”开头，但不像样板行，必须保留
        long_line = "来源：国家统计局数据显示，2025 年第一季度同比增长 5.4%，高于市场预期。"
        self.assertEqual(ds.clean_summary(long_line), long_line)

    def test_empty(self):
        self.assertEqual(ds.clean_summary(None), "")
        self.assertEqual(ds.clean_summary("   "), "")

    def test_json_output_is_cleaned_but_raw_is_not(self):
        item = make_item("https://a.com/1", summary="<strong>重点</strong> 正文")
        code, out, _, _ = run_cli(["search", "q", "--json"], search_payload(item))
        self.assertEqual(json.loads(out)["results"][0]["summary"], "重点 正文")
        code, out, _, _ = run_cli(["search", "q", "--raw"], search_payload(item))
        self.assertEqual(json.loads(out)["results"][0]["summary"], "<strong>重点</strong> 正文")


class TestFetchTotalBudget(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ds-totalbudget-")
        self.base = ["fetch", "--cache-dir", self.dir, "--delay", "0", "--no-cache"]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _payload(self, size=600):
        return lambda kind, body: {"title": "t", "content": "正" * size}

    def test_total_budget_respected(self):
        code, out, _, _ = run_cli(self.base + ["https://a.com/1", "https://a.com/2", "--total-max-chars", "600"], self._payload())
        self.assertEqual(code, ds.EXIT_OK)
        self.assertLessEqual(out.count("正"), 600)
        self.assertIn("已截断", out)

    def test_budget_consumed_marks_omitted(self):
        code, out, _, _ = run_cli(
            self.base + ["https://a.com/%d" % i for i in range(1, 4)] + ["--total-max-chars", "1"],
            self._payload(500),
        )
        self.assertEqual(code, ds.EXIT_OK)
        self.assertIn("预算省略", out)
        self.assertIn("https://a.com/3", out)  # 条目仍在，只是正文被省略

    def test_no_budget_means_only_max_chars(self):
        code, out, _, _ = run_cli(self.base + ["https://a.com/1"], self._payload(500))
        self.assertEqual(code, ds.EXIT_OK)
        self.assertEqual(out.count("正"), 500)  # 未截断

    def test_json_reports_allocation(self):
        code, out, _, _ = run_cli(
            self.base + ["https://a.com/1", "https://a.com/2", "--total-max-chars", "300", "--json"],
            self._payload(600),
        )
        obj = json.loads(out)
        self.assertEqual(obj["total_max_chars"], 300)
        self.assertEqual(obj["results"][0]["allocated"], 150)
        self.assertTrue(all(r["allocated"] is not None for r in obj["results"]))

    def test_invalid_budget_rejected(self):
        code, _, _, _ = run_cli(self.base + ["https://a.com/1", "--total-max-chars", "-5"], self._payload())
        self.assertEqual(code, "raised:UsageError")


class TestMaxUrls(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ds-maxurls-")
        self.base = ["fetch", "--cache-dir", self.dir, "--delay", "0"]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_default_limit_is_three(self):
        payload = {"title": "t", "content": "c"}
        code, _, _, calls = run_cli(self.base + ["https://a.com/%d" % i for i in range(4)], payload)
        self.assertEqual(code, "raised:UsageError")
        self.assertEqual(calls, [])

    def test_can_raise_limit(self):
        payload = {"title": "t", "content": "c"}
        code, _, _, calls = run_cli(
            self.base + ["https://a.com/%d" % i for i in range(4)] + ["--max-urls", "4"], payload
        )
        self.assertEqual(code, ds.EXIT_OK)
        self.assertEqual(len(calls), 4)

    def test_zero_means_unlimited(self):
        payload = {"title": "t", "content": "c"}
        code, _, _, calls = run_cli(
            self.base + ["https://a.com/%d" % i for i in range(5)] + ["--max-urls", "0"], payload
        )
        self.assertEqual(code, ds.EXIT_OK)
        self.assertEqual(len(calls), 5)


class TestCacheLocation(unittest.TestCase):
    def test_default_dir_is_inside_skill(self):
        prev = os.environ.pop("DEEPSEEK_SEARCH_CACHE", None)
        try:
            root = ds.cache_root()
        finally:
            if prev is not None:
                os.environ["DEEPSEEK_SEARCH_CACHE"] = prev
        self.assertEqual(root, os.path.join(ds.SKILL_ROOT, ".cache", "fetch"))
        self.assertTrue(root.startswith(ds.SKILL_ROOT))

    def test_cli_dir_wins(self):
        self.assertEqual(ds.cache_root("/tmp/x"), "/tmp/x")

    def test_env_override(self):
        os.environ["DEEPSEEK_SEARCH_CACHE"] = "/tmp/env-cache"
        try:
            self.assertEqual(ds.cache_root(), "/tmp/env-cache")
            self.assertEqual(ds.cache_root("/tmp/cli"), "/tmp/cli")
        finally:
            os.environ.pop("DEEPSEEK_SEARCH_CACHE", None)

    def test_unwritable_default_falls_back_to_tmpdir(self):
        with contextlib.redirect_stderr(io.StringIO()) as errbuf:
            got = ds.prepare_cache_dir("/dev/null/nope")
        self.assertIn("改用", errbuf.getvalue())
        self.assertIn("deepseek-search-cache", got)

    def test_unwritable_explicit_dir_raises(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ds.UsageError):
                ds.prepare_cache_dir("/dev/null/nope", explicit=True)

    def test_prepare_creates_and_returns_dir(self):
        d = tempfile.mkdtemp(prefix="ds-cachedir-")
        try:
            target = os.path.join(d, "a", "b")
            self.assertEqual(ds.prepare_cache_dir(target), target)
            self.assertTrue(os.path.isdir(target))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestRetry(unittest.TestCase):
    def setUp(self):
        self._orig_post = ds.http_post
        self._orig_sleep = ds.time.sleep
        ds.time.sleep = lambda *_: None

    def tearDown(self):
        ds.http_post = self._orig_post
        ds.time.sleep = self._orig_sleep

    def test_retries_on_429_then_succeeds(self):
        calls = []

        def fake(url, payload, timeout, key):
            calls.append(url)
            if len(calls) == 1:
                return 429, json.dumps({"error": {"message": "slow down"}}), {"Retry-After": "1"}
            return 200, json.dumps({"results": []}), {}

        ds.http_post = fake
        data = ds.api_post("search", {"query": "x"}, 5, "k")
        self.assertEqual(data, {"results": []})
        self.assertEqual(len(calls), 2)

    def test_gives_up_after_three_attempts(self):
        ds.http_post = lambda *a, **k: (503, "", {})
        with self.assertRaises(ds.ApiError) as ctx:
            ds.api_post("search", {"query": "x"}, 5, "k")
        self.assertEqual(ctx.exception.status, 503)

    def test_does_not_retry_400(self):
        calls = []

        def fake(*a, **k):
            calls.append(1)
            return 400, json.dumps({"error": {"message": "bad"}}), {}

        ds.http_post = fake
        with self.assertRaises(ds.ApiError) as ctx:
            ds.api_post("search", {"query": "x"}, 5, "k")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(len(calls), 1)


class TestFormatting(unittest.TestCase):
    def test_compact_format_keeps_fields_and_drops_labels(self):
        text = ds.format_search_text([make_item("https://a.com/1", score=2.4, authority=3)], 800, 0)
        self.assertIn("[1] t:https://a.com/1  (site · 2025-06-01 · 2.40 · auth3)", text)
        self.assertIn("https://a.com/1", text)
        self.assertNotIn("Summary:", text)  # 默认不再有标签
        self.assertNotIn("Icon:", text)
        self.assertNotIn("favicon", text)

    def test_output_url_strips_tracking_params(self):
        item = make_item("https://a.com/p?id=3&utm_source=tg&spm=x", title="正常标题")
        text = ds.format_search_text([item], 800, 0)
        self.assertIn("https://a.com/p?id=3", text)
        self.assertNotIn("utm_source", text)

    def test_compact_omits_missing_meta(self):
        item = make_item("https://a.com/1")
        item.update({"site_name": None, "date": None, "authority": None, "rerank_score": 1.2})
        text = ds.format_search_text([item], 800, 0)
        self.assertIn("(1.20)", text)
        self.assertNotIn("unknown", text)

    def test_full_meta_format(self):
        text = ds.format_search_text(
            [make_item("https://a.com/1", score=2.4, authority=3)], 800, 0, compact=False
        )
        self.assertIn("URL:       https://a.com/1", text)
        self.assertIn("Source:    site  |  date: 2025-06-01", text)
        self.assertIn("Summary:   ", text)

    def test_summary_truncation(self):
        text = ds.format_search_text([make_item("https://a.com/1", summary="z" * 900)], 100, 0)
        self.assertIn("…", text)
        self.assertNotIn("z" * 101, text)

    def test_strip_leading_title_variants(self):
        self.assertEqual(ds.strip_leading_title("# T\n\n正文", "T"), "正文")
        self.assertEqual(ds.strip_leading_title("# T - 站点\n\n正文", "T"), "正文")
        self.assertEqual(ds.strip_leading_title("# T\n\n正文", "T - T"), "正文")
        # 同一标题重复出现两次（有些站点 H1 + 正文首标题重复）
        self.assertEqual(ds.strip_leading_title("# T\n\n# T\n\n正文", "T"), "正文")
        # 标题不同则保留（可能是正文里的二级标题）
        self.assertEqual(ds.strip_leading_title("# Other\n\n正文", "T"), "# Other\n\n正文")
        self.assertEqual(ds.strip_leading_title("正文无标题", "T"), "正文无标题")

    def test_fetch_text_single_and_multi(self):
        doc = {"url": "https://a.com/1", "title": "标题", "content": "正文", "cached": False}
        self.assertTrue(ds.format_fetch_text(doc).startswith("# 标题"))
        self.assertIn("## [2/3]", ds.format_fetch_text(dict(doc, title="T2"), 2, 3))


def run_cli(argv, payload=None):
    """在测试进程中跑 run_search / run_fetch，打桩 api_post 与密钥解析。"""
    orig_post, orig_key = ds.api_post, ds.resolve_key
    calls = []

    def fake_post(kind, body, timeout, key):
        calls.append((kind, body))
        if isinstance(payload, Exception):
            raise payload
        if callable(payload):
            return payload(kind, body)
        return payload

    ds.api_post = fake_post
    ds.resolve_key = lambda path=None: "test-key"
    out, errbuf = io.StringIO(), io.StringIO()
    try:
        args = ds.build_parser().parse_args(argv)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errbuf):
            try:
                code = ds.run_search(args) if args.cmd == "search" else ds.run_fetch(args)
            except (ds.ApiError, ds.NetworkError, ds.UsageError, ds.ConfigError) as exc:
                code = "raised:%s" % type(exc).__name__
        return code, out.getvalue(), errbuf.getvalue(), calls
    finally:
        ds.api_post, ds.resolve_key = orig_post, orig_key


def search_payload(*items):
    return {"results": list(items)}


class TestRunSearchFlow(unittest.TestCase):
    def test_empty_results_message(self):
        code, out, _, _ = run_cli(["search", "q"], search_payload())
        self.assertEqual(code, ds.EXIT_OK)
        self.assertEqual(out.strip(), "No matching results.")

    def test_fallback_when_everything_filtered_out(self):
        payload = search_payload(make_item("https://a.com/1", score=1.0, authority=1))
        code, out, errbuf, _ = run_cli(["search", "q", "--min-score", "3"], payload)
        self.assertEqual(code, ds.EXIT_OK)
        self.assertIn("回退", errbuf)
        self.assertIn("[1] t:https://a.com/1", out)  # 回退后仍然展示

    def test_footer_reports_filters(self):
        payload = search_payload(
            make_item("https://a.com/1", score=2.5),
            make_item("https://a.com/1/#dup", score=2.4),
            make_item("https://a.com/2", score=0.5),
        )
        code, out, _, _ = run_cli(["search", "q", "--min-score", "1.5"], payload)
        self.assertEqual(code, ds.EXIT_OK)
        self.assertIn("显示 1 / 3 条", out)
        self.assertIn("去重 1", out)
        self.assertIn("低分 1", out)

    def test_footer_reports_limit(self):
        payload = search_payload(*[make_item("https://a.com/%d" % i) for i in range(4)])
        code, out, _, _ = run_cli(["search", "q", "--limit", "2"], payload)
        self.assertEqual(code, ds.EXIT_OK)
        self.assertIn("受 --limit 2 限制", out)

    def test_budget_footer_and_no_overflow(self):
        payload = search_payload(*[make_item("https://a.com/%d" % i, summary="z" * 300) for i in range(1, 5)])
        code, out, _, _ = run_cli(["search", "q", "--budget", "400"], payload)
        self.assertEqual(code, ds.EXIT_OK)
        self.assertIn("-- 预算", out)
        self.assertIn("省略", out)
        body = out.split("-- 预算")[0]
        self.assertLessEqual(body.count("z"), 400)  # 摘要字符不超预算

    def test_budget_in_json_stats(self):
        payload = search_payload(make_item("https://a.com/1", summary="字" * 300))
        code, out, _, _ = run_cli(["search", "q", "--budget", "100", "--json"], payload)
        stats = json.loads(out)["stats"]
        self.assertEqual(stats["budget"], 100)
        self.assertLessEqual(stats["budget_used"], 100)

    def test_json_output_shape(self):
        payload = search_payload(make_item("https://a.com/1", score=2.5))
        code, out, _, _ = run_cli(["search", "q", "--json"], payload)
        self.assertEqual(code, ds.EXIT_OK)
        obj = json.loads(out)
        self.assertEqual(obj["count"], 1)
        self.assertEqual(obj["results"][0]["url"], "https://a.com/1")
        self.assertEqual(obj["stats"]["raw"], 1)

    def test_raw_output_is_untouched(self):
        payload = search_payload(make_item("https://a.com/1", score=0.2))
        code, out, _, _ = run_cli(["search", "q", "--raw", "--min-score", "1.5"], payload)
        self.assertEqual(json.loads(out), payload)

    def test_need_content_prints_content_block_compact(self):
        payload = search_payload(make_item("https://a.com/1", content="正文片段"))
        code, out, _, _ = run_cli(["search", "q", "--need-content", "--max-content", "10"], payload)
        self.assertIn("[Content]", out)
        self.assertIn("正文片段", out)

    def test_need_content_full_meta(self):
        payload = search_payload(make_item("https://a.com/1", content="正文片段"))
        code, out, _, _ = run_cli(
            ["search", "q", "--need-content", "--max-content", "10", "--full-meta"], payload
        )
        self.assertIn("Content:   正文片段", out)

    def test_sends_need_content_false_by_default(self):
        _, _, _, calls = run_cli(["search", "q"], search_payload())
        self.assertEqual(calls[0][0], "search")
        self.assertFalse(calls[0][1]["need_content"])
        self.assertNotIn("ori_question", calls[0][1])

    def test_passes_ori_question_and_count(self):
        _, _, _, calls = run_cli(
            ["search", "q", "--count", "7", "--ori-question", "原问题"], search_payload()
        )
        self.assertEqual(calls[0][1]["count"], 7)
        self.assertEqual(calls[0][1]["ori_question"], "原问题")


class TestRunFetchFlow(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ds-fetchtest-")
        self.base = ["fetch", "--cache-dir", self.dir, "--delay", "0"]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_single_url_success_and_cache_written(self):
        payload = {"title": "标题", "content": "# 标题\n\n正文第一段\n\n正文第二段"}
        code, out, _, _ = run_cli(self.base + ["https://a.com/1"], payload)
        self.assertEqual(code, ds.EXIT_OK)
        self.assertTrue(out.startswith("# 标题"))
        self.assertNotIn("# 标题\n\n正文第一段", out)  # 开头重复标题被去掉
        self.assertIn("正文第一段", out)
        self.assertTrue(os.path.isfile(ds.cache_path(self.dir, "https://a.com/1")))

    def test_cache_hit_skips_api_call(self):
        url = "https://a.com/cached"
        ds.cache_write(ds.cache_path(self.dir, url), url, "缓存标题", "缓存正文")

        def boom(kind, body):
            raise AssertionError("缓存命中时不应该调用 API")

        code, out, errbuf, _ = run_cli(self.base + [url], boom)
        self.assertEqual(code, ds.EXIT_OK)
        self.assertIn("缓存正文", out)
        self.assertIn("[缓存命中]", errbuf)
        self.assertIn("Source: cache", out)

    def test_no_cache_forces_api_call(self):
        url = "https://a.com/cached2"
        ds.cache_write(ds.cache_path(self.dir, url), url, "旧标题", "旧正文")
        code, out, _, calls = run_cli(
            self.base + [url, "--no-cache"], {"title": "新标题", "content": "新正文"}
        )
        self.assertEqual(code, ds.EXIT_OK)
        self.assertEqual(len(calls), 1)
        self.assertIn("新正文", out)

    def test_partial_failure_exit_3(self):
        def fake(kind, body):
            if body["url"].endswith("bad"):
                raise ds.ApiError(404, "no content")
            return {"title": "ok", "content": "ok body"}

        code, out, errbuf, _ = run_cli(self.base + ["https://a.com/bad", "https://a.com/good"], fake)
        self.assertEqual(code, ds.EXIT_PARTIAL)
        self.assertIn("ok body", out)
        self.assertIn("[跳过]", errbuf)

    def test_single_url_failure_raises(self):
        code, _, _, _ = run_cli(self.base + ["https://a.com/bad"], ds.ApiError(403, "blocked"))
        self.assertEqual(code, "raised:ApiError")

    def test_empty_content_is_failure(self):
        code, _, _, _ = run_cli(self.base + ["https://a.com/empty"], {"title": "t", "content": "   "})
        self.assertEqual(code, "raised:ApiError")

    def test_keywords_excerpt_applied(self):
        content = "开头段落\n\n无关内容\n\n这里讲价格\n\n后续段落\n\n完全无关"
        code, out, _, _ = run_cli(self.base + ["https://a.com/k", "--keywords", "价格"], {"title": "t", "content": content})
        self.assertIn("这里讲价格", out)
        self.assertIn("后续段落", out)  # pad=1 的下一段
        self.assertNotIn("完全无关", out)

    def test_max_chars_truncation(self):
        code, out, _, _ = run_cli(
            self.base + ["https://a.com/l", "--max-chars", "5"], {"title": "t", "content": "0123456789"}
        )
        self.assertIn("已截断", out)
        self.assertIn("原文 10 字符", out)

    def test_multiple_urls_numbered(self):
        code, out, _, _ = run_cli(
            self.base + ["https://a.com/1", "https://a.com/2"],
            lambda kind, body: {"title": body["url"][-1], "content": "c" + body["url"][-1]},
        )
        self.assertEqual(code, ds.EXIT_OK)
        self.assertIn("## [1/2]", out)
        self.assertIn("## [2/2]", out)
        self.assertIn("\n\n---\n\n", out)

    def test_json_output_marks_truncated(self):
        code, out, _, _ = run_cli(
            self.base + ["https://a.com/j", "--max-chars", "3", "--json"],
            {"title": "t", "content": "0123456789"},
        )
        obj = json.loads(out)
        self.assertEqual(obj["count"], 1)
        self.assertTrue(obj["results"][0]["truncated"])
        self.assertEqual(obj["results"][0]["original_length"], 10)

    def test_url_validation_rejects_bad_scheme(self):
        code, _, _, calls = run_cli(self.base + ["ftp://a.com/x"], {})
        self.assertEqual(code, "raised:UsageError")
        self.assertEqual(calls, [])

    def test_duplicate_urls_deduped(self):
        code, _, _, calls = run_cli(
            self.base + ["https://a.com/x", "https://a.com/x"], {"title": "t", "content": "c"}
        )
        self.assertEqual(code, ds.EXIT_OK)
        self.assertEqual(len(calls), 1)


class TestKeyResolution(unittest.TestCase):
    def test_env_var_used_when_present(self):
        os.environ["DEEPSEEK_API_KEY"] = "env-key-123"
        try:
            self.assertEqual(ds.resolve_key(), "env-key-123")
        finally:
            os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_missing_explicit_file_raises(self):
        with self.assertRaises(ds.ConfigError):
            ds.resolve_key("/nonexistent/DeepSeek_API_Key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
