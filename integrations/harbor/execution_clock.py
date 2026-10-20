"""Task execution time, excluding measured infrastructure and postprocessing.

The child owns the clock; its supervisor reads the same small atomic snapshot.
HTTP time is provisional until its outcome is known. A request clipped by the
task budget is charged on timeout, rather than refunded as a provider failure.
"""

from contextlib import contextmanager
import json
import math
from pathlib import Path
import subprocess
import time


MODE = 'execution_excluding_infrastructure_v1'
LEGACY = 'wall_clock_v1'
INSTRUCTIONS = ('Execution time includes initialization, normal model requests, tools, '
    'model error recovery and compaction. Scheduler queueing, confirmed API/transport '
    'failures and their backoff, manual downtime, and final scoring/statistics/cleanup '
    'do not consume execution time. B and recorded usage are unchanged.')


def public_limit(limit, mode=LEGACY):
    if limit is None:
        return ('There is no cumulative task time limit. Execution time is still recorded. '
                'The remaining B, response recovery limits, individual request timeouts and '
                'infrastructure retry limits still apply. ')
    if mode == MODE:
        return f'Task execution limit: {limit:g} seconds. ' + INSTRUCTIONS + ' '
    return f'Task wall limit: {limit:g} seconds including API waits, retries, compaction and tools. '


def infrastructure_failure(result, *, budget_clipped=False):
    code = (result.diagnostic or {}).get('code')
    if code == 'timeout' and budget_clipped:
        return False
    return (result.sent is False or result.status in {401, 403, 408, 429, 500, 502, 503, 504}
            or (result.diagnostic or {}).get('layer') == 'transport' and code in {
                'connection_error', 'disconnected', 'timeout', 'certificate_error',
                'configuration_error', 'worker_error', 'queue_deadline'})


class ExecutionClock:
    def __init__(self, directory, limit, *, started=None, prior_seconds=0, now=time.monotonic):
        if (limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float))
                or not math.isfinite(limit) or limit <= 0)
                or isinstance(prior_seconds, bool) or not isinstance(prior_seconds, (int, float))
                or not math.isfinite(prior_seconds) or prior_seconds < 0):
            raise ValueError('Invalid execution clock limit or prior time')
        self.now, self.limit = now, limit
        self.started = now() if started is None else started
        self.prior_seconds = prior_seconds
        self.charged = prior_seconds
        self.phase_started = self.started
        self.phase = 'execution'
        self.guard_deadline = None
        self.excluded = {}
        self.finished = False
        self.path = Path(directory) / 'EXECUTION_CLOCK.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write()

    def elapsed(self):
        return self.charged + (self.now() - self.phase_started if self.phase == 'execution' else 0)

    def remaining(self):
        # Infinity is only used for internal min/comparison; persisted limits stay null.
        return math.inf if self.limit is None else max(0, self.limit - self.elapsed())

    def snapshot(self):
        return dict(time_accounting=MODE, execution_seconds=self.elapsed(),
            prior_execution_seconds=self.prior_seconds, execution_limit_seconds=self.limit,
            segment_started_monotonic=self.started,
            segment_wall_seconds=max(0, self.now() - self.started),
            excluded_seconds=dict(self.excluded), phase=self.phase,
            phase_started_monotonic=self.phase_started, guard_deadline=self.guard_deadline,
            charged_seconds=self.charged, finished=self.finished,
            post_execution_seconds=max(0, self.now()-self.phase_started) if self.finished else 0,
            unsettled_seconds=max(0, self.now()-self.phase_started) if self.phase == 'provider_pending' else 0)

    def _write(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.snapshot(), sort_keys=True) + '\n')
        tmp.replace(self.path)

    @contextmanager
    def excluded_phase(self, reason, *, guard_deadline=None):
        if self.phase != 'execution' or self.finished:
            raise ValueError('Overlapping execution clock phases')
        self.charged = self.elapsed()
        self.phase_started = self.now()
        self.phase, self.guard_deadline = reason, guard_deadline
        self._write()
        outcome = {'charge': False, 'reason': reason}
        try:
            yield outcome
        finally:
            duration = max(0, self.now() - self.phase_started)
            if outcome['charge']:
                self.charged += duration
            else:
                key = outcome['reason']
                self.excluded[key] = self.excluded.get(key, 0) + duration
            self.phase_started = self.now()
            self.phase, self.guard_deadline = 'execution', None
            self._write()

    def refresh_queue_guard(self, deadline):
        """Only an actively polled queue may extend its infrastructure watchdog."""
        if self.phase != 'scheduler_queue' or self.finished:
            raise ValueError('Queue heartbeat outside scheduler queue')
        self.guard_deadline = deadline
        self._write()

    def finish(self):
        if not self.finished:
            if self.phase != 'execution':
                raise ValueError('Unsettled execution clock phase')
            self.charged = self.elapsed()
            self.phase_started = self.now()
            self.phase, self.finished = 'post_execution', True
            self._write()
        return self.snapshot()


def elapsed_from_snapshot(state, now=None):
    now = time.monotonic() if now is None else now
    return state['charged_seconds'] + (
        max(0, now - state['phase_started_monotonic']) if state['phase'] == 'execution' else 0)


def wait_for_worker(process, key, directory, *, limit, began, prior=0, mode=LEGACY,
                    infrastructure_timeout=900, queue_heartbeat_path=None):
    """The task deadline never kills excluded waiting or final scoring as wall_time."""
    if queue_heartbeat_path is not None and mode == LEGACY and limit is not None:
        raise ValueError('Queue exclusion is incompatible with legacy finite wall supervision')
    if mode == LEGACY and limit is not None:
        return process.communicate(input=key, timeout=max(0, limit-prior-(time.monotonic()-began)))
    if mode not in {LEGACY, MODE}:
        raise ValueError('Unknown time accounting mode')
    path = Path(directory) / 'EXECUTION_CLOCK.json'
    first = True
    while True:
        now = time.monotonic()
        state = json.loads(path.read_text()) if path.exists() else None
        # No-clock direct callers must explicitly supply this worker-owned path.
        # A stale queue heartbeat is an infrastructure fault, never a task timeout.
        if queue_heartbeat_path is not None:
            heartbeat_path = Path(queue_heartbeat_path)
            heartbeat = json.loads(heartbeat_path.read_text()) if heartbeat_path.exists() else None
            if heartbeat and heartbeat.get('phase') == 'queue':
                if (heartbeat.get('mode') != 'queue_wait_excluded_v1'
                        or heartbeat.get('pid') != process.pid
                        or now >= heartbeat['guard_deadline']):
                    raise RuntimeError('Stale queue heartbeat; infrastructure interruption')
        elapsed = elapsed_from_snapshot(state, now) if state else prior + now-began
        if state and state['finished']:
            if now-state['phase_started_monotonic'] >= infrastructure_timeout:
                raise RuntimeError('Post-execution processing timeout; not a model time limit')
        elif limit is not None and elapsed >= limit:
            raise subprocess.TimeoutExpired(process.args, limit)
        elif state and state.get('guard_deadline') is not None and now >= state['guard_deadline']:
            raise RuntimeError('Infrastructure operation timeout; not a model time limit')
        try:
            return process.communicate(input=key if first else None, timeout=.2)
        except subprocess.TimeoutExpired:
            first = False


def worker_elapsed(directory, began, prior=0):
    path = Path(directory) / 'EXECUTION_CLOCK.json'
    if path.exists():
        return elapsed_from_snapshot(json.loads(path.read_text()))
    return prior + time.monotonic()-began


def freeze_worker_clock(directory, elapsed, stopped_at):
    """After killing the child, persist its execution boundary, excluding kill/cleanup."""
    path = Path(directory) / 'EXECUTION_CLOCK.json'
    if not path.exists():
        return
    state = json.loads(path.read_text())
    state.update(charged_seconds=elapsed, execution_seconds=elapsed, phase='post_execution',
        phase_started_monotonic=stopped_at, finished=True, guard_deadline=None,
        post_execution_seconds=max(0, time.monotonic()-stopped_at), unsettled_seconds=0)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, sort_keys=True)+'\n')
    tmp.replace(path)
