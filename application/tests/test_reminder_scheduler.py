import os
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest

from ollama_chat_app.services.reminder_scheduler import ReminderScheduler, due_occurrences


def moment(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def reminder(**values):
    return {
        "id": 1,
        "user_id": 7,
        "title": "喝一杯温水",
        "status": "active",
        "enabled": True,
        "scheduled_at": moment("2026-09-09T08:00:00+08:00"),
        "repeat_rule": "none",
        **values,
    }


@pytest.mark.parametrize(
    "update",
    [
        {"status": "paused"},
        {"enabled": False},
        {"repeat_rule": "unknown"},
        {"paused_until": "2026-09-10T00:00:00Z"},
    ],
)
def test_inactive_unknown_or_paused_until_never_notifies(update):
    assert (
        due_occurrences(reminder(**update), moment("2026-09-09T08:00:01+08:00"), "Asia/Shanghai")
        == []
    )


def test_exact_due_time_future_and_old_backlog_are_distinct():
    due = moment("2026-09-09T08:00:00+08:00")
    assert due_occurrences(reminder(), due, "Asia/Shanghai") == [due]
    assert due_occurrences(reminder(), due - timedelta(seconds=1), "Asia/Shanghai") == []
    assert due_occurrences(reminder(), due + timedelta(minutes=6), "Asia/Shanghai") == []


@pytest.mark.parametrize(
    "rule,now,expected",
    [
        ("daily", "2026-09-10T08:00:30+08:00", True),
        ("weekly", "2026-09-10T08:00:30+08:00", False),
        ("weekly", "2026-09-16T08:00:30+08:00", True),
        ("monthly", "2026-10-09T08:00:30+08:00", True),
        ("monthly", "2026-10-10T08:00:30+08:00", False),
    ],
)
def test_recurrences_use_local_wall_clock(rule, now, expected):
    assert (
        bool(due_occurrences(reminder(repeat_rule=rule), moment(now), "Asia/Shanghai")) is expected
    )


def test_monthly_missing_day_does_not_move_to_another_day():
    row = reminder(scheduled_at="2026-01-31T08:00:00+08:00", repeat_rule="monthly")
    assert due_occurrences(row, moment("2026-02-28T08:00:10+08:00"), "Asia/Shanghai") == []


def test_dst_missing_hour_skipped_and_fold_not_delivered_twice():
    spring = reminder(scheduled_at="2026-03-07T02:30:00-05:00", repeat_rule="daily")
    assert due_occurrences(spring, moment("2026-03-08T03:30:30-04:00"), "America/New_York") == []
    fall = reminder(scheduled_at="2026-10-31T01:30:00-04:00", repeat_rule="daily")
    assert due_occurrences(fall, moment("2026-11-01T01:30:30-04:00"), "America/New_York")
    assert due_occurrences(fall, moment("2026-11-01T01:30:30-05:00"), "America/New_York") == []


def test_naive_timestamp_and_unknown_timezone_rejected():
    with pytest.raises(ValueError):
        due_occurrences(
            reminder(scheduled_at="2026-09-09T08:00:00"),
            moment("2026-09-09T00:00:00Z"),
            "Asia/Shanghai",
        )


def make_scheduler(tmp_path, *, health=None, namespace="synthetic-reminders", clock=None):
    health = health or SimpleNamespace(list_reminders=lambda user_id: [reminder()])
    preferences = SimpleNamespace(
        get=lambda user_id: {"preferred_name": "小张", "timezone": "Asia/Shanghai"}
    )
    return ReminderScheduler(
        health,
        preferences,
        namespace=namespace,
        storage_dir=tmp_path,
        interval_ms=600000,
        clock=clock or (lambda: moment("2026-09-09T08:00:15+08:00")),
    )


USER = SimpleNamespace(id=7, username_normalized="synthetic-member")


def test_worker_and_persistent_dedup_across_restart(qtbot, tmp_path):
    main_thread, reads, notices = threading.get_ident(), [], []

    def read(user_id):
        reads.append(threading.get_ident())
        return [reminder()]

    scheduler = make_scheduler(tmp_path, health=SimpleNamespace(list_reminders=read))
    scheduler.reminder_due.connect(notices.append)
    scheduler.start(USER)
    qtbot.waitUntil(lambda: len(notices) == 1)
    assert notices[0]["message"] == "小张，温馨提醒：喝一杯温水"
    assert all(value != main_thread for value in reads)
    qtbot.waitUntil(lambda: scheduler._task is None)
    scheduler.check_now()
    qtbot.waitUntil(lambda: scheduler._task is None)
    assert len(notices) == 1
    scheduler.stop()
    restarted = make_scheduler(tmp_path)
    restarted.reminder_due.connect(notices.append)
    restarted.start(USER)
    qtbot.waitUntil(lambda: restarted._task is None)
    assert len(notices) == 1
    assert all("喝一杯温水".encode() not in path.read_bytes() for path in tmp_path.glob("*.dpapi"))
    restarted.stop()


def test_logout_generation_and_cross_account_data_never_emit(qtbot, tmp_path):
    started, release, notices = threading.Event(), threading.Event(), []

    def read(_user_id):
        started.set()
        release.wait(5)
        return [reminder()]

    scheduler = make_scheduler(tmp_path, health=SimpleNamespace(list_reminders=read))
    scheduler.reminder_due.connect(notices.append)
    scheduler.start(USER)
    qtbot.waitUntil(started.is_set)
    scheduler.stop()
    release.set()
    qtbot.waitUntil(lambda: not scheduler._tasks)
    assert notices == [] and not scheduler._timer.isActive()
    other = make_scheduler(
        tmp_path, health=SimpleNamespace(list_reminders=lambda _id: [reminder(user_id=8)])
    )
    errors = []
    other.reminder_due.connect(notices.append)
    other.failed.connect(errors.append)
    other.start(USER)
    qtbot.waitUntil(lambda: bool(errors))
    assert notices == []
    other.stop()


def test_two_instances_share_atomic_delivery_marker(qtbot, tmp_path):
    notices = []
    schedulers = [make_scheduler(tmp_path), make_scheduler(tmp_path)]
    for scheduler in schedulers:
        scheduler.reminder_due.connect(notices.append)
        scheduler.start(USER)
    qtbot.waitUntil(lambda: all(scheduler._task is None for scheduler in schedulers))
    assert len(notices) == 1
    for scheduler in schedulers:
        scheduler.stop()


def test_refresh_discards_pre_edit_inflight_result(qtbot, tmp_path):
    started, release, notices = threading.Event(), threading.Event(), []
    rows = [reminder()]

    def read(_id):
        before = list(rows)
        if before:
            started.set()
            release.wait(5)
        return before

    scheduler = make_scheduler(tmp_path, health=SimpleNamespace(list_reminders=read))
    scheduler.reminder_due.connect(notices.append)
    scheduler.start(USER)
    qtbot.waitUntil(started.is_set)
    rows.clear()
    scheduler.refresh()
    release.set()
    qtbot.waitUntil(lambda: not scheduler._tasks)
    assert notices == []
    scheduler.stop()
