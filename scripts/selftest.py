#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测（不访问网络）：python3 scripts/selftest.py

覆盖 改进建议.md 里的纯规则环节：URL 去重、过滤、重排、截断、关键词摘录、缓存、重试。
"""

import argparse
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
    def test_search_text_has_core_fields_and_no_icon(self):
        text = ds.format_search_text([make_item("https://a.com/1", score=2.4, authority=3)], 800, 0)
        self.assertIn("[1] t:https://a.com/1", text)
        self.assertIn("URL:       https://a.com/1", text)
        self.assertIn("score: 2.4", text)
        self.assertIn("authority: 3", text)
        self.assertIn("date: 2025-06-01", text)
        self.assertNotIn("favicon", text)
        self.assertNotIn("Icon:", text)

    def test_summary_truncation(self):
        text = ds.format_search_text([make_item("https://a.com/1", summary="z" * 900)], 100, 0)
        self.assertIn("…", text)
        self.assertNotIn("z" * 101, text)

    def test_fetch_text_single_and_multi(self):
        doc = {"url": "https://a.com/1", "title": "标题", "content": "正文", "cached": False}
        self.assertTrue(ds.format_fetch_text(doc).startswith("# 标题"))
        self.assertIn("## [2/3]", ds.format_fetch_text(dict(doc, title="T2"), 2, 3))


if __name__ == "__main__":
    unittest.main(verbosity=2)
