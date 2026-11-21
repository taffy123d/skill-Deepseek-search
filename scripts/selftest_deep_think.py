#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_deep_think.py — deepseek_deep_think.py 的离线自测（不访问网络）

运行：python3 scripts/selftest_deep_think.py
覆盖：三协议解析 / 流式事件累积 / 引用去重与截断 / 视图分层 /
      请求体约束 / 错误文案映射 / 历史状态往返 / usage 归一化。

fixtures 直接取自官方文档（guides/web_tools）的响应示例。
"""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deepseek_deep_think as dt  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
CHAT_RESPONSE = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "reasoning_content": "资料够了，可以回答。",
                "content": "东京塔 333 米，埃菲尔铁塔 330 米，所以东京塔更高。",
                "annotations": [
                    {
                        "type": "url_citation",
                        "url_citation": {
                            "url": "https://en.tokyotower.co.jp/company/",
                            "title": "Tokyo Tower 公司信息",
                            "start_index": 0,
                            "end_index": 10,
                        },
                    },
                    {
                        "type": "url_citation",
                        "url_citation": {
                            "url": "https://en.tokyotower.co.jp/company/",
                            "title": "重复的同一 URL",
                            "start_index": 10,
                            "end_index": 20,
                        },
                    },
                    {
                        "type": "url_citation",
                        "url_citation": {
                            "url": "https://www.toureiffel.paris/en/the-monument/key-figures",
                            "title": "",
                            "start_index": 20,
                            "end_index": 30,
                        },
                    },
                ],
                "server_tool_calls": [
                    {
                        "index": 0,
                        "reasoning_content": "先查两边官网的身高数据。",
                        "calls": [
                            {
                                "index": 0,
                                "id": "srvtoolu_s_00_1",
                                "type": "deepseek_web_search",
                                "kind": "deep_search",
                                "action": {"type": "search", "queries": ["Tokyo Tower height", "Eiffel Tower height"]},
                                "status": "completed",
                                "sources": [
                                    {"type": "url", "url": "https://example.com/a", "title": "a"},
                                    {"type": "url", "url": "https://example.com/b", "title": "b"},
                                ],
                            }
                        ],
                    },
                    {
                        "index": 1,
                        "reasoning_content": "打开官网确认。",
                        "calls": [
                            {
                                "index": 0,
                                "id": "srvtoolu_ds_02_1",
                                "type": "deepseek_web_search",
                                "kind": "deep_search",
                                "action": {"type": "open_page", "url": "https://en.tokyotower.co.jp/company/"},
                                "status": "completed",
                                "page": {
                                    "url": "https://en.tokyotower.co.jp/company/",
                                    "title": "Tokyo Tower is a Tokyo landmark, standing 333 meters tall.",
                                    "published_at": None,
                                },
                            },
                            {
                                "index": 1,
                                "id": "srvtoolu_ds_02_2",
                                "type": "deepseek_web_search",
                                "kind": "deep_search",
                                "action": {"type": "find_in_page", "url": "https://en.tokyotower.co.jp/", "pattern": "330"},
                                "status": "completed",
                            },
                        ],
                    },
                ],
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 1905,
        "completion_tokens": 72,
        "completion_tokens_details": {"reasoning_tokens": 48},
        "prompt_tokens_details": {"cached_tokens": 1024},
        "server_tool_use": {"tool_search": 2, "tool_fetch": 3, "tool_image_search": 0},
    },
}

MESSAGES_RESPONSE = {
    "id": "18f3b95b",
    "type": "message",
    "role": "assistant",
    "model": "deepseek-flash",
    "content": [
        {
            "type": "thinking",
            "thinking": "先搜一下两座塔的官方数据。",
            "signature": "sig-thinking",
        },
        {
            "type": "server_tool_use",
            "id": "srvtoolu_s_00_1",
            "name": "deepseek_web_search",
            "input": {"action": "search", "queries": ["Iceland population 2024", "Iceland population 2025"]},
            "caller": {"type": "direct"},
            "kind": "deep_search",
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_s_00_1",
            "content": [
                {"type": "web_search_result", "title": "t1", "url": "https://example.com/1"},
                {"type": "web_search_result", "title": "t2", "url": "https://example.com/2"},
            ],
            "kind": "deep_search",
        },
        {
            "type": "server_tool_use",
            "id": "srvtoolu_ds_02_1",
            "name": "deepseek_web_search",
            "input": {"action": "open_page", "url": "https://en.tokyotower.co.jp/company/"},
            "caller": {"type": "direct"},
            "kind": "deep_search",
        },
        {
            "type": "web_fetch_tool_result",
            "tool_use_id": "srvtoolu_ds_02_1",
            "content": {
                "type": "web_fetch_result",
                "url": "https://en.tokyotower.co.jp/company/",
                "content": {
                    "type": "document",
                    "source": {"type": "text", "media_type": "text/plain", "data": ""},
                    "title": "Tokyo Tower is a Tokyo landmark, standing 333 meters tall.",
                },
                "published_at": None,
            },
            "kind": "deep_search",
        },
        {
            "type": "server_tool_use",
            "id": "srvtoolu_rl_01_1",
            "name": "deepseek_read_url",
            "input": {"action": "read_link", "url": "https://example.com"},
            "caller": {"type": "direct"},
            "kind": "read_link",
        },
        {
            "type": "web_fetch_tool_result",
            "tool_use_id": "srvtoolu_rl_01_1",
            "content": {
                "type": "web_fetch_result",
                "url": "https://example.com",
                "content": {
                    "type": "document",
                    "source": {"type": "text", "media_type": "text/plain", "data": ""},
                    "title": "Example Domain",
                },
                "published_at": None,
            },
            "kind": "read_link",
            "signature": "tQU8k+8Ko7MY5XDAvJd0...",
        },
        {
            "type": "text",
            "text": "东京塔 333 米，更高。",
            "citations": [
                {
                    "type": "web_search_result_location",
                    "url": "https://en.tokyotower.co.jp/company/",
                    "title": "Tokyo Tower",
                    "encrypted_index": "",
                    "cited_text": "东京塔 333 米，更高。",
                }
            ],
        },
        {"type": "text", "text": "埃菲尔铁塔 330 米。"},
    ],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 1905,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 512,
        "output_tokens": 72,
        "output_tokens_details": {"thinking_tokens": 40},
        "server_tool_use": {"tool_search": 2, "tool_fetch": 3, "tool_image_search": 0},
    },
}

RESPONSES_RESPONSE = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "model": "deepseek-flash",
    "output": [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": "先查两座塔的官方身高。"}],
        },
        {
            "type": "web_search_call",
            "id": "srvtoolu_s_00_1",
            "status": "completed",
            "kind": "deep_search",
            "action": {
                "type": "search",
                "queries": ["Tokyo Tower height"],
                "sources": [
                    {"type": "url", "url": "https://example.com/news", "title": "news"},
                ],
            },
        },
        {
            "type": "web_search_call",
            "id": "srvtoolu_ds_02_1",
            "status": "completed",
            "kind": "deep_search",
            "action": {"type": "open_page", "url": "https://en.tokyotower.co.jp/company/"},
            "page": {"url": "https://en.tokyotower.co.jp/company/", "title": "Tokyo Tower", "published_at": None},
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "东京塔 333 米，比埃菲尔铁塔高。",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": "https://en.tokyotower.co.jp/company/",
                            "title": "Tokyo Tower",
                            "start_index": 0,
                            "end_index": 8,
                        }
                    ],
                }
            ],
        },
    ],
    "usage": {
        "input_tokens": 100,
        "output_tokens": 20,
        "output_tokens_details": {"thinking_tokens": 10},
        "server_tool_use": {"tool_search": 1, "tool_fetch": 1, "tool_image_search": 0},
    },
}


def make_args(**overrides):
    base = dict(
        protocol="chat",
        model="deepseek-flash",
        stream=False,
        json=False,
        brief=False,
        summary=False,
        verbose=False,
        max_citations=dt.DEFAULT_MAX_CITATIONS,
        max_answer_chars=0,
        thinking_chars=dt.DEFAULT_THINKING_CHARS,
        max_tokens=0,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
class TestParseChat(unittest.TestCase):
    def setUp(self):
        self.result = dt.parse_chat(CHAT_RESPONSE)

    def test_answer(self):
        self.assertIn("东京塔", self.result.answer)
        self.assertEqual(self.result.finish_reason, "stop")

    def test_steps_chain(self):
        actions = [step["action"] for step in self.result.steps]
        self.assertEqual(actions, ["search", "open_page", "find_in_page"])
        self.assertEqual(self.result.steps[0]["result_count"], 2)
        self.assertEqual(self.result.steps[0]["query"], "Tokyo Tower height / Eiffel Tower height")
        self.assertEqual(self.result.steps[1]["url"], "https://en.tokyotower.co.jp/company/")
        self.assertEqual(self.result.steps[2]["pattern"], "330")
        self.assertEqual(self.result.steps[0]["kind"], "deep_search")

    def test_thinking_per_step(self):
        # 每步思考（前几步在 server_tool_calls[i]，最后一步在 message.reasoning_content）
        self.assertEqual(len(self.result.thinking), 3)
        self.assertIn("官网", self.result.thinking[0])
        self.assertIn("资料够了", self.result.thinking[-1])

    def test_usage_normalized(self):
        usage = self.result.usage
        self.assertEqual(usage["input"], 1905)
        self.assertEqual(usage["output"], 72)
        self.assertEqual(usage["thinking"], 48)
        self.assertEqual(usage["cache_read"], 1024)
        self.assertEqual(usage["tool_search"], 2)
        self.assertEqual(usage["tool_fetch"], 3)

    def test_raw_assistant_keeps_protocol_fields(self):
        raw = self.result.raw_assistant
        self.assertEqual(raw["role"], "assistant")
        self.assertEqual(len(raw["server_tool_calls"]), 2)
        self.assertEqual(raw["server_tool_calls"][0]["calls"][0]["kind"], "deep_search")

    def test_error_body(self):
        result = dt.parse_chat({"error": {"message": "boom"}, "usage": {"prompt_tokens": 10}})
        self.assertEqual(result.error, "boom")
        self.assertEqual(result.usage["input"], 10)

    def test_missing_choices(self):
        self.assertTrue(dt.parse_chat({"usage": {}}).error)


class TestParseMessages(unittest.TestCase):
    def setUp(self):
        self.result = dt.parse_messages(MESSAGES_RESPONSE)

    def test_answer_concatenated(self):
        self.assertEqual(self.result.answer, "东京塔 333 米，更高。埃菲尔铁塔 330 米。")
        self.assertEqual(self.result.finish_reason, "end_turn")

    def test_citations(self):
        self.assertEqual(len(self.result.citations), 1)
        self.assertEqual(self.result.citations[0]["url"], "https://en.tokyotower.co.jp/company/")

    def test_steps_and_counts(self):
        actions = [step["action"] for step in self.result.steps]
        self.assertEqual(actions, ["search", "open_page", "read_link"])
        self.assertEqual(self.result.steps[0]["result_count"], 2)
        self.assertEqual(self.result.steps[1]["result_count"], 1)

    def test_thinking(self):
        self.assertEqual(self.result.thinking, ["先搜一下两座塔的官方数据。"])

    def test_raw_assistant_keeps_signature(self):
        blocks = self.result.raw_assistant["content"]
        signatured = [b for b in blocks if b.get("signature") == "tQU8k+8Ko7MY5XDAvJd0..."]
        self.assertEqual(len(signatured), 1)
        self.assertEqual(signatured[0]["kind"], "read_link")

    def test_error_type(self):
        result = dt.parse_messages({"type": "error", "error": {"message": "x"}, "usage": {"input_tokens": 3}})
        self.assertEqual(result.error, "x")
        self.assertEqual(result.usage["input"], 3)

    def test_failed_result_block_marks_step(self):
        obj = json.loads(json.dumps(MESSAGES_RESPONSE))
        obj["content"][2]["content"] = [{"type": "web_search_tool_result_error", "error_code": "unavailable"}]
        result = dt.parse_messages(obj)
        self.assertEqual(result.steps[0]["status"], "failed")
        self.assertEqual(result.steps[0]["result_count"], 0)


class TestParseResponses(unittest.TestCase):
    def setUp(self):
        self.result = dt.parse_responses(RESPONSES_RESPONSE)

    def test_answer_and_citations(self):
        self.assertIn("东京塔", self.result.answer)
        self.assertEqual(len(self.result.citations), 1)
        self.assertEqual(self.result.finish_reason, "completed")

    def test_steps(self):
        actions = [step["action"] for step in self.result.steps]
        self.assertEqual(actions, ["search", "open_page"])
        self.assertEqual(self.result.steps[0]["result_count"], 1)

    def test_thinking(self):
        self.assertEqual(self.result.thinking, ["先查两座塔的官方身高。"])

    def test_raw_assistant_is_output_items(self):
        self.assertEqual(len(self.result.raw_assistant), 4)
        self.assertEqual(self.result.raw_assistant[1]["type"], "web_search_call")

    def test_failed_status(self):
        result = dt.parse_responses({"status": "failed", "error": {"code": "server_error", "message": "x"}})
        self.assertEqual(result.error, "x")

    def test_incomplete_content_filter(self):
        result = dt.parse_responses(
            {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}, "output": []}
        )
        self.assertEqual(result.finish_reason, "content_filter")
        self.assertIn(result.finish_reason, dt.FINISH_MESSAGES)


# --------------------------------------------------------------------------- #
# 流式累积：还原成非流式形态后应与非流式解析一致
# --------------------------------------------------------------------------- #
class TestStreamCollectors(unittest.TestCase):
    def test_chat_stream(self):
        events = [
            (None, {"choices": [{"index": 0, "delta": {"reasoning_content": "先查"}}]}),
            (None, {"choices": [{"index": 0, "delta": {"reasoning_content": "官网"}}]}),
            (
                None,
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "server_tool_event": {"type": "step_reasoning_done", "step": 0},
                        }
                    ]
                },
            ),
            (
                None,
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "server_tool_event": {
                                "type": "call_started",
                                "step": 0,
                                "call": {
                                    "index": 0,
                                    "id": "c1",
                                    "kind": "deep_search",
                                    "action": {"type": "search", "queries": ["q1"]},
                                },
                            },
                        }
                    ]
                },
            ),
            (
                None,
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "server_tool_event": {
                                "type": "call_completed",
                                "step": 0,
                                "call": {
                                    "index": 0,
                                    "id": "c1",
                                    "kind": "deep_search",
                                    "status": "completed",
                                    "sources": [{"type": "url", "url": "https://example.com/a"}],
                                },
                            },
                        }
                    ]
                },
            ),
            (None, {"choices": [{"index": 0, "delta": {"content": "东京塔更高"}}]}),
            (
                None,
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url_citation": {"url": "https://example.com/a", "title": "A"},
                                    }
                                ]
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "server_tool_use": {"tool_search": 1}},
                },
            ),
        ]
        response = dt.collect_chat_stream(events)
        result = dt.parse_chat(response)
        self.assertEqual(result.answer, "东京塔更高")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual([s["action"] for s in result.steps], ["search"])
        self.assertEqual(result.steps[0]["result_count"], 1)
        self.assertEqual(result.thinking, ["先查官网"])
        self.assertEqual(result.usage["tool_search"], 1)
        self.assertEqual(len(result.citations), 1)

    def test_chat_stream_error_chunk(self):
        response = dt.collect_chat_stream([(None, {"error": {"message": "模型中断"}, "usage": {"prompt_tokens": 3}})])
        result = dt.parse_chat(response)
        self.assertEqual(result.error, "模型中断")
        self.assertEqual(result.usage["input"], 3)

    def test_messages_stream(self):
        events = [
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "先搜"}},
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "server_tool_use",
                        "id": "c1",
                        "name": "deepseek_web_search",
                        "input": {"action": "search", "queries": ["q1"]},
                        "kind": "deep_search",
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {
                        "type": "web_search_tool_result",
                        "tool_use_id": "c1",
                        "content": [{"type": "web_search_result", "url": "https://example.com/a"}],
                        "kind": "deep_search",
                    },
                },
            ),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 3, "content_block": {"type": "text", "text": ""}},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 3, "delta": {"type": "text_delta", "text": "东京塔更高"}},
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 3,
                    "delta": {
                        "type": "citations_delta",
                        "citation": {"type": "web_search_result_location", "url": "https://example.com/a", "title": "A"},
                    },
                },
            ),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 8, "server_tool_use": {"tool_search": 1}},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        result = dt.parse_messages(dt.collect_messages_stream(events))
        self.assertEqual(result.answer, "东京塔更高")
        self.assertEqual(result.finish_reason, "end_turn")
        self.assertEqual(result.thinking, ["先搜"])
        self.assertEqual([s["action"] for s in result.steps], ["search"])
        self.assertEqual(result.steps[0]["result_count"], 1)
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(result.usage["tool_search"], 1)

    def test_messages_stream_error_event(self):
        events = [
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 7}}}),
            ("error", {"type": "error", "error": {"message": "推理超时"}, "usage": {"output_tokens": 1}}),
        ]
        result = dt.parse_messages(dt.collect_messages_stream(events))
        self.assertEqual(result.error, "推理超时")

    def test_responses_stream(self):
        events = [
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": "先查官网"}],
                    },
                },
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 1,
                    "item": {
                        "type": "web_search_call",
                        "id": "c1",
                        "kind": "deep_search",
                        "action": {"type": "search", "queries": ["q1"]},
                    },
                },
            ),
            (
                "response.web_search_call.completed",
                {"type": "response.web_search_call.completed", "output_index": 1},
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": {
                        "type": "web_search_call",
                        "id": "c1",
                        "status": "completed",
                        "kind": "deep_search",
                        "action": {"type": "search", "queries": ["q1"], "sources": [{"type": "url", "url": "https://e/a"}]},
                    },
                },
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 2,
                    "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ""}]},
                },
            ),
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "output_index": 2, "delta": "东京塔更高"},
            ),
            (
                "response.output_text.annotation.added",
                {
                    "type": "response.output_text.annotation.added",
                    "output_index": 2,
                    "annotation": {"type": "url_citation", "url": "https://e/a", "title": "A"},
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 4}},
                },
            ),
        ]
        result = dt.parse_responses(dt.collect_responses_stream(events))
        self.assertEqual(result.answer, "东京塔更高")
        self.assertEqual(result.finish_reason, "completed")
        self.assertEqual(result.thinking, ["先查官网"])
        self.assertEqual([s["action"] for s in result.steps], ["search"])
        self.assertEqual(result.steps[0]["result_count"], 1)
        self.assertEqual(len(result.citations), 1)

    def test_responses_stream_failed(self):
        events = [
            (
                "response.failed",
                {
                    "type": "response.failed",
                    "response": {"status": "failed", "error": {"code": "server_error", "message": "中断"}},
                },
            )
        ]
        result = dt.parse_responses(dt.collect_responses_stream(events))
        self.assertEqual(result.error, "中断")


# --------------------------------------------------------------------------- #
# 视图归约
# --------------------------------------------------------------------------- #
class TestViews(unittest.TestCase):
    def setUp(self):
        self.result = dt.parse_chat(CHAT_RESPONSE)

    def test_citations_dedupe_and_title_fallback(self):
        citations = dt.normalize_citations(self.result.citations, 8)
        urls = [c["url"] for c in citations]
        self.assertEqual(
            urls,
            ["https://en.tokyotower.co.jp/company/", "https://www.toureiffel.paris/en/the-monument/key-figures"],
        )
        # title 为空时回落成 URL
        self.assertEqual(citations[1]["title"], citations[1]["url"])

    def test_citation_cap(self):
        self.assertEqual(len(dt.normalize_citations(self.result.citations, 1)), 1)

    def test_citation_cap_zero_means_unlimited(self):
        self.assertEqual(len(dt.normalize_citations(self.result.citations, 0)), 2)

    def test_help_mentions_four_routes(self):
        # 验收要求：--help 里能看到四层路由的简要提示
        text = dt.build_parser().format_help()
        for route in ("no_search", "only_search", "search_and_fetch_once", "deep_think"):
            self.assertIn(route, text)
        self.assertIn("计费", text)

    def test_trace_summary(self):
        self.assertEqual(dt.trace_summary(self.result.steps), "搜索1次，打开1页，页内查找1次")
        self.assertEqual(dt.trace_summary([]), "未使用检索工具")

    def test_thinking_digest_last_sentence(self):
        digest = dt.thinking_digest(["先看看。再确认一下。", "资料够了，可以回答。"], 120)
        self.assertEqual(digest, ["再确认一下。", "资料够了，可以回答。"])

    def test_thinking_digest_clip(self):
        digest = dt.thinking_digest(["一二三四五六七八九十。"], 5)
        self.assertEqual(len(digest[0]), 5)

    def test_tool_trace(self):
        trace = dt.tool_trace(self.result.steps)
        self.assertEqual(trace[0], {"action": "search", "query": "Tokyo Tower height / Eiffel Tower height", "result_count": 2})
        self.assertEqual(trace[1]["url"], "https://en.tokyotower.co.jp/company/")
        self.assertEqual(trace[2]["pattern"], "330")

    def test_default_view_only_answer_and_citations(self):
        view = dt.build_view(self.result, make_args())
        self.assertEqual(set(view), {"answer", "citations"})

    def test_summary_view(self):
        view = dt.build_view(self.result, make_args(summary=True))
        self.assertEqual(set(view), {"answer", "citations", "trace_summary"})

    def test_verbose_view(self):
        view = dt.build_view(self.result, make_args(verbose=True))
        self.assertEqual(set(view), {"answer", "citations", "trace_summary", "thinking_digest", "tool_trace"})

    def test_brief_view(self):
        view = dt.build_view(self.result, make_args(brief=True))
        self.assertEqual(set(view), {"answer"})

    def test_max_answer_chars(self):
        view = dt.build_view(self.result, make_args(max_answer_chars=4))
        self.assertEqual(len(view["answer"]), 4)

    def test_view_never_leaks_protocol_fields(self):
        payload = json.dumps(dt.build_view(self.result, make_args(verbose=True)), ensure_ascii=False)
        for needle in ("signature", "encrypted_index", "cited_text", "server_tool_calls", "snippet"):
            self.assertNotIn(needle, payload)


# --------------------------------------------------------------------------- #
# 请求体约束
# --------------------------------------------------------------------------- #
class TestPayload(unittest.TestCase):
    def test_chat(self):
        payload = dt.build_payload("chat", "deepseek-flash", [{"role": "user", "content": "q"}])
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["tools"], [{"type": "deepseek_web_search"}])
        self.assertEqual(payload["model"], "deepseek-flash")
        for banned in ("tool_choice", "web_search_options", "response_format", "logprobs", "top_logprobs"):
            self.assertNotIn(banned, payload)

    def test_messages(self):
        payload = dt.build_payload("messages", "deepseek-flash", [{"role": "user", "content": "q"}])
        self.assertEqual(
            payload["tools"], [{"type": "deepseek_web_search", "name": "deepseek_web_search"}]
        )

    def test_responses(self):
        payload = dt.build_payload("responses", "deepseek-flash", [{"role": "user", "content": "q"}])
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertIn("input", payload)
        self.assertNotIn("messages", payload)

    def test_max_tokens_mapping(self):
        self.assertEqual(dt.build_payload("chat", "m", [], max_tokens=100)["max_tokens"], 100)
        self.assertEqual(dt.build_payload("responses", "m", [], max_tokens=100)["max_output_tokens"], 100)
        self.assertNotIn("max_tokens", dt.build_payload("chat", "m", [], max_tokens=0))

    def test_stream_flag(self):
        self.assertTrue(dt.build_payload("chat", "m", [], stream=True)["stream"])
        self.assertNotIn("stream", dt.build_payload("chat", "m", [], stream=False))

    def test_headers(self):
        chat = dt.build_headers("chat", "KEY")
        self.assertEqual(chat["Authorization"], "Bearer KEY")
        messages = dt.build_headers("messages", "KEY")
        self.assertEqual(messages["x-api-key"], "KEY")
        self.assertNotIn("Authorization", messages)
        self.assertEqual(dt.masked_headers(chat)["Authorization"], "***")


# --------------------------------------------------------------------------- #
# 错误映射
# --------------------------------------------------------------------------- #
class TestErrors(unittest.TestCase):
    def test_documented_400_hints(self):
        cases = [
            ("tools: `deepseek_web_search` can only be declared once", "重复声明"),
            (
                "tools: other tools cannot be used together with `deepseek_web_search`, `deepseek_read_url`",
                "不能与其他服务端工具",
            ),
            ("tools: `allowed_domains` is not supported by `deepseek_web_search`", "不被"),
            ("tool_choice: `deepseek_web_search` cannot be forced", "tool_choice"),
            (
                "messages: the last `assistant` message used `deepseek_web_search` or `deepseek_read_url` and cannot be continued; resend the previous request",
                "重发原始请求",
            ),
            ("Content Exists Risk", "内容审核"),
        ]
        for message, needle in cases:
            text = dt.describe_api_error(400, message)
            self.assertIn(needle, text, msg=message)

    def test_status_heads(self):
        self.assertIn("余额不足", dt.describe_api_error(402, ""))
        self.assertIn("API Key", dt.describe_api_error(401, ""))
        self.assertIn("限流", dt.describe_api_error(429, ""))
        self.assertIn("HTTP 503", dt.describe_api_error(503, ""))

    def test_usage_appended_on_body_error(self):
        text = dt.describe_api_error(400, "boom", {"input": 5})
        self.assertIn("input=5", text)

    def test_finish_reason_guard(self):
        for reason in ("content_filter", "length", "max_tokens", "refusal", "max_output_tokens", "incomplete"):
            self.assertIn(reason, dt.FINISH_MESSAGES)
        for reason in ("stop", "end_turn", "completed", ""):
            self.assertNotIn(reason, dt.FINISH_MESSAGES)


# --------------------------------------------------------------------------- #
# usage / 产物路径 / 历史状态
# --------------------------------------------------------------------------- #
class TestUsage(unittest.TestCase):
    def test_line_mentions_free_tools(self):
        line = dt.usage_line(dt.normalize_usage(MESSAGES_RESPONSE["usage"]))
        self.assertIn("thinking=40", line)
        self.assertIn("tool_search=2", line)
        self.assertIn("不按次计费", line)

    def test_responses_naming(self):
        # Responses API 用 input_tokens_details.cached_tokens / output_tokens_details.reasoning_tokens
        usage = dt.normalize_usage(
            {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 4},
                "output_tokens": 6,
                "output_tokens_details": {"reasoning_tokens": 3},
            }
        )
        self.assertEqual((usage["input"], usage["cache_read"], usage["output"], usage["thinking"]), (10, 4, 6, 3))

    def test_missing_usage(self):
        self.assertIn("未返回", dt.usage_line({}))

    def test_tolerates_both_namings(self):
        chat = dt.normalize_usage({"prompt_tokens": 1, "completion_tokens": 2})
        messages = dt.normalize_usage({"input_tokens": 1, "output_tokens": 2})
        self.assertEqual((chat["input"], chat["output"]), (1, 2))
        self.assertEqual((messages["input"], messages["output"]), (1, 2))


class TestArtifacts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.tmp)

    def test_relative_path_resolves_under_skill_root(self):
        path = dt.artifact_path(".cache/deep-think/history.json", dt.DEFAULT_HISTORY)
        self.assertTrue(path.startswith(dt.SKILL_ROOT))

    def test_outside_path_only_warns(self):
        target = os.path.join(self.tmp, "x.json")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            resolved = dt.artifact_path(target, None)
        self.assertEqual(resolved, os.path.abspath(target))
        self.assertIn("不在 skill 目录内", stderr.getvalue())

    def test_write_atomic_and_log(self):
        path = os.path.join(self.tmp, "sub", "log.jsonl")
        dt.append_log(path, {"a": 1})
        dt.append_log(path, {"a": 2})
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().strip().split("\n")
        self.assertEqual(len(lines), 2)

    def test_history_roundtrip_keeps_protocol_fields(self):
        path = os.path.join(self.tmp, "history.json")
        result = dt.parse_messages(MESSAGES_RESPONSE)
        items = [{"role": "user", "content": "问题"}, result.raw_assistant]
        dt.save_history(path, "messages", "deepseek-flash", items)
        loaded, meta = dt.load_history(path)
        self.assertEqual(meta["protocol"], "messages")
        self.assertEqual(loaded[1]["content"][6]["signature"], "tQU8k+8Ko7MY5XDAvJd0...")

    def test_responses_history_is_flat(self):
        # responses 的 raw_assistant 是 output item 列表，写回历史时必须逐个展开
        path = os.path.join(self.tmp, "history.json")
        result = dt.parse_responses(RESPONSES_RESPONSE)
        saved = [{"role": "user", "content": "问题"}]
        saved.extend(result.raw_assistant)
        dt.save_history(path, "responses", "deepseek-flash", saved)
        loaded, _meta = dt.load_history(path)
        self.assertTrue(all(isinstance(item, dict) for item in loaded))
        self.assertEqual(
            [item.get("type") for item in loaded[1:]],
            ["reasoning", "web_search_call", "web_search_call", "message"],
        )

    def test_history_flattens_legacy_nested_array(self):
        # 旧版本把 responses 的 output items 塞成一个嵌套数组，读的时候要展平
        path = os.path.join(self.tmp, "history.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "protocol": "responses",
                    "items": [
                        {"role": "user", "content": "问题"},
                        [{"type": "message", "role": "assistant", "content": []}, "junk"],
                    ],
                },
                fh,
            )
        loaded, meta = dt.load_history(path)
        self.assertEqual(meta["protocol"], "responses")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[1]["type"], "message")

    def test_history_accepts_plain_array(self):
        path = os.path.join(self.tmp, "history.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([{"role": "user", "content": "hi"}], fh)
        loaded, meta = dt.load_history(path)
        self.assertEqual(loaded, [{"role": "user", "content": "hi"}])
        self.assertEqual(meta, {})

    def test_history_missing_file_is_empty(self):
        self.assertEqual(dt.load_history(os.path.join(self.tmp, "nope.json")), ([], {}))

    def test_build_items_appends_question(self):
        items = dt.build_items([{"role": "user", "content": "上一轮"}], "本轮", "responses")
        self.assertEqual(items[-1], {"role": "user", "content": "本轮"})
        self.assertEqual(len(items), 2)


# --------------------------------------------------------------------------- #
# CLI 行为（不联网）
# --------------------------------------------------------------------------- #
class TestCli(unittest.TestCase):
    def test_dry_run_prints_payload_and_returns_0(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = dt.main(["问题", "--dry-run", "--protocol", "messages"])
        self.assertEqual(code, dt.EXIT_OK)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["protocol"], "messages")
        self.assertEqual(payload["payload"]["tools"][0]["name"], "deepseek_web_search")

    def test_dry_run_writes_no_files(self):
        before = set(os.listdir(os.path.join(dt.SKILL_ROOT, ".cache"))) if os.path.isdir(
            os.path.join(dt.SKILL_ROOT, ".cache")
        ) else set()
        with contextlib.redirect_stdout(io.StringIO()):
            dt.main(["问题", "--dry-run", "--log", "--dump-raw"])
        after = set(os.listdir(os.path.join(dt.SKILL_ROOT, ".cache"))) if os.path.isdir(
            os.path.join(dt.SKILL_ROOT, ".cache")
        ) else set()
        self.assertEqual(before, after)

    def test_missing_question_is_usage_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = dt.main([])
        self.assertEqual(code, dt.EXIT_USAGE)

    def test_unknown_protocol_is_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = dt.main(["问题", "--protocol", "nope"])
        self.assertEqual(code, dt.EXIT_USAGE)

    def test_negative_limits_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dt.main(["问题", "--max-citations", "-1"]), dt.EXIT_USAGE)

    def test_missing_key_is_usage_error(self):
        env_key = os.environ.pop("DEEPSEEK_API_KEY", None)
        env_file = os.environ.pop("DEEPSEEK_API_KEY_FILE", None)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                code = dt.main(["问题", "--key-file", "/nonexistent/DeepSeek_API_Key"])
        finally:
            if env_key is not None:
                os.environ["DEEPSEEK_API_KEY"] = env_key
            if env_file is not None:
                os.environ["DEEPSEEK_API_KEY_FILE"] = env_file
        self.assertEqual(code, dt.EXIT_USAGE)

    def test_cross_protocol_history_rejected(self):
        # 状态文件与协议绑定：跨协议回传在服务端必然 422，本地直接拒绝
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "h.json")
            dt.save_history(path, "messages", "deepseek-flash", [{"role": "user", "content": "q"}])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                code = dt.main(["问题", "--protocol", "chat", "--history", path, "--dry-run"])
            self.assertEqual(code, dt.EXIT_USAGE)
            self.assertIn("状态文件与协议绑定", stderr.getvalue())
        finally:
            os.unlink(path)
            os.rmdir(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
