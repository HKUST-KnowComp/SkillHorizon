"""FinanceOps public-observation policies, without historical worker imports.

Extracted from financeops_v6_obligations.AttemptPolicy and
financeops_gpt_k_resume.RecordOnly; keep the observation semantics unchanged.
"""
from collections import Counter
from copy import deepcopy


KINDS = {'match-remittance': 'matching', 'review-credit-note': 'notes',
         'post-reconciliation': 'work_output', 'close-stage': 'stage_close',
         'review-release': 'review', 'publish-release': 'publication'}
KINDS.update({'normalize-source': 'normalized', 'aggregate-source': 'work_output',
    'review-adjustment': 'decision', 'prepare-adjustment': 'plan',
    'commit-adjustment': 'adjustment_receipt', 'snapshot-component': 'work_output'})


class AttemptPolicy:
    def __init__(self, project, B):
        self.B = B
        self.sources = set(project['sources'])
        self.artifacts = {}
        self.cycles = Counter()
        self.failures = Counter()
        self.completed = {}
        self.terminal = None
        self.notified = set()
        self.versions = {}

    def reference(self, args, field):
        value = args.get(field)
        return self.artifacts.get(value, {}) if isinstance(value, str) else {}

    def identities(self, call):
        if not isinstance(call, dict) or call.get('name') not in ('execute_step', 'run_skill_script'):
            return []
        outer = call.get('arguments', {})
        if not isinstance(outer, dict):
            return []
        skill, args = outer.get('skill_id'), outer.get('arguments', {})
        if not isinstance(args, dict) or not isinstance(skill, str) or skill not in KINDS:
            return []
        if skill in ('match-remittance', 'review-credit-note', 'normalize-source', 'review-adjustment', 'snapshot-component'):
            target = args.get('source_id')
            if not isinstance(target, str) or target not in self.sources:
                return []
            if skill == 'review-adjustment':
                case = args.get('case_id')
                return [(skill, target, case)] if isinstance(case, str) and case else []
            if skill == 'snapshot-component':
                view = args.get('view')
                return [(skill, target, view)] if view in ('current', 'historical') else []
        elif skill == 'aggregate-source':
            target = self.reference(args, 'normalized_id').get('source_id')
        elif skill in ('prepare-adjustment', 'commit-adjustment'):
            decision = self.reference(args, 'decision_id')
            if skill == 'commit-adjustment':
                plan = self.reference(args, 'plan_id')
                decision = self.artifacts.get(plan.get('data', {}).get('decision_id'), {})
            sid, case = decision.get('source_id'), decision.get('data', {}).get('case_id')
            return [(skill, sid, case)] if isinstance(sid, str) and isinstance(case, str) else []
        elif skill == 'post-reconciliation':
            target = self.artifacts.get(args.get('matching_id'), {}).get('source_id') if isinstance(args.get('matching_id'), str) else None
        elif skill == 'close-stage':
            target = args.get('stage')
            if type(target) is not int or target < 1:
                return []
        else:
            target = 'project'
        return [(skill, target)] if isinstance(target, (str, int)) and target != '' else []

    def dependencies(self, aid, seen=None):
        seen = set() if seen is None else seen
        if aid in seen:
            return set()
        seen.add(aid)
        deps = set()
        for ref in self.artifacts.get(aid, {}).get('inputs', []):
            if 'source' in ref:
                deps.add((ref['source'], ref['scope']))
            elif 'artifact' in ref:
                deps.update(self.dependencies(ref['artifact'], seen))
        return deps

    def input_versions(self, aid, seen=None):
        seen = set() if seen is None else seen
        if aid in seen:
            return {}
        seen.add(aid)
        out = {}
        for ref in self.artifacts.get(aid, {}).get('inputs', []):
            if 'source' in ref:
                out[ref['source'], ref['scope']] = ref['revision']
            elif 'artifact' in ref:
                out.update(self.input_versions(ref['artifact'], seen))
        return out

    def reopen_observed_changes(self):
        for goal, aid in list(self.completed.items()):
            deps = self.input_versions(aid)
            if goal[0] == 'commit-adjustment':
                deps = {key:value for key,value in deps.items() if key == (goal[1], 'case/' + goal[2])}
            if any((sid, scope) in self.versions and self.versions[sid, scope] != revision
                   and sid in self.notified for (sid, scope), revision in deps.items()):
                self.cycles[goal] += 1
                del self.completed[goal]

    def learn(self, obj):
        if not isinstance(obj, dict):
            return
        aid = obj.get('id')
        if isinstance(aid, str) and isinstance(obj.get('versions'), dict):
            self.versions.update({(aid, scope):revision for scope, revision in obj['versions'].items()})
        if isinstance(aid, str) and obj.get('kind') and isinstance(obj.get('inputs'), list):
            self.artifacts[aid] = deepcopy(obj)
            if obj.get('current') is True:
                self.versions.update(self.input_versions(aid))
        self.reopen_observed_changes()

    def observe(self, call, result):
        goals = self.identities(call)
        if result.get('ok') is False:
            for goal in goals:
                # A public stale rejection may establish reopening before a query
                # reveals the exact changed revision; never consult future events.
                if goal in self.completed and result.get('error', {}).get('code') == 'STALE_ARTIFACT':
                    deps = self.dependencies(self.completed[goal])
                    if any(sid in self.notified for sid, _ in deps):
                        self.cycles[goal] += 1
                        del self.completed[goal]
                if goal in self.completed:
                    continue
                key = goal, self.cycles[goal]
                self.failures[key] += 1
                if self.failures[key] >= 3:
                    self.terminal = dict(goal=list(goal), cycle=self.cycles[goal], failures=self.failures[key])
            return
        if result.get('ok') is not True:
            return
        self.learn(result.get('object'))
        for item in result.get('items', []):
            self.learn(item.get('decision_metadata'))
        self.learn(result)
        aid = result.get('id')
        if isinstance(aid, str) and result.get('kind') and isinstance(result.get('inputs'), list):
            self.artifacts[aid] = deepcopy(result)
        for goal in goals:
            # Completion is the phase-specific, current accepted artifact,
            # never a generic ok, a query, or the model's own assertion.
            if aid in self.artifacts and result.get('kind') == KINDS[goal[0]] and (
                    result.get('current') is True or result.get('kind') == 'stage_close' and result.get('notices')):
                self.completed[goal] = aid
        for notice in result.get('notices', []):
            self.notified.add(notice['source_id'])
            scope = {'note_withdrawal': 'notes', 'invoice_party_correction': 'invoices'}.get(notice.get('type'))
            if notice.get('type') == 'feed_correction':
                scope = 'records'
            if scope is None:
                continue
            changed = (notice['source_id'], scope)
            for goal, artifact in list(self.completed.items()):
                if changed in self.dependencies(artifact):
                    self.cycles[goal] += 1
                    del self.completed[goal]

    def summary(self):
        return dict(K=3, B=self.B, mapping='financeops_public_artifact_obligations_v1',
            local_failure_boundary='third_failure_terminates_task', terminal=self.terminal,
            counters=[dict(goal=list(g), cycle=c, failures=n) for (g, c), n in self.failures.items()],
            no_total_response_or_cumulative_token_cap=True)


class RecordOnly:
    def observe(self, call, result):
        try:
            super().observe(call, result)
        except AttributeError:
            if result.get('ok') is not False or result.get('error', {}).get('code') != 'ARTIFACT_KIND':
                raise
        finally:
            # The inherited mapping records failures; none is an online stop.
            self.terminal = None

    def summary(self):
        result = super().summary()
        result.update(K=None, local_failure_mode='record_only',
                      local_failure_boundary='record_only', terminal=None,
                      counters_carried_from_saved_events=True)
        return result
