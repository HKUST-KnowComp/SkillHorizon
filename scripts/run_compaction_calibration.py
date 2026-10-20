"""Execute exactly one qualified synthetic calibration, without logging credentials."""

import argparse
import os
from pathlib import Path
import shlex

from skillhorizon.experiment.config import RunConfig
from skillhorizon.experiment.context import prepare_request, input_size
from skillhorizon.experiment.runner import run_task
from skillhorizon.experiment.tasks import load_task
from skillhorizon.experiment.transport import HttpTransport
from skillhorizon.util import load_json


def credential(path):
    for name in ("OPENLUX_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(name):
            return os.environ[name]
    # Parse only credential assignments. No shell execution or variable expansion.
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key.strip() in {"OPENLUX_API_KEY", "OPENAI_API_KEY"}:
            parsed = shlex.split(value, comments=True)
            if len(parsed) == 1 and parsed[0]:
                return parsed[0]
    raise ValueError("Authorized provider credential unavailable")


class ProgressTransport(HttpTransport):
    count = 0

    def send(self, payload):
        self.count += 1
        print({"request": self.count, "role": "compaction" if not payload["tools"] else "actor",
               "local_input_tokens": input_size(payload)}, flush=True)
        response, metadata = super().send(payload)
        print({"request": self.count, "http_status": metadata.get("http_status"),
               "status": response.get("status")}, flush=True)
        return response, metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("qualification", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    root = args.qualification.resolve()
    plan = load_json(root / "PLAN.json")
    qualified = load_json(root / "QUALIFICATION.json")
    if not qualified["passed"] or not qualified["zero_model_calls"]:
        raise ValueError("Mechanical qualification must pass first")
    if plan["provider"] != "https://api.openlux.ai/v1":
        raise ValueError("This calibration is scoped to the frozen OpenLux route")
    config = RunConfig.from_record(plan["config"])
    task = load_task(Path(plan["task"]))
    role, _, _, stop = prepare_request(config, task["messages"], task["tools"], task)
    if role != "compaction" or stop:
        raise ValueError("This pilot must trigger the configured compaction")
    if (root / "api").exists():
        raise ValueError("Calibration already started; no implicit reruns")
    report = run_task(plan["task"], root / "api", config,
                      ProgressTransport(plan["provider"], credential(args.env_file), config))
    print({key: report.get(key) for key in ("valid", "outcome", "task_success", "provider_requests",
          "accepted_model_turns", "executed_tool_calls", "input_tokens", "output_tokens", "compaction")}, flush=True)


if __name__ == "__main__":
    main()
