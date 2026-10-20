"""Cross-process pacing for one pilot's physical provider attempts, not quotas."""

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import random
import time
from uuid import uuid4

from .provider_guard import GuardError, _write


def _process_snapshot(pid):
    """Return Linux process state and start marker when available."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
        # comm may contain spaces and parentheses; the final ')' is the
        # delimiter before state (field 3) and starttime (field 22).
        fields = stat.rsplit(")", 1)[1].split()
        return {"state": fields[0], "start": fields[19]}
    except (FileNotFoundError, OSError, IndexError):
        return None


def _process_start_identity(pid):
    snapshot = _process_snapshot(pid)
    return snapshot["start"] if snapshot is not None else None


def _process_record(pid):
    snapshot = _process_snapshot(pid)
    return {"pid": pid, "start": snapshot["start"] if snapshot else None,
            "state": snapshot["state"] if snapshot else None}


def _process_alive(record):
    try:
        os.kill(record["pid"], 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # We cannot prove that this lease belongs to a dead process.
        return True
    expected = record.get("start")
    snapshot = _process_snapshot(record["pid"])
    # Missing identity data is fail-closed: retain the lease.
    if snapshot is None:
        return True
    if snapshot["state"] == "Z":
        return False
    return expected is None or snapshot["start"] == expected


class SharedTraffic:
    def __init__(self, root, *, interval=3.0, cooldown=30.0, adaptive=False,
                 operation_deadline=None, stopped=None, independent_slots=False,
                 initial_concurrency=8, max_concurrency=32, read_existing=False):
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                   and math.isfinite(x) and x > 0 for x in (interval, cooldown)):
            raise ValueError("Positive pacing settings required")
        self.root = Path(root)
        self.operation_deadline, self.stopped = operation_deadline, stopped
        self.root.mkdir(parents=True, exist_ok=True)
        self.policy = {"interval": interval, "cooldown": cooldown}
        if type(adaptive) is not bool:
            raise ValueError("adaptive must be boolean")
        if adaptive:
            self.policy["adaptive"] = True
        if type(independent_slots) is not bool:
            raise ValueError("independent_slots must be boolean")
        if independent_slots:
            if (type(initial_concurrency) is not int or type(max_concurrency) is not int
                    or not 1 <= initial_concurrency <= max_concurrency <= 32):
                raise ValueError("Invalid concurrency settings")
            self.policy.update(independent_slots=True, initial_concurrency=initial_concurrency,
                               max_concurrency=max_concurrency)
        if read_existing and (self.root / "state.json").exists():
            # state.json is atomically replaced; read-only construction needs no
            # mutating lease lock. Admission still locks and rechecks policy.
            self.read()
            return
        with self.lock():
            if not (self.root / "state.json").exists():
                _write(self.root / "state.json", {"policy": self.policy, "next_start": 0,
                    "cooldown_until": 0, "admissions": 0, "rate_limits": 0})
            self.read()

    def stop(self, reason, **details):
        # Keep the first cause: later cancellation must not overwrite it.
        # Publish a fully written first cause atomically, without reacquiring the
        # traffic lock (callers may already hold it).
        import tempfile
        path = self.root / "STOP_REASON.json"
        with tempfile.NamedTemporaryFile(mode="w", dir=self.root, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(dict(reason=reason, time=time.time(), **details), handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        (self.root / "STOP").touch(exist_ok=True)

    def completed(self, status, *, diagnostic=None, now=None):
        transport_failure = (status in {408, 500, 502, 503, 504}
                             or (diagnostic or {}).get("code") in {"timeout", "disconnected", "connection_error", "worker_error"})
        if not self.policy.get("adaptive") or not (100 <= status <= 599 or transport_failure):
            return
        now = time.time() if now is None else now
        with self.lock(maintenance=True):
            state = self.read()
            recent = [r for r in state.get("recent_responses", []) if now - r["time"] <= 60]
            recent.append({"time": now, "status": status, "transport_failure": transport_failure})
            state["recent_responses"] = recent
            if self.policy.get("independent_slots"):
                from .traffic_adaptation import completed
                completed(self, state, status, transport_failure, now)
                _write(self.root / "state.json", state)
                return
            limited = sum(r["status"] == 429 for r in recent)
            failed = sum(r.get("transport_failure", False) for r in recent)
            unhealthy = failed >= 8 and failed / len(recent) >= .3
            if state.get("concurrency_limit", 32) == 32 and (limited >= 5 and limited / len(recent) >= .2 or unhealthy):
                state.update(concurrency_limit=16, reduced_at=now,
                             reduction_evidence={"responses": len(recent), "http_429": limited,
                                                 "transport_failures": failed})
                _write(self.root / "HEALTH_ALERT.json", {"action": "reduced_to_16", **state["reduction_evidence"]})
            after = [r for r in recent if r["time"] > state.get("reduced_at", now)]
            if (len(after) >= 16 and sum(r.get("transport_failure", False) for r in after) / len(after) >= .5):
                _write(self.root / "HEALTH_ALERT.json", {"action": "circuit_open", "responses": len(after)})
                self.stop("transport_failure_circuit", responses=len(after))
            _write(self.root / "state.json", state)

    @contextmanager
    def admitted(self, deadline, stopped, *, queue_wait_mode=None, heartbeat=None):
        """FIFO admission and pacing; never hold the state lock during network IO."""
        excluded = queue_wait_mode == 'queue_wait_excluded_v1'
        if queue_wait_mode is not None and not excluded or excluded and heartbeat is None:
            raise ValueError('Queue exclusion requires an explicit heartbeat')
        token = uuid4().hex
        # None here retains the ordinary bounded file-lock wait, not an infinite
        # lock. The single FIFO ticket itself has no logical request deadline.
        lock_deadline = None if excluded else deadline
        try:
            with self.lock(lock_deadline, stopped):
                state = self.read()
                state.setdefault("waiting", []).append({"token": token, **_process_record(os.getpid())})
                _write(self.root / "state.json", state)
            while True:
                if stopped() or (self.root / "STOP").exists():
                    raise GuardError("CONTROLLER_STOPPED", 503)
                if not excluded and time.monotonic() >= deadline:
                    raise GuardError("QUEUE_DEADLINE", 504)
                if excluded:
                    heartbeat(token=token, reason='fifo_slot_or_cooldown')
                with self.lock(lock_deadline, stopped):
                    state = self.read()
                    now = time.monotonic()
                    leases = state.setdefault("inflight", {})
                    # A worker can die after reserving its lease but before
                    # publishing RequestStarted. Reclaim only that dead
                    # worker's traffic state; its own provider ledger remains
                    # usage-unknown and therefore cannot be retried.
                    dead_tokens = {token for token, value in leases.items()
                                   if not _process_alive(value)}
                    for dead_token in dead_tokens:
                        leases.pop(dead_token, None)
                    if state.get("launching") in dead_tokens:
                        state["launching"] = None
                    state["waiting"] = [item for item in state["waiting"] if _process_alive(item)]
                    if dead_tokens:
                        _write(self.root / "state.json", state)
                    parallel = self.policy.get("independent_slots", False)
                    limit = state.setdefault("concurrency_limit", self.policy.get("initial_concurrency", 32))
                    if (state["waiting"][0]["token"] == token and len(leases) < limit
                            and (parallel or not state.get("launching"))
                            and now >= max(state["next_start"], state["cooldown_until"])):
                        state["waiting"].pop(0)
                        leases[token] = {**_process_record(os.getpid()), "time": time.time()}
                        if parallel:
                            state["next_start"] = now + self.policy["interval"]
                        else:
                            state["launching"] = token
                        state["admissions"] += 1
                        state["peak_inflight"] = max(state.get("peak_inflight", 0), len(leases))
                        _write(self.root / "state.json", state)
                        break
                time.sleep(.05 if excluded else min(.05, max(0, deadline - time.monotonic())))
            yield token
        finally:
            # Publish before trying the lock: this request has ended even when
            # its owning process remains alive and maintenance times out.
            _write(self.root / f"finished-{token}.json", {"token": token})
            try:
                with self.lock(maintenance=True):
                    pass  # lock() commits all published completions.
            except GuardError as error:
                _write(self.root / "HEALTH_ALERT.json", dict(action="cleanup_deferred",
                    code=error.code, time=time.time()))

    def _reap_finished(self):
        """Under the state lock, reclaim only explicitly finished request tokens."""
        markers = list(self.root.glob("finished-*.json"))
        if not markers:
            return
        tokens = {path.name[len("finished-"):-len(".json")] for path in markers}
        state = self.read()
        state["waiting"] = [item for item in state.get("waiting", []) if item["token"] not in tokens]
        state["inflight"] = {token: lease for token, lease in state.get("inflight", {}).items()
                             if token not in tokens}
        if state.get("launching") in tokens:
            state["launching"] = None
            state["next_start"] = max(state["next_start"], time.monotonic() + self.policy["interval"])
        # Commit first. A crash or failed write must leave completion evidence
        # available for the next holder; replaying a marker is idempotent.
        _write(self.root / "state.json", state)
        for path in markers:
            path.unlink(missing_ok=True)

    def started(self, token):
        with self.lock():
            state = self.read()
            if self.policy.get("independent_slots"):
                if token not in state.get("inflight", {}):
                    raise GuardError("TRAFFIC_ADMISSION_INVALID", 503)
                return
            if state.get("launching") != token:
                raise GuardError("TRAFFIC_ADMISSION_INVALID", 503)
            state["launching"] = None
            state["next_start"] = time.monotonic() + self.policy["interval"]
            _write(self.root / "state.json", state)

    @contextmanager
    def lock(self, deadline=None, stopped=None, *, maintenance=False):
        deadline = time.monotonic() + 2 if deadline is None else deadline
        if not maintenance and self.operation_deadline is not None:
            deadline = min(deadline, self.operation_deadline)
        stopped = None if maintenance else (stopped if stopped is not None else self.stopped)
        fd = os.open(self.root / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if stopped is not None and stopped():
                        raise GuardError("CONTROLLER_STOPPED", 503)
                    if time.monotonic() >= deadline:
                        raise GuardError("TRAFFIC_LOCK_TIMEOUT", 503)
                    time.sleep(min(.01, max(0, deadline - time.monotonic())))
            self._reap_finished()
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read(self):
        value = json.loads((self.root / "state.json").read_text())
        if value["policy"] != self.policy:
            raise ValueError("Shared pacing configuration changed")
        return value

    def observe(self, result):
        if result.status != 429:
            return
        if result.retry_after_error is not None and result.retry_after_seconds is None:
            # An unparseable server cooldown cannot safely be shortened.
            self.stop("invalid_retry_after")
            return
        with self.lock():
            state = self.read()
            state["rate_limits"] += 1
            if self.policy.get("independent_slots"):
                now = time.time()
                if now - state.get("limited_at", 0) >= 10:
                    limit = state.get("concurrency_limit", self.policy["initial_concurrency"])
                    state.update(concurrency_limit=max(1, limit // 2), limited_at=now,
                                 adjusted_at=now, healthy_completions=0)
            # Shared cooldown affects every worker, including compact and retry sends.
            delay = max(self.policy["cooldown"], result.retry_after_seconds or 0)
            state["cooldown_until"] = max(state["cooldown_until"], time.monotonic() + delay + random.uniform(0, 3))
            _write(self.root / "state.json", state)
