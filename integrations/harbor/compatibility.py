"""One reviewed synthetic request through the actual Harbor Chat/parser path."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import threading

from .backend import write_record
from .provider_guard import BudgetProxy, PRODUCTION_URL, make_server
from .smoke_control import termination_signals


PROMPT = ('Synthetic interface check only. Return this JSON object without any '
          'extra text: {"analysis":"Interface check", "plan":"No actions", '
          '"commands":[], "task_complete":true}. Do not call tools.')


def check_response(response, parser):
    accepted = isinstance(response.content, str) and bool(response.content.strip())
    if not accepted:
        return {"harbor_chat_accepted": False, "terminus_parser_accepted": False}
    result = parser.parse_response(response.content)
    return {"harbor_chat_accepted": True,
            "terminus_parser_accepted": not bool(result.error),
            "parser_warning": bool(result.warning),
            "no_commands": not bool(result.commands),
            "completion_flag": result.is_task_complete is True}


async def exercise(output, api_base):
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    from harbor.llms.chat import Chat
    (output / "agent").mkdir()
    agent = Terminus2(logs_dir=output / "agent", model_name="openai/gpt-5.6-sol",
        api_base=api_base, reasoning_effort="medium", max_turns=1,
        llm_call_kwargs={"max_tokens": 4096},
        model_info={"max_input_tokens": 475904, "max_output_tokens": 4096},
        session_id="synthetic-compat-v1")
    response = await asyncio.wait_for(Chat(agent._llm).chat(PROMPT, **agent._llm_call_kwargs), 150)
    return check_response(response, agent._parser)


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    write_record(output / "STARTED.json", {"model": "gpt-5.6-sol", "reasoning_effort": "medium",
        "attempts_limit": 1, "estimated_input_limit": 1024, "output_limit": 4096,
        "business_tools_enabled": False, "automatic_successor": False})
    key = sys.stdin.read(8192).strip()
    if not key or "\r" in key or "\n" in key or key.startswith("sk-proj-"):
        raise ValueError("Invalid authorized credential channel")
    proxy = BudgetProxy(output / "provider", model="gpt-5.6-sol", upstream_url=PRODUCTION_URL,
        api_key=key, timeout=120, max_attempts=1, max_input_tokens=1024, max_output_tokens=4096)
    del key
    os.environ["OPENAI_API_KEY"] = "harbor-proxy-dummy"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    server = make_server(proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    protocol = {"harbor_chat_accepted": False, "terminus_parser_accepted": False}
    exception_type = None
    try:
        protocol = asyncio.run(exercise(output, f"http://127.0.0.1:{server.server_port}/v1"))
    except Exception as error:
        exception_type = type(error).__name__
    finally:
        proxy.stop_new_requests()
        server.shutdown()
        server.server_close()
        thread.join(10)
        state = proxy.snapshot()
        settled = (state["attempts"] == 1 and len(state["records"]) == 1
                   and state["records"][0]["outcome"] == "settled"
                   and state["records"][0]["usage"] is not None)
        passed = settled and all(protocol.get(k) is True for k in (
            "harbor_chat_accepted", "terminus_parser_accepted", "no_commands", "completion_flag"))
        receipt = {"scope": "tiny interface only, not business or length performance",
            "usage_settled": settled, "protocol": protocol, "exception_type": exception_type,
            "passed": passed, "provider": state, "business_calls": 0}
        write_record(output / "RESULT.json", receipt)
        print(json.dumps(receipt), flush=True)
    return passed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with termination_signals():
        passed = run(args.output.resolve())
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
