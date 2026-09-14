"""DPAPI-protected pending writes stored beside the snapshot in its SQLite mirror.

The original encrypted file outbox is imported once and kept as a recovery copy.
Only an acknowledged operation is removed. No token or password belongs here.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QLockFile

from ..security.private_payload import _safe_path, protect_payload
from .cloud_client import CloudAPIError
from .offline_outbox import OfflineOutbox


class _PrivateQueueLock:
    """Serialize all read-modify-write operations across threads and app processes."""

    def __init__(self, path):
        self.path = Path(path)
        self.thread_lock = threading.RLock()
        self.depth = threading.local()
        self.file_lock = None

    def __enter__(self):
        self.thread_lock.acquire()
        count = getattr(self.depth, "count", 0)
        try:
            if count == 0:
                path = _safe_path(self.path)
                path.parent.mkdir(parents=True, exist_ok=True)
                _safe_path(path)
                lock = QLockFile(str(path))
                lock.setStaleLockTime(0)
                if not lock.tryLock(3000):
                    raise CloudAPIError("unavailable")
                self.file_lock = lock
            self.depth.count = count + 1
            return self
        except Exception:
            self.thread_lock.release()
            raise

    def __exit__(self, *_):
        self.depth.count -= 1
        if self.depth.count == 0:
            self.file_lock.unlock()
            self.file_lock = None
        self.thread_lock.release()


class SqliteOfflineOutbox(OfflineOutbox):
    def __init__(self, cache_root):
        self.cache_root = _safe_path(Path(cache_root))
        super().__init__(self.cache_root / "outbox")
        self._lock = _PrivateQueueLock(self.root / "pending-writes.lock")

    def database_path(self, identity):
        return _safe_path(self.cache_root / f"cloud-{identity.cache_key}.sqlite")

    @staticmethod
    def _identity(identity):
        return [identity.endpoint.rstrip("/"), identity.server_instance_id, identity.actor_id]

    def _encrypt(self, identity, rows):
        raw = json.dumps({"identity": self._identity(identity), "operations": rows},
                         ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        return "dpapi-v1:" + base64.b64encode(protect_payload(
            raw, "outbox/" + identity.cache_key)).decode("ascii")

    def _connect(self, identity):
        path = self.database_path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite's transient journal stores ciphertext only. Reject existing aliases.
        for suffix in ("", "-journal", "-wal", "-shm"):
            _safe_path(Path(str(path) + suffix))
        connection = sqlite3.connect(path, timeout=5)
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("CREATE TABLE IF NOT EXISTS offline_intents "
                           "(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                           "payload TEXT NOT NULL)")
        return connection

    def _load(self, identity):
        with closing(self._connect(identity)) as connection, connection:
            row = connection.execute(
                "SELECT payload FROM offline_intents WHERE singleton=1").fetchone()
            if row is None:
                # The row remains present even when empty, so an acknowledged
                # legacy operation cannot be re-imported from the retained copy.
                operations = super()._load(identity)
                connection.execute("INSERT INTO offline_intents VALUES(1,?)",
                                   (self._encrypt(identity, operations),))
                return operations
        try:
            if not row[0].startswith("dpapi-v1:"):
                raise ValueError
            value = json.loads(protect_payload(
                base64.b64decode(row[0][9:], validate=True),
                "outbox/" + identity.cache_key, decrypt=True))
            if value.get("identity") != self._identity(identity):
                raise ValueError
            if type(value.get("operations")) is not list:
                raise ValueError
            return value["operations"]
        except Exception:
            raise CloudAPIError("protocol") from None

    def _save(self, identity, operations):
        ciphertext = self._encrypt(identity, operations)
        with closing(self._connect(identity)) as connection, connection:
            connection.execute("INSERT INTO offline_intents VALUES(1,?) "
                               "ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload",
                               (ciphertext,))
