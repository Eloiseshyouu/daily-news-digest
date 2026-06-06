# Daily News Cloud Job

This project sends a daily news digest by email from GitHub Actions.

## What It Does

- Runs every day at 09:00 Asia/Tokyo time.
- Reads RSS feeds from `feeds.json`.
- Builds a short email digest.
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

Edit `feeds.json`. Each item needs a `name` and `url`.

