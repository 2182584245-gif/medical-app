"""One explicit distinction between a queued edit and a confirmed cloud write."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

QUEUED_MESSAGE = (
    "已暂存待同步，尚未写入云端。联网后需要重新在线登录并完成同步；"
    "请勿重复提交，可在“待同步事项”查看或处理冲突。"
)


def show_queued_result(result, parent, *, label=None) -> bool:
    # Imported lazily so a local-only installation never initializes offline
    # storage. Do not duck-type IDs or accept a generic dict as a server result.
    try:
        from ..services.offline_outbox import QueuedOperation
    except ImportError:
        return False
    if not isinstance(result, QueuedOperation):
        return False
    if label is not None:
        label.setObjectName("HealthPending")
        label.style().unpolish(label)
        label.style().polish(label)
        label.setText(QUEUED_MESSAGE)
        label.show()
    else:
        QMessageBox.information(parent, "已暂存待同步", QUEUED_MESSAGE)
    return True
