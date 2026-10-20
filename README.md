
# Daily News Cloud Job

> 2026-10-20：旧版 RSS 邮件流程已暂停。免费 AI 中文编辑候选版见
> [FREE_EDITION_SETUP.md](FREE_EDITION_SETUP.md)。新工作流默认不发送；
> 配置免费项目、验收真实样稿后才能启用。下面保留的是旧版本说明，
> 不要仅重新开启 `Daily News Digest` 旧工作流。

This project sends a Chinese daily news digest by email from GitHub Actions.
The digest is tuned for a Chinese student living in Japan.

## What It Does

- Runs every day at 09:00 Asia/Tokyo time.
- Reads multi-region news RSS feeds from `feeds.json`.
- Builds a Chinese HTML email with a plain-text fallback.
- Prioritizes Japan, China, and global trend news.
- Defaults to about 6 important items per day.
- Focuses on Japan prices, yen, student life, policy, safety, China-Japan relations, China economy and diplomacy, overseas Chinese issues, US/Europe/Middle East/Russia-Ukraine/Asia-Pacific security, and global markets.
- Sends the digest through SMTP.
- Can also be run manually from GitHub Actions.

## Setup

1. Create a new GitHub repository and upload these files.
2. In the repository, go to `Settings -> Secrets and variables -> Actions`.
3. Add these repository secrets:

| Secret | Example | Notes |
| --- | --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` | Your mail provider SMTP server |
| `SMTP_PORT` | `587` | Usually `587` for TLS |
| `SMTP_USER` | `you@gmail.com` | SMTP login |
| `SMTP_PASSWORD` | app password | Use an app password, not your normal password |
| `MAIL_FROM` | `you@gmail.com` | Sender address |
| `MAIL_TO` | `you@gmail.com` | Recipient address |

For Gmail, enable 2-step verification and create an app password.

## Schedule

GitHub Actions cron uses UTC. The workflow uses:

```yaml
cron: "0 0 * * *"
```

That is 09:00 in Japan Standard Time.

## Test Locally

Set the environment variables, then run:

```bash
python3 news_digest.py
```

To preview the email without sending:

```bash
DRY_RUN=1 python3 news_digest.py
```

## Change News Sources

Edit `feeds.json`. Each item needs a `name`, `category`, and `url`.

Preferred source direction: Reuters, AP, AFP, BBC, FT, WSJ, Bloomberg,
Nikkei, NHK, Kyodo, Asahi, The Japan Times, Caixin, SCMP, Al Jazeera,
and official institutions.
