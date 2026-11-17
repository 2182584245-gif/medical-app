"""Credential-free, bounded direct HTTPS probes and local network adapter labels.

An active adapter is not proof of Internet access; the backend probe is separate.
No IP-geolocation vendor, SSID upload, account, token or health data is involved.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal
from PySide6.QtNetwork import QNetworkInterface

from ..config import DEFAULT_ALIYUN_ENDPOINT
from ..workers.task import FunctionTask
from .network_probe import probe_backend


def local_network_description() -> str:
    labels = []
    try:
        for interface in QNetworkInterface.allInterfaces():
            flags = interface.flags()
            if (not flags & QNetworkInterface.InterfaceFlag.IsUp
                    or not flags & QNetworkInterface.InterfaceFlag.IsRunning
                    or flags & QNetworkInterface.InterfaceFlag.IsLoopBack):
                continue
            if not interface.addressEntries():
                continue
            kind = interface.type().name
            label = {"Wifi": "无线网络", "Ethernet": "有线网络", "Virtual": "虚拟网络"}.get(
                kind, "网络适配器")
            # Adapter description stays in this device's UI, never sent to a server.
            name = " ".join(interface.humanReadableName().split())[:48]
            labels.append(f"{label} · {name}")
    except Exception:
        return "系统网络信息暂不可用"
    return " / ".join(labels[:3]) or "未发现活动网络适配器"




class NetworkStatusMonitor(QObject):
    changed = Signal(dict)

    def __init__(self, endpoint: str = "", parent=None, *, probe=probe_backend, interval_ms=5000):
        super().__init__(parent)
        self.endpoint = endpoint
        self._probe = probe
        self._generation = 0
        self._task = None
        self._running = False
        self._network_label = "等待检测"
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.check_now)

    def start(self):
        if self._running:
            return
        self._running = True
        self.timer.start()
        self.check_now()

    def stop(self):
        self.timer.stop()
        self._running = False
        self._generation += 1

    def check_now(self):
        if not self._running or self._task is not None:
            return
        generation = self._generation
        self._network_label = local_network_description()
        task = FunctionTask(self._probe, self.endpoint)
        self._task = task
        task.signals.result.connect(lambda value: self._accept(generation, value))
        task.signals.error.connect(lambda _: self._accept(generation, {
            "state": "unknown", "latency_ms": None, "checked_at": datetime.now(UTC).isoformat()}))
        task.signals.finished.connect(lambda: self._finished(task))
        QThreadPool.globalInstance().start(task)

    def _accept(self, generation, value):
        if generation != self._generation or not self._running:
            return
        region = ("中国·北京（阿里云服务节点）"
                  if self.endpoint.rstrip("/") == DEFAULT_ALIYUN_ENDPOINT else
                  "仅本机" if not self.endpoint else "自定义服务（属地未验证）")
        self.changed.emit({**value, "network": self._network_label,
                           "service_location": region,
                           "host": urlsplit(self.endpoint).hostname or "本地"})

    def _finished(self, task):
        if self._task is task:
            self._task = None