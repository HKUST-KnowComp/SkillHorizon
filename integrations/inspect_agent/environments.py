"""Opt-in routes to existing seven environments; no world/scorer changes."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
from integrations.harbor.backend import write_record
from integrations.harbor.declarations import DeclarationState, NAMES
from integrations.harbor.controlled_agent import ALLOWED, FIELDOPS_ALLOWED
from integrations.harbor.docops_controlled import DocOpsGateway, DocOpsAttemptPolicy
from integrations.harbor.socialops_controlled import SocialGateway
from integrations.harbor.socialops_obligations import SocialObligationPolicy
from integrations.harbor.logisticsops_controlled import LogisticsOpsGateway, LogisticsOpsAttemptPolicy
from integrations.harbor.researchops_controlled import ResearchOpsGateway, ResearchOpsAttemptPolicy
from integrations.harbor.factoryops_controlled import FactoryOpsGateway, FactoryOpsAttemptPolicy
from integrations.harbor.fieldops_obligations import FieldObligationPolicy
from .checkpoint import pack, unpack

GATEWAYS = {'FinanceOps':None, 'FieldOps':None, 'SocialOps':SocialGateway,
            'LogisticsOps':LogisticsOpsGateway, 'ResearchOps':ResearchOpsGateway,
            'DocOps':DocOpsGateway, 'FactoryOps':FactoryOpsGateway}
POLICIES = {'FieldOps':FieldObligationPolicy, 'SocialOps':SocialObligationPolicy,
            'LogisticsOps':LogisticsOpsAttemptPolicy, 'ResearchOps':ResearchOpsAttemptPolicy,
            'DocOps':DocOpsAttemptPolicy, 'FactoryOps':FactoryOpsAttemptPolicy}


def snapshot_gateway(gateway_class):
    """Resume only this adapter's settled snapshots, without tool-log replay."""
    class SnapshotGateway(gateway_class):
        supports_explicit_continuation = True
        def __init__(self, task, directory, *, max_calls, continuation_source=None):
            if continuation_source is None:
                super().__init__(task,directory,max_calls=max_calls)
                return
            source=Path(continuation_source)
            saved=json.loads((source/'INSPECT_GATEWAY.json').read_text())
            if saved['gateway'] != gateway_class.__name__ or saved['max_calls'] != max_calls:
                raise ValueError('Gateway checkpoint identity/B differs')
            self.task,self.directory=Path(task),Path(directory)
            self.directory.mkdir(parents=True,exist_ok=False)
            shutil.copytree(source/'runtime',self.directory/'runtime')
            for filename in ('declarations.jsonl','tools.jsonl','claim_measurement.jsonl'):
                if (source/filename).exists():shutil.copyfile(source/filename,self.directory/filename)
            # Constructors only reload the persisted world. No business actions.
            if gateway_class is SocialGateway:
                from integrations.socialops.declaration_runtime import DeclarationReachSocial as Runtime
                from integrations.socialops.claim_sources import CreationEvidence
                self.evidence=CreationEvidence()
                self.evidence.__dict__.update(unpack(saved['evidence']))
            elif gateway_class is LogisticsOpsGateway:
                from integrations.logisticsops.declaration_runtime import DeclarationFreight as Runtime
            elif gateway_class is ResearchOpsGateway:
                from integrations.researchops.declaration_runtime import DeclarationLibrary as Runtime
            elif gateway_class is FactoryOpsGateway:
                from integrations.factoryops.declaration_runtime import DeclarationProduction as Runtime
            else:
                from integrations.docops.runtime import SkillPlanning as Runtime
            self.env=Runtime(self.task,self.directory/'runtime')
            self.client=self
            self.__dict__.update(unpack(saved['counters']))
            self.declarations=DeclarationState([])
            self.declarations.__dict__.update(unpack(saved['declarations']))
        def save_inspect_checkpoint(self):
            saved={'gateway':gateway_class.__name__,'max_calls':self.max_calls,
                'counters':pack({k:deepcopy(getattr(self,k)) for k in
                    ('dispatches','events','max_calls','claim_results') if hasattr(self,k)}),
                'declarations':pack(vars(self.declarations))}
            if hasattr(self,'evidence'):saved['evidence']=pack(vars(self.evidence))
            write_record(self.directory/'INSPECT_GATEWAY.json',saved)
    return SnapshotGateway


def environment_options(name, task, *, B, public_task, attempt_policy=None,
                        declarations_consume_B=False, protocol_recovery_limit=4,
                        task_entry_mode=None):
    """Pass exact approved public messages; do not synthesize domain prompts.

    Native FinanceOps V6 and the five native domains use their existing runtimes.
    FieldOps and legacy FinanceOps exports use PrivateGateway; legacy FinanceOps
    must supply its selected policy explicitly (V5/V6 differ).
    """
    if name not in GATEWAYS:raise ValueError('Unknown environment')
    entry_options = {}
    if task_entry_mode is not None:
        from .task_entry import prepare_task_entry
        entry_options['task_entry'] = prepare_task_entry(name, task, task_entry_mode)
    if name == 'FinanceOps' and (Path(task)/'task.json').is_file():
        from .financeops import FinanceGateway, FinancePolicy
        if attempt_policy is None:
            project=json.loads((Path(task)/'initial/project.json').read_text())
            attempt_policy=FinancePolicy(project,B,declarations_consume_B=declarations_consume_B,
                                        protocol_recovery_limit=protocol_recovery_limit)
        from skillhorizon.dataflow_v5.skills import tools
        return dict(max_calls=B,public_task=deepcopy(public_task),attempt_policy=attempt_policy,
                    tool_profile={t['name'] for t in tools()} | NAMES,gateway_factory=FinanceGateway,
                    **entry_options)
    if attempt_policy is None:
        if name == 'FinanceOps':
            raise ValueError('Supply the selected FinanceOps attempt_policy explicitly')
        attempt_policy=POLICIES[name](B,declarations_consume_B=declarations_consume_B,
            protocol_recovery_limit=protocol_recovery_limit,local_failure_limit=None,wall_time_seconds=None)
    cls=GATEWAYS[name]
    profile=(FIELDOPS_ALLOWED if name=='FieldOps' else ALLOWED) if cls is None else (
        set(json.loads((Path(task)/'PUBLIC_TOOLS.json').read_text())) | NAMES)
    return dict(max_calls=B,public_task=deepcopy(public_task),attempt_policy=attempt_policy,
                tool_profile=profile,gateway_factory=snapshot_gateway(cls) if cls else None,
                **entry_options)
