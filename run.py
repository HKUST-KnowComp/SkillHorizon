"""Construct SkillHorizon tasks and evaluate agents through Harbor + Inspect."""
import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(ROOT / "assets/tokenizers"))

DOMAINS = {
    "SocialOps": ("socialops", "C_06_01_01", "DeclarationReachSocial", "reach_witness", "evaluate",
                  {"campaigns": 2, "waves": 2}, {"use_scripts": True, "exercise_recovery": False}),
    "LogisticsOps": ("logisticsops", "C_09_01_35", "DeclarationFreight", "freight_witness", "freight_evaluate",
                     {"cohorts": 2, "legs": 3}, {"use_script": True}),
    "ResearchOps": ("researchops", "C_11_01_01", "DeclarationLibrary", "witness", "evaluate",
                    {"sections": 2, "rounds": 2, "studies_per_section": 1,
                     "alias_copies": 0, "distractors_per_section": 0},
                    {"use_scripts": True, "efficient_reads": True}),
    "DocOps": ("docops", "C_25_05_01", "SkillPlanning", "witness", "evaluate",
               {"width": 2, "depth": 2}, {"use_script": True, "record_skills": False}),
    "FactoryOps": ("factoryops", "C_50_05_01", "DeclarationProduction", "witness", "evaluate",
                   {"levels": 2, "terminal_quantity": 1}, {"use_scripts": True, "efficient_reads": True}),
}
ENVIRONMENTS = ("FinanceOps", "FieldOps", *DOMAINS)


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def events_from(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def build(args):
    config = read(args.config) if args.config else {}
    if not isinstance(config, dict):
        raise ValueError("--config must contain a JSON object of constructor parameters")
    if args.environment in DOMAINS:
        source = args.source_root / "envs" / DOMAINS[args.environment][1]
        if not (source / "data/database").is_dir() or not (source / "mcp_servers").is_dir():
            raise ValueError(
                f"Missing resources for {args.environment}: {source}. "
                "Set SKILLHORIZON_EXTERNAL_ASSETS or pass --source-root; "
                "see docs/RESOURCES.md for the required layout."
            )
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    task = root / "task"
    if args.environment == "FinanceOps":
        from skillhorizon.financeops.build import build as construct
        parameters = dict(family="heterogeneous_integration", variant="branch_specific_transforms",
                          seed=args.seed, stages=1, width=1)
        parameters.update(config)
        status = construct({"id": f"example-{args.seed}", "parameters": parameters}, task)
        if status["status"] != "passed":
            raise RuntimeError("Reference construction failed; inspect task/STATUS.json")
        public = {"messages": read(task / "task.json")["messages"]}
        events = [dict(name=e["skill_id"], arguments=e["arguments"], output=e["result"])
                  for e in events_from(task / "reference/actions.jsonl")]
        task_path = "task"
    elif args.environment == "FieldOps":
        from skillhorizon.fieldops.declaration_tasks import build_declaration_operations
        from integrations.harbor.export import export_task
        parameters = dict(seed=args.seed, groups=8, device_counts=[1] * 8, max_calls=2000)
        parameters.update(config)
        build_declaration_operations(task, **parameters)
        public = {"messages": read(task / "task.json")["messages"]}
        events = [dict(name=e["skill_id"], arguments=e["arguments"], output=e["result"])
                  for e in events_from(task / "reference/model_skill_actions.jsonl")]
        export_task(task, root / "harbor-task", max_calls=parameters["max_calls"])
        task_path = "harbor-task"
    else:
        package, source, cls, witness, evaluator, defaults, witness_options = DOMAINS[args.environment]
        prefix = "integrations." + package
        parameters = dict(seed=args.seed, background=False, runtime_identity="task_local_v1",
                          scenario="standard", **defaults)
        parameters.update(config)
        importlib.import_module(prefix + ".declaration_build").build(
            args.source_root / "envs" / source, task, **parameters)
        runtime_module = ".runtime" if args.environment == "DocOps" else ".declaration_runtime"
        env = getattr(importlib.import_module(prefix + runtime_module), cls)(task, task / "runtime")
        write(task / "PUBLIC_TOOLS.json", sorted(env.metadata))
        events = []

        async def call(name, **arguments):
            value = env.call(name, arguments)
            events.append(dict(name=name, arguments=arguments, output=value))
            if value["status"] != "success":
                raise RuntimeError("Reference action failed: " + name)
            return value["data"]

        options = dict(witness_options)
        if args.environment != "DocOps":
            options.update(skill_protocol="skill_declaration_v1", max_calls=args.reference_max_calls)
        asyncio.run(importlib.import_module(prefix + "." + witness).run(call, **options))
        score = importlib.import_module(prefix + "." + evaluator).evaluate(task, task / "runtime")
        write(root / "REFERENCE_SCORE.json", score)
        if not score["accepted"]:
            raise RuntimeError("Reference business evaluation failed")
        public = {"messages": [{"role": "user", "content": json.dumps(read(task / "public/task.json"))}]}
        task_path = "task"
    from skillhorizon.experiment.history_measurement import tokens
    reference_tokens = sum(tokens({"type": "call", "name": e["name"], "arguments": e["arguments"]})
                           + tokens({"type": "result", "result": e["output"]}) for e in events)
    write(root / "public.json", public)
    write(root / "reference.json", events)
    manifest = dict(environment=args.environment, task=task_path, public="public.json",
                    parameters=parameters, reference_calls=len(events),
                    reference_interaction_tokens=reference_tokens, provider_calls=0)
    write(root / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2))


async def trial(args, scripted=False):
    from harbor.agents.factory import AgentFactory
    from harbor.models.trial.config import AgentConfig
    from harbor.models.agent.context import AgentContext
    from integrations.inspect_agent.harbor import (
        RuntimeBinding, bind_runtime, release_runtime, SkillHorizonInspectVerifier)
    from integrations.inspect_agent.environments import environment_options
    from integrations.harbor.final_confirmation import FinalConfirmation
    from integrations.harbor.action_protocol_v4 import PROTOCOL
    from integrations.harbor.execution_clock import MODE

    bundle = args.task.resolve()
    manifest = read(bundle / "manifest.json")
    task = bundle / manifest["task"]
    public = read(bundle / manifest["public"])
    if scripted:
        from integrations.inspect_agent.fixtures import MockProxy, FINAL
        replies = [json.dumps({"type": "actions", "actions": [{"name": e["name"],
                   "arguments": e["arguments"]}]}) for e in read(bundle / "reference.json")]
        proxy = MockProxy(replies + [FINAL, FINAL])
    else:
        from integrations.harbor.provider_reliable import ReliableBudgetProxy, ProviderPolicy
        key = os.environ.get(args.api_key_env)
        if not key:
            raise ValueError("Set the named API key environment variable before running")
        proxy = ReliableBudgetProxy(args.output.resolve() / "provider", model=args.model,
            upstream_url=args.endpoint, api_key=key,
            policy=ProviderPolicy(args.max_provider_responses, None, None, 8192, 96),
            output_limit_feedback=True, retry_connection_errors=True)
    root = args.output.resolve()
    # Provider construction owns its ledger; reject reuse before constructing it in main().
    root.mkdir(parents=True, exist_ok=True)
    instruction = "\n".join(message["content"] for message in public["messages"])
    options = environment_options(manifest["environment"], task, B=args.max_calls, public_task=public)
    options.update(action_protocol=PROTOCOL, final_confirmation=FinalConfirmation(), time_accounting=MODE)
    name = "submission-trial"
    bind_runtime(name, RuntimeBinding(task, proxy, instruction, options))
    try:
        config = AgentConfig(import_path="integrations.inspect_agent.harbor:SkillHorizonInspect",
                             model_name=proxy.model, kwargs={"binding": name})
        actor = AgentFactory.create_agent_from_config(config, logs_dir=root / "agent")
        context = AgentContext()
        await actor.setup(None)
        await actor.run(instruction, None, context)
        verifier = SkillHorizonInspectVerifier(task=None, environment=None,
                                              trial_paths=SimpleNamespace(trial_dir=root))
        reward = (await verifier.verify()).rewards
        result = read(root / "inspect_private/RESULT.json")
        from scoring import native_goal
        summary = dict(environment=manifest["environment"], harness="Harbor + Inspect",
                       mode="scripted_reference" if scripted else "model",
                       reward=reward, stop_reason=result["stop_reason"],
                       dispatch_attempts=result["dispatch_attempts"], business=result["business"])
        summary["local_goals"] = native_goal(manifest["environment"], result["business"])
        if scripted:
            summary["provider_calls"] = 0
        write(root / "summary.json", summary)
        print(json.dumps(summary, indent=2))
    finally:
        release_runtime(name)


def main():
    if sys.argv[1:2] == ["native"]:
        from integrations.native_agents.__main__ import main as native_main
        return native_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build", help="Construct a task and execute its public reference, without a model")
    p.add_argument("--environment", choices=ENVIRONMENTS, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--config", type=Path, help="JSON object of constructor keyword arguments")
    p.add_argument("--source-root", type=Path,
                   default=Path(os.environ.get("SKILLHORIZON_EXTERNAL_ASSETS") or ROOT / "assets"),
                   help="Resource root containing envs/ (default: SKILLHORIZON_EXTERNAL_ASSETS or assets/)")
    p.add_argument("--reference-max-calls", type=int, default=2000)
    for command in ("run", "check-reference"):
        p = sub.add_parser(command)
        p.add_argument("--task", type=Path, required=True, help="Bundle containing manifest.json")
        p.add_argument("--output", type=Path, required=True)
        p.add_argument("--max-calls", type=int, required=True, help="Business tool allowance B")
        if command == "run":
            p.add_argument("--model", required=True)
            p.add_argument("--api-key-env", default="SKILLHORIZON_API_KEY")
            p.add_argument("--endpoint", default="https://api.openlux.ai/v1/chat/completions")
            p.add_argument("--max-provider-responses", type=int, required=True)
    p = sub.add_parser("smoke", help="Framework-only zero-API check")
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("native", add_help=False,
        help="Opt-in native Harbor agents: prepare a task or run an explicit job")
    p.add_argument("native_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a fresh output directory")
    if hasattr(args, "max_calls") and args.max_calls <= 0:
        parser.error("--max-calls must be positive")
    if args.command == "build":
        build(args)
    elif args.command == "smoke":
        from integrations.inspect_agent.__main__ import smoke
        asyncio.run(smoke(args.output.resolve()))
    else:
        asyncio.run(trial(args, scripted=args.command == "check-reference"))


if __name__ == "__main__":
    sys.exit(main())
