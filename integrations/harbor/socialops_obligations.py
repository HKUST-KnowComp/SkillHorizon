"""Independent SocialOps obligation counters, using only public call receipts.

Historical broad-stage counting remains in socialops_controlled.py.
No business runtime or evaluator is consulted by this policy.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json

from .socialops_controlled import SocialAttemptPolicy, failed


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


from .execution_clock import public_limit, LEGACY

class SocialObligationPolicy(SocialAttemptPolicy):
    mapping = {
        'bind_campaign_learning': 'learning_binding',
        'update_content_post': 'content_preparation',
        'attach_asset_to_post': 'content_preparation',
        'request_post_approval': 'approval', 'run_content_review': 'approval',
        'schedule_post': 'scheduling', 'cancel_scheduled_post': 'scheduling',
        'bind_audience_exposure': 'exposure_binding',
        'bind_audience_reach': 'reach_binding',
        'publish_post': 'publication', 'observe_post': 'observation',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.versions, self.bindings, self.snapshots = {}, {}, {}
        self.publications, self.conversions, self.public = {}, {}, {}
        self.clock = None
        self.published = set()

    def identity(self, call):
        name, args = call['name'], call['arguments']
        pid = args.get('post_id')
        if name == 'run_content_review':
            rid = args.get('approval_request_id')
            pid = self.approvals.get(rid) if isinstance(rid, str) else None
        if name in self.mapping and isinstance(pid, str) and pid:
            return ('post', pid, self.mapping[name])
        if name == 'run_skill_script' and args.get('skill_id') == 'reconcile-audience-exposure':
            inputs = args.get('inputs')
            pid = inputs.get('post_id') if isinstance(inputs, dict) else None
            if isinstance(pid, str) and pid:
                return ('post', pid, 'exposure_binding')
        if name == 'run_skill_script' and args.get('skill_id') == 'schedule-cross-platform-release':
            inputs = args.get('inputs')
            requests = inputs.get('requests') if isinstance(inputs, dict) else None
            if isinstance(requests, list) and len(requests) == 1 and isinstance(requests[0], dict):
                pid = requests[0].get('post_id')
                if isinstance(pid, str) and pid:
                    return ('post', pid, 'scheduling')
        if name in {'launch_ab_test', 'complete_campaign_experiment'}:
            test_id = args.get('ab_test_id')
            bid = args.get('brief_id') if name == 'launch_ab_test' else self.tests.get(test_id) if isinstance(test_id, str) else None
            if isinstance(bid, str) and bid:
                return ('brief', bid, 'experiment')
        return None

    def _complete(self, identity, marker):
        # The receipt's semantic value is a witness, not a source of new cycles.
        # After independently witnessed invalidation, the same valid value can
        # complete the new cycle (e.g. rescheduling a genuinely canceled slot).
        self.completion_markers[identity] = deepcopy(marker)
        self.completed.add(identity)

    def reopen_post(self, pid, goals):
        for goal in goals:
            self._reopen(('post', pid, goal))

    def _invalidate_clock_bindings(self):
        if not self.clock:
            return
        now = instant(self.clock)
        windows = {r['segment_id']: r['memory_hours'] * 3600
                   for r in self.public.get('exposure_policy', {}).get('segments', [])}
        reach = {r['segment_id']: r for r in self.public.get('reach_policy', {}).get('segments', [])}
        for key, binding in self.bindings.items():
            _, pid, goal = key
            if key not in self.completed or pid in self.published:
                continue
            if goal == 'exposure_binding':
                # Decay is monotone: one known contribution decreasing proves
                # invalidation; a mere clock increment or rounded-equal load does not.
                for rid in binding['prior_publication_ids']:
                    row = self.publications.get(rid)
                    if row is None:
                        continue
                    for seg in set(binding['audience_loads']) & set(row['audience_segment_ids']):
                        if seg not in windows:
                            continue
                        w = windows[seg]
                        def contribution(t):
                            age = int((t - instant(row['published_at'])).total_seconds())
                            return row['allocated_units'] * max(0, w-age) // w
                        if contribution(now) != contribution(instant(binding['as_of'])):
                            self._reopen(key)
            elif goal == 'reach_binding':
                ids = binding['conversion_snapshot_ids']
                if not all(rid in self.conversions for rid in ids):
                    continue
                if not all(seg in reach for seg in binding['eligible_cohorts']):
                    continue
                # Binding supplied the complete then-active source set. Compare
                # both exact sources and deduplicated cohorts after expiration.
                sources, suppressed = [], {s:set() for s in binding['eligible_cohorts']}
                for rid in ids:
                    row = self.conversions[rid]
                    positive = False
                    for seg in set(suppressed) & set(row['converted_cohorts']):
                        if now < instant(row['observed_until']) + timedelta(hours=reach[seg]['conversion_cooldown_hours']):
                            suppressed[seg].update(row['converted_cohorts'][seg])
                            positive |= bool(row['converted_cohorts'][seg])
                    if positive:
                        sources.append(rid)
                eligible = {s: sorted(set(reach[s]['cohort_ids'])-v) for s,v in suppressed.items()}
                if sorted(ids) != sorted(sources) or eligible != binding['eligible_cohorts']:
                    self._reopen(key)

    def _learn(self, name, args, data):
        if name == 'read_task':
            self.public.update(data)
        if isinstance(data.get('clock'), str):
            self.clock = data['clock']
        if name in {'read_campaign_brief'}:
            brief = data['brief']
            for pid in brief['variant_post_ids'] + [brief['deployment_post_id']]:
                self.posts[pid] = brief['brief_id']
        if name in {'request_post_approval', 'read_approval', 'run_content_review'}:
            rid = data.get('approval_request_id')
            pid = data.get('post_id', args.get('post_id'))
            if rid and pid:
                self.approvals[rid] = pid
        if name == 'list_approval_requests':
            for value in data.values():
                if isinstance(value, list):
                    for row in value:
                        if isinstance(row, dict) and row.get('approval_request_id') and row.get('post_id'):
                            self.approvals[row['approval_request_id']] = row['post_id']
        if name == 'launch_ab_test' and data.get('ab_test_id'):
            self.tests[data['ab_test_id']] = data['brief_id']
        if name in {'list_audience_exposures', 'list_audience_conversions'}:
            dest, field = (self.publications, 'publication_id') if name.endswith('exposures') else (self.conversions, 'snapshot_id')
            for row in data.get('records', []):
                dest[row[field]] = deepcopy(row)
        if name in {'publish_post', 'read_post_publication'} and data.get('publication_id'):
            self.publications[data['publication_id']] = dict(publication_id=data['publication_id'],
                published_at=data['published_at'], audience_segment_ids=data['content']['audience_segment_ids'],
                allocated_units=data['exposure']['allocated_units'])
        if name == 'observe_post' and data.get('conversion'):
            row = data['conversion']
            self.conversions[row['snapshot_id']] = deepcopy(row)

    def observe(self, call, result):
        name, args, data = call['name'], call['arguments'], result.get('data', {})
        bad = failed(call, result)
        identity = self.identity(call)
        pid = identity[1] if identity and identity[0]=='post' else args.get('post_id')
        if not bad:
            new_publication = data.get('publication_id') not in self.publications
            self._learn(name, args, data)
            identity = self.identity(call)
            pid = identity[1] if identity and identity[0]=='post' else pid
            if pid and data.get('post_version'):
                old = self.versions.get(pid)
                version = data['post_version']
                # Runtime's content mutation receipt increases version iff the
                # snapshot changed; those changes actually cancel approval/slot
                # and invalidate version-bound audience receipts.
                if old is not None and version != old:
                    self.reopen_post(pid, ('content_preparation','approval','scheduling','exposure_binding','reach_binding'))
                self.versions[pid] = version
            if name == 'bind_campaign_learning' and data.get('learning') is not None:
                self._complete(identity, data['learning'])
            if name in {'bind_audience_exposure','bind_audience_reach'} and data.get('post_version'):
                self.bindings[identity] = deepcopy(data)
                self.clock = data['as_of']
                self._complete(identity, data)
            if name == 'run_content_review' and data.get('environment_decision') == 'approved':
                snapshot = data['content_snapshot']
                self.versions[pid] = snapshot['version']
                self.snapshots[pid] = deepcopy(snapshot)
                self._complete(identity, snapshot)
                self._complete(('post',pid,'content_preparation'), snapshot)
            if name == 'schedule_post' and data.get('status') == 'scheduled':
                self._complete(identity, data.get('scheduled_for'))
            if name == 'cancel_scheduled_post':
                self._reopen(identity)
            if name == 'publish_post' and data.get('publication_id'):
                self._complete(identity, data['publication_id'])
                self.published.add(pid)
                new = self.publications[data['publication_id']]
                for key, binding in self.bindings.items():
                    # Re-publishing returns an immutable old receipt, not new exposure.
                    if (new_publication and key[2] == 'exposure_binding' and key[1] not in self.published
                            and data['publication_id'] not in binding['prior_publication_ids']):
                        if new['allocated_units'] > 0 and set(new['audience_segment_ids']) & set(binding['audience_loads']):
                            self._reopen(key)
            if name == 'observe_post' and data.get('snapshot_id'):
                self._complete(identity, data['snapshot_id'])
                conversion = data.get('conversion', {})
                for key, binding in self.bindings.items():
                    if key[2] == 'reach_binding' and key[1] not in self.published:
                        # A new positive source changes the exact required set,
                        # even when all its cohorts were already suppressed.
                        if conversion.get('snapshot_id') not in binding['conversion_snapshot_ids'] and any(
                            conversion.get('converted_cohorts', {}).get(s) for s in binding['eligible_cohorts']):
                            self._reopen(key)
            if name == 'complete_campaign_experiment' and data.get('report_id'):
                self._complete(identity, data['report_id'])
            if name == 'poll_campaign_updates':
                for event in data.get('events', []):
                    for post_id in event.get('affected_post_ids', []):
                        self.reopen_post(post_id, ('content_preparation','approval','scheduling','exposure_binding','reach_binding'))
            self._invalidate_clock_bindings()
        else:
            message = result.get('message', '')
            if pid and name == 'publish_post':
                if message == 'CURRENT_EXPOSURE_BINDING_REQUIRED':
                    self._reopen(('post',pid,'exposure_binding'))
                elif message == 'CURRENT_REACH_BINDING_REQUIRED':
                    self._reopen(('post',pid,'reach_binding'))
            # Direct verification of the previously accepted inputs, now
            # publicly rejected, is another explicit invalidation witness.
            binding = self.bindings.get(identity)
            if binding and all(args.get(k) == v for k,v in binding.items() if k not in {'as_of','post_version'}):
                if message in {'EXACT_POSITIVE_EXPOSURE_ORIGINS','ACTUAL_DECAYED_AUDIENCE_LOAD',
                               'EXACT_OBSERVED_CONVERSION_ORIGINS','ACTUAL_DEDUPLICATED_REACH'}:
                    self._reopen(identity)
            if identity is not None and identity not in self.completed:
                key = (identity, self.cycles[identity])
                self.failures[key] += 1
                record = dict(identity=list(identity), cycle=self.cycles[identity], failures=self.failures[key], tool=name)
                self.failure_events.append(record)
                if self.local_failure_limit is not None and self.failures[key] >= self.local_failure_limit:
                    self.terminal = record
        return dict(failed=bad, terminate=self.terminal is not None, local_failure=self.terminal)

    def public_instructions(self):
        return (f'Tool budget B={self.B}; every business/query/script attempt, including rejected attempts, costs one; '
            f'begin_skill and finish_skill do not consume B. {self.local_limit_instructions()} '
            'Per-post independent obligations are '
            'learning binding; content preparation (update/attach, completed by actual approval); approval (request/review); '
            'scheduling (schedule/cancel and uniquely attributable allocation script); exposure binding; reach binding; '
            'publication; observation. Experiment launch/completion share one per-brief experiment obligation. '
            'A verified binding completes its own obligation immediately. Approval needs actual approved review, scheduling '
            'needs a reserved slot, publication/observation/experiment need their actual business receipts. '
            'Tool/parameter changes, prerequisite repairs, new artifact IDs and skill declarations never reset an unfinished '
            'obligation. A new cycle is allowed only after public evidence that a previously completed obligation became invalid. '
            'Merely advancing time, increasing an unrelated version or repeating an old receipt does not open a cycle. '
            'Valid empty queries do not count as failures. A batch stops at its first public failure: its unattempted suffix '
            f'is uncharged and is never automatically resumed. {self.protocol_recovery_limit} consecutive invalid action/final '
            'envelopes terminate as protocol error. No total-response or cumulative token cap. '
            f"{public_limit(self.wall_time_seconds, getattr(self, 'time_accounting', LEGACY))}"
            '8192 per output, compaction at estimated 200000 input tokens.')

    def summary(self):
        return {**super().summary(), 'definition':'independent_obligation_v1', 'action_mapping':self.mapping,
            'accounts':[dict(identity=list(key[0]), cycle=key[1], failures=value,
                completed=key[0] in self.completed and key[1]==self.cycles[key[0]]) for key,value in self.failures.items()]}
