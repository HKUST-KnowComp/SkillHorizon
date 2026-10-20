"""Synthetic-only fixtures: no credentials, network or real task inputs."""
from copy import deepcopy
import json
from pathlib import Path
from integrations.harbor.controlled_agent import ALLOWED
from integrations.harbor.final_confirmation import FinalConfirmation
from integrations.harbor.factoryops_controlled import FactoryOpsAttemptPolicy
from integrations.harbor.action_protocol_v4 import PROTOCOL

class Encoding:
    def encode(self, text, **kwargs):
        return list(range(len(text)//4))


class MockProxy:
    model='fixture'
    encoding=Encoding()
    output_limit_feedback=True
    def __init__(self, replies):
        self.replies=list(replies); self.requests=[]; self.sends=0
    def forward(self, request, request_id):
        assert 'tools' not in request
        assert request['reasoning_effort']=='medium'
        self.requests.append(deepcopy(request)); self.sends+=1
        value=self.replies.pop(0)
        if isinstance(value,BaseException):raise value
        return json.dumps({'choices':[{'message':{'content':value},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}).encode()
    def snapshot(self):
        return {'physical_sends':self.sends,'unknown_usage_records':0}


class MockGateway:
    supports_explicit_continuation=True
    calls=[]
    def __init__(self, export, root, max_calls, continuation_source=None):
        self.root=Path(root);self.root.mkdir();self.client=self
    def tools(self):
        return {'tools':[{'name':n,'parameters':{'type':'object','properties':{},'additionalProperties':False}}
                         for n in sorted(ALLOWED)]}
    def call(self,name,arguments,**metadata):
        self.calls.append((name,arguments,metadata))
        return {'ok':True,'value':'observed'}
    def seal_and_score(self):return {'accepted':False}
    def measure_claims(self):return {'status':'fixture'}
    def close(self):pass


def options(B=5, **kwargs):
    return dict(max_calls=B,action_protocol=PROTOCOL,attempt_policy=FactoryOpsAttemptPolicy(B,
        declarations_consume_B=False,protocol_recovery_limit=4,local_failure_limit=None),
        final_confirmation=FinalConfirmation(),gateway_factory=MockGateway,
        public_task={'messages':[{'role':'user','content':'Public goal.'}]},**kwargs)


def actions(*names):
    return json.dumps({'type':'actions','actions':[{'name':n,'arguments':{}} for n in names]})
FINAL='{"type":"final","text":"Incomplete."}'


