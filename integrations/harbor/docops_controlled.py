"""DocOps public adapter and uncapped, public-evidence obligation records."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import shutil

from .backend import append_event, write_record
from .declarations import DeclarationState, NAMES, TOOLS
from .socialops_controlled import SocialAttemptPolicy


from .execution_clock import public_limit, LEGACY

class DocOpsAttemptPolicy(SocialAttemptPolicy):
    mapping = {
        'run_skill_script': 'contract_reconciliation', 'revise_page_block': 'contract_reconciliation',
        'record_decision': 'decision_recording',
        'request_page_review': 'review_acceptance', 'run_document_review': 'review_acceptance',
        'approve_review_and_transition_page': 'review_acceptance',
        'publish_decision_summary': 'publication', 'synchronize_tracker': 'tracker_sync',
        'resolve_comment_thread': 'comment_resolution',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.local_failure_limit is not None:
            raise ValueError('DocOps requires K=null')
        self.objects, self.attempts, self.obligation_events = {}, 0, []
        self.unassigned_failures = []
        self.elapsed_reader = None

    def location(self):
        return dict(dispatch_attempts=self.attempts,
            elapsed_seconds=self.elapsed_reader() if self.elapsed_reader else None)

    def identity(self, call):
        name, args = call['name'], call['arguments']
        goal = self.mapping.get(name)
        if not goal:
            return None
        pid = args.get('page_id', args.get('target_page_id'))
        if not pid:
            for key in ('block_id', 'review_id', 'item_id', 'thread_id'):
                if isinstance(args.get(key), str):
                    pid = self.objects.get(args[key])
                    if pid:
                        break
        if name == 'run_skill_script':
            try:
                pid = args['inputs']['requirements']['capacity_policy']['page_id']
            except (KeyError, TypeError):
                return None
        if name == 'resolve_comment_thread':
            tid = args.get('thread_id')
            return ('thread', tid, goal) if isinstance(tid, str) and tid else None
        return ('page', pid, goal) if isinstance(pid, str) and pid else None

    def learn(self, value):
        if isinstance(value, dict):
            pid = value.get('page_id')
            if isinstance(pid, str):
                for k in ('block_id', 'contract_block_id', 'requirements_block_id', 'review_id',
                          'thread_id', 'item_id', 'tracker_item_id', 'decision_id'):
                    if isinstance(value.get(k), str):
                        self.objects[value[k]] = pid
            for child in value.values():
                self.learn(child)
        elif isinstance(value, list):
            for child in value:
                self.learn(child)

    def _complete(self, identity, marker):
        if identity is None:
            return
        if identity not in self.completed:
            self.obligation_events.append(dict(event='completed', identity=list(identity),
                cycle=self.cycles[identity], failures=self.failures[(identity,self.cycles[identity])], **self.location()))
        super()._complete(identity, marker)

    def _reopen(self, identity):
        if identity in self.completed:
            self.obligation_events.append(dict(event='invalidated', identity=list(identity),
                cycle=self.cycles[identity], **self.location()))
        super()._reopen(identity)

    def observe(self, call, result):
        self.attempts += self.charge(call)
        name, args = call['name'], call['arguments']
        data = result.get('data', {})
        bad = (result.get('ok') is False or result.get('status') == 'error'
            or 'gateway_error' in result or 'declaration_error' in result
            or (name == 'run_document_review' and data.get('status') == 'changes_requested'))
        # Even a failed business review can contain valid public object bindings.
        if result.get('status') == 'success':
            self.learn(data)
            if name == 'request_page_review' and data.get('review_id') and args.get('page_id'):
                self.objects[data['review_id']] = args['page_id']
        identity = self.identity(call)
        if bad:
            record = dict(tool=name, **self.location())
            if identity is None:
                self.unassigned_failures.append(record)
            elif identity not in self.completed:
                key = (identity,self.cycles[identity])
                self.failures[key] += 1
                self.failure_events.append(dict(record, identity=list(identity), cycle=key[1], failures=self.failures[key]))
        else:
            affected = data.get('invalidated_page_ids', data.get('invalidated_pages', []))
            for pid in affected:
                for goal in set(self.mapping.values()) - {'comment_resolution'}:
                    self._reopen(('page',pid,goal))
            if name == 'record_decision' and data.get('decision_id'):
                self._complete(identity, data['decision_id'])
            if name == 'run_document_review' and data.get('status') == 'approved' and identity:
                self._complete(('page',identity[1],'contract_reconciliation'), data.get('page_version'))
            if name == 'approve_review_and_transition_page' and data.get('accepted_decision_ids'):
                self._complete(identity, (data['review_id'],data['page_version']))
            if name == 'publish_decision_summary' and data.get('publication_id'):
                self._complete(identity,data['publication_id'])
            if name == 'synchronize_tracker' and data.get('status') == 'approved':
                self._complete(identity,data.get('publication_id'))
            if name == 'resolve_comment_thread':
                if args.get('resolution_action') == 'reopen':
                    self._reopen(identity)
                elif data.get('status') in {'resolved','closed'}:
                    self._complete(identity,(data.get('thread_id'),data.get('updated_at')))
        return dict(failed=bad, terminate=False, local_failure=None)

    def public_instructions(self):
        return (f'Tool budget B={self.B}; each actual query, script or business attempt, including rejected '
            'attempts, costs one. begin_skill and finish_skill do not consume B. Local failures are recorded '
            'only, with no K stopping limit. Independent per-page obligations are contract reconciliation '
            '(verified by passing public review), decision recording, review acceptance, publication and tracker '
            'synchronization; comment resolution is per thread. Merely changing parameters, creating new '
            'review IDs, repairing prerequisites or redeclaring skills does not reset unfinished failures. '
            'Only public invalidation of a completed obligation opens a new cycle. A batch pauses at its '
            'first public rejection or failed review; its unattempted suffix is not charged or automatically '
            f'resumed. {self.protocol_recovery_limit} consecutive invalid action/final envelopes end as a '
            'protocol error. No total-response or cumulative input/output token caps. '
            f"{public_limit(self.wall_time_seconds, getattr(self, 'time_accounting', LEGACY))}"
            'Single output 8192; compaction at estimated 200000 input tokens.')

    def summary(self):
        return {**super().summary(), 'definition':'docops_public_obligations_v1', 'action_mapping':self.mapping,
            'obligation_events':self.obligation_events, 'unassigned_failures':self.unassigned_failures,
            'accounts':[dict(identity=list(i), cycle=c, failures=n) for (i,c),n in self.failures.items()]}


class DocOpsGateway:
    def __init__(self, task, directory, *, max_calls):
        from integrations.docops.runtime import SkillPlanning
        self.task, self.directory = Path(task), Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        shutil.copytree(self.task/'checkpoint', self.directory/'runtime')
        self.env = SkillPlanning(self.task,self.directory/'runtime')
        self.client = self
        self.declarations = DeclarationState(s['name'] for s in self.env.public['skills'])
        self.dispatches, self.events, self.max_calls = 0, 0, max_calls

    def tools(self):
        return {'tools':[dict(type='function',name=n,description=v['description'],parameters=v['input_schema'])
            for n,v in sorted(self.env.metadata.items())]+deepcopy(TOOLS)}

    def call(self, name, arguments, **metadata):
        self.events += 1
        if name in NAMES:
            event=self.declarations.propose(name,arguments,self.events,self.dispatches)
            self.declarations.accept(event)
            append_event(self.directory/'declarations.jsonl',event)
            return event['receipt']['result']
        if self.dispatches >= self.max_calls:
            raise RuntimeError('Controller budget enforcement mismatch')
        result=self.env.call(name,arguments)
        self.dispatches += 1
        append_event(self.directory/'tools.jsonl',dict(skill_id=name,arguments=arguments,result=result,
            business_dispatch=self.dispatches,**metadata))
        return result

    def seal_and_score(self):
        from integrations.docops.evaluate import evaluate
        result=evaluate(self.task,self.directory/'runtime')
        write_record(self.directory/'EVALUATION.json',result)
        return result

    def measure_claims(self):
        result=dict(protocol='skill_declaration_v1',declared_runs=len(self.declarations.runs),
            claims=self.declarations.claims(),executed_business_calls=self.dispatches,gateway_calls=self.events,
            semantic_claim_grading='not_run; declarations and tool associations preserved; no replay')
        write_record(self.directory/'CLAIMS.json',result)
        return result

    def close(self):
        pass
