"""Explicit Harbor job declarations; generating a file does not start a trial."""

import argparse
import json
from pathlib import Path

from . import HARBOR_COMMIT
from .backend import write_record


def job_config(task, jobs_dir, *, agent="oracle", model=None, api_base=None,
               max_turns=12, total_window=480000, compact_at=200000, max_output=4096,
               observed=False, agent_timeout=None):
    task = Path(task).resolve()
    metadata = json.loads((task / "migration.json").read_text())
    if metadata["harbor_commit"] != HARBOR_COMMIT:
        raise ValueError("Task was exported for a different Harbor revision")
    if agent not in {"oracle", "terminus-2"}:
        raise ValueError("Unsupported migration agent")
    actor = {"name": agent}
    if agent == "oracle":
        if not metadata["oracle_in_controller_only_solution"]:
            raise ValueError("Oracle assets were not exported")
    else:
        if any(type(x) is not int or x <= 0 for x in (max_turns, total_window, compact_at, max_output)):
            raise ValueError("Positive integer turn and window budgets required")
        if max_output > 32768:
            raise ValueError("Output reserve exceeds supported maximum")
        if not model or not api_base or not 0 < compact_at < total_window - max_output:
            raise ValueError("Explicit model, route and consistent context policy required")
        if observed:
            actor = {"import_path": "integrations.harbor.agent:SkillHorizonTerminus"}
        if agent_timeout is not None:
            if type(agent_timeout) is not int or agent_timeout <= 0:
                raise ValueError("Positive integer agent timeout required")
            actor["override_timeout_sec"] = agent_timeout
        actor.update(model_name=model, kwargs={
            "api_base": api_base, "reasoning_effort": "medium", "max_turns": max_turns,
            "enable_summarize": True,
            "proactive_summarization_threshold": total_window - max_output - compact_at,
            "model_info": {"max_input_tokens": total_window - max_output, "max_output_tokens": max_output},
            "llm_call_kwargs": {"max_tokens": max_output},
            "store_all_messages": True,
            "trajectory_config": {"linear_history": True},
        })
    return {"jobs_dir": str(Path(jobs_dir).resolve()), "n_attempts": 1, "n_concurrent_trials": 1,
            "retry": {"max_retries": 0}, "agents": [actor], "tasks": [{"path": str(task)}],
            "environment": {"type": "docker", "delete": False, "kwargs": {"keep_containers": True}},
            "verifier": {"import_path": "integrations.harbor.verifier:SkillHorizonVerifier",
                         "kwargs": {"max_calls": metadata["max_calls"],
                                    "require_code_origins": metadata.get("code_origins_version") == 1}}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--agent", choices=["oracle", "terminus-2"], default="oracle")
    parser.add_argument("--model")
    parser.add_argument("--api-base")
    args = parser.parse_args()
    if args.config.exists():
        raise ValueError("Use a fresh job config")
    write_record(args.config, job_config(args.task, args.jobs_dir, agent=args.agent,
                                        model=args.model, api_base=args.api_base))


if __name__ == "__main__":
    main()
