import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import news_sources as sources


CUTOFF = datetime(2026, 10, 20, 0, tzinfo=timezone.utc)
BODY = "Confirmed reporting with sufficient context and full public article body. " * 12


def feed(url="https://example.com/article", published="2026-10-19T21:00:00Z", extra=""):
    return (
        '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
        f"<title>News</title><link>{url}</link><pubDate>{published}</pubDate>"
        f"{extra}</item></channel></rss>"
    )


class SourceTests(unittest.TestCase):
    def test_cjk_full_body_floor_does_not_accept_short_leads(self):
        body = "这是已经公开的新闻正文，包含事件经过和背景信息。" * 10
        self.assertLess(len(body), 400)
        self.assertEqual(sources._extract("<article><p>" + body + "</p></article>", CUTOFF), body)
        self.assertIsNone(sources._extract("<article><p>" + body[:180] + "</p></article>", CUTOFF))

    def test_sports_url_does_not_consume_collection_slot(self):
        self.assertEqual(sources._feed_items(feed(url="https://example.com/sports/match"), CUTOFF), [])

    def test_aware_cutoff_required(self):
        with self.assertRaises(ValueError):
            sources.collect_sources(CUTOFF.replace(tzinfo=None))

    def test_basic_timezone_offset_supported(self):
        self.assertEqual(sources._date("2026-10-20T06:00:02+0900"),
                         datetime(2026, 10, 19, 21, 0, 2, tzinfo=timezone.utc))

    def test_recommendations_cannot_pad_a_truncated_article(self):
        markup = "<main><p>Short truncated lead...</p>" + f'<a href="/other"><p>{BODY}</p></a><ul><li><p>{BODY}</p></li></ul></main>'
        self.assertIsNone(sources._extract(markup, CUTOFF))

    def test_rthk_body_container_excludes_surrounding_page(self):
        markup = f'<div>Navigation</div><div class="itemFullText">{BODY}<br/>Second paragraph.</div><p>Other news</p>'
        body = sources._extract(markup, CUTOFF, body_class="itemFullText")
        self.assertIn("Second paragraph.", body)
        self.assertNotIn("Navigation", body)
        self.assertNotIn("Other news", body)

    def test_https_domains_and_subdomains_only(self):
        self.assertEqual(sources._safe_url("https://a.example.com/x#part", ["example.com"]),
                         "https://a.example.com/x")
        for url in ["http://example.com", "https://example.com.evil.test",
                    "https://evil-example.com", "https://user@example.com",
                    "https://example.com:8080", "https://127.0.0.1"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                sources._safe_url(url, ["example.com"])

    def test_redirect_is_validated_before_request(self):
        handler = sources._SafeRedirect(["example.com"])
        with self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, "", {}, "https://evil.test/x")

    def test_dates_unknown_naive_future_old_are_excluded(self):
        for date in ["", "unknown", "2026-10-19T21:00:00",
                     "2026-10-20T00:00:01Z", "2026-10-18T11:59:59Z"]:
            with self.subTest(date=date):
                self.assertEqual(sources._feed_items(feed(published=date), CUTOFF), [])
        self.assertEqual(len(sources._feed_items(feed(published="2026-10-18T12:00:00Z"), CUTOFF)), 1)

    def test_modified_after_cutoff_excluded(self):
        xml = feed(extra="<dateModified>2026-10-20T00:01:00Z</dateModified>")
        self.assertEqual(sources._feed_items(xml, CUTOFF), [])
        markup = f'<main><p>{BODY}</p></main><meta itemprop="dateModified" content="2026-10-20T01:00:00Z">'
        self.assertIsNone(sources._extract(markup, CUTOFF))

    def test_article_paragraphs_not_navigation(self):
        markup = f"<nav><p>{BODY}</p></nav><article><p>{BODY}</p><aside><p>Advertisement</p></aside></article>"
        self.assertEqual(sources._extract(markup, CUTOFF), BODY.strip())
        self.assertIsNone(sources._extract(f"<div>{BODY}</div>", CUTOFF))

    def test_short_and_description_only_rejected(self):
        self.assertIsNone(sources._extract("<article><p>Short.</p></article>", CUTOFF))
        entry = sources._feed_items(feed(extra=f"<description>{BODY}</description>"), CUTOFF)[0]
        self.assertEqual(entry["content"], "")

    def test_content_encoded_and_limits(self):
        entry = sources._feed_items(feed(extra=f"<content:encoded><![CDATA[<p>{BODY}</p>]]></content:encoded>"), CUTOFF)[0]
        self.assertEqual(sources._extract(entry["content"], CUTOFF, fragment=True), BODY.strip())
        self.assertEqual(len(sources._extract(f"<main><p>{BODY * 10}</p></main>", CUTOFF)), 3000)

    def test_jsonld_paywall_or_modified_rejected(self):
        for metadata in [{"isAccessibleForFree": False},
                         {"@graph": [{"dateModified": "2026-10-20T00:01:00Z"}]}]:
            markup = f'<script type="application/ld+json">{json.dumps(metadata)}</script><article><p>{BODY}</p></article>'
            self.assertIsNone(sources._extract(markup, CUTOFF))

    def test_dtd_rejected(self):
        with self.assertRaises(ValueError):
            sources._feed_items('<!DOCTYPE rss><rss/>', CUTOFF)

    def test_late_rss_content_does_not_fall_back_to_page(self):
        config = {"id": "test", "publisher": "test", "region": "global",
                  "url": "https://example.com/rss", "allowed_domains": ["example.com"]}
        content = f'<p>{BODY}</p><meta itemprop="dateModified" content="2026-10-20T01:00:00Z">'
        xml = feed(extra=f"<content:encoded><![CDATA[{content}]]></content:encoded>")
        with patch.object(sources, "_fetch", return_value=(xml, config["url"])) as fetcher, self.assertLogs(sources.LOG):
            self.assertEqual(sources._collect_one(config, CUTOFF), [])
            fetcher.assert_called_once()

    def test_source_collection_and_coverage(self):
        configs = [{"id": region, "publisher": region, "region": region,
                    "url": f"https://{region}.example.com/rss", "allowed_domains": ["example.com"]}
                   for region in ["japan", "china", "global"]]

        def fetch(url, domains):
            if url.endswith("/rss"):
                return feed(url=url.replace("/rss", "/article")), url
            return f"<article><p>{BODY}</p></article>", url

        with patch.object(sources, "_load_config", return_value=configs), patch.object(sources, "_fetch", side_effect=fetch):
            items = sources.collect_sources(CUTOFF)
            self.assertEqual(len(items), 3)
            self.assertEqual(set(items[0]), {"id", "title", "url", "publisher", "published_at", "text", "region"})
        with patch.object(sources, "_load_config", return_value=configs[:2]), patch.object(sources, "_fetch", side_effect=fetch):
            with self.assertRaises(sources.SourceCollectionError):
                sources.collect_sources(CUTOFF)

    def test_failed_feed_preserves_other_sources_and_safe_log(self):
        config = {"id": "test", "publisher": "test", "region": "global",
                  "url": "https://example.com/rss?secret=should-not-log", "allowed_domains": ["example.com"]}
        with patch.object(sources, "_fetch", side_effect=OSError("sensitive contents")), self.assertLogs(sources.LOG) as captured:
            self.assertEqual(sources._collect_one(config, CUTOFF), [])
        self.assertIn("host=example.com", captured.output[0])
        self.assertNotIn("secret", captured.output[0])
        self.assertNotIn("sensitive", captured.output[0])


if __name__ == "__main__":
    unittest.main()
