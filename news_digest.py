import email.message
import html
import json
import os
import smtplib
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from typing import Iterable


MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "5"))
TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))


@dataclass
class NewsItem:
    source: str
    title: str
    link: str
    published: str
    summary: str


def load_feeds(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8") as file:
        feeds = json.load(file)
    if not isinstance(feeds, list):
        raise ValueError("feeds.json must contain a list")
    return feeds


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "daily-news-digest/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def first_text(element: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        child = element.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def parse_rss(source: str, xml_text: str) -> list[NewsItem]:
    root = ET.fromstring(xml_text)
    items: list[NewsItem] = []

    if root.tag.endswith("rss"):
        nodes = root.findall("./channel/item")
        for node in nodes[:MAX_ITEMS_PER_FEED]:
            items.append(
                NewsItem(
                    source=source,
                    title=first_text(node, ["title"]),
                    link=first_text(node, ["link"]),
                    published=first_text(node, ["pubDate"]),
                    summary=strip_html(first_text(node, ["description"])),
                )
            )
        return items

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    nodes = root.findall("./atom:entry", ns)
    for node in nodes[:MAX_ITEMS_PER_FEED]:
        link_node = node.find("atom:link", ns)
        link = link_node.attrib.get("href", "") if link_node is not None else ""
        items.append(
            NewsItem(
                source=source,
                title=first_text(node, ["{http://www.w3.org/2005/Atom}title"]),
                link=link,
                published=first_text(
                    node,
                    [
                        "{http://www.w3.org/2005/Atom}published",
                        "{http://www.w3.org/2005/Atom}updated",
                    ],
                ),
                summary=strip_html(
                    first_text(
                        node,
                        [
                            "{http://www.w3.org/2005/Atom}summary",
                            "{http://www.w3.org/2005/Atom}content",
                        ],
                    )
                ),
            )
        )
    return items


def strip_html(value: str) -> str:
    text = html.unescape(value)
    output = []
    in_tag = False
    for char in text:
        if char == "<":
            in_tag = True
            continue
        if char == ">":
            in_tag = False
            continue
        if not in_tag:
            output.append(char)
    return " ".join("".join(output).split())


def collect_news(feeds: list[dict[str, str]]) -> tuple[list[NewsItem], list[str]]:
    items: list[NewsItem] = []
    errors: list[str] = []
    for feed in feeds:
        name = feed.get("name", "").strip()
        url = feed.get("url", "").strip()
        if not name or not url:
            errors.append(f"Skipped invalid feed entry: {feed!r}")
            continue
        try:
            items.extend(parse_rss(name, fetch_text(url)))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return items, errors


def build_email(items: list[NewsItem], errors: list[str]) -> email.message.EmailMessage:
    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"Daily News Digest - {today}"

    lines = [subject, ""]
    if not items:
        lines.append("No news items were collected.")
    current_source = None
    for item in items:
        if item.source != current_source:
            current_source = item.source
            lines.extend(["", f"## {current_source}"])
        lines.append(f"- {item.title}")
        if item.published:
            lines.append(f"  Published: {item.published}")
        if item.summary:
            lines.append(f"  {item.summary[:280]}")
        if item.link:
            lines.append(f"  {item.link}")

    if errors:
        lines.extend(["", "## Feed Errors"])
        lines.extend(f"- {error}" for error in errors)

    body = "\n".join(lines).strip() + "\n"

    message = email.message.EmailMessage()
    message["Subject"] = subject
    message["From"] = required_env("MAIL_FROM")
    message["To"] = required_env("MAIL_TO")
    message["Date"] = formatdate(localtime=True)
    message.set_content(body)
    return message


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def send_email(message: email.message.EmailMessage) -> None:
    host = required_env("SMTP_HOST")
    port = int(required_env("SMTP_PORT"))
    user = required_env("SMTP_USER")
    password = required_env("SMTP_PASSWORD")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(user, password)
        server.send_message(message)


def main() -> int:
    feeds = load_feeds(Path("feeds.json"))
    items, errors = collect_news(feeds)
    message = build_email(items, errors)

    if os.getenv("DRY_RUN") == "1":
        print(message.get_content())
        return 0

    send_email(message)
    print(f"Sent {len(items)} news items to {message['To']}")
    if errors:
        print(f"Feed errors: {len(errors)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

