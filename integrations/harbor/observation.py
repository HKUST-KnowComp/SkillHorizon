"""Non-decision hooks for the pinned Terminus agent; no alternate actor loop."""

from contextlib import contextmanager
import json
import time
import uuid


REQUEST_HEADER = "X-SkillHorizon-Request-ID"


def content_tokens(value):
    import tiktoken
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=()))


class ObservationMixin:
    def __init__(self, *args, observe=True, origin_manifest=None, **kwargs):
        self._origin_manifest = origin_manifest
        self._observe = observe
        self._events = []
        self._observation_failed = False
        self._actor_content_tokens = 0
        self._terminal_content_tokens = 0
        self._query_depth = 0
        super().__init__(*args, **kwargs)

    def _tokens(self, value):
        try:
            return content_tokens(value)
        except Exception:
            self._observation_failed = True
            return None

    def _event(self, kind, **fields):
        if not self._observe:
            return
        try:
            event = {"sequence": len(self._events) + 1, "kind": kind, **fields}
            self._events.append(event)
            directory = self.logs_dir.parent / "controller_observation"
            directory.mkdir(mode=0o700, exist_ok=True)
            with (directory / "events.jsonl").open("a") as handle:
                handle.write(json.dumps(event) + "\n")
        except Exception:
            # An instrumentation failure must not silently become a model failure.
            self._observation_failed = True

    @contextmanager
    def _request_scope(self):
        previous = self._llm_call_kwargs
        request_id = uuid.uuid4().hex
        self._llm_call_kwargs = {**previous, "timeout": 900,
            "extra_headers": {**previous.get("extra_headers", {}), REQUEST_HEADER: request_id}}
        try:
            yield request_id
        finally:
            self._llm_call_kwargs = previous

    async def _query_llm(self, chat, prompt, original_instruction="", session=None):
        start = time.monotonic()
        with self._request_scope() as request_id:
            if self._observe:
                self._event("actor_query_start", request_id=request_id,
                    active_content_tokens=self._tokens([*chat.messages, {"role": "user", "content": prompt}]),
                    active_messages=len(chat.messages) + 1)
            self._query_depth += 1
            try:
                result = await super()._query_llm(chat, prompt, original_instruction, session)
            except BaseException as error:
                self._event("actor_query_error", request_id=request_id,
                    error_type=type(error).__name__, seconds=time.monotonic() - start)
                raise
            finally:
                self._query_depth -= 1
            if self._observe:
                # Native truncated-output recovery recursively calls this hook.
                count = self._tokens(result.content) if self._query_depth == 0 else None
                if count is not None:
                    self._actor_content_tokens += count
                self._event("actor_query_end", request_id=request_id,
                    accepted_result_content_tokens=count, recursive_return=self._query_depth > 0,
                    seconds=time.monotonic() - start)
            return result

    async def _run_subagent(self, *args, **kwargs):
        with self._request_scope() as request_id:
            self._event("compact_provider_start", request_id=request_id)
            try:
                result = await super()._run_subagent(*args, **kwargs)
            except BaseException as error:
                self._event("compact_provider_error", request_id=request_id, error_type=type(error).__name__)
                raise
            self._event("compact_provider_end", request_id=request_id)
            return result

    async def _summarize(self, chat, original_instruction, session):
        start = time.monotonic()
        if self._observe:
            self._event("compact_start", active_content_tokens=self._tokens(chat.messages),
                        active_messages=len(chat.messages))
        try:
            result = await super()._summarize(chat, original_instruction, session)
        except BaseException as error:
            self._event("compact_error", error_type=type(error).__name__)
            raise
        if self._observe:
            handoff, references = result
            self._event("compact_end", active_content_tokens=self._tokens(chat.messages),
                handoff_content_tokens=self._tokens(handoff),
                subagent_trajectories=len(references or []), seconds=time.monotonic() - start)
        return result

    def _limit_output_length(self, output, max_bytes=10000):
        result = super()._limit_output_length(output, max_bytes)
        if self._observe:
            count = self._tokens(result)
            if count is not None:
                self._terminal_content_tokens += count
            self._event("terminal_output", raw_bytes=len(output.encode()),
                visible_bytes=len(result.encode()), truncated=result != output,
                visible_content_tokens=count)
        return result

    async def _execute_commands(self, commands, session):
        self._event("command_batch_start", command_count=len(commands))
        try:
            result = await super()._execute_commands(commands, session)
        except BaseException as error:
            self._event("command_batch_error", error_type=type(error).__name__)
            raise
        self._event("command_batch_end", timed_out=bool(result[0]))
        return result

    async def run(self, instruction, environment, context):
        from .native_origins import scope
        objects = {"agent": self, "environment": environment, "parser": getattr(self, "_parser", None)}
        with scope(self._origin_manifest, self.logs_dir.parent, "observer", objects=objects):
            return await self._observed_run(instruction, environment, context)

    async def _observed_run(self, instruction, environment, context):
        try:
            return await super().run(instruction, environment, context)
        finally:
            if self._observe:
                try:
                    context.metadata = {**(context.metadata or {}), "skillhorizon_observation": {
                        "events": len(self._events), "observation_failed": self._observation_failed,
                        "token_measure": "o200k_base content estimate, not provider input or reference L_ref",
                        "accepted_actor_result_content_tokens": self._actor_content_tokens,
                        "terminal_observation_content_tokens": self._terminal_content_tokens,
                        "excludes_truncated_actor_content_and_copied_compact": True,
                        "script_internal_reads": "unobserved",
                        "provider_attempts_source": "controller ledger"}}
                except Exception:
                    self._observation_failed = True
