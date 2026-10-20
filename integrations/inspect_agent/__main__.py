"""Run one synthetic Harbor -> Inspect demo, never a provider experiment."""
import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from harbor.agents.factory import AgentFactory
from harbor.models.trial.config import AgentConfig
from harbor.models.agent.context import AgentContext
from .fixtures import MockProxy, options, actions, FINAL
from .harbor import RuntimeBinding, bind_runtime, release_runtime, SkillHorizonInspectVerifier


async def smoke(root):
    root.mkdir(parents=True,exist_ok=False)
    proxy=MockProxy([actions('read_project','unknown','read_table'),FINAL,FINAL])
    binding='local-smoke'
    bind_runtime(binding,RuntimeBinding(root,proxy,'Synthetic public task.',options()))
    try:
        config=AgentConfig(import_path='integrations.inspect_agent.harbor:SkillHorizonInspect',
                          model_name=proxy.model,kwargs={'binding':binding})
        actor=AgentFactory.create_agent_from_config(config,logs_dir=root/'agent')
        context=AgentContext()
        await actor.setup(None)
        await actor.run('Synthetic public task.',None,context)
        verifier=SkillHorizonInspectVerifier(task=None,trial_paths=SimpleNamespace(trial_dir=root),environment=None)
        reward=(await verifier.verify()).rewards
        print(json.dumps({'real_provider_calls':0,'stop_reason':context.metadata['stop_reason'],
                          'synthetic_reward':reward,'receipt':context.metadata['receipt']}))
    finally:release_runtime(binding)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke',type=Path,required=True,help='Fresh output directory for synthetic-only demo')
    args=parser.parse_args()
    asyncio.run(smoke(args.smoke.resolve()))


if __name__=='__main__':main()
