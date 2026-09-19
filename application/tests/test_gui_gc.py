"""Subprocess isolation is essential: these tests deliberately change global GC."""

import json
import os
import subprocess
import sys
import textwrap

import pytest


def run_python(body):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("repetition", range(3))
def test_cyclic_widgets_are_destroyed_on_gui_not_allocating_worker(repetition):
    result = run_python("""
        import gc
        import json
        import threading
        import weakref
        from PySide6.QtCore import Qt, QThreadPool, QTimer
        from PySide6.QtWidgets import QApplication, QLabel, QWidget
        from ollama_chat_app.workers.gui_gc import install_gui_gc
        from ollama_chat_app.workers.task import FunctionTask

        app = QApplication([])
        gui_thread = threading.get_ident()
        gc.collect()
        gc.set_threshold(10, 2, 2)
        destroyed_on, finalized_on, worker_on, styles, results = [], [], [], [], []
        gate = threading.Event()

        class CyclicWidget(QWidget):
            def __del__(self):
                finalized_on.append(threading.get_ident())

        def make_cycle():
            widget = CyclicWidget()
            widget.child = QLabel("disposable", widget)
            widget.cycle = widget
            widget.destroyed.connect(
                lambda: destroyed_on.append(threading.get_ident()),
                Qt.ConnectionType.DirectConnection,
            )
            return weakref.ref(widget)

        def allocate():
            worker_on.append(threading.get_ident())
            assert gate.wait(5), "GUI did not repaint"
            for index in range(1000):
                batch = [{"value": [number, index]} for number in range(100)]
                batch.append(batch)
            return True

        task = FunctionTask(allocate)
        guard = install_gui_gc()
        assert not gc.isenabled()
        references = [make_cycle() for _ in range(20)]
        active = QWidget()
        active.label = QLabel("still visible", active)
        active.show()
        style_timer = QTimer()
        style_timer.setInterval(1)

        def change_style():
            active.setStyleSheet("QWidget { background: #%06x; }" % (len(styles) % 32))
            styles.append(threading.get_ident())
            if len(styles) >= 5:
                gate.set()

        def finish(value):
            style_timer.stop()
            guard.collect_now()
            results.append(value)
            app.quit()

        style_timer.timeout.connect(change_style)
        task.signals.result.connect(finish)
        task.signals.error.connect(lambda exc: (print(repr(exc)), app.quit()))
        style_timer.start()
        QTimer.singleShot(0, lambda: QThreadPool.globalInstance().start(task))
        QTimer.singleShot(10000, app.quit)
        app.exec()
        assert QThreadPool.globalInstance().waitForDone(3000)
        guard.collect_now()
        assert guard.shutdown(wait_ms=0)
        print(json.dumps({"gui_thread": gui_thread, "worker_threads": worker_on,
            "destroyed_on": destroyed_on, "finalized_on": finalized_on,
            "live_cycles": sum(ref() is not None for ref in references),
            "styles": len(styles), "result": results, "restored": gc.isenabled()}))
    """)
    assert result["worker_threads"] and result["worker_threads"] != [result["gui_thread"]]
    assert result["destroyed_on"] == [result["gui_thread"]] * 20
    assert result["finalized_on"] == [result["gui_thread"]] * 20
    assert result["live_cycles"] == 0
    assert result["styles"] >= 5
    assert result["result"] == [True]
    assert result["restored"] is True


@pytest.mark.parametrize("app_kind", ["none", "core"])
def test_function_task_without_gui_keeps_original_gc_policy(app_kind):
    result = run_python(f"""
        import gc
        import json
        from PySide6.QtCore import QCoreApplication
        from ollama_chat_app.workers.gui_gc import install_gui_gc
        from ollama_chat_app.workers.task import FunctionTask
        app = QCoreApplication([]) if {app_kind!r} == "core" else None
        gc.enable()
        values = []
        task = FunctionTask(lambda: 42)
        task.signals.result.connect(values.append)
        task.run()
        assert install_gui_gc() is None
        print(json.dumps({{"values": values, "gc_enabled": gc.isenabled()}}))
    """)
    assert result == {"values": [42], "gc_enabled": True}


def test_quit_and_busy_shutdown_never_restore_gc_before_worker_finishes():
    result = run_python("""
        import gc
        import json
        import threading
        from PySide6.QtCore import QThreadPool, QTimer
        from PySide6.QtWidgets import QApplication
        from ollama_chat_app.workers.gui_gc import install_gui_gc
        from ollama_chat_app.workers.task import FunctionTask
        app = QApplication([])
        gc.enable()
        guard = install_gui_gc()
        assert install_gui_gc() is guard
        started, release = threading.Event(), threading.Event()
        def work():
            started.set()
            assert release.wait(10)
        task = FunctionTask(work)
        QThreadPool.globalInstance().start(task)
        assert started.wait(5)
        QTimer.singleShot(0, app.quit)
        app.exec()
        assert not gc.isenabled(), "aboutToQuit must not enable GC"
        assert not guard.shutdown(wait_ms=1)
        assert not gc.isenabled(), "timed-out shutdown must not enable GC"
        release.set()
        assert guard.shutdown(wait_ms=3000)
        assert gc.isenabled()
        assert guard.shutdown(wait_ms=0)
        # Embedders that originally disabled GC retain that policy.
        gc.disable()
        second = install_gui_gc()
        assert second is not guard
        assert second.shutdown(wait_ms=0)
        print(json.dumps({"original_disabled_preserved": not gc.isenabled()}))
    """)
    assert result == {"original_disabled_preserved": True}


def test_gc_collection_rejects_worker_thread_and_main_startup_error_restores_policy():
    result = run_python("""
        import gc
        import json
        import threading
        from PySide6.QtWidgets import QApplication
        from ollama_chat_app.workers.gui_gc import install_gui_gc
        import ollama_chat_app.main as entry
        checks = []
        def fail_start(app):
            guard = install_gui_gc()
            assert not gc.isenabled()
            def worker():
                assert install_gui_gc() is guard
                try:
                    guard.collect_now()
                except RuntimeError:
                    checks.append("rejected")
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
            raise ValueError("test-only startup failure")
        entry._run_application = fail_start
        gc.enable()
        try:
            entry.main()
        except ValueError:
            checks.append("propagated")
        assert QApplication.instance() is not None
        print(json.dumps({"checks": checks, "restored": gc.isenabled()}))
    """)
    assert result == {"checks": ["rejected", "propagated"], "restored": True}


def test_first_install_from_worker_does_not_create_qt_objects_or_change_gc():
    result = run_python("""
        import gc
        import json
        import threading
        from PySide6.QtWidgets import QApplication
        from ollama_chat_app.workers.gui_gc import install_gui_gc
        app = QApplication([])
        children_before = len(app.children())
        gc.enable()
        returned = []
        def work():
            returned.append(install_gui_gc() is None)
            returned.append(gc.isenabled())
        thread = threading.Thread(target=work)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert len(app.children()) == children_before
        assert not hasattr(app, "_medical_gui_gc_guard")
        guard = install_gui_gc()
        assert guard is not None
        assert not gc.isenabled()
        assert guard.shutdown(wait_ms=0)
        print(json.dumps({"returned": returned, "restored": gc.isenabled()}))
    """)
    assert result == {"returned": [True, True], "restored": True}
