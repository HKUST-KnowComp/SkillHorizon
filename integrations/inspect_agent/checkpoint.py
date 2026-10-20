"""Durable policy boundaries. Ambiguous requests/tools are never replayed."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from integrations.harbor.backend import write_record
from integrations.harbor.declarations import strict_json


def pack(x):
    if x is None or type(x) in (str, bool, int, float):
        return x
    if isinstance(x, (dict, Counter)):
        return {'kind':'counter' if isinstance(x,Counter) else 'dict',
                'items':[[pack(k),pack(v)] for k,v in x.items()]}
    if isinstance(x, (list, tuple, set, frozenset)):
        return {'kind':type(x).__name__, 'items':[pack(v) for v in x]}
    raise TypeError('Unsupported checkpoint value: '+type(x).__name__)


def unpack(x):
    if not isinstance(x, dict):
        return x
    kind, items = x['kind'], x['items']
    if kind in ('dict','counter'):
        value = {unpack(k):unpack(v) for k,v in items}
        return Counter(value) if kind == 'counter' else value
    return {'list':list,'tuple':tuple,'set':set,'frozenset':frozenset}[kind](unpack(v) for v in items)


def object_state(obj):
    # Runtime clock callbacks are rebound, never serialized or imported.
    return {k:v for k,v in vars(obj).items() if k != 'elapsed_reader'}


class Checkpoint:
    def __init__(self, root, continuation):
        self.root = Path(root)
        self.path = self.root/'INSPECT_CHECKPOINT.json'
        self.saved = None
        self.replay_value = None
        self.bound = False
        self.data = {'version':2, 'phase':'initializing'}
        if continuation is not None:
            if set(continuation) != {'inspect_checkpoint'}:
                raise ValueError('Use an Inspect checkpoint; legacy migration is not implicit')
            source = Path(continuation['inspect_checkpoint']).resolve()
            if self.root.resolve() == source.parent:
                raise ValueError('Continuation requires a separate output directory')
            self.saved = json.loads(source.read_text())
            result_path=source.parent/'RESULT.json'
            if result_path.exists():
                result=json.loads(result_path.read_text())
                business=result.get('business') or {}
                if (business.get('accepted') is True or business.get('task_success') is True
                        or (business.get('business') or {}).get('task_success') is True):
                    raise ValueError('Successful business state is not resumable')
            if self.saved.get('version') != 2 or self.saved['phase'] not in ('ready','response_accepted'):
                raise ValueError('Unsettled or terminal checkpoint: explicit reconciliation required')
            self.data['source'] = str(source)

    def continuation(self, proxy, policy, confirmation, *, time_accounting, wall_time_seconds,
                     task_entry=None):
        if self.saved is None:
            return None
        saved = self.saved
        if saved.get('task_entry') != task_entry:
            raise ValueError('Continuation task entry mode, instructions or snapshot differs')
        if (saved.get('time_accounting') != time_accounting
                or 'wall_time_seconds' not in saved or saved['wall_time_seconds'] != wall_time_seconds):
            raise ValueError('Continuation time mode/limit differs')
        if proxy.snapshot() != saved['provider']:
            raise ValueError('Provider ledger was not inherited exactly')
        old_policy = unpack(saved['policy_state'])
        for key in ('B','local_failure_limit','declarations_consume_B','protocol_recovery_limit'):
            if getattr(policy,key) != old_policy[key]:
                raise ValueError('Continuation policy differs')
        if saved['policy_class'] != type(policy).__module__+'.'+type(policy).__qualname__:
            raise ValueError('Continuation policy class differs')
        if saved['confirmation_class'] != type(confirmation).__module__+'.'+type(confirmation).__qualname__:
            raise ValueError('Continuation final policy differs')
        old_confirmation = unpack(saved['confirmation_state'])
        if old_confirmation.get('confirmed'):
            raise ValueError('Confirmed final is terminal')
        policy.__dict__.update(old_policy)
        confirmation.__dict__.update(old_confirmation)
        if getattr(proxy, 'execution_clock', None) is not None:
            raise ValueError('Resume clock is created from the checkpoint, not reset externally')
        result = deepcopy(saved['boundary'])
        result.update(business_source=saved['business_source'], zero_b_recovery=saved['recovery'],
                      diagnostics_state=unpack(saved['diagnostics_state']),
                      prior_execution_seconds=saved['execution_seconds'])
        if saved['phase'] == 'response_accepted':
            self.replay_value = deepcopy(saved)
            result['expected_next_request'] = saved['request']
        return result

    def bind(self, gateway, policy, confirmation, recovery, diagnostics, proxy, elapsed,
             action_protocol, max_calls, export, *, time_accounting, wall_time_seconds, task_entry=None):
        if self.saved and (self.saved['action_protocol'] != action_protocol
                or self.saved['max_calls'] != max_calls or self.saved['export'] != str(export.resolve())):
            raise ValueError('Checkpoint task/protocol/B differs')
        self.gateway, self.policy, self.confirmation = gateway, policy, confirmation
        self.recovery, self.diagnostics, self.proxy, self.elapsed = recovery, diagnostics, proxy, elapsed
        self.data.update(action_protocol=action_protocol,max_calls=max_calls,export=str(export.resolve()),
                         business_source=str((self.root/'business').resolve()),
                         time_accounting=time_accounting, wall_time_seconds=wall_time_seconds)
        if task_entry is not None:
            self.data['task_entry'] = deepcopy(task_entry)
        self.bound = True

    def persist(self):
        self.root.mkdir(parents=True, exist_ok=True)
        write_record(self.path, self.data)

    def ready(self, *, stop_reason=None, **boundary):
        terminal = (stop_reason in ('actor_final','tool_attempt_budget','business_call_budget')
                    or self.confirmation.confirmed or self.data['phase'] == 'terminal')
        data = dict(self.data)
        data.update(phase='terminal' if terminal else 'ready', boundary=deepcopy(boundary),
            policy_state=pack(object_state(self.policy)), recovery=self.recovery.summary(),
            policy_class=type(self.policy).__module__+'.'+type(self.policy).__qualname__,
            confirmation_class=type(self.confirmation).__module__+'.'+type(self.confirmation).__qualname__,
            confirmation_state=pack(vars(self.confirmation)),
            diagnostics_state=pack(vars(self.diagnostics)),
            provider=self.proxy.snapshot(), execution_seconds=self.elapsed())
        if terminal:
            data['terminal_stop_reason'] = stop_reason or 'actor_final'
        if hasattr(self.gateway,'save_inspect_checkpoint'):
            self.gateway.save_inspect_checkpoint()
        # Retain the saved accepted reply while crossing into a successor.
        # A crash here must not turn it into a fresh provider request.
        if self.replay_value is not None:
            for key in ('phase','request','request_id','response','rejected'):
                data[key]=self.replay_value.get(key)
        write_record(self.path, data)
        self.data = data

    def request_pending(self, request, request_id):
        self.data.update(phase='request_pending', request=deepcopy(request),request_id=request_id)
        self.persist()

    def response_accepted(self, raw, rejection=None):
        self.data.update(phase='response_accepted', response=strict_json(raw),
            rejected={'usage':rejection.usage,'maximum':rejection.maximum} if rejection else None,
            provider=self.proxy.snapshot(),execution_seconds=self.elapsed())
        self.persist()

    def replay(self, request):
        saved = self.replay_value
        if saved is None:
            return None
        if request != saved['request']:
            raise ValueError('Accepted response does not match reconstructed input')
        self.replay_value = None
        self.data.update(phase='response_accepted', request=request, response=saved['response'],
                         rejected=saved.get('rejected'),provider=self.proxy.snapshot(),
                         execution_seconds=self.elapsed())
        self.persist()
        return saved

    def tool_pending(self, call, *, charged, dispatch_attempts):
        self.data.update(phase='tool_pending',call=deepcopy(call),
            pending_attempt={'charged':charged, 'dispatch_attempts':dispatch_attempts},
            policy_state=pack(object_state(self.policy)), recovery=self.recovery.summary(),
            confirmation_state=pack(vars(self.confirmation)))
        self.persist()

    def tool_returned(self):
        # Only a complete policy/history boundary can authorize continuation.
        self.data['phase']='settling_tools'
        self.persist()

    def finish(self, stop):
        if not self.bound:
            return
        if stop in ('actor_final','tool_attempt_budget','business_call_budget'):
            self.data['phase']='terminal'
        self.data.update(stop_reason=stop,provider=self.proxy.snapshot(),execution_seconds=self.elapsed())
        self.persist()
