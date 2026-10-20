"""Explicit Harbor BaseAgent/Verifier entry, using a controller-owned binding.

Bindings are installed by a Python driver in the Harbor process. They contain
already-configured proxies, never credential paths or values in Harbor config.
There is no automatic experiment launch, credential lookup, or default switch.
"""
from dataclasses import dataclass
from pathlib import Path
import json
from harbor.agents.base import BaseAgent
from harbor.agents.options import AgentOptions
from harbor.verifier.base import BaseVerifier
from harbor.models.verifier.result import VerifierResult
from .agent import run_async
from .runtime import PIN
from .scoring import business_accepted


@dataclass
class RuntimeBinding:
    export: Path
    proxy: object
    instruction: str
    options: dict
    used: bool = False
    receipt_path: Path | None = None


_BINDINGS = {}


def bind_runtime(name, binding):
    if not name or name in _BINDINGS:
        raise ValueError('Use a unique binding for each Harbor trial')
    _BINDINGS[name]=binding


def release_runtime(name):
    _BINDINGS.pop(name,None)


class InspectOptions(AgentOptions):
    binding: str


class SkillHorizonInspect(BaseAgent):
    options_model=InspectOptions
    @staticmethod
    def name():return 'skillhorizon-inspect'
    def version(self):return 'inspect-'+PIN+'-text-json-v1'
    async def setup(self, environment):
        if self.mcp_servers or self.skills_dir or self.load_trajectory:
            raise ValueError('External tools/workspace/Harbor trajectory replay are not supported')
        if self.options.binding not in _BINDINGS:
            raise ValueError('Explicit controller runtime binding is required')
    async def run(self, instruction, environment, context):
        binding=_BINDINGS[self.options.binding]
        if binding.used or instruction != binding.instruction or self.model_name != binding.proxy.model:
            raise ValueError('Binding already used or Harbor instruction/model differs')
        binding.used=True
        # Outside /logs/agent mounted into the agent sandbox. Model receives no
        # host path, shell, file tool or access to the Harbor environment object.
        root=self.logs_dir.parent/'inspect_private'
        result=await run_async(binding.export,root,binding.proxy,**binding.options)
        binding.receipt_path=root/'RESULT.json'
        context.metadata={'harness':'inspect_text_json_v1','inspect_version':PIN,
                          'stop_reason':result['stop_reason'],
                          'provider_accounting':result['provider'],
                          'receipt':str(binding.receipt_path)}
        # Do not fill Harbor totals with accepted-only usage: unknown usage and
        # all physical sends remain in the authoritative provider ledger.


class SkillHorizonInspectVerifier(BaseVerifier):
    async def verify(self):
        path=self.trial_paths.trial_dir/'inspect_private/RESULT.json'
        result=json.loads(path.read_text())
        evaluation = result.get('business_evaluation')
        if (result.get('score_exception') or
                evaluation is not None and evaluation.get('status') != 'valid' or
                evaluation is None and result.get('exception')):
            raise RuntimeError('No valid domain evaluation is available')
        try:
            accepted = business_accepted(result.get('business'))
        except ValueError as error:
            raise RuntimeError(str(error)) from None
        if evaluation is not None and evaluation.get('accepted') is not accepted:
            raise RuntimeError('Business evaluation and validity record disagree')
        return VerifierResult(rewards={'reward':float(accepted)})
