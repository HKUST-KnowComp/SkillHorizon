"""Opt-in nonqueue logical request time. No authority to grant retries.

An unfinished active interval has an unknown duration after a crash. Reserve its
entire remaining budget durably, then settle actual time on an orderly boundary.
This deliberately fails closed on recovery rather than granting a fresh limit.
"""
from contextlib import contextmanager
import math
import os
from pathlib import Path
import time

from .provider_guard import GuardError, _decode, _write

MODE = 'queue_wait_excluded_v1'
HEARTBEAT_SECONDS = 5
HEARTBEAT_GRACE = 45


class RequestClock:
    def __init__(self, directory, request_id, limit, *, existing=False, now=time.monotonic):
        self.now, self.limit, self.request_id = now, limit, request_id
        self.directory = Path(directory)
        folder = self.directory / 'request_clocks'
        folder.mkdir(exist_ok=True)
        self.path = folder / f'{request_id}.json'
        self.heartbeat_path = self.directory / 'QUEUE_WAIT.json'
        self.elapsed = 0.0
        self.migration = None
        if self.path.exists():
            state = _decode(self.path.read_bytes())
            if (state.get('mode') != MODE or state.get('request_id') != request_id
                    or state.get('limit_seconds') != limit
                    or state.get('phase') not in {'active', 'queue', 'settled'}):
                raise GuardError('REQUEST_CLOCK_INVALID', 503)
            consumed = state.get('nonqueue_seconds')
            reserve = state.get('unsettled_upper_seconds')
            if any(isinstance(x, bool) or not isinstance(x, (int, float))
                   or not math.isfinite(x) or x < 0 for x in (consumed, reserve)):
                raise GuardError('REQUEST_CLOCK_INVALID', 503)
            expected_reserve = max(0, limit-consumed) if state['phase'] == 'active' else 0
            if reserve != expected_reserve or state.get('remaining_seconds') != max(0, limit-consumed):
                raise GuardError('REQUEST_CLOCK_INVALID', 503)
            self.migration = state.get('migration')
            self.elapsed = consumed + reserve
        elif existing:
            # A successor must supply explicit, evidenced migration, never reset.
            raise GuardError('REQUEST_CLOCK_MIGRATION_REQUIRED', 503)
        self.active_since = self.now()
        self.last_heartbeat = -math.inf
        self._save('active')

    def consumed(self):
        return self.elapsed + (max(0, self.now()-self.active_since)
                               if self.active_since is not None else 0)

    def remaining(self):
        return max(0, self.limit-self.consumed())

    def deadline(self):
        return self.now() + self.remaining()

    def _save(self, phase):
        elapsed = self.consumed()
        _write(self.path, dict(mode=MODE, request_id=self.request_id,
            limit_seconds=self.limit, phase=phase, nonqueue_seconds=elapsed,
            remaining_seconds=max(0, self.limit-elapsed),
            unsettled_upper_seconds=max(0, self.limit-elapsed) if phase == 'active' else 0,
            updated_at=time.time(), migration=self.migration))

    @contextmanager
    def queued(self, execution_clock=None):
        self.elapsed = self.consumed()
        self.active_since = None
        self._save('queue')
        def heartbeat(**details):
            now = self.now()
            if now-self.last_heartbeat < HEARTBEAT_SECONDS:
                return
            self.last_heartbeat = now
            _write(self.heartbeat_path, dict(mode=MODE, request_id=self.request_id,
                pid=os.getpid(), phase='queue', heartbeat_monotonic=now,
                guard_deadline=now+HEARTBEAT_GRACE, **details))
            if execution_clock is not None:
                execution_clock.refresh_queue_guard(now+HEARTBEAT_GRACE)
        try:
            heartbeat(reason='traffic_initialization')
            yield heartbeat
        finally:
            self.active_since = self.now()
            self._save('active')
            _write(self.heartbeat_path, dict(mode=MODE, request_id=self.request_id,
                pid=os.getpid(), phase='inactive', heartbeat_monotonic=self.now()))

    def finish(self):
        self.elapsed = self.consumed()
        self.active_since = None
        self._save('settled')
