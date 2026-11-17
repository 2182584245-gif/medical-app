"""Foreground-session reminders; no background service or medical decisions."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import QLockFile, QObject, QThreadPool, QTimer, Signal

from ..security.private_payload import read_private_json, write_private_json
from ..workers.task import FunctionTask


def _aware(value) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("提醒时间必须包含时区。")
    return result.astimezone(UTC)


def due_occurrences(reminder: dict, now: datetime, timezone: str, *, lookback_seconds=300):
    """Return at most one recent occurrence, preserving local wall time over DST.

    Monthly rules skip absent dates; nonexistent DST times are skipped and a
    repeated wall-clock time uses its first occurrence only. Never replay an
    old backlog on app startup or after a long suspend.
    """
    now = _aware(now)
    if reminder.get("status") != "active" or reminder.get("enabled") is False:
        return []
    paused_until = reminder.get("paused_until")
    if paused_until and _aware(paused_until) > now:
        return []
    start = _aware(reminder["scheduled_at"])
    rule = reminder.get("repeat_rule")
    if rule not in {None, "", "none", "daily", "weekly", "monthly"}:
        return []  # Unknown future rules fail closed rather than over-reminding.
    lower = now - timedelta(seconds=lookback_seconds)
    if rule in {None, "", "none"}:
        return [start] if lower < start <= now else []
    zone = ZoneInfo(timezone)
    local_start = start.astimezone(zone)
    candidates = []
    for day in (now.astimezone(zone).date() - timedelta(days=1), now.astimezone(zone).date()):
        if day < local_start.date():
            continue
        if rule == "weekly" and day.weekday() != local_start.weekday():
            continue
        if rule == "monthly" and day.day != local_start.day:
            continue
        wall = datetime.combine(day, local_start.time().replace(tzinfo=None))
        candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) != wall:
            continue
        if start <= candidate and lower < candidate <= now:
            candidates.append(candidate)
    return candidates[-1:]


class ReminderScheduler(QObject):
    """Main-thread signals, worker reads, generation-guarded per-account ledger.

    Construct on the GUI thread. ``start(user)`` and ``stop()`` delimit the
    authenticated session. ``reminder_due`` carries no other user's data.
    Persistent delivery markers are written BEFORE notifying (at most once on
    this device; a crash between the write and display may miss one reminder).
    Does not advance cloud rows, create medication advice, or run after exit.
    """

    reminder_due = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        health_service,
        preferences_service,
        *,
        namespace: str,
        parent=None,
        storage_dir=None,
        clock=None,
        interval_ms=15000,
    ):
        super().__init__(parent)
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("提醒需要明确的数据来源。")
        self.health_service = health_service
        self.preferences_service = preferences_service
        self.namespace = namespace
        self.storage_dir = Path(
            storage_dir
            or (
                Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
                / "HealthLife"
                / "private-reminders"
            )
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self._generation = 0
        self._user = None
        self._task = None
        self._tasks = []
        self._seen = {}
        self._loaded = False
        self._timer = QTimer(self)
        self._timer.setInterval(max(1000, int(interval_ms)))
        self._timer.timeout.connect(self.check_now)

    def start(self, user):
        self.stop()
        if user is None or type(getattr(user, "id", None)) is not int or user.id <= 0:
            raise ValueError("提醒需要已登录的账户。")
        self._user = user
        self._timer.start()
        self.check_now()

    def stop(self):
        self._timer.stop()
        self._generation += 1
        self._user = None
        self._seen = {}
        self._loaded = False
        # Running reads cannot be forcibly killed; their results are discarded.
        self._task = None

    def _identity(self, user):
        stable = f"{self.namespace}\0{user.id}\0{user.username_normalized}"
        return hashlib.sha256(stable.encode()).hexdigest()

    def refresh(self):
        """Call after reminder edits/deletes to discard an older in-flight read."""
        self._generation += 1
        self._task = None
        self.check_now()

    def check_now(self):
        if self._user is None or self._task is not None:
            return
        user, generation = self._user, self._generation
        identity = self._identity(user)
        path = self.storage_dir / (identity + ".dpapi")
        purpose = "reminder-delivery/v1/" + identity
        seen, loaded = dict(self._seen), self._loaded

        def read():
            previous = seen if loaded else (read_private_json(path, purpose) or {})
            if not isinstance(previous, dict):
                raise ValueError("提醒去重状态无效。")
            # Data are fetched afresh, not retained across a failed permission check.
            preferences = self.preferences_service.get(user.id) if self.preferences_service else {}
            reminders = self.health_service.list_reminders(user.id)
            now = _aware(self.clock())
            timezone = str(preferences.get("timezone") or "Asia/Shanghai")
            preferred = str(
                preferences.get("preferred_name") or preferences.get("nickname") or "您"
            )[:80]
            cutoff = (now - timedelta(days=35)).timestamp()
            kept = {
                key: value
                for key, value in previous.items()
                if isinstance(key, str) and isinstance(value, (float, int)) and value >= cutoff
            }
            notices = []
            for row in reminders:
                if type(row.get("user_id")) is not int or row["user_id"] != user.id:
                    raise ValueError("提醒资料身份不匹配。")
                for occurrence in due_occurrences(row, now, timezone):
                    marker = f"{row['id']}:{occurrence.isoformat()}"
                    if marker not in kept:
                        notices.append(
                            {
                                "user_id": user.id,
                                "reminder_id": row["id"],
                                "title": str(row["title"])[:160],
                                "preferred_name": preferred,
                                "scheduled_at": occurrence.isoformat(),
                                "timezone": timezone,
                                "message": f"{preferred}，温馨提醒：{str(row['title'])[:160]}",
                                "_marker": marker,
                            }
                        )
            return {"notices": notices[:20], "seen": kept, "now": now.timestamp()}

        task = FunctionTask(read)
        self._task = task
        self._tasks.append(task)

        def accepted(result):
            if generation != self._generation or self._user is not user:
                return
            if not result["notices"]:
                self._seen, self._loaded = result["seen"], True
                return

            # Persist in another worker; do not block the UI on DPAPI/disk I/O.
            def persist():
                self.storage_dir.mkdir(parents=True, exist_ok=True)
                lock = QLockFile(str(path) + ".lock")
                lock.setStaleLockTime(120000)
                if not lock.tryLock(500):
                    raise ValueError("另一个应用正在核验提醒。")
                try:
                    ledger = read_private_json(path, purpose) or {}
                    if not isinstance(ledger, dict):
                        raise ValueError("提醒去重状态无效。")
                    cutoff = result["now"] - 35 * 86400
                    ledger = {
                        key: value
                        for key, value in ledger.items()
                        if isinstance(key, str)
                        and isinstance(value, (float, int))
                        and value >= cutoff
                    }
                    fresh = [
                        notice for notice in result["notices"] if notice["_marker"] not in ledger
                    ]
                    ledger = {**ledger, **result["seen"]}
                    for notice in fresh:
                        ledger[notice["_marker"]] = result["now"]
                    write_private_json(path, purpose, ledger)
                    return {"ledger": ledger, "notices": fresh}
                finally:
                    lock.unlock()

            writer = FunctionTask(persist)
            self._tasks.append(writer)
            self._task = writer

            def delivered(saved):
                if generation != self._generation or self._user is not user:
                    return
                self._seen, self._loaded = saved["ledger"], True
                for notice in saved["notices"]:
                    self.reminder_due.emit(
                        {key: value for key, value in notice.items() if not key.startswith("_")}
                    )

            writer.signals.result.connect(delivered)
            writer.signals.error.connect(rejected)
            writer.signals.finished.connect(lambda: finished(writer))
            QThreadPool.globalInstance().start(writer)

        def rejected(_error):
            if generation == self._generation and self._user is user:
                self.failed.emit("暂时无法核验当前提醒；未使用旧资料发送提醒。")

        def finished(completed):
            if completed in self._tasks:
                self._tasks.remove(completed)
            if self._task is completed:
                self._task = None

        task.signals.result.connect(accepted)
        task.signals.error.connect(rejected)
        task.signals.finished.connect(lambda: finished(task))
        QThreadPool.globalInstance().start(task)
