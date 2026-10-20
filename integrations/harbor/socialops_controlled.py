"""SocialOps public-tool adapter and failure records with an optional local limit."""

from collections import Counter
from copy import deepcopy
from pathlib import Path
import json
import shutil

from .backend import append_event, write_record
from .declarations import DeclarationState, NAMES, TOOLS


def failed(call, result):
    return (result.get('ok') is False or 'gateway_error' in result
        or result.get('status') == 'error' or 'declaration_error' in result
        or (call['name'] == 'run_content_review'
            and result.get('data', {}).get('environment_decision') == 'rejected'))


from .execution_clock import public_limit, LEGACY

class SocialAttemptPolicy:
    """Object/stage counters depend only on calls and visible returned facts."""
    prepare_tools = {'bind_campaign_learning', 'update_content_post', 'attach_asset_to_post',
                     'request_post_approval', 'run_content_review'}
    release_tools = {'schedule_post', 'cancel_scheduled_post', 'bind_audience_exposure',
                     'bind_audience_reach', 'publish_post'}

    def __init__(self, B, *, declarations_consume_B, protocol_recovery_limit, wall_time_seconds=None,
                 local_failure_limit=3):
        # The default supports frozen callers; new protocols explicitly pass None.
        if local_failure_limit is not None and (type(local_failure_limit) is not int or local_failure_limit < 1):
            raise ValueError('Expected a positive local failure limit or None')
        self.B = B
        self.local_failure_limit = local_failure_limit
        self.wall_time_seconds = wall_time_seconds
        self.declarations_consume_B = declarations_consume_B
        self.protocol_recovery_limit = protocol_recovery_limit
        self.approvals, self.tests, self.posts = {}, {}, {}
        self.cycles, self.failures = Counter(), Counter()
        self.completed, self.completion_markers = set(), {}
        self.terminal = None
        self.failure_events = []

    def charge(self, call):
        return int(self.declarations_consume_B or call['name'] not in NAMES)

    def identity(self, call):
        name, args = call['name'], call['arguments']
        pid = args.get('post_id')
        if name == 'run_content_review':
            pid = self.approvals.get(args.get('approval_request_id'))
        if name in self.prepare_tools and isinstance(pid, str) and pid:
            return ('post', pid, 'prepare_and_approve')
        if name in self.release_tools and isinstance(pid, str) and pid:
            return ('post', pid, 'release')
        if name == 'observe_post' and isinstance(pid, str) and pid:
            return ('post', pid, 'observe')
        bid = args.get('brief_id') if name == 'launch_ab_test' else self.tests.get(args.get('ab_test_id'))
        if name in {'launch_ab_test', 'complete_campaign_experiment'} and isinstance(bid, str) and bid:
            return ('brief', bid, 'experiment')
        if name == 'run_skill_script':
            inputs = args.get('inputs', {})
            if not isinstance(inputs, dict):
                return None
            requests = inputs.get('requests', [])
            if isinstance(requests, list) and len(requests)==1 and isinstance(requests[0], dict):
                pid = requests[0].get('post_id')
                if isinstance(pid, str) and pid:
                    return ('post', pid, 'release')
        # Queries, clock, notices and ambiguous multi-object scripts have no
        # unique unfinished execution stage. Their attempts still consume B.
        return None

    def before(self, call):
        return self.terminal if self.local_failure_limit is not None else None

    def _reopen(self, identity):
        if identity in self.completed:
            self.cycles[identity] += 1
            self.completed.remove(identity)

    def _complete(self, identity, marker):
        if marker == self.completion_markers.get(identity):
            return  # repeating an old receipt cannot clear new failures
        self.completion_markers[identity] = marker
        self.completed.add(identity)

    def observe(self, call, result):
        identity = self.identity(call)
        bad = failed(call, result)
        if bad and identity is not None and identity not in self.completed:
            key = (identity, self.cycles[identity])
            self.failures[key] += 1
            record = dict(identity=list(identity), cycle=self.cycles[identity],
                failures=self.failures[key], tool=call['name'])
            self.failure_events.append(record)
            if self.local_failure_limit is not None and self.failures[key] >= self.local_failure_limit:
                self.terminal = record
        if not bad:
            name, args, data = call['name'], call['arguments'], result.get('data', {})
            if name == 'read_campaign_brief':
                brief = data['brief']
                for pid in brief['variant_post_ids'] + [brief['deployment_post_id']]:
                    self.posts[pid] = brief['brief_id']
            if name in {'request_post_approval', 'read_approval', 'run_content_review'} and data.get('approval_request_id') and data.get('post_id'):
                self.approvals[data['approval_request_id']] = data['post_id']
            if name == 'request_post_approval' and data.get('approval_request_id'):
                self.approvals[data['approval_request_id']] = args['post_id']
            if name == 'list_approval_requests':
                for value in data.values():
                    if isinstance(value, list):
                        for row in value:
                            if isinstance(row, dict) and row.get('approval_request_id') and row.get('post_id'):
                                self.approvals[row['approval_request_id']] = row['post_id']
            if name == 'launch_ab_test' and data.get('ab_test_id'):
                self.tests[data['ab_test_id']] = data['brief_id']
            if name in {'update_content_post', 'attach_asset_to_post', 'bind_campaign_learning'} and data.get('post_version'):
                key = ('post', args['post_id'], 'prepare_and_approve')
                old = self.completion_markers.get(key)
                if isinstance(old, int) and data['post_version'] > old:
                    self._reopen(key)
            if name == 'run_content_review' and data.get('environment_decision') == 'approved':
                self._complete(('post', data['post_id'], 'prepare_and_approve'), data['content_snapshot']['version'])
            if name == 'publish_post' and data.get('publication_id'):
                self._complete(('post', args['post_id'], 'release'), data['publication_id'])
            if name == 'observe_post' and data.get('snapshot_id'):
                self._complete(('post', args['post_id'], 'observe'), data['snapshot_id'])
            if name == 'complete_campaign_experiment' and data.get('report_id'):
                self._complete(('brief', data['brief_id'], 'experiment'), data['report_id'])
            if name == 'poll_campaign_updates':
                for notice in data.get('events', []):
                    for pid in notice.get('affected_post_ids', []):
                        self._reopen(('post', pid, 'prepare_and_approve'))
        return dict(failed=bad, terminate=self.terminal is not None,
                    local_failure=self.terminal)

    def public_instructions(self):
        declarations = 'count as attempts' if self.declarations_consume_B else 'do not consume B'
        return (f'Tool budget B={self.B}; each actual business or query attempt, including rejected attempts, costs one. '
            f'begin_skill and finish_skill {declarations}. {self.local_limit_instructions()} '
            'Preparation, content binding and approval '
            'share one post stage; scheduling, exposure/reach binding and publication share a release stage; observations '
            'use a post observation stage and experiment launch/completion use a brief experiment stage. Creating a new '
            'approval request, changing parameters, fixing prerequisites or declarations does not reset unfinished failures. '
            'Valid empty queries are not failures. Stop each batch on its first public rejection or failed review; its unattempted '
            f'suffix is not charged or resumed automatically. {self.protocol_recovery_limit} consecutive malformed/incomplete '
            'action envelopes end the task as a protocol failure. There is no total-response or cumulative token cap. '
            f"{public_limit(self.wall_time_seconds, getattr(self, 'time_accounting', LEGACY))}"
            'single output is 8192, history compaction triggers at estimated 200000 input tokens.')

    def local_limit_instructions(self):
        if self.local_failure_limit is None:
            return ('Local failures are recorded for analysis only; there is no local failure-count stopping limit. '
                    'You may continue correcting failures within the remaining tool and wall-time budgets.')
        return (f'Local K={self.local_failure_limit}: reaching this accumulated public failure count for one '
                'object/obligation ends the entire task immediately.')

    def summary(self):
        return dict(K=self.local_failure_limit, B=self.B, formula='R+ceil(A/2)',
            local_failure_mode='record_only' if self.local_failure_limit is None else 'terminate',
            local_failure_boundary=(None if self.local_failure_limit is None else
                'immediate_third_failure_ends_entire_task' if self.local_failure_limit == 3 else
                'local_limit_ends_entire_task'),
            declarations_consume_B=self.declarations_consume_B, protocol_recovery_limit=self.protocol_recovery_limit,
            failure_events=self.failure_events, terminal=self.terminal, batch_first_failure_pause=True,
            wall_time_seconds=self.wall_time_seconds)


class SocialGateway:
    """Only a validated public-tool catalogue reaches the model; scoring stays private.

    Each actor runs in its own OS process, including the legacy native backend.
    The actor never gets a Python entrypoint, paths, environment or private state.
    """
    def __init__(self, task, directory, *, max_calls):
        from integrations.socialops.declaration_runtime import DeclarationReachSocial
        from integrations.socialops.claim_sources import CreationEvidence
        self.task, self.directory = Path(task), Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        shutil.copytree(self.task/'checkpoint', self.directory/'runtime')
        self.env = DeclarationReachSocial(self.task, self.directory/'runtime')
        self.client = self
        self.declarations = DeclarationState(s['name'] for s in self.env.public['skills'])
        self.evidence = CreationEvidence()
        self.dispatches, self.events, self.claim_results = 0, 0, []
        self.max_calls = max_calls

    def tools(self):
        return {'tools': [dict(type='function', name=n, description=v['description'], parameters=v['input_schema'])
                         for n,v in sorted(self.env.metadata.items())] + deepcopy(TOOLS)}

    def call(self, name, arguments, **metadata):
        self.events += 1
        if name in NAMES:
            event = self.declarations.propose(name, arguments, self.events, self.dispatches)
            self.declarations.accept(event)
            append_event(self.directory/'declarations.jsonl', event)
            if name == 'finish_skill' and arguments['status']=='completed' and event['outcome']=='accepted':
                from integrations.socialops.claim_goals import C06Goals
                claim = next(c for c in self.declarations.claims() if c['run_id']==arguments['run_id'])
                measured = C06Goals(self.env, self.evidence).assess(claim)
                self.claim_results.append(measured)
                append_event(self.directory/'claim_measurement.jsonl', measured)
            return event['receipt']['result']
        if self.dispatches >= self.max_calls:
            raise RuntimeError('Controller budget enforcement mismatch')
        before = deepcopy(self.env.world)
        result = self.env.call(name, arguments)
        action = dict(skill_id=name, arguments=arguments, result=result)
        self.evidence.observe(before, self.env, action, self.dispatches)
        self.dispatches += 1
        append_event(self.directory/'tools.jsonl', {**action, 'business_dispatch':self.dispatches, **metadata})
        return result

    def seal_and_score(self):
        from integrations.socialops.evaluate import evaluate
        result = evaluate(self.task, self.directory/'runtime')
        write_record(self.directory/'EVALUATION.json', result)
        return result

    def measure_claims(self):
        result = dict(protocol='skill_declaration_v1', declared_runs=len(self.declarations.runs),
            completed_claims=len(self.claim_results), claims=self.claim_results,
            executed_business_calls=self.dispatches, gateway_calls=self.events)
        write_record(self.directory/'CLAIMS.json', result)
        return result

    def close(self):
        pass
