"""Keep Python cyclic collection of Qt wrappers on the GUI thread.

Python's automatic cyclic collector runs in whichever thread happens to allocate
an object. A now-unreachable QWidget cycle can therefore be finalized by a pool
worker, even though every explicit widget operation was on the GUI thread. Qt
does not permit that. While QApplication is running, replace automatic cyclic
collection with a GUI-owned timer. Normal reference counting is unchanged.

This does not make worker access to widgets safe, nor intercept a third-party
thread that explicitly calls gc.collect(). Application workers must remain
data-only. All application FunctionTasks currently use Qt's global thread pool.
"""

from __future__ import annotations

import gc
import time

from PySide6.QtCore import QObject, QThread, QThreadPool, QTimer, Slot
from PySide6.QtWidgets import QApplication

_ATTRIBUTE = "_medical_gui_gc_guard"


class GuiGarbageCollector(QObject):
    """One QApplication-owned collector, created and stopped on the GUI thread."""

    def __init__(self, app: QApplication) -> None:
        if QThread.currentThread() != app.thread():
            raise RuntimeError("GUI garbage collection must be installed on the GUI thread")
        super().__init__(app)
        self._was_enabled = gc.isenabled()
        self._closed = False
        self._last_full_collection = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.collect_if_due)
        # Do not reenable automatic collection in aboutToQuit: workers may still
        # be draining after Qt's event loop has stopped.
        app.aboutToQuit.connect(self.stop)
        gc.disable()
        self._timer.start()

    def _assert_gui_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Qt wrapper collection must run on the GUI thread")

    @Slot()
    def collect_if_due(self) -> None:
        self._assert_gui_thread()
        if self._closed:
            return
        now = time.monotonic()
        if now - self._last_full_collection >= 30:
            self.collect_now()
        elif gc.get_count()[0] >= max(1, gc.get_threshold()[0]):
            gc.collect(0)

    def collect_now(self) -> int:
        """Full collection at a GUI event boundary; also useful during shutdown."""
        self._assert_gui_thread()
        self._last_full_collection = time.monotonic()
        return gc.collect()

    @Slot()
    def stop(self) -> None:
        self._assert_gui_thread()
        self._timer.stop()

    def shutdown(self, *, wait_ms: int = 3000) -> bool:
        """Restore prior GC policy only after application workers have stopped.

        A timed-out pool deliberately leaves automatic GC disabled for the rest
        of process shutdown. Reenabling it while a late worker is allocating can
        reproduce the very cross-thread finalization this guard prevents.
        """
        self._assert_gui_thread()
        if self._closed:
            return True
        self.stop()
        if not QThreadPool.globalInstance().waitForDone(max(0, wait_ms)):
            return False
        self.collect_now()
        self._closed = True
        if self._was_enabled:
            gc.enable()
        return True


def install_gui_gc() -> GuiGarbageCollector | None:
    """Idempotently protect an existing GUI app; leave CLI/core use unchanged.

    main() installs this before creating windows. FunctionTask also calls it so
    embedding the desktop window (including tests) has the same protection.
    A task constructed on a non-GUI thread must not create a QObject/timer there;
    it can reuse a guard that the application's GUI entrypoint already installed.
    """
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return None
    guard = getattr(app, _ATTRIBUTE, None)
    if guard is not None and not guard._closed:
        return guard
    if QThread.currentThread() != app.thread():
        return None
    guard = GuiGarbageCollector(app)
    setattr(app, _ATTRIBUTE, guard)
    return guard
