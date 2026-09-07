from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError


class AuthCapacityError(RuntimeError):
    """A bounded local authentication budget was exhausted."""


class AuthRateLimiter:
    """One-process guard only; never trusts an HTTP forwarding header itself."""

    def __init__(
        self, per_client: int, global_limit: int, *, max_clients: int = 2048, clock=time.monotonic
    ) -> None:
        self.per_client = per_client
        self.global_limit = global_limit
        self.max_clients = max_clients
        self.clock = clock
        self._clients: dict[str, deque] = {}
        self._global: deque = deque()
        self._lock = threading.Lock()

    def check(self, client: str) -> None:
        now = self.clock()
        cutoff = now - 60
        with self._lock:
            while self._global and self._global[0] <= cutoff:
                self._global.popleft()
            if len(self._global) >= self.global_limit:
                raise AuthCapacityError()
            if client not in self._clients:
                for key in list(self._clients):
                    if self._clients[key][-1] <= cutoff:
                        del self._clients[key]
                if len(self._clients) >= self.max_clients:
                    raise AuthCapacityError()
                self._clients[client] = deque()
            attempts = self._clients[client]
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= self.per_client:
                raise AuthCapacityError()
            attempts.append(now)
            self._global.append(now)


class Security:
    def __init__(self, pepper: str, *, concurrency: int = 2) -> None:
        self._pepper = bytes.fromhex(pepper)
        self._hash_slots = threading.BoundedSemaphore(concurrency)
        self.hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    @contextmanager
    def hash_slot(self):
        if not self._hash_slots.acquire(blocking=False):
            raise AuthCapacityError()
        try:
            yield
        finally:
            self._hash_slots.release()

    def hash_password(self, password: str) -> str:
        with self.hash_slot():
            return self.hasher.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        with self.hash_slot():
            try:
                return self.hasher.verify(password_hash, password)
            except (VerificationError, InvalidHashError):
                return False

    def issue_token(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        return token, self.token_digest(token)

    def token_digest(self, token: str) -> str:
        return hmac.new(self._pepper, token.encode("ascii"), hashlib.sha256).hexdigest()


def request_digest(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
