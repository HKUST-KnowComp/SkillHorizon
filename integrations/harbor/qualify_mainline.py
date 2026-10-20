"""Actual native Harbor, observation parity and transient retry, no paid calls."""

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

from .backend import write_record
from .provider_guard import make_server
from .provider_reliable import ProviderPolicy, ReliableBudgetProxy
from .qualify_provider import FakePane


class FixtureProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(payload)
        code = self.server.failures.pop(0) if self.server.failures else 200
        if code == 200:
            self.server.accepted.append(payload)
        content = '{"analysis":"Synthetic check","plan":"No actions","commands":[],"task_complete":true}'
        body = json.dumps({"id": "fixture", "object": "chat.completion", "created": 1,
            "model": "gpt-5.6-sol", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
            if code == 200 else {"error": {"message": "synthetic transient", "type": "rate_limit"}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if code != 200:
            self.send_header("Retry-After", "0")
        self.end_headers()
        self.wfile.write(body)


async def exercise(root, api_base, observed):
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    from harbor.agents.factory import AgentFactory
    from harbor.models.trial.config import AgentConfig
    from harbor.llms.chat import Chat
    from .agent import SkillHorizonTerminus, preflight_config
    root.mkdir()
    cls = SkillHorizonTerminus if observed else Terminus2
    origin_options = {"origin_manifest": str(root / "controller-only-origin-fixture.json")} if observed else {}
    options = dict(api_base=api_base, reasoning_effort="medium", max_turns=32,
        llm_call_kwargs={"max_tokens": 8192}, model_info={"max_input_tokens": 471808, "max_output_tokens": 8192},
        session_id="synthetic-mainline-qualification", **origin_options)
    config = AgentConfig(import_path="integrations.harbor.agent:SkillHorizonTerminus", kwargs=options) if observed else AgentConfig(name="terminus-2", kwargs=options)
    if observed:
        preflight_config(config)
    else:
        AgentFactory.run_preflight(config)
    agent = cls(logs_dir=root, model_name="openai/gpt-5.6-sol", **options)
    chat = Chat(agent._llm)
    result = await agent._query_llm(chat, "Synthetic actor check.")
    parsed = agent._parser.parse_response(result.content)
    handoff, references = await agent._summarize(chat, "Synthetic actor check.", FakePane())
    if not handoff or len(references or []) != 3:
        raise AssertionError("Native three-stage compact failed")
    return {"response": result.content, "handoff": handoff,
            "parsed_type": type(parsed).__name__, "events": getattr(agent, "_events", [])}


def normalize(value):
    normalized = {key: item for key, item in value.items() if key != "session_id"}
    normalized.setdefault("n", 1)
    normalized.setdefault("stream", False)
    return normalized


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ["OPENAI_API_KEY"] = "harbor-proxy-dummy"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FixtureProvider)
    upstream.requests, upstream.accepted, upstream.failures = [], [], []
    proxy = ReliableBudgetProxy(root / "provider", model="gpt-5.6-sol",
        upstream_url=f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions",
        api_key="fake-upstream-dummy", testing_only=True,
        policy=ProviderPolicy(max_responses=4, max_infra_failures=12, max_input_tokens=1000000,
                              max_output_tokens=32768, per_output=8192))
    gateway = make_server(proxy)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (upstream, gateway)]
    for thread in threads:
        thread.start()
    try:
        baseline = asyncio.run(exercise(root / "native", f"http://127.0.0.1:{upstream.server_port}/v1", False))
        baseline_payloads = [normalize(x) for x in upstream.accepted]
        upstream.requests, upstream.accepted, upstream.failures = [], [], [429, 503]
        observed = asyncio.run(exercise(root / "observed", f"http://127.0.0.1:{gateway.server_port}/v1", True))
        state = proxy.snapshot()
        parity = baseline_payloads == [normalize(x) for x in upstream.accepted]
        origin_private = "controller-only-origin-fixture" not in json.dumps(upstream.requests) and "origin_manifest" not in json.dumps(upstream.requests)
        if baseline["response"] != observed["response"] or baseline["handoff"] != observed["handoff"]:
            raise AssertionError("Observer changed native return content")
        if not parity or not origin_private or len(baseline_payloads) != 4 or len(upstream.requests) != 6:
            raise AssertionError("Native payload parity or retry counts failed")
        if (state["attempts"] != 6 or state["locked"] != "budget_exhausted"
                or state["accounting"]["task"]["responses"] != 4
                or state["accounting"]["infra"]["failures"] != 2
                or state["accounting"]["task"]["completion_tokens"] != 40
                or state["reserved_output_tokens"] != 16384
                or [r["status"] for r in state["records"]] != [429, 503, 200, 200, 200, 200]):
            raise AssertionError("Retry settlement or unknown reservations failed")
        receipt = {"status": "passed", "real_provider_calls": 0,
            "native_observer_payload_parity": parity, "transient_retry_recovered": True,
            "origin_kwarg_excluded_from_native_payload": origin_private,
            "real_terminus_compact_calls": 3, "fake_actual_attempts": state["attempts"],
            "direct_baseline_attempts": 4, "task_budget_excludes_infra": True,
            "observer_events": observed["events"], "provider": state}
        write_record(root / "QUALIFICATION.json", receipt)
        print(json.dumps({k: v for k, v in receipt.items() if k not in {"provider", "observer_events"}}))
    finally:
        proxy.stop_new_requests()
        for server in (gateway, upstream):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(5)


if __name__ == "__main__":
    main()
