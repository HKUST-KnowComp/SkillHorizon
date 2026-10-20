"""FactoryOps portfolio public adapter; business state stays in the frozen runtime."""
from pathlib import Path
import shutil

from .backend import write_record
from .declarations import DeclarationState
from .docops_controlled import DocOpsGateway
from .socialops_controlled import SocialAttemptPolicy
from .execution_clock import public_limit, LEGACY


class FactoryOpsAttemptPolicy(SocialAttemptPolicy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.local_failure_limit is not None:
            raise ValueError('FactoryOps requires record-only local failures')
        self.attempts = 0
        self.elapsed_reader = None

    def observe(self, call, result):
        self.attempts += self.charge(call)
        bad = (result.get('ok') is False or result.get('status') == 'error'
               or 'gateway_error' in result or 'declaration_error' in result)
        if bad:
            args = call['arguments']
            objects = {k:v for k,v in args.items() if k.endswith('_id') and isinstance(v,str)}
            self.failure_events.append(dict(tool=call['name'], public_object_ids=objects,
                dispatch_attempts=self.attempts,
                elapsed_seconds=self.elapsed_reader() if self.elapsed_reader else None,
                public_error=result.get('message',result.get('gateway_error',result.get('declaration_error')))))
        return dict(failed=bad, terminate=False, local_failure=None)

    def public_instructions(self):
        return (f'Tool budget B={self.B}; each actual query, script or business attempt, including rejected '
            'attempts, costs one. begin_skill and finish_skill do not consume B. Local failures are recorded '
            'only; there is no local failure-count stopping limit. A batch pauses at its first public failure; '
            'the unattempted suffix is not charged and is never automatically resumed. Decide new actions '
            f'from the returned feedback. {self.protocol_recovery_limit} consecutive invalid action/final '
            'envelopes terminate as protocol error. No total-response or cumulative input/output token caps. '
            + public_limit(self.wall_time_seconds,getattr(self,'time_accounting',LEGACY))
            + 'Single output 8192; compaction at estimated 200000 input tokens.')

    def summary(self):
        return {**super().summary(), 'definition':'factoryops_public_failure_records_v1',
                'failure_scope':'Public tool and supplied object IDs; obligation/cycle analysis deferred; no inferred local stop'}


class FactoryOpsGateway(DocOpsGateway):
    def __init__(self, task, directory, *, max_calls):
        from integrations.factoryops.declaration_runtime import DeclarationProduction
        self.task,self.directory = Path(task),Path(directory)
        self.directory.mkdir(parents=True,exist_ok=False)
        shutil.copytree(self.task/'checkpoint',self.directory/'runtime')
        self.env = DeclarationProduction(self.task,self.directory/'runtime')
        self.client = self
        self.declarations = DeclarationState(s['name'] for s in self.env.public['skills'])
        self.dispatches,self.events,self.max_calls = 0,0,max_calls

    def seal_and_score(self):
        from integrations.factoryops.evaluate import evaluate
        result = evaluate(self.task,self.directory/'runtime')
        write_record(self.directory/'EVALUATION.json',result)
        return result
