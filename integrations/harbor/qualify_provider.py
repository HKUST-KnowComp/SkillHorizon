"""Real Terminus/LiteLLM actor and three-stage compact against a loopback fake."""

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

from .backend import write_record
from .provider_guard import BudgetProxy, make_server


class FakeProvider(BaseHTTPRequestHandler):
    requests = []
    content = "Synthetic routing fixture; no business task solved."

    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(payload)
        value = {"id": "synthetic-" + str(len(self.requests)), "object": "chat.completion", "created": 1,
            "model": "gpt-5.6-sol", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": self.content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                "provider_extension": "synthetic-extension-must-not-enter-ledger",
                "prompt_tokens_details": {"cached_tokens": None, "text_tokens": 100,
                    "cache_write_tokens": 0, "image_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": None, "text_tokens": 10}}}
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakePane:
    async def capture_pane(self, **kwargs):
        return "Synthetic terminal screen."


class DiagnosticProxy(BudgetProxy):
    def _payload(self, value):
        write_record(self.directory / "last_fake_request.json", value)
        return super()._payload(value)


async def exercise(output, api_base):
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    from harbor.llms.chat import Chat
    (output / "agent").mkdir()
    agent = Terminus2(logs_dir=output / "agent", model_name="openai/gpt-5.6-sol", api_base=api_base,
        reasoning_effort="medium", max_turns=12, llm_call_kwargs={"max_tokens": 4096},
        model_info={"max_input_tokens": 475904, "max_output_tokens": 4096},
        session_id="synthetic-proxy-qualification")
    chat = Chat(agent._llm)
    await chat.chat("Synthetic actor routing check.", **agent._llm_call_kwargs)
    handoff, references = await agent._summarize(chat, "Synthetic routing check", FakePane())
    if not handoff or len(references or []) != 3:
        raise AssertionError("Three-stage compact did not complete")
    if len(list((output / "agent").glob("trajectory.summarization-1-*.json"))) != 3:
        raise AssertionError("Compact trajectory artifacts missing")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--compatibility", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ["OPENAI_API_KEY"] = "harbor-proxy-dummy"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    if args.compatibility:
        FakeProvider.content = '{"analysis":"Interface check","plan":"No actions","commands":[],"task_complete":true}'
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    proxy = DiagnosticProxy(args.output / "provider", model="gpt-5.6-sol",
        upstream_url="http://127.0.0.1:" + str(upstream.server_port) + "/v1/chat/completions",
        api_key="fake-upstream-dummy", testing_only=True,
        **({"max_attempts": 1, "max_input_tokens": 1024, "max_output_tokens": 4096} if args.compatibility else {}))
    gateway = make_server(proxy)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (upstream, gateway)]
    for thread in threads:
        thread.start()
    try:
        api_base = "http://127.0.0.1:" + str(gateway.server_port) + "/v1"
        protocol = None
        if args.compatibility:
            from .compatibility import exercise as compatibility_exercise
            protocol = asyncio.run(compatibility_exercise(args.output, api_base))
            if not all(protocol.get(k) is True for k in (
                    "harbor_chat_accepted", "terminus_parser_accepted", "no_commands", "completion_flag")):
                raise AssertionError("Actual compatibility entry/parser failed")
        else:
            asyncio.run(exercise(args.output, api_base))
        state = proxy.snapshot()
        count = 1 if args.compatibility else 4
        expected_lock = "budget_exhausted" if args.compatibility else None
        if len(FakeProvider.requests) != count or state["attempts"] != count or state["locked"] != expected_lock:
            raise AssertionError("Unexpected request count or ledger lock")
        if "synthetic-extension-must-not-enter-ledger" in json.dumps(state):
            raise AssertionError("Provider extension entered accounting ledger")
        if any(r["usage"]["prompt_tokens_details"]["cached_tokens"] is not None for r in state["records"]):
            raise AssertionError("Unknown cache detail was changed to zero")
        receipt = {"real_provider_calls": 0, "fake_upstream_calls": count,
            "real_harbor_actor_chat_calls": 1, "real_terminus_compact_calls": 0 if args.compatibility else 3,
            "compatibility_protocol": protocol,
            "proxy_ledger": state, "status": "passed"}
        write_record(args.output / "QUALIFICATION.json", receipt)
        print(json.dumps({k: v for k, v in receipt.items() if k != "proxy_ledger"}))
    finally:
        for server in (gateway, upstream):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(5)


if __name__ == "__main__":
    main()
