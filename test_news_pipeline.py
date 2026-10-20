import copy
import hashlib
import json
import unittest
from datetime import datetime
from unittest.mock import patch

import news_pipeline as p


class SafetyTests(unittest.TestCase):
    def test_japan_day_and_nine_cutoff(self):
        c = p.context(datetime.fromisoformat("2026-10-20T23:50:00+00:00"))
        self.assertEqual(c["date"], "2026-10-21")
        self.assertEqual(c["cutoff"], "2026-10-21T09:00:00+09:00")

    def test_delivery_window(self):
        for time in ("08:59:59", "09:30:01", "12:00:00"):
            with self.assertRaises(RuntimeError):
                p.delivery_window("2026-10-20", datetime.fromisoformat(f"2026-10-20T{time}+09:00"))
        p.delivery_window("2026-10-20", datetime.fromisoformat("2026-10-20T09:00:00+09:00"))

    def test_collection_must_finish_before_cutoff(self):
        cutoff = "2026-10-20T09:00:00+09:00"
        p.collection_deadline(cutoff, datetime.fromisoformat("2026-10-20T08:59:59+09:00"))
        for stamp in ("2026-10-20T09:00:00+09:00", "2026-10-20T09:20:00+09:00",
                      "2026-10-20T08:54:00"):
            with self.assertRaises(RuntimeError):
                p.collection_deadline(cutoff, datetime.fromisoformat(stamp))

    def test_unknown_send_prevents_duplicate(self):
        with self.assertRaises(RuntimeError):
            p.no_previous_claim([{"name": "daily-news-claim-2026-10-20"}], "2026-10-20")
        p.no_previous_claim([{"name": "daily-news-claim-2026-10-19"}], "2026-10-20")

    def test_review_hash_and_all_checks(self):
        from test_news_editor import fixtures, passing_review
        sources, draft = fixtures()
        bundle = {
            "draft": draft,
            "sources": sources,
            "review": passing_review(draft),
            "review_hash": p.digest_hash(draft),
        }
        p.verify_review(bundle)
        modified = copy.deepcopy(bundle)
        modified["draft"]["items"][0]["short"] += "未经审核的新内容"
        with self.assertRaises(RuntimeError):
            p.verify_review(modified)
        bundle["review"]["checks"]["facts_grounded"] = False
        with self.assertRaises(RuntimeError):
            p.verify_review(bundle)

    def test_free_gate_before_any_network(self):
        with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen") as network:
            with self.assertRaisesRegex(RuntimeError, "付费"):
                p.prepare(True)
            network.assert_not_called()

    def test_missing_api_key_stops_before_network(self):
        with patch.dict("os.environ", {"GEMINI_FREE_TIER_CONFIRMED": "1"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                p.prepare(True)

    def test_only_one_recipient(self):
        for value in ("a@example.com,b@example.com", "a@example.com\nBcc:x@example.com", "Invalid"):
            with patch.dict("os.environ", {"MAIL_TO": value}):
                with self.assertRaises(RuntimeError):
                    p.recipient()

    def test_cannot_send_locally(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                p.send()

    def send_fixture(self):
        from test_news_editor import fixtures, passing_review
        sources, draft = fixtures()
        bundle = {
            "draft": draft, "sources": sources, "review": passing_review(draft),
            "review_hash": p.digest_hash(draft), "collected_at": "2026-10-20T08:54:00+09:00",
        }
        claim = {
            "date": draft["date"], "digest_hash": p.digest_hash(bundle),
            "recipient_hash": hashlib.sha256(b"reader@example.com").hexdigest(),
            "message_id": "<offline-test@example.com>", "run_id": "1", "run_attempt": "1",
        }
        env = {
            "GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1",
            "MAIL_TO": "reader@example.com", "MAIL_FROM": "sender@example.com",
            "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
            "SMTP_USER": "offline", "SMTP_PASSWORD": "offline",
        }
        return bundle, claim, env

    def test_smtp_unknown_result_is_not_retried(self):
        bundle, claim, env = self.send_fixture()
        with patch.dict("os.environ", env, clear=True), \
             patch.object(p.Path, "read_text", side_effect=[json.dumps(bundle), json.dumps(claim)]), \
             patch.object(p.Path, "write_text") as write, \
             patch.object(p, "artifacts", return_value=[{"name": p.artifact_name("claim", claim["date"])}]), \
             patch.object(p, "artifact_file", return_value=claim), \
             patch.object(p, "wait_until"), patch.object(p, "delivery_window"), \
             patch.object(p.smtplib, "SMTP") as smtp:
            smtp.return_value.send_message.side_effect = TimeoutError("Unknown delivery")
            with self.assertRaises(TimeoutError):
                p.send()
            self.assertEqual(smtp.return_value.send_message.call_count, 1)
            write.assert_not_called()

    def test_rerun_attempt_cannot_reuse_claim(self):
        bundle, claim, env = self.send_fixture()
        env["GITHUB_RUN_ATTEMPT"] = "2"
        with patch.dict("os.environ", env, clear=True), \
             patch.object(p.Path, "read_text", side_effect=[json.dumps(bundle), json.dumps(claim)]), \
             patch.object(p, "artifacts", return_value=[{"name": p.artifact_name("claim", claim["date"])}]), \
             patch.object(p, "artifact_file", return_value=claim), \
             patch.object(p.smtplib, "SMTP") as smtp:
            with self.assertRaisesRegex(RuntimeError, "运行尝试"):
                p.send()
            smtp.assert_not_called()

    def test_render_contains_chinese_body_source_links_and_real_capture_time(self):
        bundle, _, _ = self.send_fixture()
        title, plain, page = p.render(bundle)
        self.assertIn("每日重点新闻简报", title)
        self.assertIn("08:54", plain)
        self.assertIn('lang="zh-CN"', page)
        self.assertIn('href="https://example.org/news/0"', page)
        self.assertLess(plain.index("详细版："), plain.index("来源："))
        self.assertLess(plain.index("来源："), plain.index("为什么重要："))
        self.assertLess(page.index("详细版："), page.index("来源："))
        self.assertLess(page.index("来源："), page.index("为什么重要："))

    def test_preview_and_test_are_not_labelled_as_nine_am_edition(self):
        bundle, _, _ = self.send_fixture()
        bundle["edition_type"] = "preview"
        bundle["delivery_mode"] = "test"
        title, plain, page = p.render(bundle)
        self.assertTrue(title.startswith("【测试邮件】"))
        self.assertIn("样稿", title)
        self.assertIn("并非上午9点版", plain)

    def test_claim_test_requires_manual_github_run(self):
        for env in ({}, {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "schedule"}):
            with patch.dict("os.environ", env, clear=True), patch.object(p, "artifacts") as network:
                with self.assertRaisesRegex(RuntimeError, "手动"):
                    p.claim_test()
                network.assert_not_called()

    def test_test_send_skips_daily_window_but_keeps_unique_claim(self):
        bundle, claim, env = self.send_fixture()
        bundle.update(edition_type="preview", delivery_mode="test")
        claim.update(kind="test-claim", date=datetime.now(p.JST).date().isoformat(),
                     digest_hash=p.digest_hash(bundle))
        env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        with patch.dict("os.environ", env, clear=True), \
             patch.object(p.Path, "read_text", side_effect=[json.dumps(bundle), json.dumps(claim)]), \
             patch.object(p.Path, "write_text"), \
             patch.object(p, "artifacts", return_value=[{"name": p.artifact_name("test-claim", claim["date"])}]), \
             patch.object(p, "artifact_file", return_value=claim), \
             patch.object(p, "wait_until") as wait, patch.object(p, "delivery_window") as window, \
             patch.object(p.smtplib, "SMTP") as smtp:
            smtp.return_value.send_message.return_value = {}
            p.send()
            wait.assert_not_called()
            window.assert_not_called()
            message = smtp.return_value.send_message.call_args.args[0]
            self.assertTrue(message["Subject"].startswith("【测试邮件】"))


if __name__ == "__main__":
    unittest.main()
