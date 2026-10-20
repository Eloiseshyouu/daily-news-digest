"""Offline safety tests. All examples are synthetic, not actual news."""

import copy
import json
import os
import time
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import news_editor as editor


DAY = "2026-10-20"
CUTOFF = "2026-10-20T09:00:00+09:00"
FAKE_KEY = "offline_testing_key_not_a_real_secret"
FREE_ENV = {"GEMINI_FREE_TIER_CONFIRMED": "1", "GEMINI_API_KEY": FAKE_KEY}


def fixtures():
    sources, items = [], []
    # Intentionally synthetic; these fixtures must never be published as news.
    for index, area in enumerate(("甲市", "乙市", "丙市", "丁市")):
        short = f"{area}交通部门今天公布测试线路恢复通行的安排，首班车定于上午八点发出。"
        new_fact = f"{area}新增事实是测试线路已完成检查，早班车恢复而夜间班次仍维持原计划。"
        detail = (
            f"{area}交通部门今天发布测试线路运营通告，说明检查工作已经结束，并公布首班车的发车时间。"
            "此前线路因例行检修暂停服务，乘客须改乘临时接驳车；本次通告明确恢复的是早间班次，"
            "夜间服务安排保持不变。工作人员表示，站内将继续张贴乘车提示，并安排人员引导换乘。"
            "通告没有宣布票价调整，也没有提供下一阶段扩充班次的时间表。"
            "需要乘坐该线路的人应按通告中的车站名单和时刻表安排行程，其他线路不在本次恢复范围。"
            "以上均为离线测试材料，不代表真实交通消息，不得用于实际新闻投递。"
        )
        items.append({
            "section": "日本", "title": f"{area}测试线路今早恢复通行",
            "short": short,
            "why": f"{area}乘客需要确认自己的车次是否属于恢复范围，以便安排当日出行。",
            "detail": detail, "source_ids": [f"source-{index}"], "new_fact": new_fact,
        })
        sources.append({
            "id": f"source-{index}", "title": f"{area}测试交通通告",
            "url": f"https://example.org/news/{index}", "publisher": "测试媒体",
            "published_at": f"2026-10-20T08:0{index}:00+09:00",
            "text": f"{short}\n{new_fact}\n{detail}", "region": "japan",
        })
    draft = {
        "date": DAY, "cutoff": CUTOFF, "items": items,
        "top3": [{"item_index": i, "text": item["short"]} for i, item in enumerate(items[:3])],
    }
    return sources, draft


def passing_review(draft):
    return {
        "draft_hash": editor.draft_sha256(draft), "passed": True,
        "checks": {name: True for name in editor.CHECKS}, "issues": [],
        "items": [
            {
                "item_index": index, "all_factual_claims_covered": True,
                "evidence": [
                    {"claim": item[name], "source_id": item["source_ids"][0], "quote": item[name]}
                    for name in ("short", "new_fact")
                ],
            }
            for index, item in enumerate(draft["items"])
        ],
    }


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.sources, self.draft = fixtures()
        self.env = patch.dict(os.environ, FREE_ENV, clear=True)
        self.env.start()
        # A regression must never accidentally make a live request.
        self.no_network = patch(
            "news_editor.urllib.request.build_opener",
            side_effect=AssertionError("Live network is prohibited in offline tests"),
        )
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.addCleanup(self.env.stop)

    def call(self, history=None, sources=None):
        return editor.edit_digest(
            self.sources if sources is None else sources,
            [] if history is None else history,
            DAY, CUTOFF,
        )

    def test_free_confirmation_and_key_required_before_network(self):
        for env in ({}, {"GEMINI_API_KEY": FAKE_KEY}, {"GEMINI_FREE_TIER_CONFIRMED": "1"}):
            with self.subTest(env=list(env)), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(editor.EditorError):
                    self.call()

    def test_opaque_key_formats_and_local_length_boundaries_are_supported(self):
        # Synthetic strings only; these are not claims about Google's key grammar.
        keys = (
            FAKE_KEY,
            "test.auth." + "offline-segment_+" * 20 + "/=",
            "future-prefix." + "".join(chr(code) for code in range(33, 127)),
            "x" * editor.MIN_API_KEY_CHARS,
            "x" * editor.MAX_API_KEY_CHARS,
        )
        for index, key in enumerate(keys):
            with self.subTest(case=index), patch.dict(os.environ, {"GEMINI_API_KEY": key}):
                with patch("news_editor._generate_json", side_effect=[
                    self.draft, passing_review(self.draft),
                ]) as generate:
                    bundle = self.call()
                self.assertTrue(editor.verify_reviewed_digest(bundle))
                self.assertEqual(generate.call_count, 2)
                for call in generate.call_args_list:
                    self.assertEqual(call.kwargs["api_key"], key)

    def test_key_validation_rejects_whitespace_controls_and_unreasonable_lengths(self):
        invalid = [
            None, 123, "", "x" * (editor.MIN_API_KEY_CHARS - 1),
            "x" * (editor.MAX_API_KEY_CHARS + 1),
            " " + FAKE_KEY, FAKE_KEY + " ", FAKE_KEY + "\u00a0",
            FAKE_KEY + "\u200b", FAKE_KEY + "密钥", FAKE_KEY + "\u0085",
            *(FAKE_KEY + chr(code) for code in range(32)),
            FAKE_KEY + "\x7f",
        ]
        for index, key in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaisesRegex(editor.EditorError, "^GEMINI_API_KEY is missing or invalid.$"):
                    editor._validate_api_key(key)
        # Leading/trailing whitespace must not be silently stripped in the entry point.
        for index, key in enumerate((" " + FAKE_KEY, FAKE_KEY + "\n", FAKE_KEY + "\t")):
            with self.subTest(entry_case=index), patch.dict(os.environ, {"GEMINI_API_KEY": key}):
                with patch("news_editor._generate_json") as generate:
                    with self.assertRaises(editor.EditorError):
                        self.call()
                generate.assert_not_called()

    def test_paid_or_unknown_model_is_rejected(self):
        with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-4-argon"}):
            with self.assertRaisesRegex(editor.EditorError, "free text"):
                self.call()

    def test_pass_requires_independent_call_and_binds_exact_draft(self):
        responses = [copy.deepcopy(self.draft), passing_review(self.draft)]
        with patch("news_editor._generate_json", side_effect=responses) as generate:
            bundle = self.call()
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(generate.call_args_list[1].args[0], editor._REVIEW_INSTRUCTION)
        self.assertTrue(editor.verify_reviewed_digest(bundle))
        bundle["draft"]["items"][0]["title"] += "已被改写"
        self.assertFalse(editor.verify_reviewed_digest(bundle))

    def test_one_revision_gets_fresh_review_and_new_hash(self):
        rejection = passing_review(self.draft)
        rejection["passed"] = False
        rejection["checks"]["reader_relevance"] = False
        rejection["issues"] = ["重要性说明需要具体说明乘客受影响的范围。"]
        revised = copy.deepcopy(self.draft)
        revised["items"][0]["why"] = "甲市需要乘坐早班车的乘客可以恢复原有路线，夜间出行者仍须另行核对班次。"
        with patch("news_editor._generate_json", side_effect=[
            self.draft, rejection, revised, passing_review(revised),
        ]) as generate:
            bundle = self.call()
        self.assertEqual(generate.call_count, 4)
        self.assertEqual(bundle["review_hash"], editor.draft_sha256(revised))
        self.assertNotEqual(bundle["review_hash"], editor.draft_sha256(self.draft))

    def test_second_rejection_is_terminal(self):
        rejection = passing_review(self.draft)
        rejection["passed"] = False
        rejection["issues"] = ["事实证据不足，不能发布。"]
        with patch("news_editor._generate_json", side_effect=[
            self.draft, rejection, self.draft, rejection,
        ]) as generate:
            with self.assertRaisesRegex(editor.EditorError, "one revision"):
                self.call()
        self.assertEqual(generate.call_count, 4)

    def test_hallucinated_source_and_quotes_cannot_pass(self):
        draft = copy.deepcopy(self.draft)
        draft["items"][0]["source_ids"] = ["invented-id"]
        issues = editor._draft_issues(draft, self.sources, [], DAY, CUTOFF)
        self.assertTrue(any("来源" in issue for issue in issues))
        review = passing_review(self.draft)
        review["items"][0]["evidence"][0]["quote"] = "这段原文根本不存在于实际抓取的任何新闻正文之中。"
        self.assertTrue(editor._review_issues(review, self.draft, self.sources))

    def test_empty_partial_and_truthy_reviews_cannot_pass(self):
        for kind in ("missing_check", "string_true", "issues", "missing_evidence", "wrong_hash"):
            review = passing_review(self.draft)
            if kind == "missing_check":
                review["checks"].pop("facts_grounded")
            elif kind == "string_true":
                review["checks"]["facts_grounded"] = "true"
            elif kind == "issues":
                review["issues"] = ["未解决的问题"]
            elif kind == "missing_evidence":
                review["items"] = []
            else:
                review["draft_hash"] = "0" * 64
            with self.subTest(kind=kind):
                self.assertTrue(editor._review_issues(review, self.draft, self.sources))

    def test_send_verifier_rejects_missing_evidence_and_extra_review_fields(self):
        for change in (
            lambda review: review.update(items=[]),
            lambda review: review["items"][0].update(evidence=[]),
            lambda review: review.update(rewritten_draft={}),
        ):
            review = passing_review(self.draft)
            change(review)
            bundle = {
                "draft": self.draft, "review": review,
                "review_hash": editor.draft_sha256(self.draft),
            }
            self.assertFalse(editor.verify_reviewed_digest(bundle))

    def test_chinese_length_repetition_and_top3_validation(self):
        cases = [
            lambda d: d["items"][0].update(short="Government announces new changes for all local travellers."),
            lambda d: d["items"][0].update(title="日本の交通ニュースです"),
            lambda d: d["items"][0].update(detail="内容太少，不能据此写新闻。"),
            lambda d: d["items"][0].update(short=d["items"][0]["title"]),
            lambda d: d["items"].append(copy.deepcopy(d["items"][0])),
            lambda d: d["top3"][1].update(item_index=0),
            lambda d: d["top3"][1].update(item_index=True),
            lambda d: d.update(items=[]),
        ]
        self.assertEqual(editor._draft_issues(self.draft, self.sources, [], DAY, CUTOFF), [])
        for index, mutate in enumerate(cases):
            draft = copy.deepcopy(self.draft)
            mutate(draft)
            with self.subTest(case=index):
                self.assertTrue(editor._draft_issues(draft, self.sources, [], DAY, CUTOFF))

    def test_local_failures_cannot_be_overruled_by_review(self):
        self.draft["items"][0]["detail"] = "详情未达到最低证据要求。"
        with patch("news_editor._generate_json", side_effect=[
            self.draft, passing_review(self.draft), self.draft, passing_review(self.draft),
        ]):
            with self.assertRaises(editor.EditorError):
                self.call()

    def test_future_sources_never_enter_prompt(self):
        future = copy.deepcopy(self.sources[0])
        future.update(id="late", text="此内容晚于截止时间，绝不应进入模型。" * 20)
        future["published_at"] = "2026-10-20T09:01:00+09:00"
        with patch("news_editor._generate_json", side_effect=[
            self.draft, passing_review(self.draft),
        ]) as generate:
            self.call(sources=self.sources + [future])
        self.assertNotIn("late", [s["id"] for s in generate.call_args_list[0].args[1]["sources"]])

    def test_ambiguous_timestamp_and_wrong_cutoff_fail(self):
        self.sources[0]["published_at"] = "2026-10-20"
        with self.assertRaises(editor.EditorError):
            self.call()
        with self.assertRaises(editor.EditorError):
            editor.edit_digest(self.sources, [], DAY, "2026-10-20T09:01:00+09:00")

    def test_old_only_story_and_historical_copy_fail(self):
        old = copy.deepcopy(self.sources)
        old[0]["published_at"] = "2026-10-19T08:59:00+09:00"
        self.assertTrue(editor._draft_issues(self.draft, old, [], DAY, CUTOFF))
        previous = copy.deepcopy(self.draft)
        previous["date"] = "2026-10-19"
        history = editor._prepare_history([previous], DAY, FAKE_KEY)
        self.assertTrue(editor._draft_issues(self.draft, self.sources, history, DAY, CUTOFF))

    def test_private_extra_fields_are_not_forwarded(self):
        self.sources[0]["smtp_password"] = "do-not-forward-this"
        self.sources[0]["text"] += f"\ncontact@example.com {FAKE_KEY}"
        old = {
            "date": "2026-10-19", "title": "与本期不同的历史测试事件",
            "short": "此前另一个地区公布了不同的交通检查计划。",
            "recipient": "private@example.com", "user_identity": "private-user-identity",
        }
        with patch("news_editor._generate_json", side_effect=[
            self.draft, passing_review(self.draft),
        ]) as generate:
            self.call(history=[old])
        sent = json.dumps(generate.call_args_list[0].args[1], ensure_ascii=False)
        for secret in ("do-not-forward-this", "private-user-identity", "contact@example.com", FAKE_KEY):
            self.assertNotIn(secret, sent)

    def test_credential_urls_and_duplicate_ids_fail(self):
        self.sources[0]["url"] = "https://example.org/news?api_key=do-not-leak"
        with self.assertRaises(editor.EditorError):
            self.call()
        self.sources, self.draft = fixtures()
        self.sources[1]["id"] = self.sources[0]["id"]
        with self.assertRaises(editor.EditorError):
            self.call()

    def test_network_error_is_not_retried_or_revised(self):
        with patch("news_editor._generate_json", side_effect=editor.EditorError("HTTP 429")) as generate:
            with self.assertRaises(editor.EditorError):
                self.call()
        self.assertEqual(generate.call_count, 1)

    def fake_transport(self, raw):
        response = MagicMock()
        response.status = 200
        response.read.return_value = raw
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        return opener

    def invoke_transport(self, key=FAKE_KEY):
        return editor._generate_json(
            "测试指令", {"public": "公开测试新闻"},
            editor.DRAFT_SCHEMA, api_key=key, model=editor.DEFAULT_MODEL,
            deadline=time.monotonic() + 10,
        )

    def test_rest_request_has_no_search_tools_or_key_in_body(self):
        payload = {"candidates": [{
            "finishReason": "STOP", "content": {"parts": [{"text": '{"result":"测试"}'}]},
        }]}
        opener = self.fake_transport(json.dumps(payload).encode())
        with patch("news_editor.urllib.request.build_opener", return_value=opener):
            self.assertEqual(self.invoke_transport(), {"result": "测试"})
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(
            request.full_url,
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
        )
        self.assertNotIn("tools", body)
        self.assertNotIn(FAKE_KEY, request.data.decode())
        self.assertEqual(request.get_header("X-goog-api-key"), FAKE_KEY)
        self.assertEqual(body["generationConfig"]["responseFormat"]["text"]["mimeType"], "APPLICATION_JSON")

    def test_opaque_auth_key_is_only_transmitted_in_unchanged_header(self):
        key = "test.auth." + "offline_key-segment." * 20 + "segment+/="
        payload = {"candidates": [{
            "finishReason": "STOP", "content": {"parts": [{"text": '{"result":"测试"}'}]},
        }]}
        opener = self.fake_transport(json.dumps(payload).encode())
        with patch("news_editor.urllib.request.build_opener", return_value=opener):
            self.assertEqual(self.invoke_transport(key), {"result": "测试"})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("X-goog-api-key"), key)
        self.assertNotIn(key, request.full_url)
        self.assertNotIn(key, request.data.decode())

    def test_transport_rejects_header_injection_before_opening_connection(self):
        key = FAKE_KEY + "\r\nX-Injected-Header: value"
        with patch("news_editor.urllib.request.build_opener") as opener:
            with self.assertRaises(editor.EditorError) as caught:
                self.invoke_transport(key)
        opener.assert_not_called()
        self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_blocked_truncated_duplicate_json_and_empty_output_fail(self):
        bad_payloads = [
            {"promptFeedback": {"blockReason": "SAFETY"}},
            {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{}"}]}}]},
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"x":1,"x":2}'}]}}]},
            {"candidates": []},
        ]
        for payload in bad_payloads:
            opener = self.fake_transport(json.dumps(payload).encode())
            with self.subTest(payload=payload), patch(
                "news_editor.urllib.request.build_opener", return_value=opener
            ):
                with self.assertRaises(editor.EditorError):
                    self.invoke_transport()

    def test_http_error_does_not_leak_provider_body_or_key(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://example.org", 429, FAKE_KEY, {}, None
        )
        with patch("news_editor.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(editor.EditorError) as caught:
                self.invoke_transport()
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertIn("429", str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_deadline_prevents_request(self):
        with self.assertRaises(editor.EditorError):
            editor._generate_json(
                "test", {}, {}, api_key=FAKE_KEY, model=editor.DEFAULT_MODEL,
                deadline=time.monotonic() - 1,
            )


if __name__ == "__main__":
    unittest.main()
