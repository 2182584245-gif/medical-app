"""Apply authorized snapshots to visible read-only widgets, never to editors.

The entry point is deliberately separate from each workspace's interactive
``refresh``: those methods may fetch extra data or reset editing controls.
"""

from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QListWidget, QTableWidget

from ..services.cloud_client import CloudAPIError
from ..services.cloud_sync import SyncError


def _item_key(item):
    if item is None:
        return None
    value = item.data(Qt.ItemDataRole.UserRole)
    if isinstance(value, dict):
        return ("id", value.get("id")) if value.get("id") is not None else None
    if isinstance(value, int):
        return ("id", value)
    return ("text", item.text()) if item.text() else None


@contextmanager
def preserve_views(*widgets):
    """Keep current row identity and scroll; suppress network-bearing signals.

    A deleted selection becomes no selection, not another account/appointment.
    Only use with read-only lists/tables, never with a form or chat composer.
    """
    states = []
    for widget in widgets:
        if not isinstance(widget, (QListWidget, QTableWidget)):
            raise TypeError("Only read-only list and table views may be refreshed")
        row = widget.currentRow()
        item = widget.item(row) if isinstance(widget, QListWidget) else widget.item(row, 0)
        states.append(
            (
                widget,
                _item_key(item),
                widget.verticalScrollBar().value(),
                widget.horizontalScrollBar().value(),
                widget.blockSignals(True),
            )
        )
    try:
        yield
    finally:
        for widget, key, vertical, horizontal, blocked in states:
            widget.blockSignals(True)
            count = widget.count() if isinstance(widget, QListWidget) else widget.rowCount()
            selected = -1
            if key is not None:
                for row in range(count):
                    item = (
                        widget.item(row) if isinstance(widget, QListWidget) else widget.item(row, 0)
                    )
                    if _item_key(item) == key:
                        selected = row
                        break
            widget.clearSelection()
            if isinstance(widget, QListWidget):
                widget.setCurrentRow(selected)
            else:
                widget.setCurrentCell(selected, 0 if selected >= 0 else -1)
                if selected >= 0:
                    widget.selectRow(selected)
            widget.verticalScrollBar().setValue(vertical)
            widget.horizontalScrollBar().setValue(horizontal)
            widget.blockSignals(blocked)


def _owned(rows, field, actor):
    return type(rows) is list and all(
        type(row) is dict and type(row.get(field)) is int and row[field] == actor for row in rows
    )


def _scoped(resources, role, actor):
    if role == "member":
        if resources["profile"].get("user_id") != actor:
            return False
        for name in ("life_records", "reminders"):
            if not _owned(resources[name], "user_id", actor):
                return False
        return _owned(resources["appointments"], "member_user_id", actor)
    if role == "advisor":
        return _owned(resources["members"], "advisor_user_id", actor) and _owned(
            resources["appointments"], "advisor_user_id", actor
        )
    return role == "operator"


def refresh_current_workspace(window, snapshot) -> tuple[str, ...]:
    """Refresh the visible business view from the current authorized mirror.

    Call for every ``snapshot_changed`` emission, before any revision early
    return, so closing a modal or changing page can receive the same revision.
    No RPC, weather request, dialog, chat change, navigation or form write occurs.
    Authorization loss is handled by MainWindow's existing logout signal.
    """
    sync = getattr(window, "cloud_sync_service", None)
    user = getattr(window, "_current_user", None)
    workspace = getattr(window, "_active_workspace", None)
    if (
        not getattr(window, "cloud_mode", False)
        or sync is None
        or user is None
        or workspace is None
    ):
        return ()
    if QApplication.activeModalWidget() is not None:
        return ()
    if getattr(workspace, "current_user", None) is None or workspace.current_user.id != user.id:
        return ()
    pages = getattr(window, "pages", None)
    if pages is not None and pages.currentWidget() is not workspace:
        return ()
    apply = getattr(workspace, "refresh_snapshot", None)
    if not callable(apply):
        return ()
    try:
        if not sync.client.is_authenticated or sync.authorization_blocked:
            return ()
        generation = sync.cache_generation
        current = sync.authorized_snapshot(user.id, sync.client.base_url)
        if any(
            snapshot.get(key) != current.get(key)
            for key in ("actor_id", "server_instance_id", "revision", "complete")
        ):
            return ()
        # Never render the caller's arbitrary resources, even if its identity
        # fields match. The mirror supplies the validated, current data.
        if not _scoped(current["resources"], user.role_code, user.id):
            return ()
        if not sync.snapshot_generation_is_current(generation):
            return ()
        return tuple(apply(current["resources"]))
    except (CloudAPIError, SyncError, AttributeError, KeyError, TypeError, ValueError):
        # Periodic refresh must not surface a modal, nor fall back to the network.
        return ()


__all__ = ["preserve_views", "refresh_current_workspace"]
