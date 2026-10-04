from datetime import datetime, timezone
from email import message_from_bytes
from zoneinfo import ZoneInfo

import pytest

from news_aggregator import __main__ as cli
from news_aggregator import mailer
from news_aggregator.__main__ import due_periods
from news_aggregator.aggregator import Article

from .test_aggregator import FIXTURES

TZ = ZoneInfo("Asia/Kolkata")
SUNDAY_8 = datetime(2026, 10, 4, 8, 0, tzinfo=TZ)


def test_nothing_due_before_send_hour(tmp_path):
    assert due_periods(datetime(2026, 10, 4, 7, 59, tzinfo=TZ), tmp_path) == []


def test_daily_due_on_weekday_window_ends_at_send_hour(tmp_path):
    jobs = due_periods(datetime(2026, 10, 5, 8, 37, tzinfo=TZ), tmp_path)
    assert jobs == [("daily", datetime(2026, 10, 5, 8, 0, tzinfo=TZ))]


def test_daily_and_weekly_due_on_sunday(tmp_path):
    jobs = due_periods(datetime(2026, 10, 4, 9, 5, tzinfo=TZ), tmp_path)
    assert [p for p, _ in jobs] == ["daily", "weekly"]
    assert all(end == SUNDAY_8 for _, end in jobs)


def test_already_delivered_is_not_due_again(tmp_path):
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "2026-10-04.md").write_text("x")
    jobs = due_periods(datetime(2026, 10, 4, 10, 0, tzinfo=TZ), tmp_path)
    assert [p for p, _ in jobs] == ["weekly"]


def test_smtp_config_from_env():
    cfg = mailer.SmtpConfig.from_env({
        "SMTP_HOST": "smtp.example.com",
        "SMTP_USERNAME": "bot@example.com",
        "EMAIL_TO": "a@example.com, b@example.com",
    })
    assert cfg.port == 587
    assert cfg.sender == "bot@example.com"
    assert cfg.recipients == ("a@example.com", "b@example.com")
    with pytest.raises(ValueError, match="SMTP_HOST"):
        mailer.SmtpConfig.from_env({"EMAIL_TO": "a@example.com"})


def test_render_html_escapes_and_blocks_unsafe_links():
    ts = datetime(2026, 10, 4, 1, tzinfo=timezone.utc)
    html = mailer.render_html(
        [
            Article("Lab <x>", "<script>alert(1)</script>", "javascript:alert(1)", ts, "a & b", "research"),
            Article("Lab", "Launch", "https://lab.example.com/launch", ts, "", "product"),
        ],
        "daily",
        SUNDAY_8,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="#"' in html
    assert 'href="https://lab.example.com/launch"' in html
    assert html.index("Research") < html.index("Product &amp; Announcements")


def test_subject():
    assert mailer.subject_for("daily", SUNDAY_8, 1) == "Daily AI News Digest — Sun, Oct 04, 2026 (1 article)"
    assert mailer.subject_for("weekly", SUNDAY_8, 3) == "Weekly AI News Digest — Sep 27 – Oct 04, 2026 (3 articles)"


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port):
        self.logged_in = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, secret):
        self.logged_in = user

    def send_message(self, message):
        FakeSMTP.sent.append(message)


def test_scheduled_cli_emails_then_writes_digests(tmp_path, monkeypatch):
    feeds = tmp_path / "feeds.toml"
    feeds.write_text(f'[[feeds]]\nname = "Example"\nurl = "{(FIXTURES / "rss.xml").as_posix()}"\n')
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "bot@example.com")
    monkeypatch.setenv("EMAIL_TO", "me@example.com")
    gh_output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))
    real_send = mailer.send_message
    monkeypatch.setattr(mailer, "send_message", lambda cfg, msg: real_send(cfg, msg, smtp_factory=FakeSMTP))
    FakeSMTP.sent = []

    args = [
        "--period", "scheduled", "--email", "--timezone", "Asia/Kolkata",
        "--feeds", str(feeds), "--output-dir", str(tmp_path / "digests"),
        "--now", "2026-10-04T03:00:00+00:00",  # 08:30 IST on a Sunday
    ]
    assert cli.main(args) == 0

    subjects = [m["Subject"] for m in FakeSMTP.sent]
    assert subjects[0].startswith("Daily AI News Digest")
    assert subjects[1].startswith("Weekly AI News Digest")
    weekly = message_from_bytes(FakeSMTP.sent[1].as_bytes())
    html_part = next(p for p in weekly.walk() if p.get_content_type() == "text/html")
    assert "Agentic workflows for developers" in html_part.get_payload(decode=True).decode()
    assert (tmp_path / "digests" / "daily" / "2026-10-04.md").exists()
    assert (tmp_path / "digests" / "weekly" / "2026-W40.md").exists()
    assert "IST" in (tmp_path / "digests" / "daily" / "2026-10-04.md").read_text()

    # A later run the same day sends nothing new.
    FakeSMTP.sent = []
    assert cli.main(args[:-1] + ["2026-10-04T05:00:00+00:00"]) == 0
    assert FakeSMTP.sent == []


def test_failed_send_does_not_mark_digest_delivered(tmp_path, monkeypatch):
    feeds = tmp_path / "feeds.toml"
    feeds.write_text(f'[[feeds]]\nname = "Example"\nurl = "{(FIXTURES / "rss.xml").as_posix()}"\n')
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM", "bot@example.com")
    monkeypatch.setenv("EMAIL_TO", "me@example.com")

    def boom(cfg, msg):
        raise OSError("smtp down")

    monkeypatch.setattr(mailer, "send_message", boom)
    with pytest.raises(OSError):
        cli.main([
            "--period", "scheduled", "--email", "--timezone", "UTC",
            "--feeds", str(feeds), "--output-dir", str(tmp_path / "digests"),
            "--now", "2026-10-05T08:10:00",
        ])
    assert not (tmp_path / "digests" / "daily" / "2026-10-05.md").exists()
