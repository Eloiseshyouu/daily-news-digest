"""Collect public article bodies without search APIs, model calls, or credentials.

All timestamps must be timezone-aware. Missing/invalid publication times, later
modifications, short article bodies and paywalled pages fail closed. RSS title and
description are never substituted for an article body.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("source_config.json")
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 10
MAX_ITEMS_PER_SOURCE = 8
MAX_TEXT_CHARS = 3000
MIN_TEXT_CHARS = 400
MAX_CANDIDATES = 36
MAX_TOTAL_CHARS = 90000
LOG = logging.getLogger(__name__)
HARD_NEWS = re.compile(
    r"ukrain|russia|gaza|israel|iran|china|japan|taiwan|trump|tariff|sanction|"
    r"missile|ceasefire|earthquake|tsunami|flood|curfew|inflation|central bank|"
    r"\bgdp\b|interest rate|oil price|deadly|killed|"
    r"日本|中国|中國|中日|物価|物價|減税|消費税|円安|日銀|日元|"
    r"烏克蘭|乌克兰|俄羅斯|俄罗斯|ウクライナ|ロシア|"
    r"ガザ|加沙|伊朗|イラン|イスラエル|以色列|台湾|台灣|"
    r"北朝鮮|韓国|韓國|地震|津波|洪水|災害|宵禁|外出禁止|"
    r"襲擊|袭击|攻撃|爆炸|核武|關稅|关税|貿易|贸易|"
    r"人行|利率|經濟|经济|內需|内需|人民幣|人民币",
    re.IGNORECASE,
)
SOFT_NEWS_URL = re.compile(r"/(?:sport[s]?|football|culture|entertainment|lifeandstyle)/", re.I)


class SourceCollectionError(RuntimeError):
    """There is not enough verified, timely source material to write a digest."""


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value.strip())
        result = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            result = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(timezone.utc)


def _safe_url(url: str, domains: list[str]) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (ValueError, UnicodeError):
        raise ValueError("invalid_url") from None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not host
        or any(ord(char) <= 32 for char in url)
        or not any(host == domain or host.endswith("." + domain) for domain in domains)
    ):
        raise ValueError("disallowed_url")
    return urllib.parse.urlunsplit(parsed._replace(fragment=""))


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, domains: list[str]) -> None:
        self.domains = domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = _safe_url(newurl, self.domains)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str, domains: list[str]) -> tuple[str, str]:
    url = _safe_url(url, domains)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "daily-news-digest/2.0 (public RSS reader)",
            "Accept-Encoding": "identity",
        },
    )
    # No cookies, credentials, environment proxies, or paywall workarounds.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _SafeRedirect(domains)
    )
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        final_url = _safe_url(response.geturl(), domains)
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_BYTES:
            raise ValueError("oversized_response")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("unexpected_content_encoding")
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized_response")
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace"), final_url


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first(node: ET.Element, names: set[str]) -> str:
    for child in node:
        if _local(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def _json_metadata(value, modified: list[str], published: list[str]) -> bool:
    """Collect metadata only; never use hidden JSON article text as a body."""
    paid = False
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "dateModified" and isinstance(item, str):
                modified.append(item)
            elif key == "datePublished" and isinstance(item, str):
                published.append(item)
            elif key == "isAccessibleForFree" and (
                item is False or isinstance(item, str) and item.lower() == "false"
            ):
                paid = True
            if isinstance(item, (dict, list)):
                paid = _json_metadata(item, modified, published) or paid
    elif isinstance(value, list):
        for item in value:
            paid = _json_metadata(item, modified, published) or paid
    return paid


class _ArticleParser(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}
    _SKIP = {"script", "style", "nav", "footer", "header", "aside", "form",
             "noscript", "figure", "button", "ul", "ol"}

    def __init__(self, fragment: bool = False, body_class: str | None = None):
        super().__init__(convert_charrefs=True)
        self.fragment = fragment
        self.stack: list[tuple[str, bool, bool]] = []
        self.paragraph: list[str] | None = None
        self.paragraphs: list[str] = []
        self.modified: list[str] = []
        self.published: list[str] = []
        self.paywall = False
        self.json_parts: list[str] | None = None
        self.fragment_parts: list[str] = []
        self.body_class = body_class
        self.body_depth: int | None = None
        self.scoped_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        inherited_scope = self.stack[-1][1] if self.stack else self.fragment
        inherited_skip = self.stack[-1][2] if self.stack else False
        scope = inherited_scope or tag in {"article", "main"} or data.get("itemprop") == "articleBody"
        skip = inherited_skip or tag in self._SKIP or data.get("aria-hidden") == "true" or "hidden" in data
        skip = skip or data.get("role") in {"dialog", "navigation"}
        if self.body_class and self.body_class in data.get("class", "").split():
            self.body_depth = len(self.stack) + 1
        key = (data.get("property") or data.get("name") or data.get("itemprop") or "").lower()
        if key in {"article:modified_time", "og:updated_time", "datemodified", "date.modified", "last-modified"}:
            value = data.get("content") or data.get("datetime")
            if value:
                self.modified.append(value)
        if key in {"article:published_time", "datepublished", "date.published"}:
            value = data.get("content") or data.get("datetime")
            if value:
                self.published.append(value)
        if key == "isaccessibleforfree" and data.get("content", "").lower() == "false":
            self.paywall = True
        if tag == "script" and data.get("type", "").lower() == "application/ld+json":
            self.json_parts = []
        if tag == "p" and scope and not skip and not any(item[0] == "a" for item in self.stack):
            if self.paragraph:
                self._finish_paragraph()
            self.paragraph = []
        if tag == "br" and self.paragraph is not None:
            self.paragraph.append(" ")
        if tag == "br" and self.body_depth is not None:
            self.scoped_parts.append("\n")
        if tag not in self._VOID:
            self.stack.append((tag, scope, skip))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def _finish_paragraph(self):
        if self.paragraph is not None:
            value = _compact("".join(self.paragraph))
            if value:
                self.paragraphs.append(value)
            self.paragraph = None

    def handle_endtag(self, tag):
        if tag == "p":
            self._finish_paragraph()
        if tag == "script" and self.json_parts is not None:
            try:
                self.paywall = _json_metadata(
                    json.loads("".join(self.json_parts)), self.modified, self.published
                ) or self.paywall
            except (ValueError, RecursionError):
                pass
            self.json_parts = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if self.body_depth is not None and index < self.body_depth:
                    self.body_depth = None
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.json_parts is not None:
            self.json_parts.append(data)
        skip = self.stack[-1][2] if self.stack else False
        if not skip and self.paragraph is not None:
            self.paragraph.append(data)
        if self.fragment and not skip:
            self.fragment_parts.append(data)
        if self.body_depth is not None and not skip:
            self.scoped_parts.append(data)

    def body(self) -> str:
        self._finish_paragraph()
        if self.body_class:
            return "\n\n".join(_compact(part) for part in "".join(self.scoped_parts).splitlines() if _compact(part))
        body = "\n\n".join(dict.fromkeys(self.paragraphs))
        # Full content:encoded may contain plain text or <div>/<br> instead of p.
        if self.fragment and not body:
            body = _compact(" ".join(self.fragment_parts))
        return body


def _eligible_metadata(parser: _ArticleParser, cutoff: datetime) -> bool:
    if parser.paywall:
        return False
    for value in parser.modified + parser.published:
        timestamp = _date(value)
        if timestamp is None or timestamp > cutoff:
            return False
    return True


def _extract(markup: str, cutoff: datetime, *, fragment: bool = False, body_class: str | None = None) -> str | None:
    parser = _ArticleParser(fragment=fragment, body_class=body_class)
    parser.feed(markup)
    parser.close()
    if not _eligible_metadata(parser, cutoff):
        return None
    body = parser.body()
    paywall_phrases = (
        "subscribe to continue reading", "subscribe to read the full story",
        "this article is for subscribers", "訂閱後即可閱讀全文", "订阅后即可阅读全文",
    )
    if any(phrase in body.lower() for phrase in paywall_phrases):
        return None
    # CJK characters carry substantially more information than a Latin letter.
    # This is only a noise floor; the reviewer still rejects insufficient evidence.
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff]", body))
    if len(body) < MIN_TEXT_CHARS and not (len(body) >= 220 and cjk >= 180):
        return None
    return body[:MAX_TEXT_CHARS]


def _feed_items(xml_text: str, cutoff: datetime, limit: int = MAX_ITEMS_PER_SOURCE) -> list[dict]:
    if "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
        raise ValueError("xml_entities_not_allowed")
    root = ET.fromstring(xml_text)
    result = []
    floor = cutoff - timedelta(hours=36)
    for node in root.iter():
        if _local(node.tag) not in {"item", "entry"}:
            continue
        published = _date(_first(node, {"pubdate", "published", "date"}))
        if published is None or not floor <= published <= cutoff:
            continue
        modified_values = [
            "".join(child.itertext()).strip()
            for child in node
            if _local(child.tag) in {"updated", "modified", "datemodified"}
        ]
        if any(_date(value) is None or _date(value) > cutoff for value in modified_values):
            continue
        url = _first(node, {"link"})
        if not url:
            for child in node:
                if _local(child.tag) == "link" and child.get("rel", "alternate") == "alternate":
                    url = child.get("href", "")
                    break
        title = _compact(_first(node, {"title"}))
        # Only full content fields, never the RSS description or Atom summary.
        full_content = _first(node, {"encoded", "content"})
        if title and url:
            result.append({"title": title, "url": url, "published": published, "content": full_content})
    result = [item for item in result if not SOFT_NEWS_URL.search(item["url"])]
    result.sort(key=lambda item: (min(len(HARD_NEWS.findall(item["title"])), 3), item["published"]), reverse=True)
    return result[:limit]


def _log_failure(url: str, stage: str, error: Exception | None = None) -> None:
    # URLs, query values, response bodies and exception strings may be sensitive.
    host = urllib.parse.urlsplit(url).hostname or "invalid-host"
    LOG.warning("source_skipped host=%s stage=%s error=%s",
                host, stage, type(error).__name__ if error else "rejected")


def _collect_one(source: dict, cutoff: datetime) -> list[dict]:
    domains = source["allowed_domains"]
    try:
        xml_text, _ = _fetch(source["url"], domains)
        entries = _feed_items(xml_text, cutoff, source.get("max_items", MAX_ITEMS_PER_SOURCE))
    except (OSError, ValueError, ET.ParseError, LookupError) as error:
        _log_failure(source["url"], "feed", error)
        return []
    collected = []
    for entry in entries:
        try:
            url = _safe_url(entry["url"], domains)
            if entry["content"]:
                content_parser = _ArticleParser(fragment=True)
                content_parser.feed(entry["content"])
                content_parser.close()
                if not _eligible_metadata(content_parser, cutoff):
                    _log_failure(url, "rss_content_metadata")
                    continue
            body = _extract(entry["content"], cutoff, fragment=True) if entry["content"] else None
            if not body:
                article_html, url = _fetch(url, domains)
                body = _extract(article_html, cutoff, body_class=source.get("body_class"))
            if not body:
                _log_failure(url, "article_body_or_cutoff")
                continue
            collected.append({
                "id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:20],
                "title": entry["title"],
                "url": url,
                "publisher": source["publisher"],
                "published_at": entry["published"].isoformat(),
                "text": body,
                "region": source["region"],
            })
        except (OSError, ValueError, LookupError) as error:
            _log_failure(entry["url"], "article", error)
    return collected


def _load_config() -> list[dict]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    sources = [source for source in config["sources"] if source.get("enabled", True)]
    if not isinstance(sources, list) or not sources:
        raise SourceCollectionError("No configured news sources.")
    for source in sources:
        if source.get("region") not in {"japan", "china", "global"}:
            raise SourceCollectionError("Invalid source region.")
        domains = source.get("allowed_domains")
        if not isinstance(domains, list) or not domains or not all(
            isinstance(domain, str) and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain)
            and "." in domain for domain in domains
        ):
            raise SourceCollectionError("Invalid source domain allowlist.")
        if not source.get("publisher") or not source.get("id"):
            raise SourceCollectionError("Source publisher/id is missing.")
        if not isinstance(source.get("max_items", MAX_ITEMS_PER_SOURCE), int) or not 1 <= source.get("max_items", MAX_ITEMS_PER_SOURCE) <= 12:
            raise SourceCollectionError("Invalid source item limit.")
        _safe_url(source["url"], domains)
    return sources


def collect_sources(cutoff: datetime) -> list[dict]:
    """Return <=30 timely article bodies, or raise if coverage is insufficient.

    At most four feed workers operate at a time. Each worker fetches at most six
    articles sequentially. Feeds share a 36-hour cutoff window, then a round-robin
    merge keeps smaller Japan/China feeds represented alongside global feeds.
    """
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be a timezone-aware datetime")
    cutoff = cutoff.astimezone(timezone.utc)
    sources = _load_config()
    with ThreadPoolExecutor(max_workers=4) as executor:
        groups = list(executor.map(lambda source: _collect_one(source, cutoff), sources))
    queues = [deque(group) for group in groups if group]
    selected, seen, total_chars = [], set(), 0
    while queues and len(selected) < MAX_CANDIDATES:
        next_queues = []
        for queue in queues:
            item = queue.popleft()
            if item["url"] not in seen and total_chars + len(item["text"]) <= MAX_TOTAL_CHARS:
                selected.append(item)
                seen.add(item["url"])
                total_chars += len(item["text"])
            if queue:
                next_queues.append(queue)
            if len(selected) == MAX_CANDIDATES:
                break
        queues = next_queues
    regions = {item["region"] for item in selected}
    publishers = {item["publisher"] for item in selected}
    if not {"japan", "china", "global"} <= regions or len(publishers) < 3:
        raise SourceCollectionError(
            "Insufficient public article coverage: need Japan, China, global "
            "and at least three publishers; no digest may be produced."
        )
    return selected
