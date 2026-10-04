"""Render digests as HTML email and send them over SMTP."""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .aggregator import CATEGORY_TITLES, PERIODS, Article

DEFAULT_SMTP_PORT = 587


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: tuple[str, ...]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SmtpConfig":
        env = os.environ if env is None else env
        missing = [key for key in ("SMTP_HOST", "EMAIL_TO") if not env.get(key)]
        if missing:
            raise ValueError(f"Missing required email settings: {', '.join(missing)}")
        username = env.get("SMTP_USERNAME", "")
        sender = env.get("EMAIL_FROM") or username
        if not sender:
            raise ValueError("Set EMAIL_FROM (or SMTP_USERNAME) to the sender address")
        recipients = tuple(addr.strip() for addr in env["EMAIL_TO"].split(",") if addr.strip())
        if not recipients:
            raise ValueError("EMAIL_TO must contain at least one address")
        return cls(
            host=env["SMTP_HOST"],
            port=int(env.get("SMTP_PORT") or DEFAULT_SMTP_PORT),
            username=username,
            password=env.get("SMTP_PASSWORD", ""),
            sender=sender,
            recipients=recipients,
        )


def _safe_url(url: str) -> str:
    return url if url.lower().startswith(("https://", "http://")) else "#"


def subject_for(period: str, now: datetime, count: int) -> str:
    title = "Daily" if period == "daily" else "Weekly"
    if period == "weekly":
        start = now - PERIODS[period]
        span = f"{start:%b %d} – {now:%b %d, %Y}"
    else:
        span = f"{now:%a, %b %d, %Y}"
    return f"{title} AI News Digest — {span} ({count} article{'s' if count != 1 else ''})"


def render_html(articles: list[Article], period: str, now: datetime) -> str:
    start = now - PERIODS[period]
    title = "Daily" if period == "daily" else "Weekly"
    esc = html.escape
    parts = [
        "<!DOCTYPE html><html><body style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
        "max-width:720px;margin:auto;color:#1f2328;line-height:1.45\">",
        f"<h1 style=\"font-size:22px\">{title} AI News Digest</h1>",
        f"<p style=\"color:#59636e\">{esc(f'{start:%Y-%m-%d %H:%M %Z}')} → {esc(f'{now:%Y-%m-%d %H:%M %Z}')}"
        f" · {len(articles)} article(s)</p>",
    ]
    if not articles:
        parts.append("<p>No new AI posts from the tracked blogs in this period.</p>")

    for category, heading in CATEGORY_TITLES.items():
        by_source: dict[str, list[Article]] = {}
        for article in articles:
            if article.category == category:
                by_source.setdefault(article.source, []).append(article)
        if not by_source:
            continue
        parts.append(f"<h2 style=\"font-size:19px;border-bottom:1px solid #d1d9e0\">{esc(heading)}</h2>")
        for source in sorted(by_source, key=str.lower):
            parts.append(f"<h3 style=\"font-size:16px;margin-bottom:4px\">{esc(source)}</h3><ul>")
            for article in by_source[source]:
                parts.append(
                    f"<li style=\"margin-bottom:8px\"><a href=\"{esc(_safe_url(article.link), quote=True)}\">"
                    f"{esc(article.title)}</a> <span style=\"color:#59636e\">— {article.published:%Y-%m-%d}</span>"
                )
                if article.summary:
                    parts.append(f"<br><span style=\"color:#59636e\">{esc(article.summary)}</span>")
                parts.append("</li>")
            parts.append("</ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def build_message(config: SmtpConfig, subject: str, text: str, html_body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=config.sender.rsplit("@", 1)[-1] or None)
    message.set_content(text)
    message.add_alternative(html_body, subtype="html")
    return message


def send_message(config: SmtpConfig, message: EmailMessage, smtp_factory=None) -> None:
    context = ssl.create_default_context()
    if smtp_factory is not None:
        client = smtp_factory(config.host, config.port)
    elif config.port == 465:
        client = smtplib.SMTP_SSL(config.host, config.port, context=context, timeout=60)
    else:
        client = smtplib.SMTP(config.host, config.port, timeout=60)
    with client:
        if smtp_factory is None and config.port != 465:
            client.starttls(context=context)
        if config.username:
            client.login(config.username, config.password)
        client.send_message(message)
