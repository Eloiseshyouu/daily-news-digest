"""Candidate v2 pipeline. Fail closed; only the prepare command can use AI.

The deployed legacy workflow is deliberately not changed by this module.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
from pathlib import Path
import smtplib
import ssl
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
OUT = Path("preview")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_hash(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少配置：{name}")
    return value


def recipient() -> str:
    value = required("MAIL_TO")
    # One recipient only: a partial SMTP delivery must not trigger retries.
    if parseaddr(value)[1] != value or value.count("@") != 1 or any(
        c in value for c in "\r\n,; "
    ):
        raise RuntimeError("MAIL_TO必须是单个邮箱地址")
    return value.lower()


def context(now: datetime | None = None) -> dict:
    now = (now or datetime.now(JST)).astimezone(JST)
    cutoff = now.replace(hour=9, minute=0, second=0, microsecond=0)
    return {"date": now.date().isoformat(), "cutoff": cutoff.isoformat()}


def delivery_window(day: str, now: datetime | None = None) -> None:
    now = (now or datetime.now(JST)).astimezone(JST)
    start = datetime.fromisoformat(day).replace(tzinfo=JST, hour=9)
    if not start <= now <= start + timedelta(minutes=30):
        raise RuntimeError("不在当日09:00—09:30发送窗口内，不补发过时邮件")


def collection_deadline(cutoff: str, captured_at: datetime | None = None) -> None:
    captured_at = captured_at or datetime.now(JST)
    limit = datetime.fromisoformat(cutoff)
    if captured_at.tzinfo is None or limit.tzinfo is None or captured_at >= limit:
        raise RuntimeError("资料未能在09:00前采集完成，停止本日发送")


def wait_until(hour: int, minute: int) -> None:
    now = datetime.now(JST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while (seconds := (target - datetime.now(JST)).total_seconds()) > 0:
        time.sleep(min(seconds, 30))


def artifact_name(kind: str, day: str) -> str:
    return f"daily-news-{kind}-{day}"


def github_json(path: str) -> dict:
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": "Bearer " + required("GITHUB_TOKEN"),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def artifacts() -> list[dict]:
    repo = required("GITHUB_REPOSITORY")
    if len(repo.split("/")) != 2 or not all(
        part and all(c.isalnum() or c in "-_." for c in part)
        for part in repo.split("/")
    ):
        raise RuntimeError("仓库名称无效")
    found = []
    # If the safety ledger cannot be fully inspected, do not assume nothing sent.
    for page in range(1, 11):
        result = github_json(f"/repos/{repo}/actions/artifacts?per_page=100&page={page}")
        batch = result["artifacts"]
        found.extend(batch)
        if len(batch) < 100:
            return found
    raise RuntimeError("发送记录过多，无法完整核对；停止发送")


def no_previous_claim(records: list[dict], day: str) -> None:
    if any(r["name"] == artifact_name("claim", day) for r in records):
        raise RuntimeError("当天已有发送占位记录；可能已发送或结果未知，禁止自动重发")


class SafeArtifactRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # GitHub redirects downloads to signed storage URLs; never forward its token.
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            if not newurl.startswith("https://"):
                raise RuntimeError("拒绝非HTTPS制品下载")
            redirected.remove_header("Authorization")
        return redirected


def artifact_file(record: dict, filename: str) -> dict:
    repo = required("GITHUB_REPOSITORY")
    url = f"https://api.github.com/repos/{repo}/actions/artifacts/{int(record['id'])}/zip"
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + required("GITHUB_TOKEN")}
    )
    opener = urllib.request.build_opener(SafeArtifactRedirect())
    with opener.open(request, timeout=20) as response:
        blob = response.read(2_000_001)
    if len(blob) > 2_000_000:
        raise RuntimeError("发送记录制品超过大小限制")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        info = archive.getinfo(filename)
        if info.file_size > 1_000_000:
            raise RuntimeError("发送记录文件超过大小限制")
        return json.loads(archive.read(info))


def recent_history(records: list[dict], day: str) -> list[dict]:
    selected = sorted(
        (
            r for r in records
            if r["name"].startswith("daily-news-sent-")
            and r["name"] < artifact_name("sent", day)
        ),
        key=lambda r: r["name"],
        reverse=True,
    )[:5]
    # Unreadable history is an error, not permission to send yesterday's news again.
    return [artifact_file(r, "digest.json")["draft"] for r in selected]


def verify_review(bundle: dict) -> None:
    from news_editor import verify_reviewed_digest
    if not verify_reviewed_digest(bundle):
        raise RuntimeError("审核未通过、检查不完整，或终稿已发生改变")


def render(bundle: dict) -> tuple[str, str, str]:
    verify_review(bundle)
    draft = bundle["draft"]
    sources = {item["id"]: item for item in bundle["sources"]}
    preview = bundle.get("edition_type") == "preview"
    stamp_cutoff = datetime.fromisoformat(draft["cutoff"]).astimezone(JST).strftime("%H:%M")
    title = f"每日重点新闻简报｜{draft['date']}｜日本时间" + (stamp_cutoff + "样稿" if preview else "9:00")
    if bundle.get("delivery_mode") == "test":
        title = "【测试邮件】" + title
    plain = [title, "今天先看这3件事"]
    top = []
    for item in draft["top3"]:
        plain.append("• " + item["text"])
        top.append("<li>" + html.escape(item["text"]) + "</li>")
    cards = []
    section = None
    for item in draft["items"]:
        if item["section"] != section:
            section = item["section"]
            plain.extend(["", section])
            cards.append("<h2>" + html.escape(section) + "</h2>")
        plain.extend(["", item["title"], "一句话短版：" + item["short"], "详细版：" + item["detail"], "来源："])
        links = []
        for source_id in item["source_ids"]:
            source = sources[source_id]
            if not source["url"].startswith("https://"):
                raise RuntimeError("来源链接不安全")
            label = source["publisher"]
            plain.append(label + "：" + source["url"])
            links.append(
                f'<a href="{html.escape(source["url"], quote=True)}">'
                f'{html.escape(label)}</a>'
            )
        plain.append("为什么重要：" + item["why"])
        cards.append(
            '<article><h3>' + html.escape(item["title"]) + '</h3><p class="short">'
            + '一句话短版：' + html.escape(item["short"])
            + '</p><p><b>详细版：</b>' + html.escape(item["detail"])
            + '</p><p class="sources">来源：' + " · ".join(links)
            + '</p><p><b>为什么重要：</b>' + html.escape(item["why"]) + "</p></article>"
        )
    stamp = datetime.fromisoformat(bundle["collected_at"]).astimezone(JST).strftime("%H:%M")
    note = f"资料采集于日本时间{stamp}；" + (
        f"这是测试样稿，选材截点为{stamp_cutoff}，并非上午9点版。" if preview
        else "不纳入09:00以后公开的消息。"
    ) + "AI编辑并二次审稿，仍可能有遗漏。"
    plain.extend(["", note])
    page = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<style>body{margin:0;background:#f5f6f7;color:#202a36;font-family:'
        '"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.75}'
        'main{max-width:720px;margin:auto;padding:24px 18px;background:white}'
        'h1{font-size:23px}h2{font-size:18px;color:#0b665d;margin-top:28px}'
        'h3{font-size:18px;margin-bottom:6px}article{border-top:1px solid #ddd;padding:14px 0}'
        'p{margin:10px 0}.short{font-weight:600}a{color:#006d77}'
        '.sources,footer{font-size:13px;color:#667085}li{margin:8px 0}</style>'
        '<main><h1>' + html.escape(title) + '</h1><h2>今天先看这3件事</h2><ol>'
        + "".join(top) + "</ol>" + "".join(cards) + "<footer>" + html.escape(note)
        + "</footer></main></html>"
    )
    return title, "\n".join(plain), page


def prepare(dry_run: bool) -> None:
    # Check free-only configuration before fetching articles or making any model request.
    if os.getenv("GEMINI_FREE_TIER_CONFIRMED") != "1":
        raise RuntimeError("尚未确认Gemini项目未关联付费账单，禁止模型调用")
    required("GEMINI_API_KEY")
    from news_editor import edit_digest
    from news_sources import collect_sources

    if not dry_run:
        collection_deadline(context()["cutoff"])
        wait_until(8, 53)
    ctx = context()
    if dry_run:
        ctx["cutoff"] = datetime.now(JST).isoformat()
    records = artifacts() if os.getenv("GITHUB_ACTIONS") == "true" else []
    if not dry_run and os.getenv("GITHUB_ACTIONS") != "true":
        raise RuntimeError("正式发送仅允许在具有持久化记录的GitHub工作流执行")
    if not dry_run:
        no_previous_claim(records, ctx["date"])
    history = recent_history(records, ctx["date"])
    sources = collect_sources(datetime.fromisoformat(ctx["cutoff"]))
    collected_at = datetime.now(JST).isoformat()
    if not dry_run:
        # Publication timestamps alone cannot reveal every later article edit.
        # Only a body actually captured before 09:00 is eligible for delivery.
        collection_deadline(ctx["cutoff"], datetime.fromisoformat(collected_at))
    bundle = edit_digest(sources, history, ctx["date"], ctx["cutoff"], preview=dry_run)
    bundle.update(sources=sources, collected_at=collected_at,
                  edition_type="preview" if dry_run else "daily")
    verify_review(bundle)
    title, plain, page = render(bundle)
    OUT.mkdir(exist_ok=True)
    (OUT / "digest.json").write_text(canonical(bundle), encoding="utf-8")
    (OUT / "digest.html").write_text(page, encoding="utf-8")
    (OUT / "digest.txt").write_text(plain, encoding="utf-8")
    if not dry_run:
        address_hash = hashlib.sha256(recipient().encode()).hexdigest()
        claim = {
            "date": ctx["date"],
            "digest_hash": digest_hash(bundle),
            "recipient_hash": address_hash,
            "message_id": f"<news-{ctx['date']}-{address_hash[:16]}@daily-news-digest.invalid>",
            "run_id": required("GITHUB_RUN_ID"),
            "run_attempt": required("GITHUB_RUN_ATTEMPT"),
            "status": "sending_or_unknown",
        }
        (OUT / "claim.json").write_text(canonical(claim), encoding="utf-8")
    print("中文稿与独立审稿已完成；" + ("仅预览，未发邮件。" if dry_run else "等待持久化发送占位。"))


def claim_test() -> None:
    """Promote the inspected preview unchanged; never regenerate for the test."""
    if os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise RuntimeError("测试邮件仅允许显式手动运行")
    bundle = json.loads((OUT / "digest.json").read_text(encoding="utf-8"))
    verify_review(bundle)
    if bundle.get("edition_type") != "preview":
        raise RuntimeError("只能将预览稿用于测试邮件")
    now = datetime.now(JST)
    captured = datetime.fromisoformat(bundle["collected_at"])
    if not timedelta(0) <= now - captured <= timedelta(hours=6):
        raise RuntimeError("测试样稿超过6小时或采集时间无效")
    day = now.date().isoformat()
    if any(r["name"] == artifact_name("test-claim", day) for r in artifacts()):
        raise RuntimeError("今天已有测试邮件占位，禁止重发")
    bundle["delivery_mode"] = "test"
    address_hash = hashlib.sha256(recipient().encode()).hexdigest()
    claim = {
        "date": day, "kind": "test-claim", "digest_hash": digest_hash(bundle),
        "recipient_hash": address_hash,
        "message_id": f"<news-test-{day}-{address_hash[:16]}@daily-news-digest.invalid>",
        "run_id": required("GITHUB_RUN_ID"), "run_attempt": required("GITHUB_RUN_ATTEMPT"),
        "status": "sending_or_unknown",
    }
    _, plain, page = render(bundle)
    for name, content in (("digest.json", canonical(bundle)), ("claim.json", canonical(claim)),
                          ("digest.txt", plain), ("digest.html", page)):
        (OUT / name).write_text(content, encoding="utf-8")
    print("已检查现有样稿；等待保存唯一测试发送占位。")


def send() -> None:
    if os.getenv("GITHUB_ACTIONS") != "true":
        raise RuntimeError("不允许绕过GitHub持久化发送记录")
    bundle = json.loads((OUT / "digest.json").read_text(encoding="utf-8"))
    claim = json.loads((OUT / "claim.json").read_text(encoding="utf-8"))
    if claim["digest_hash"] != digest_hash(bundle):
        raise RuntimeError("准备稿发生变化")
    if claim["recipient_hash"] != hashlib.sha256(recipient().encode()).hexdigest():
        raise RuntimeError("收件人发生变化")
    kind = claim.get("kind", "claim")
    if kind not in {"claim", "test-claim"}:
        raise RuntimeError("发送类型无效")
    test = kind == "test-claim"
    if test and (os.getenv("GITHUB_EVENT_NAME") != "workflow_dispatch" or bundle.get("delivery_mode") != "test"):
        raise RuntimeError("测试邮件必须为手动运行且明确标记")
    if not test and bundle.get("edition_type") == "preview":
        raise RuntimeError("预览稿不能冒充正式早报")
    matches = [r for r in artifacts() if r["name"] == artifact_name(kind, claim["date"])]
    if len(matches) != 1 or artifact_file(matches[0], "claim.json") != claim:
        raise RuntimeError("必须先成功保存且核验唯一发送占位，才可发信")
    if (
        claim["run_id"] != required("GITHUB_RUN_ID")
        or claim["run_attempt"] != required("GITHUB_RUN_ATTEMPT")
    ):
        raise RuntimeError("发送占位不属于本次运行尝试")
    if not test:
        cutoff = datetime.fromisoformat(bundle["draft"]["cutoff"]).astimezone(JST)
        if cutoff.isoformat() != claim["date"] + "T09:00:00+09:00":
            raise RuntimeError("正式早报必须使用当日09:00截点")
        collection_deadline(bundle["draft"]["cutoff"], datetime.fromisoformat(bundle["collected_at"]))
    title, plain, page = render(bundle)
    if not test:
        wait_until(9, 0)
        delivery_window(claim["date"])
    elif datetime.now(JST).date().isoformat() != claim["date"]:
        raise RuntimeError("测试发送不能跨日")
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = required("MAIL_FROM")
    message["To"] = recipient()
    message["Date"] = format_datetime(datetime.now(JST))
    message["Message-ID"] = claim["message_id"]
    message.set_content(plain)
    message.add_alternative(page, subtype="html")
    port = int(required("SMTP_PORT"))
    if port not in (465, 587):
        raise RuntimeError("只支持加密SMTP端口465或587")
    tls = ssl.create_default_context()
    server = (
        smtplib.SMTP_SSL(required("SMTP_HOST"), port, timeout=30, context=tls)
        if port == 465 else smtplib.SMTP(required("SMTP_HOST"), port, timeout=30)
    )
    try:
        server.ehlo()
        if port == 587:
            server.starttls(context=tls)
            server.ehlo()
        server.login(required("SMTP_USER"), required("SMTP_PASSWORD"))
        if server.send_message(message):
            raise RuntimeError("SMTP未接受收件人")
        # Never automatically retry SMTP: an exception may follow an accepted message.
    finally:
        try:
            server.quit()
        except Exception:
            server.close()
    (OUT / "sent.json").write_text(
        canonical({"message_id": claim["message_id"], "sent_at": datetime.now(JST).isoformat()}),
        encoding="utf-8",
    )
    print("SMTP已接受唯一测试邮件。" if test else "SMTP已接受今日唯一邮件。")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "send", "claim-test"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.dry_run)
    elif args.command == "claim-test":
        claim_test()
    elif args.dry_run:
        raise RuntimeError("send不接受dry-run；预览请用prepare --dry-run")
    else:
        send()


if __name__ == "__main__":
    main()
