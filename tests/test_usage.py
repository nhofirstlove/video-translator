import json
from datetime import date

from video_translator.usage import UsageTracker, usage_file


def test_usage_file_name_has_month(tmp_path):
    path = usage_file(tmp_path, today=date(2026, 3, 9))
    assert path == tmp_path / "usage" / "elevenlabs_2026-03.json"


def test_tracker_records_and_persists(tmp_path):
    path = tmp_path / "usage" / "elevenlabs_2026-03.json"
    tracker = UsageTracker(path, monthly_budget=100)
    assert (tracker.used, tracker.remaining) == (0, 100)
    assert tracker.can_spend(100)
    assert not tracker.can_spend(101)
    tracker.record(30)
    tracker.record(20)
    assert (tracker.used, tracker.remaining) == (50, 50)
    assert json.loads(path.read_text(encoding="utf-8"))["used"] == 50
    assert not list(path.parent.glob("*.tmp"))
    again = UsageTracker(path, monthly_budget=100)
    assert again.used == 50


def test_tracker_remaining_never_negative_and_bad_file_ignored(tmp_path):
    path = tmp_path / "u.json"
    path.write_text("not json", encoding="utf-8")
    tracker = UsageTracker(path, monthly_budget=10)
    assert tracker.used == 0
    tracker.record(25)
    assert tracker.remaining == 0
    assert not tracker.can_spend(1)
