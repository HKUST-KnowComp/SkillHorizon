"""Native FinanceOps V6 task route, reusing its runtime and offline scorer."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
from .finance_policy import AttemptPolicy as ObligationPolicy, RecordOnly
from integrations.harbor.factoryops_controlled import FactoryOpsAttemptPolicy
from integrations.harbor.declarations import DeclarationState, NAMES, TOOLS
from integrations.harbor.backend import append_event, write_record
from skillhorizon.dataflow_v5.skills import SKILLS, tools
from skillhorizon.financeops.runtime import Runtime
from skillhorizon.dataflow_v5.verification import score
from .checkpoint import pack, unpack


class FinancePolicy(RecordOnly, ObligationPolicy):
    def __init__(self, project, B, *, declarations_consume_B=False, protocol_recovery_limit=4):
        super().__init__(project,B)
        self.declarations_consume_B=declarations_consume_B
        self.protocol_recovery_limit=protocol_recovery_limit
        self.local_failure_limit=None
        self.wall_time_seconds=None
    charge=FactoryOpsAttemptPolicy.charge
    public_instructions=FactoryOpsAttemptPolicy.public_instructions
    def before(self,call):return None
    def observe(self,call,result):
        super().observe(call,result)
        return {'failed':result.get('ok') is False or 'declaration_error' in result or 'gateway_error' in result,
                'terminate':False}


class FinanceGateway:
    supports_explicit_continuation=True
    def __init__(self,task,directory,*,max_calls,continuation_source=None):
        self.task,self.directory=Path(task),Path(directory)
        spec=json.loads((self.task/'task.json').read_text())
        if spec['format']!='skillhorizon.dataflow_v6.tasks.v1':raise ValueError('Expected FinanceOps V6 task')
        self.directory.mkdir(parents=True,exist_ok=False)
        self.initial=json.loads((self.task/'initial/project.json').read_text())
        self.declarations=DeclarationState(SKILLS)
        self.max_calls=max_calls
        self.dispatches=self.events=0
        if continuation_source is None:
            shutil.copyfile(self.task/'initial/project.json',self.directory/'project.json')
        else:
            source=Path(continuation_source)
            saved=json.loads((source/'INSPECT_GATEWAY.json').read_text())
            if saved['max_calls']!=max_calls:raise ValueError('Gateway B differs')
            shutil.copyfile(source/'project.json',self.directory/'project.json')
            for name in ('declarations.jsonl','tools.jsonl','actions.jsonl'):
                if (source/name).exists():shutil.copyfile(source/name,self.directory/name)
            self.dispatches,self.events=saved['dispatches'],saved['events']
            self.declarations.__dict__.update(unpack(saved['declarations']))
        self.env=Runtime(self.directory,persist=False)
        self.client=self
    def tools(self):return {'tools':tools()+deepcopy(TOOLS)}
    def call(self,name,arguments,**metadata):
        self.events+=1
        if name in NAMES:
            event=self.declarations.propose(name,arguments,self.events,self.dispatches)
            self.declarations.accept(event)
            append_event(self.directory/'declarations.jsonl',event)
            result={'ok':event['outcome']!='rejected',**event['receipt']['result']}
            if not result['ok']:
                result['error']={'code':result['declaration_error'],
                    'message':'Use the public declaration schema and an existing run_id for finish_skill.'}
            return result
        if self.dispatches>=self.max_calls:raise RuntimeError('Gateway B mismatch')
        result=self.env.call(name,arguments)
        # Same runtime state; the controller owns durable commit.
        write_record(self.directory/'project.json',self.env.p)
        self.dispatches+=1
        append_event(self.directory/'tools.jsonl',dict(name=name,arguments=arguments,result=result,
                                                     business_dispatch=self.dispatches,**metadata))
        return result
    def save_inspect_checkpoint(self):
        write_record(self.directory/'INSPECT_GATEWAY.json',dict(max_calls=self.max_calls,
            dispatches=self.dispatches,events=self.events,declarations=pack(vars(self.declarations))))
    def seal_and_score(self):
        value=score(self.env,self.initial)
        write_record(self.directory/'EVALUATION.json',value)
        return value
    def measure_claims(self):
        return {'claims':self.declarations.claims(),'semantic_claim_grading':'not_run'}
    def close(self):pass
