from datetime import datetime
from zoneinfo import ZoneInfo

from news_aggregator import __main__ as cli
from news_aggregator.__main__ import due_periods

from .test_aggregator import FIXTURES

TZ = ZoneInfo("Asia/Kolkata")
SUNDAY_8 = datetime(2026, 10, 4, 8, 0, tzinfo=TZ)


def test_nothing_due_before_send_hour(tmp_path):
    assert due_periods(datetime(2026, 10, 4, 7, 59, tzinfo=TZ), tmp_path) == []


def test_weekly_due_on_sunday(tmp_path):
    jobs = due_periods(datetime(2026, 10, 4, 9, 5, tzinfo=TZ), tmp_path)
    assert jobs == [("weekly", SUNDAY_8)]


def test_no_weekly_digest_on_weekday(tmp_path):
    jobs = due_periods(datetime(2026, 10, 5, 8, 37, tzinfo=TZ), tmp_path)
    assert jobs == []


def test_already_delivered_is_not_due_again(tmp_path):
    weekly_dir = tmp_path / "weekly"
    weekly_dir.mkdir()
    (weekly_dir / "2026-W40.md").write_text("x")
    jobs = due_periods(datetime(2026, 10, 4, 10, 0, tzinfo=TZ), tmp_path)
    assert jobs == []


def _feeds(tmp_path):
    feeds = tmp_path / "feeds.toml"
    feeds.write_text(f'[[feeds]]\nname = "Example"\nurl = "{(FIXTURES / "rss.xml").as_posix()}"\n')
    return feeds


def test_outputs_list_only_weekly_digests_with_articles(tmp_path, monkeypatch):
    gh_output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))
    assert cli.main([
        "--period", "scheduled", "--timezone", "UTC", "--feeds", str(_feeds(tmp_path)),
        "--output-dir", str(tmp_path / "d"), "--now", "2026-10-04T08:10:00",
    ]) == 0
    out = gh_output.read_text()
    assert f"digest_paths={tmp_path / 'd' / 'weekly' / '2026-W40.md'}\n" in out
    assert f"nonempty_digest_paths={tmp_path / 'd' / 'weekly' / '2026-W40.md'}\n" in out


def test_scheduled_cli_writes_weekly_digest_once(tmp_path):
    args = [
        "--period", "scheduled", "--timezone", "Asia/Kolkata",
        "--feeds", str(_feeds(tmp_path)), "--output-dir", str(tmp_path / "d"),
        "--now", "2026-10-04T03:00:00+00:00",  # 08:30 IST on a Sunday
    ]
    assert cli.main(args) == 0
    weekly = tmp_path / "d" / "weekly" / "2026-W40.md"
    assert "IST" in weekly.read_text()
    assert "Agentic workflows for developers" in weekly.read_text()

    # A later run the same day does not rebuild the already-delivered weekly digest.
    weekly.write_text("delivered")
    assert cli.main(args[:-1] + ["2026-10-04T05:00:00+00:00"]) == 0
    assert weekly.read_text() == "delivered"
