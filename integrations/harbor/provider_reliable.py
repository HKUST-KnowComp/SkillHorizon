"""Version-3 task budget, separate infra costs and bounded request recovery.

No provider SDK or business-write retry loop. Construct with an explicit frozen
ProviderPolicy and a fresh ledger directory; use provider_guard.make_server.
Raw payload/response pairs live only in a bounded process-local cache. A known
request ID without that cache is never resent, including after restart.
"""

from collections import OrderedDict
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import asdict, dataclass
import fcntl
import ipaddress
import math
import os
from pathlib import Path
import re
import time
import urllib.parse

import tiktoken

from . import provider_guard as guard
from .provider_quota import account_outage
from .usage_accounting import experiment_cost_summary


@dataclass(frozen=True)
class ProviderPolicy:
    max_responses: int | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    per_output: int
    max_infra_failures: int
    total_window: int = 480000
    send_timeout: float = 180
    logical_timeout: float = 780
    client_timeout: float = 900
    max_extra_retries: int = 3
    backoffs: tuple = (2, 4, 8)
    max_retry_after: int = 60
    version: int = 3

    def __post_init__(self):
        for value in (self.max_responses, self.max_input_tokens, self.max_output_tokens):
            if value is not None and (not guard._integer(value) or not 0 < value <= guard.MAX_DIAGNOSTIC_INTEGER):
                raise guard.GuardError("INVALID_POLICY")
        for value in (self.max_infra_failures, self.per_output):
            if not guard._integer(value) or not 0 < value <= guard.MAX_DIAGNOSTIC_INTEGER:
                raise guard.GuardError("INVALID_POLICY")
        if (self.per_output > 32768 or type(self.total_window) is not int or self.total_window != 480000
                or type(self.version) is not int or self.version != 3
                or type(self.max_extra_retries) is not int or self.max_extra_retries != 3
                or self.backoffs != (2, 4, 8) or any(type(v) is not int for v in self.backoffs)
                or type(self.max_retry_after) is not int or self.max_retry_after != 60):
            raise guard.GuardError("INVALID_POLICY")
        for value, cap in [(self.send_timeout, 180), (self.logical_timeout, 780), (self.client_timeout, 900)]:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= cap:
                raise guard.GuardError("INVALID_POLICY")
        if not self.send_timeout <= self.logical_timeout < self.client_timeout:
            raise guard.GuardError("INVALID_POLICY")


def _valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) is not None


def response_usage(response, total_tolerance=0):
    """Normalize an explicitly tolerated total discrepancy; preserve raw response."""
    try:
        return guard._usage(response), None
    except guard.DiagnosticError as error:
        if total_tolerance != 1 or error.diagnostic.get("code") != "total_mismatch":
            raise
        reported = response["usage"]
        total = reported["prompt_tokens"] + reported["completion_tokens"]
        delta = reported["total_tokens"] - total
        if abs(delta) != 1:
            raise
        normalized = guard._usage({"usage": {**reported, "total_tokens": total}})
        return normalized, {"field": "total_tokens", "reported": reported["total_tokens"],
                            "normalized": total, "delta": delta}


class ReliableBudgetProxy(guard.BudgetProxy):
    requires_request_id = True
    transient_statuses = frozenset({408, 429, 500, 502, 503, 504})
    locked_states = frozenset({"usage_unknown", "logical_interrupted", "fatal", "retry_exhausted",
                               "logical_deadline", "budget_exhausted", "budget_exceeded", "controller_stopped",
                               "infra_budget_exhausted"})

    def __init__(self, state_dir, *, model, upstream_url, api_key, policy,
                 testing_only=False, cache_limit_bytes=64 * 1024 * 1024, traffic=None,
                 usage_total_tolerance=0, retry_connection_errors=False,
                 retry_output_limit=False, resume_request_id=None, output_limit_retry_first=False,
                 output_limit_feedback=False, retry_chat_invalid=False, queue_wait_mode=None):
        from .request_clock import MODE
        if queue_wait_mode not in (None, MODE):
            raise guard.GuardError('INVALID_POLICY')
        self.queue_wait_mode = queue_wait_mode
        if type(retry_chat_invalid) is not bool:
            raise guard.GuardError("INVALID_POLICY")
        self.retry_chat_invalid = retry_chat_invalid
        if type(output_limit_feedback) is not bool:
            raise guard.GuardError("INVALID_POLICY")
        if output_limit_feedback and (retry_output_limit or output_limit_retry_first):
            raise guard.GuardError("INVALID_POLICY")
        self.output_limit_feedback = output_limit_feedback
        if type(retry_connection_errors) is not bool:
            raise guard.GuardError("INVALID_POLICY")
        self.retry_connection_errors = retry_connection_errors
        if type(retry_output_limit) is not bool or (resume_request_id is not None and
                (not retry_output_limit or not _valid_id(resume_request_id))):
            raise guard.GuardError("INVALID_POLICY")
        self.retry_output_limit = retry_output_limit
        if type(output_limit_retry_first) is not bool or (output_limit_retry_first and not retry_output_limit):
            raise guard.GuardError("INVALID_POLICY")
        self.output_limit_retry_first = output_limit_retry_first
        self._resume_request_id = resume_request_id
        if type(usage_total_tolerance) is not int or usage_total_tolerance not in (0, 1):
            raise guard.GuardError("INVALID_POLICY")
        self.usage_total_tolerance = usage_total_tolerance
        if not isinstance(policy, ProviderPolicy):
            raise guard.GuardError("INVALID_POLICY")
        if not isinstance(model, str) or not model or not isinstance(api_key, str) or not api_key or any(c in api_key for c in "\r\n"):
            raise guard.GuardError("INVALID_CONFIGURATION")
        try:
            if type(testing_only) is not bool:
                raise ValueError("target")
            if testing_only:
                parts = urllib.parse.urlsplit(upstream_url)
                if (parts.scheme != "http" or not ipaddress.ip_address(parts.hostname).is_loopback
                        or parts.username or parts.password or parts.query or parts.fragment
                        or parts.path != "/v1/chat/completions" or upstream_url.strip() != upstream_url
                        or parts.port is None or not 0 < parts.port < 65536):
                    raise ValueError("target")
            elif (upstream_url != guard.PRODUCTION_URL or
                  (policy.send_timeout, policy.logical_timeout, policy.client_timeout) != (180, 780, 900)):
                raise ValueError("production")
        except Exception:
            raise guard.GuardError("INVALID_CONFIGURATION") from None
        if not guard._integer(cache_limit_bytes) or not 0 < cache_limit_bytes <= 256 * 1024 * 1024:
            raise guard.GuardError("INVALID_CACHE_LIMIT")
        self.model, self.upstream_url, self._key = model, upstream_url, api_key
        self.config = policy
        self.execution_deadline = None
        self.traffic = traffic
        self.policy = {**asdict(policy), "backoffs": list(policy.backoffs), "model": model,
                       "upstream_url": upstream_url, "reasoning_effort": "medium", "testing_only": testing_only,
                       "encoding": "o200k_base", "overhead": "256+16_per_message"}
        if queue_wait_mode is not None:
            self.policy['queue_wait_mode'] = queue_wait_mode
        if usage_total_tolerance:
            self.policy["usage_total_tolerance"] = usage_total_tolerance
        if retry_connection_errors:
            self.policy["retry_connection_errors"] = True
        if retry_output_limit:
            self.policy["retry_output_limit"] = True
        if output_limit_retry_first:
            self.policy["output_limit_retry_first"] = True
        if output_limit_feedback:
            self.policy["output_limit_feedback"] = 'strict_total_8192_feedback_v1'
        if retry_chat_invalid:
            self.policy["retry_chat_invalid"] = True
        self._request_output_limit = policy.per_output
        self.encoding = tiktoken.get_encoding("o200k_base")
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._cache_limit = cache_limit_bytes
        self.directory = Path(state_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "state.json"
        with self._lock():
            if not self.path.exists():
                guard._write(self.path, {"version": 3, "policy": self.policy, "attempts": 0,
                    "local_rejections": 0, "estimated_input_tokens": 0, "reserved_input_tokens": 0,
                    "reserved_output_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                    "pending": None, "locked": None, "requests": {}, "records": []})
            self._load()

    @contextmanager
    def _admission_lock(self, deadline):
        fd = os.open(self.directory / "state.lock", os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            clock = getattr(self, 'execution_clock', None)
            scope = (clock.excluded_phase('scheduler_lock', guard_deadline=deadline + 10)
                     if clock is not None else nullcontext())
            with scope:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError:
                        if self._stop_requested():
                            raise guard.GuardError("CONTROLLER_STOPPED", 503)
                        if time.monotonic() >= deadline:
                            raise guard.GuardError("REQUEST_WAIT_TIMEOUT", 504)
                        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            yield
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self):
        try:
            state = guard._decode(self.path.read_bytes())
            counters = ("attempts", "local_rejections", "estimated_input_tokens", "reserved_input_tokens",
                        "reserved_output_tokens", "prompt_tokens", "completion_tokens", "total_tokens")
            if (type(state["version"]) is not int or state["version"] != 3 or state["policy"] != self.policy
                    or any(not guard._integer(state[k]) for k in counters)
                    or (self.config.max_responses is not None and
                        state["attempts"] > self.config.max_responses + self.config.max_infra_failures)
                    or state["locked"] not in self.locked_states | {None}
                    or not isinstance(state["requests"], dict) or not isinstance(state["records"], list)
                    or len(state["records"]) + (state["pending"] is not None) != state["attempts"]):
                raise ValueError("ledger")
            known = dict(prompt_tokens=0, completion_tokens=0, total_tokens=0)
            reserved_input = reserved_output = estimate = 0
            sends = {rid: 0 for rid in state["requests"]}
            accepted = {rid: 0 for rid in state["requests"]}
            entries = list(state["records"])
            if state["pending"] is not None:
                entries.append(state["pending"])
            for index, record in enumerate(entries, 1):
                if (not guard._integer(record["attempt"]) or record["attempt"] != index
                        or record["request_id"] not in sends
                        or not guard._integer(record["estimated_input_tokens"])
                        or type(record["max_output_tokens"]) is not int or record["max_output_tokens"] != self.config.per_output):
                    raise ValueError("record")
                sends[record["request_id"]] += 1
                estimate += record["estimated_input_tokens"]
                usage = record.get("usage")
                if record is not state["pending"]:
                    if type(record.get("task_accepted")) is not bool:
                        raise ValueError("missing admission class")
                    if record["task_accepted"]:
                        if record["status"] != 200 or usage is None:
                            raise ValueError("invalid accepted response")
                        accepted[record["request_id"]] += 1
                if usage is None:
                    reserved_input += record["estimated_input_tokens"]
                    reserved_output += record["max_output_tokens"]
                else:
                    normalized = guard._usage({"usage": usage})
                    for key in known:
                        known[key] += normalized[key]
            for rid, info in state["requests"].items():
                send_limit = info.get("authorized_send_limit", 4)
                if (not _valid_id(rid) or info["state"] not in {"active", "complete", "failed"}
                        or not guard._integer(info["sends"]) or info["sends"] != sends[rid]
                        or not guard._integer(send_limit) or not 4 <= send_limit <= max(4, self.config.max_infra_failures + 1)
                        or info["sends"] > send_limit
                        or accepted[rid] != int(info["state"] == "complete")):
                    raise ValueError("logical")
            if (estimate != state["estimated_input_tokens"] or reserved_input != state["reserved_input_tokens"]
                    or reserved_output != state["reserved_output_tokens"] or any(state[k] != v for k, v in known.items())):
                raise ValueError("totals")
        except Exception:
            raise guard.GuardError("STATE_INVALID", 503) from None
        original = state["locked"]
        if state["pending"] is not None:
            state["locked"] = "usage_unknown"
        elif any(info["state"] == "active" for info in state["requests"].values()):
            if state["locked"] is None:
                state["locked"] = "logical_interrupted"
        elif state["locked"] is None:
            if self._infra_exhausted(state):
                state["locked"] = "infra_budget_exhausted"
            elif self._exhausted(state):
                state["locked"] = "budget_exhausted"
            elif self._stop_requested():
                state["locked"] = "controller_stopped"
        if original != state["locked"]:
            guard._write(self.path, state)
        return state

    @staticmethod
    def accounting(state):
        task = dict(responses=0, input_budget_tokens=0, prompt_tokens=0, completion_tokens=0, total_tokens=0)
        infra = dict(failures=0, prompt_tokens=0, completion_tokens=0, total_tokens=0, unknown_usage_attempts=0)
        for record in state["records"]:
            usage = record["usage"]
            if record["task_accepted"]:
                task["responses"] += 1
                task["input_budget_tokens"] += max(record["estimated_input_tokens"], usage["prompt_tokens"])
                target = task
            else:
                infra["failures"] += 1
                infra["unknown_usage_attempts"] += usage is None
                target = infra
            if usage is not None:
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    target[key] += usage[key]
        return {"task": task, "infra": infra,
                "experiment_cost": experiment_cost_summary([{"task": task}]),
                "unknown_cost_exposure": {"input_estimate": state["reserved_input_tokens"],
                    "output_reserve": state["reserved_output_tokens"], "pending": state["pending"] is not None},
                "operational_cost_totals_are_partial": bool(infra["unknown_usage_attempts"] or state["pending"] is not None)}

    def snapshot(self):
        state = super().snapshot()
        return {**state, "accounting": self.accounting(state)}

    def _infra_exhausted(self, state):
        if self.output_limit_feedback:
            # Output corrections have no local/cumulative failure cap. Keep all
            # usage in the ledger, including inherited historical rejections.
            failures = sum(not r['task_accepted'] and
                (r.get('diagnostic') or {}).get('code') != 'output_limit_exceeded'
                for r in state['records'])
            return failures >= self.config.max_infra_failures
        return self.accounting(state)["infra"]["failures"] >= self.config.max_infra_failures

    def _exhausted(self, state):
        task = self.accounting(state)["task"]
        return any(limit is not None and used >= limit for used, limit in (
            (task["responses"], self.config.max_responses),
            (task["input_budget_tokens"], self.config.max_input_tokens),
            (task["completion_tokens"], self.config.max_output_tokens)))

    def _payload(self, value):
        payload, estimate, maximum = super()._payload(value)
        if maximum != self.config.per_output:
            raise ValueError("output policy")
        if estimate + maximum > self.config.total_window:
            raise ValueError("window")
        return payload, estimate, maximum

    def _finish_error(self, state, request_id, code, lock="fatal"):
        state["requests"][request_id].update(state="failed", error=code)
        if state["locked"] is None:
            state["locked"] = lock
        guard._write(self.path, state)
        raise guard.GuardError(code, 503)

    def _cache_response(self, request_id, payload, raw):
        size = len(payload) + len(raw)
        if size > self._cache_limit:
            return
        while self._cache and self._cache_bytes + size > self._cache_limit:
            _, (old_payload, old_raw) = self._cache.popitem(last=False)
            self._cache_bytes -= len(old_payload) + len(old_raw)
        self._cache[request_id] = payload, raw
        self._cache_bytes += size

    def _wait_retry(self, delay, deadline, *, infrastructure=False):
        clock = getattr(self, 'execution_clock', None)
        scope = (clock.excluded_phase('api_backoff', guard_deadline=deadline + 10)
                 if clock is not None and infrastructure else nullcontext())
        with scope:
            return self._wait_retry_impl(delay, deadline)

    def _wait_retry_impl(self, delay, deadline):
        end = time.monotonic() + delay
        if end >= deadline:
            return "LOGICAL_DEADLINE"
        while time.monotonic() < end:
            if self._stop_requested():
                return "CONTROLLER_STOPPED"
            time.sleep(min(0.05, max(0, end - time.monotonic())))
        return "CONTROLLER_STOPPED" if self._stop_requested() else None

    @staticmethod
    def _chat_error(response):
        choices = response.get("choices")
        if not isinstance(choices, list): return "choices_not_list"
        if len(choices) != 1: return "choices_count_not_one"
        if not isinstance(choices[0], dict): return "choice_not_object"
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict): return "message_not_object"
        if message.get("role") != "assistant": return "role_not_assistant"
        if "content" not in message: return "content_missing"
        if message["content"] is not None and not isinstance(message["content"], str): return "content_type_invalid"
        if not isinstance(choice.get("finish_reason"), str) or not choice["finish_reason"]: return "finish_reason_missing_or_invalid"
        if "index" in choice and (type(choice["index"]) is not int or choice["index"] != 0): return "index_invalid"
        return None

    @staticmethod
    def _valid_chat(response):
        return ReliableBudgetProxy._chat_error(response) is None

    def set_execution_deadline(self, deadline):
        if deadline is None:
            self.execution_deadline = None
            return
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise guard.GuardError("INVALID_EXECUTION_DEADLINE")
        self.execution_deadline = deadline if self.execution_deadline is None else min(self.execution_deadline, deadline)

    def _checkpoint_resume_allowed(self, state, request_id):
        """A scoped recovery proxy may admit a separately authorized checkpoint.

        Ordinary requests, including unknown sends, remain non-replayable.
        """
        return False

    def forward(self, value, *, request_id):
        deadline = time.monotonic() + self.config.logical_timeout
        clock = getattr(self, 'execution_clock', None)
        if self.queue_wait_mode and self.execution_deadline is not None:
            raise guard.GuardError('QUEUE_MODE_INCOMPATIBLE_WALL_DEADLINE', 503)
        if self.execution_deadline is not None and clock is None:
            deadline = min(deadline, self.execution_deadline)
        if not _valid_id(request_id):
            self.reject("INVALID_REQUEST_ID")
        try:
            payload, estimate, maximum = self._payload(value)
        except Exception:
            self.reject()
        with self._admission_lock(deadline), ExitStack() as request_scope:
            state = self._load()
            cached = self._cache.get(request_id)
            if cached is not None:
                if cached[0] != payload:
                    state["local_rejections"] += 1
                    guard._write(self.path, state)
                    raise guard.GuardError("REQUEST_ID_CONFLICT", 409)
                return cached[1]
            first_ordinal = 0
            send_limit = 4
            if request_id in state["requests"]:
                previous = state["requests"][request_id]
                send_limit = previous.get("authorized_send_limit", 4)
                last = state["records"][-1] if state["records"] else {}
                resumable = (request_id == self._resume_request_id
                    and previous["state"] == "failed" and previous["error"] == "UPSTREAM_BUDGET_EXCEEDED"
                    and 0 < previous["sends"] < send_limit and last.get("request_id") == request_id
                    and last.get("status") == 200 and not last.get("task_accepted")
                    and (last.get("usage") or {}).get("completion_tokens", 0) > maximum)
                infra_resume = (request_id == getattr(self, 'infra_resume_request_id', None)
                    and previous['state'] == 'failed'
                    and previous['error'] in {'CONTROLLER_STOPPED', 'UNSENT_TRAFFIC_WAIT'}
                    and 0 <= previous['sends'] < send_limit and state['pending'] is None
                    and not any(r.get('task_accepted') for r in state['records']
                                if r['request_id'] == request_id))
                chat_resume = (self.retry_chat_invalid
                    and request_id == getattr(self, 'chat_resume_request_id', None)
                    and previous['state'] == 'failed' and previous['error'] == 'UPSTREAM_CHAT_INVALID'
                    and 0 < previous['sends'] < 4 and state['pending'] is None
                    and last.get('request_id') == request_id and last.get('status') == 200
                    and last.get('usage') is not None
                    and not any(r.get('task_accepted') for r in state['records'] if r['request_id'] == request_id))
                time_resume = (request_id == getattr(self, 'time_resume_request_id', None)
                    and previous['state']=='failed' and previous['error']=='PRIOR_TASK_LIMIT_INTERRUPTED'
                    and 0<previous['sends']<4 and state['pending'] is None
                    and last.get('request_id')==request_id
                    and not any(r.get('task_accepted') for r in state['records'] if r['request_id']==request_id))
                http_resume = (request_id == getattr(self, 'http_resume_request_id', None)
                    and previous['state'] == 'failed' and previous['error'] == 'UPSTREAM_FATAL'
                    and 0 < previous['sends'] < 4 and state['pending'] is None
                    and last.get('request_id') == request_id and last.get('status') == 403
                    and not any(r.get('task_accepted') for r in state['records'] if r['request_id'] == request_id))
                resumable = (resumable or infra_resume or chat_resume or time_resume or http_resume
                             or self._checkpoint_resume_allowed(state, request_id))
                if not resumable:
                    state["local_rejections"] += 1
                    guard._write(self.path, state)
                    raise guard.GuardError("REQUEST_REPLAY_UNAVAILABLE", 409)
                first_ordinal = previous["sends"]
                self._resume_request_id = None
                self.infra_resume_request_id = None
                self.chat_resume_request_id = None
                self.time_resume_request_id = None
                self.http_resume_request_id = None
            if state["locked"]:
                state["local_rejections"] += 1
                guard._write(self.path, state)
                raise guard.GuardError("CONTROLLER_STOPPED" if state["locked"] == "controller_stopped" else "PROVIDER_LOCKED", 503)
            logical_clock = None
            if self.queue_wait_mode:
                from .request_clock import RequestClock
                logical_clock = RequestClock(self.directory, request_id, self.config.logical_timeout,
                                             existing=request_id in state['requests'])
                request_scope.callback(logical_clock.finish)
                deadline = logical_clock.deadline()
            state["requests"][request_id] = {"state": "active", "sends": first_ordinal, "error": None}
            if send_limit > 4:
                state["requests"][request_id]["authorized_send_limit"] = send_limit
            guard._write(self.path, state)
            retry_cycle_start = max(0, send_limit - 4)
            for ordinal in range(first_ordinal, send_limit):
                if logical_clock is not None:
                    deadline = logical_clock.deadline()
                if clock is not None and clock.remaining() <= 0:
                    self._finish_error(state, request_id, 'LOGICAL_DEADLINE', 'logical_deadline')
                if self._stop_requested():
                    self._finish_error(state, request_id, "CONTROLLER_STOPPED", "controller_stopped")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._finish_error(state, request_id, "LOGICAL_DEADLINE", "logical_deadline")
                if self._infra_exhausted(state):
                    self._finish_error(state, request_id, "INFRA_BUDGET_EXCEEDED", "infra_budget_exhausted")
                task = self.accounting(state)["task"]
                if ((self.config.max_responses is not None and task["responses"] >= self.config.max_responses)
                        or (self.config.max_input_tokens is not None and task["input_budget_tokens"] + estimate > self.config.max_input_tokens)
                        or (self.config.max_output_tokens is not None and task["completion_tokens"] + maximum > self.config.max_output_tokens)):
                    state["local_rejections"] += 1
                    self._finish_error(state, request_id, "BUDGET_EXCEEDED", "budget_exhausted")
                pending = None
                def reserve_send():
                    nonlocal pending
                    state["attempts"] += 1
                    if (self.config.max_responses is not None and
                            state["attempts"] > self.config.max_responses + self.config.max_infra_failures):
                        raise guard.GuardError("STATE_INVALID", 503)
                    state["requests"][request_id]["sends"] += 1
                    state["estimated_input_tokens"] += estimate
                    state["reserved_input_tokens"] += estimate
                    state["reserved_output_tokens"] += maximum
                    pending = {"attempt": state["attempts"], "request_id": request_id,
                               "estimated_input_tokens": estimate, "max_output_tokens": maximum,
                               "admitted_at": time.time()}
                    state["pending"] = pending
                    guard._write(self.path, state)
                def cancel_unsent():
                    if pending is not None:
                        state['attempts'] -= 1
                        state['requests'][request_id]['sends'] -= 1
                        state['estimated_input_tokens'] -= estimate
                        state['reserved_input_tokens'] -= estimate
                        state['reserved_output_tokens'] -= maximum
                        state['pending'] = None
                if logical_clock is None:
                    reserve_send()
                remaining = deadline - time.monotonic()
                stopped = self._stop_requested()
                if stopped or remaining <= 0:
                    cancel_unsent()
                    self._finish_error(state, request_id, "CONTROLLER_STOPPED" if stopped else "LOGICAL_DEADLINE",
                                       "controller_stopped" if stopped else "logical_deadline")
                try:
                    def admitted_send():
                        reserve_send()
                        pending['transport_called_at'] = time.time()
                        guard._write(self.path, state)
                    if logical_clock is None:
                        pending['transport_called_at'] = time.time()
                    extra = ({'request_clock': logical_clock, 'on_admitted': admitted_send}
                             if logical_clock is not None else {})
                    result = guard._send(self.upstream_url, self._key, payload,
                                         min(self.config.send_timeout, remaining),
                                         stop_path=self.directory / "STOP", **({"traffic_spec": {
                                             "root": str(self.traffic.root), **self.traffic.policy},
                                             "logical_deadline": deadline}
                                             if self.traffic is not None else {}),
                                         **({'execution_clock': clock, 'logical_deadline': deadline}
                                            if clock is not None and self.traffic is None else
                                            {'execution_clock': clock} if clock is not None else {}), **extra)
                    if not isinstance(result, guard.TransportResult):
                        result = guard.TransportResult(*result)
                except Exception:
                    result = guard.TransportResult(0, b"", guard._diagnostic("transport", "worker_error"),
                        sent=False if logical_clock is not None and pending is None else None)
                if logical_clock is not None:
                    deadline = logical_clock.deadline()
                if result.sent is False:
                    cancel_unsent()
                    code = (result.diagnostic or {}).get('code')
                    if logical_clock is not None:
                        state['requests'][request_id]['pre_send_diagnostic'] = result.diagnostic
                    stopped = code == 'controller_stopped'
                    error = 'CONTROLLER_STOPPED' if stopped else ('LOGICAL_DEADLINE'
                            if code == 'logical_deadline' else 'UNSENT_TRAFFIC_WAIT')
                    self._finish_error(state, request_id, error,
                                       "controller_stopped" if stopped else "logical_deadline")
                if result.sent is None:
                    # The transport did not publish a definitive send marker.
                    # Retrying could duplicate a request that reached the
                    # provider, so preserve the reservation and freeze the
                    # ledger until its external usage is reconciled.
                    diagnostic = result.diagnostic or guard._diagnostic(
                        "transport", "send_state_unknown")
                    record = dict(pending, status=result.status, usage=None,
                                  diagnostic=diagnostic, task_accepted=False,
                                  send_state="unknown",
                                  send_started_at=result.send_started_at,
                                  send_finished_at=time.time(),
                                  retry_after_seconds=result.retry_after_seconds,
                                  retry_after_error=result.retry_after_error)
                    state["records"].append(record)
                    state["pending"] = None
                    guard._write(self.path, state)
                    self._finish_error(state, request_id, "USAGE_UNKNOWN", "usage_unknown")
                diagnostic, usage, decoded = result.diagnostic, None, None
                usage_adjustment = None
                if diagnostic is None and result.status != 200:
                    diagnostic = guard._diagnostic("transport", "non_200_status" if result.status else "transport_unknown")
                if result.status == 200 and diagnostic is None:
                    try:
                        decoded = guard._decode_response(result.body)
                        usage, usage_adjustment = response_usage(decoded, self.usage_total_tolerance)
                    except guard.DiagnosticError as error:
                        diagnostic = error.diagnostic
                    except Exception:
                        diagnostic = guard._diagnostic("decode", "validation_error")
                elif result.status != 200 and result.body:
                    try:
                        usage = guard._usage(guard._decode_response(result.body))
                    except (guard.DiagnosticError, ValueError, TypeError):
                        pass
                if usage is not None:
                    state["reserved_input_tokens"] -= estimate
                    state["reserved_output_tokens"] -= maximum
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        state[key] += usage[key]
                record = dict(pending, status=result.status, usage=usage, diagnostic=diagnostic, task_accepted=False,
                              send_state="attempted" if result.sent is True else "unknown",
                              send_started_at=result.send_started_at,
                              send_finished_at=time.time(),
                              retry_after_seconds=result.retry_after_seconds, retry_after_error=result.retry_after_error)
                if result.status in {400,401,403,404,429,500,502,503,504} and result.body:
                    # Keep a bounded, redacted provider reason, never credentials or
                    # the whole error body. Previous logs lost this distinction.
                    try:
                        body=guard._decode(result.body)
                        error=body.get('error',body) if isinstance(body,dict) else {}
                        if isinstance(error,str):error={'message':error}
                        safe={}
                        for name in ('code','type','message'):
                            value=error.get(name) if isinstance(error,dict) else None
                            if isinstance(value,(str,int)):
                                value=str(value).replace(self._key,'[redacted]')
                                value=re.sub(r'(?i)(?:sk-|bearer\s+)[A-Za-z0-9_.\-]+','[redacted]',value)
                                value=re.sub(r'[A-Za-z0-9_\-]{28,}','[redacted-id]',value)
                                safe[name]=value[:600]
                        if safe:record['provider_error']=safe
                    except (ValueError,TypeError,UnicodeError):
                        pass
                if usage_adjustment is not None:
                    record["usage_adjustment"] = usage_adjustment
                state["records"].append(record)
                state["pending"] = None
                guard._write(self.path, state)
                if account_outage(record):
                    if self.traffic is not None:
                        (self.traffic.root / 'STOP').touch(exist_ok=True)
                    self._finish_error(state, request_id, 'UPSTREAM_FATAL', 'fatal')
                if self.traffic is not None and not result.cooldown_notified:
                    try:
                        self.traffic.observe(result)
                    except Exception:
                        (self.traffic.root / "STOP").touch(exist_ok=True)
                        self._finish_error(state, request_id, "TRAFFIC_STATE_ERROR", "controller_stopped")
                chat_invalid = False
                if (self.retry_chat_invalid and result.status == 200 and diagnostic is None
                        and usage is not None and not self._valid_chat(decoded)):
                    chat_invalid = True
                    diagnostic = record['diagnostic'] = dict(layer='chat', code=self._chat_error(decoded))
                    folder = self.directory / 'rejected_chat_responses'
                    folder.mkdir(exist_ok=True)
                    limit = 262144
                    guard._write(folder / f'{record["attempt"]:06d}.json', dict(
                        request_id=request_id, diagnostic=diagnostic, body_bytes=len(result.body),
                        truncated=len(result.body) > limit,
                        body=result.body[:limit].decode('utf-8', errors='replace')))
                    guard._write(self.path, state)
                output_limit_exceeded = False
                if result.status == 200 and diagnostic is None and usage is not None:
                    output_limit_exceeded = usage["completion_tokens"] > maximum
                    cumulative_exceeded = (
                            (self.config.max_input_tokens is not None and task["input_budget_tokens"] + max(estimate, usage["prompt_tokens"]) > self.config.max_input_tokens)
                            or (self.config.max_output_tokens is not None and task["completion_tokens"] + usage["completion_tokens"] > self.config.max_output_tokens))
                    if output_limit_exceeded and self.output_limit_feedback and not cumulative_exceeded:
                        if not self._valid_chat(decoded):
                            self._finish_error(state, request_id, "UPSTREAM_CHAT_INVALID")
                        from .output_recovery import OutputLimitFeedback
                        (self.directory / 'rejected_responses').mkdir(exist_ok=True)
                        guard._write(self.directory / 'rejected_responses' / f'{record["attempt"]:06d}.json', decoded)
                        record['diagnostic'] = guard._diagnostic('budget', 'output_limit_exceeded')
                        record['recovery'] = 'public_feedback_no_blind_retry'
                        state['requests'][request_id].update(state='failed', error='OUTPUT_LIMIT_FEEDBACK')
                        guard._write(self.path, state)
                        raise OutputLimitFeedback(result.body, usage, maximum)
                    if output_limit_exceeded or cumulative_exceeded:
                        # Preserve the rejected public model response for diagnosis;
                        # never execute it or relax the task's existing limits.
                        guard._write(self.directory / "rejected_budget_response.json", decoded)
                        (self.directory / "rejected_responses").mkdir(exist_ok=True)
                        guard._write(self.directory / "rejected_responses" / f'{record["attempt"]:06d}.json', decoded)
                        retry_rejected_output = (output_limit_exceeded and self.retry_output_limit
                                                 and self.output_limit_retry_first)
                        if (cumulative_exceeded and not retry_rejected_output) or not self.retry_output_limit:
                            self._finish_error(state, request_id, "UPSTREAM_BUDGET_EXCEEDED", "budget_exceeded")
                        diagnostic = record["diagnostic"] = guard._diagnostic("budget", "output_limit_exceeded")
                        guard._write(self.path, state)
                if result.status == 200 and diagnostic is None and usage is not None:
                    if not self._valid_chat(decoded):
                        self._finish_error(state, request_id, "UPSTREAM_CHAT_INVALID")
                    if time.monotonic() >= deadline or clock is not None and clock.remaining() <= 0:
                        self._finish_error(state, request_id, "LOGICAL_DEADLINE", "logical_deadline")
                    state["requests"][request_id]["state"] = "complete"
                    record["task_accepted"] = True
                    if self._exhausted(state):
                        state["locked"] = "budget_exhausted"
                    elif self._stop_requested():
                        state["locked"] = "controller_stopped"
                    guard._write(self.path, state)
                    self._cache_response(request_id, payload, result.body)
                    return result.body
                if self._stop_requested() or diagnostic and diagnostic.get("code") == "controller_stopped":
                    self._finish_error(state, request_id, "CONTROLLER_STOPPED", "controller_stopped")
                if result.status != 200 and diagnostic and diagnostic.get("code") == "context_length_exceeded":
                    self._finish_error(state, request_id, "CONTEXT_LENGTH_EXCEEDED")
                if diagnostic and diagnostic.get("code") in {"certificate_error", "configuration_error"}:
                    self._finish_error(state, request_id, "UPSTREAM_FATAL")
                if result.status not in {0, 200}:
                    transient = result.status in self.transient_statuses
                else:
                    transient = chat_invalid or output_limit_exceeded or (diagnostic and diagnostic.get("layer") == "transport"
                                 and (diagnostic.get("code") in {"disconnected", "timeout"}
                                      or self.retry_connection_errors
                                      and diagnostic.get("code") == "connection_error"))
                if not transient:
                    self._finish_error(state, request_id, "UPSTREAM_FATAL")
                if result.retry_after_error is not None:
                    self._finish_error(state, request_id, "RETRY_AFTER_REJECTED")
                if (result.retry_after_seconds is not None and
                        (not guard._integer(result.retry_after_seconds) or result.retry_after_seconds > self.config.max_retry_after)):
                    self._finish_error(state, request_id, "RETRY_AFTER_REJECTED")
                if self._infra_exhausted(state):
                    self._finish_error(state, request_id, "INFRA_BUDGET_EXCEEDED", "infra_budget_exhausted")
                if ordinal == send_limit - 1:
                    if chat_invalid:
                        self._finish_error(state, request_id, "CHAT_INVALID_RETRY_EXHAUSTED", "retry_exhausted")
                    if output_limit_exceeded:
                        if self.output_limit_retry_first:
                            self._finish_error(state, request_id, "OUTPUT_LIMIT_RETRY_EXHAUSTED", "retry_exhausted")
                        self._finish_error(state, request_id, "UPSTREAM_BUDGET_EXCEEDED", "budget_exceeded")
                    self._finish_error(state, request_id, "RETRY_EXHAUSTED", "retry_exhausted")
                delay = max(self.config.backoffs[ordinal - retry_cycle_start], result.retry_after_seconds or 0)
                from .execution_clock import infrastructure_failure
                interruption = self._wait_retry(delay, deadline,
                    **({'infrastructure': infrastructure_failure(result)} if clock is not None else {}))
                if interruption:
                    self._finish_error(state, request_id, interruption,
                                       "controller_stopped" if interruption == "CONTROLLER_STOPPED" else "logical_deadline")
