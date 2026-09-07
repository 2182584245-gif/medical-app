"""Run synchronous cloud facades off the GUI thread without freezing repaint/timers.

The local event loop preserves existing synchronous service call signatures.
During the operation all other UI input is blocked by a modal busy dialog;
parent-window closing and nested cloud operations are rejected. This is not an
automatic retry or a cancellable write: interruption cannot prove rollback.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt, QThread, QThreadPool, Slot
from PySide6.QtWidgets import QApplication, QProgressDialog

from .task import FunctionTask

_busy = False


def on_gui_thread() -> bool:
    application = QApplication.instance()
    return application is not None and QThread.currentThread() == application.thread()


class _BridgeState(QObject):
    def __init__(self, loop):
        super().__init__()
        self.loop = loop
        self.result = None
        self.error = None
        self.completed = False

    @Slot(object)
    def succeeded(self, value):
        self.result = value

    @Slot(object)
    def failed(self, value):
        self.error = value

    @Slot()
    def finished(self):
        self.completed = True
        self.loop.quit()

    def eventFilter(self, _watched, event):
        if event.type() == QEvent.Type.Close and not self.completed:
            # Close events start accepted; consuming them alone still allows
            # QWidget.close() to hide the parent on some Qt platforms.
            event.ignore()
            return True
        return False


def run_cloud_operation(function, *args, **kwargs):
    """Return the worker result; propagate only the facade's already-safe errors."""
    global _busy
    if not on_gui_thread():
        return function(*args, **kwargs)
    if _busy:
        from ..services.cloud_client import CloudAPIError
        raise CloudAPIError("limited")
    _busy = True
    parent = QApplication.activeWindow()
    loop = QEventLoop()
    state = _BridgeState(loop)
    dialog = QProgressDialog(
        "正在安全连接云端…\n免费服务首次启动可能需要约 90 秒。\n"
        "请等待结果，不要重复提交；操作不会自动重试。", "", 0, 0, parent,
    )
    dialog.setWindowTitle("云端处理中")
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setCancelButton(None)
    dialog.setMinimumDuration(0)
    dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint
                          | Qt.WindowType.WindowTitleHint)
    dialog.installEventFilter(state)
    if parent is not None:
        parent.installEventFilter(state)
    task = FunctionTask(function, *args, **kwargs)
    task.signals.result.connect(state.succeeded)
    task.signals.error.connect(state.failed)
    task.signals.finished.connect(state.finished)
    try:
        dialog.show()
        QThreadPool.globalInstance().start(task)
        if not state.completed:
            loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        if state.error is not None:
            raise state.error
        return state.result
    finally:
        if parent is not None:
            parent.removeEventFilter(state)
        dialog.removeEventFilter(state)
        dialog.close()
        dialog.deleteLater()
        _busy = False
