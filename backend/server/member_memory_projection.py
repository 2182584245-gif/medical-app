"""Coalesced cloud daily projections, after the business response is committed.

Original business tables remain authoritative. The read API always recomputes a
requested historical day, so a delayed projection never becomes false evidence.
No network/model call or typed user content enters this worker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import deque
import threading
import time
from types import SimpleNamespace

MUTATION_DOMAINS = {
    'health': ('life_records', 'reminders'),
    'expansion': ('life_records', 'goals_habits', 'reading'),
    'service_management': ('services_appointments',),
    'commerce': ('shopping',),
    'chat': ('ai_records',),
    'forum': ('forum',),
    'sync': ('life_records', 'reminders', 'goals_habits', 'reading',
             'shopping', 'services_appointments', 'ai_records', 'forum'),
}
BEIJING = timezone(timedelta(hours=8))


class DailyProjection:
    def __init__(self, auth, memory, *, clock=None, monotonic=None):
        self.auth, self.memory = auth, memory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._owners = {}
        self._ready = deque()
        self._active = 0
        self._closed = False

    def schedule(self, background, principal, domains):
        """Queue committed work; at most two response tasks drain all owners."""
        if principal.user.role_code != 'member':
            return False
        selected = set(domains) & set(MUTATION_DOMAINS['sync']) | (set(domains) & {'operations'})
        if not selected:
            return False
        owner = principal.user.id
        if type(owner) is not int or owner <= 0:
            return False
        token_digest=getattr(principal,'token_digest',None)
        if type(token_digest)is not str or len(token_digest)!=64:
            return False
        with self._lock:
            if self._closed:
                return False
            now = self.monotonic()
            state = self._owners.get(owner)
            if state is None:
                # Retain only the non-secret authenticated digest, never the
                # bearer token, user object or typed contents. The real writer
                # fence can then reject a clear/logout committed during work.
                if len(self._owners) >= 4096:
                    for key, item in list(self._owners.items()):
                        if not item['running'] and not item['queued'] and now - item['last'] > 300:
                            self._owners.pop(key)
                    if len(self._owners) >= 4096:
                        return False
                state = self._owners[owner] = {'last': now, 'running': False,
                    'queued': False, 'pending': set(), 'failed': False,'token_digest':token_digest}
            state['token_digest']=token_digest
            state['pending'].update(selected)
            if not state['running'] and not state['queued']:
                state['queued'] = True
                state['failed'] = False
                self._ready.append(owner)
            workers = min(2 - self._active, len(self._ready))
            self._active += workers
        for _ in range(workers):
            background.add_task(self._drain)
        return True

    def _refresh_owner(self, owner, domains, token_digest):
        # The request already authenticated this ID. Scope each source read to
        # that member again, preserving the authenticated digest so the normal
        # database write fence remains active after reset or logout.
        principal = SimpleNamespace(user=SimpleNamespace(id=owner, role_code='member'), token_digest=token_digest)
        try:
            with self.auth.identity(principal):
                config = self.memory.settings(owner)
                if not config['enabled']:
                    return True
                today = self.clock().astimezone(BEIJING).date()
                for age in range(8):
                    if self._closed:
                        break
                    self.memory.core.refresh_day(owner, (today - timedelta(days=age)).isoformat(), domains)
            return True
        except Exception:
            # Projection failure does not turn a committed business operation
            # into an ambiguous retry. The dated read endpoint repairs it.
            return False

    def _drain(self):
        registered = True
        try:
            while True:
                with self._lock:
                    if self._closed or not self._ready:
                        # Release the reserved slot atomically with the empty
                        # queue decision. Otherwise a new schedule can enqueue
                        # between return and finally, see both workers active,
                        # and leave its accepted work without a drainer.
                        self._active -= 1
                        registered = False
                        return
                    owner = self._ready.popleft()
                    state = self._owners[owner]
                    state['queued'] = False
                    state['running'] = True
                    domains = tuple(sorted(state['pending']))
                    token_digest=state['token_digest']
                    state['pending'].clear()
                completed = self._refresh_owner(owner, domains,token_digest)
                with self._lock:
                    state['running'] = False
                    state['last'] = self.monotonic()
                    if not completed:
                        # No automatic immediate failure loop. A later mutation
                        # for this owner retries the union; dated reads repair
                        # it independently without replaying business actions.
                        state['pending'].update(domains)
                        state['failed'] = True
                    elif state['pending'] and not self._closed:
                        state['queued'] = True
                        self._ready.append(owner)
                    else:
                        self._owners.pop(owner, None)
        finally:
            if registered:
                with self._lock:
                    self._active -= 1

    def close(self):
        with self._lock:
            self._closed = True
            self._ready.clear()

