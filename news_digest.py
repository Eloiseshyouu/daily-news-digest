
import email.message
import html
import json
import os
import re
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
from zoneinfo import ZoneInfo


MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "8"))
MAX_DIGEST_ITEMS = int(os.getenv("MAX_DIGEST_ITEMS", "6"))
MAX_SUMMARY_CHARS = int(os.getenv("MAX_SUMMARY_CHARS", "240"))
TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
DISPLAY_TIMEZONE = ZoneInfo(os.getenv("DIGEST_TIMEZONE", "Asia/Tokyo"))

CATEGORY_ORDER = [
    "日本",
    "中国与中日关系",
    "国际与大国关系",
    "战争与冲突",
    "经济与市场",
    "今日观察",
    "其他",
]
CATEGORY_LABELS = {
    "japan": "日本",
    "china": "中国与中日关系",
    "global": "国际与大国关系",
    "official": "今日观察",
}
SOURCE_LABELS = {
    "Reuters Japan": "Reuters / 日本线索",
    "Japan Life and Policy": "日本生活与政策线索",
    "China Economy and Diplomacy": "中国经济与外交线索",
    "Global Geopolitics": "全球地缘政治线索",
    "Global Economy and Markets": "全球经济市场线索",
    "Official Institutions": "官方机构线索",
}
KEY_TERMS = {
    "日本": "物价 通胀 日元 汇率 留学 签证 在留 治安 地震 中日 Japan yen visa student",
    "中国与中日关系": "中国 经济 外交 对日 海外中国人 China Chinese economy Japan",
    "国际与大国关系": "美国 欧洲 中东 俄乌 亚太 安全 oil market Ukraine Middle East Asia-Pacific",
    "战争与冲突": "战争 冲突 袭击 导弹 无人机 停火 军演 Ukraine Russia Gaza Iran missile drone war",
    "经济与市场": "经济 市场 通胀 利率 央行 日元 美元 油价 关税 出口 inflation market yen dollar oil tariff",
    "今日观察": "政府 央行 外务省 外交部 IMF World Bank Bank of Japan Ministry",
}
AUTHORITY_TERMS = [
    "reuters",
    "associated press",
    " ap ",
    "afp",
    "bbc",
    "financial times",
    "wall street journal",
    "bloomberg",
    "nikkei",
    "nhk",
    "kyodo",
    "asahi",
    "japan times",
    "caixin",
    "scmp",
    "al jazeera",
]


@dataclass
class NewsItem:
    source: str
    category: str
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
    request = urllib.request.Request(url, headers={"User-Agent": "daily-news-digest/1.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def first_text(element: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        child = element.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def strip_html(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def compact_text(value: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    text = re.sub(r"\s+", " ", strip_html(value)).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def parse_rss(source: str, category: str, xml_text: str) -> list[NewsItem]:
    root = ET.fromstring(xml_text)
    items: list[NewsItem] = []
    if root.tag.endswith("rss"):
        for node in root.findall("./channel/item")[:MAX_ITEMS_PER_FEED]:
            items.append(
                NewsItem(
                    source=source,
                    category=category,
                    title=first_text(node, ["title"]),
                    link=first_text(node, ["link"]),
                    published=first_text(node, ["pubDate"]),
                    summary=strip_html(first_text(node, ["description"])),
                )
            )
        return items

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for node in root.findall("./atom:entry", ns)[:MAX_ITEMS_PER_FEED]:
        link_node = node.find("atom:link", ns)
        items.append(
            NewsItem(
                source=source,
                category=category,
                title=first_text(node, ["{http://www.w3.org/2005/Atom}title"]),
                link=link_node.attrib.get("href", "") if link_node is not None else "",
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


def category_label(value: str) -> str:
    return CATEGORY_LABELS.get(value, value or "其他")


def display_category(item: NewsItem) -> str:
    base = category_label(item.category)
    haystack = f"{item.title} {item.summary}".lower()
    if base == "国际与大国关系":
        if any(term.lower() in haystack for term in KEY_TERMS["战争与冲突"].split()):
            return "战争与冲突"
        if any(term.lower() in haystack for term in KEY_TERMS["经济与市场"].split()):
            return "经济与市场"
    return base


def source_label(value: str) -> str:
    return SOURCE_LABELS.get(value, value)


def collect_news(feeds: list[dict[str, str]]) -> tuple[list[NewsItem], list[str]]:
    items: list[NewsItem] = []
    errors: list[str] = []
    for feed in feeds:
        name = feed.get("name", "").strip()
        url = feed.get("url", "").strip()
        category = feed.get("category", "other").strip()
        if not name or not url:
            errors.append(f"Skipped invalid feed entry: {feed!r}")
            continue
        try:
            items.extend(parse_rss(name, category, fetch_text(url)))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return items, errors


def item_key(item: NewsItem) -> str:
    return re.sub(r"\W+", "", item.title.lower())


def relevance_score(item: NewsItem) -> int:
    label = display_category(item)
    haystack = f" {item.title} {item.summary} {item.source} ".lower()
    score = sum(3 for term in KEY_TERMS.get(label, "").split() if term.lower() in haystack)
    score += {"日本": 4, "中国与中日关系": 3, "今日观察": 1}.get(label, 2)
    if any(term in haystack for term in AUTHORITY_TERMS):
        score += 2
    return score


def select_digest_items(items: list[NewsItem], limit: int = MAX_DIGEST_ITEMS) -> list[NewsItem]:
    seen: set[str] = set()
    unique_items: list[NewsItem] = []
    for item in items:
        key = item_key(item)
        if key and key not in seen:
            seen.add(key)
            unique_items.append(item)

    selected: list[NewsItem] = []
    for category in CATEGORY_ORDER:
        category_items = [item for item in unique_items if display_category(item) == category]
        if category_items:
            selected.append(max(category_items, key=relevance_score))

    remaining = [item for item in unique_items if item not in selected]
    remaining.sort(key=relevance_score, reverse=True)
    selected.extend(remaining)
    return selected[:limit]


def group_items(items: list[NewsItem]) -> dict[str, list[NewsItem]]:
    grouped: dict[str, list[NewsItem]] = {}
    for item in items:
        grouped.setdefault(display_category(item), []).append(item)
    return grouped


def digest_title() -> str:
    today = datetime.now(DISPLAY_TIMEZONE).strftime("%Y年%m月%d日")
    return f"每日重点新闻简报｜{today}｜日本时间9:00"


def short_explain(item: NewsItem) -> str:
    return compact_text(item.summary, 90) or "这是一条需要关注的最新新闻，建议结合原文继续确认细节。"


def why_it_matters(item: NewsItem) -> str:
    label = display_category(item)
    reasons = {
        "日本": "它可能影响日本生活成本、日元走势、政策环境、出行安全或中日关系，对在日本留学和日常预算有直接参考价值。",
        "中国与中日关系": "它可能影响中国经济、外交关系、中日互动和海外中国人的处境，也会影响未来就业、签证氛围和跨境流动判断。",
        "国际与大国关系": "它可能影响能源、通胀、汇率、供应链和国际安全，日本和中国都会被间接牵动。",
        "战争与冲突": "战争和安全局势会影响能源、航运、旅行风险和地区外交气氛，日本生活成本与安全判断也可能被牵动。",
        "经济与市场": "它可能影响日元、人民币、物价、机票、能源和就业市场，对留学预算和换汇时点有现实参考。",
        "今日观察": "官方机构的表态常常会影响政策预期、市场反应和后续外交走向，需要和媒体报道分开看。",
    }
    return reasons.get(label, "它可能改变当天的信息背景，值得放进整体判断里。")


def detailed_explain(item: NewsItem) -> str:
    label = display_category(item)
    context = {
        "日本": "接下来重点看日本政府、央行、地方机构或主要媒体是否有后续说明，以及它是否外溢到生活成本、日元和社会安全。",
        "中国与中日关系": "接下来重点看官方回应、市场反应、其他国家是否跟进，以及这件事是否影响中日关系或海外中国人处境。",
        "国际与大国关系": "接下来重点看各方是否升级行动、能源和金融市场是否明显反应，以及日本和中国是否出现政策或舆论连锁影响。",
        "战争与冲突": "接下来重点看冲突是否扩大、航运和能源价格是否反应，以及日本政府是否发布旅行、安全或外交提醒。",
        "经济与市场": "接下来重点看央行和市场是否重新定价，以及日元、人民币、油价和日本生活成本是否跟着变化。",
        "今日观察": "接下来重点看政策是否落地、市场是否重新定价，以及媒体报道是否能从不同地区互相印证。",
    }.get(label, "接下来重点看是否有更多权威来源确认，以及这件事是否进入更大的政治、经济或社会趋势。")
    summary = compact_text(item.summary, 260)
    return f"{summary} {context}" if summary else context


def build_plain_body(subject: str, items: list[NewsItem], errors: list[str]) -> str:
    lines = [
        subject,
        "",
        "注：本简报按“日本时间9:00口径”整理已发布并可交叉核对的信息；若9:00前后出现重大突发，可后续补更。",
        "",
        "今天先看这3件事",
    ]
    lines.extend(f"- {item.title}" for item in items[:3])
    if not items:
        lines.extend(["- 今天暂时没有抓取到足够新闻。", "", "今天暂时没有抓取到新闻。"])

    grouped = group_items(items)
    for category in CATEGORY_ORDER:
        category_items = grouped.get(category, [])
        if not category_items:
            continue
        lines.extend(["", f"## {category}"])
        for index, item in enumerate(category_items, 1):
            lines.extend(
                [
                    f"{index}. {item.title}",
                    f"   短版：{short_explain(item)}",
                    f"   为什么重要：{why_it_matters(item)}",
                    f"   详细版：{detailed_explain(item)}",
                    f"   来源：{source_label(item.source)}",
                ]
            )
            if item.published:
                lines.append(f"   时间：{item.published}")
            if item.link:
                lines.append(f"   原文：{item.link}")

    if errors:
        lines.extend(["", "## 抓取提醒"])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines).strip() + "\n"


def build_html_body(subject: str, items: list[NewsItem], errors: list[str]) -> str:
    def link_for(item: NewsItem) -> str:
        source = html.escape(source_label(item.source))
        if item.link:
            return f'<a href="{html.escape(item.link)}">来源：{source}</a>'
        return f"<span>来源：{source}</span>"

    sections = []
    grouped = group_items(items)
    for category in CATEGORY_ORDER:
        category_items = grouped.get(category, [])
        if not category_items:
            continue
        cards = []
        for index, item in enumerate(category_items, 1):
            cards.append(
                f"""
                <article class="item">
                  <div class="source">{index:02d} · {html.escape(source_label(item.source))}</div>
                  <h3>{html.escape(item.title)}</h3>
                  <p><b>一句话短版：</b>{html.escape(short_explain(item))}</p>
                  <p><b>为什么重要：</b>{html.escape(why_it_matters(item))}</p>
                  <p><b>详细版：</b>{html.escape(detailed_explain(item))}</p>
                  <p class="meta">{html.escape(item.published) if item.published else ""} {link_for(item)}</p>
                </article>
                """
            )
        sections.append(f'<section><h2>{html.escape(category)}</h2>{"".join(cards)}</section>')

    warnings = ""
    if errors:
        warnings = "<section><h2>抓取提醒</h2><ul>" + "".join(
            f"<li>{html.escape(error)}</li>" for error in errors
        ) + "</ul></section>"

    top3 = "".join(f"<li>{html.escape(item.title)}</li>" for item in items[:3])
    if not top3:
        top3 = "<li>今天暂时没有抓取到足够新闻。</li>"

    today = datetime.now(DISPLAY_TIMEZONE).strftime("%Y年%m月%d日")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <style>
    body{{margin:0;background:#f6f7f9;color:#1f2937;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif;line-height:1.6}}
    main{{max-width:760px;margin:0 auto;padding:28px 18px 40px}}
    header{{background:#111827;color:white;padding:26px;border-radius:8px}}
    h1{{margin:0;font-size:25px;line-height:1.3}} h2{{font-size:18px;margin:0 0 12px;color:#111827}} h3{{font-size:17px;margin:0 0 10px;color:#111827}}
    .intro{{color:#d1d5db;margin:12px 0 0}} .pill{{display:inline-block;margin-top:14px;padding:5px 10px;border-radius:6px;background:rgba(255,255,255,.12);font-size:13px}}
    section{{background:white;border:1px solid #e5e7eb;border-radius:8px;margin-top:18px;padding:18px 20px}}
    .top3{{border-color:#d1fae5}} .top3 h2{{color:#064e3b}} .top3 li{{font-weight:650;margin:6px 0}}
    .focus li{{font-size:14px;margin:4px 0}} .item{{border-top:1px solid #eef0f3;padding:15px 0}} .item:first-of-type{{border-top:0}}
    .source,.meta{{color:#6b7280;font-size:13px}} p{{margin:8px 0}} b,a{{color:#0f766e}} a{{font-weight:700;text-decoration:none}}
    footer{{text-align:center;color:#6b7280;font-size:12px;margin-top:18px}}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{html.escape(subject)}</h1>
      <p class="intro">早上好，这是今天的每日重点新闻简报。按“在日中国留学生”的视角筛选，让你用最少时间抓住真正重要的变化。</p>
      <span class="pill">{html.escape(today)} · 共 {len(items)} 条</span>
    </header>
    <section class="top3"><h2>今天先看这3件事</h2><ol>{top3}</ol></section>
    <section class="focus"><h2>今日筛选口径</h2><ul>
      <li>日本：物价、日元、留学生活、政策、社会安全、中日关系</li>
      <li>中国：中国经济、外交、对日关系、海外中国人可能受影响的事</li>
      <li>全球大势：美国、欧洲、中东、俄乌、亚太安全、全球经济市场</li>
    </ul></section>
    {"".join(sections) if sections else "<section>今天暂时没有抓取到新闻。</section>"}
    {warnings}
    <footer>本邮件由 GitHub Actions 自动发送。来源以多地区权威媒体和官方机构公开信息为线索，保留原文链接供你继续判断。</footer>
  </main>
</body>
</html>"""


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def build_email(items: list[NewsItem], errors: list[str]) -> email.message.EmailMessage:
    subject = digest_title()
    message = email.message.EmailMessage()
    message["Subject"] = subject
    message["From"] = required_env("MAIL_FROM")
    message["To"] = required_env("MAIL_TO")
    message["Date"] = formatdate(localtime=True)
    message.set_content(build_plain_body(subject, items, errors))
    message.add_alternative(build_html_body(subject, items, errors), subtype="html")
    return message


def send_email(message: email.message.EmailMessage) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP(required_env("SMTP_HOST"), int(required_env("SMTP_PORT")), timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(required_env("SMTP_USER"), required_env("SMTP_PASSWORD"))
        server.send_message(message)


def preview_content(message: email.message.EmailMessage) -> str:
    for part in message.walk():
        if part.get_content_type() == "text/plain":
            return part.get_content()
    return message.as_string()


def main() -> int:
    items, errors = collect_news(load_feeds(Path("feeds.json")))
    items = select_digest_items(items)
    message = build_email(items, errors)
    if os.getenv("DRY_RUN") == "1":
        print(preview_content(message))
        return 0
    send_email(message)
    print(f"Sent {len(items)} news items to {message['To']}")
    if errors:
        print(f"Feed errors: {len(errors)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
