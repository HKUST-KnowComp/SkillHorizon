"""FieldOps independent public business obligations; no private state/scoring access."""
from collections import Counter


from .execution_clock import public_limit, LEGACY

class FieldObligationPolicy:
    def __init__(self, B, *, declarations_consume_B=False, protocol_recovery_limit=4,
                 local_failure_limit=3, wall_time_seconds=None):
        self.local_failure_limit=local_failure_limit
        self.wall_time_seconds=wall_time_seconds
        self.B=B;self.declarations_consume_B=declarations_consume_B
        self.protocol_recovery_limit=protocol_recovery_limit
        self.devices={};self.services={};self.artifacts={}
        self.completed=set();self.cycles=Counter();self.failures=Counter()
        self.failure_events=[];self.terminal=None

    def charge(self, call):
        return int(self.declarations_consume_B or call['name'] not in {'begin_skill','finish_skill'})

    def before(self, call):
        return self.terminal

    def stage(self, args):
        if isinstance(args.get('arguments'),dict):
            return self.stage(args['arguments'])
        if type(args.get('stage')) is int:
            return str(args['stage'])
        keys=args.get('device_ids', [args['device_id']] if isinstance(args.get('device_id'),str) else [])
        if isinstance(keys,list) and keys:
            stages={str(self.devices[k]['stage']) for k in keys if isinstance(k,str) and k in self.devices}
            if len(stages)==1 and all(isinstance(k,str) and k in self.devices for k in keys):
                return next(iter(stages))
        for field in ('plan_id','draft_id','check_id','isolation_id','repair_id'):
            key=args.get(field)
            if isinstance(key,str) and key in self.artifacts:
                return self.stage(self.artifacts[key])
        return None

    def identity(self, call):
        if call.get('name') not in {'execute_step','run_skill_script'}:
            return None
        outer=call.get('arguments',{});args=outer.get('arguments',{})
        if not isinstance(args,dict):return None
        skill=outer.get('skill_id');stage=self.stage(args)
        if skill in {'repair-device','test-device','retest-device'}:
            key=args.get('device_id')
            if isinstance(key,str) and key:
                return ('device',key,'repair' if skill=='repair-device' else 'measurement')
        if skill=='accept-system':return ('system','all','acceptance')
        if skill=='release-isolation' and isinstance(args.get('isolation_id'),str):
            return ('isolation',args['isolation_id'],'release')
        goals={'plan-isolation':'feasible_plan','isolate-devices':'isolation',
               'restore-group':'restoration','commission-service':'commission'}
        if skill in goals and stage is not None:return ('service',stage,goals[skill])
        return None

    def reopen(self, identity):
        if identity in self.completed:
            self.completed.remove(identity);self.cycles[identity]+=1

    def invalidate_stages(self, stages, *, measurements=True):
        stages={str(s) for s in stages}
        for identity in list(self.completed):
            typ,key,goal=identity
            if (typ=='service' and key in stages and goal!='isolation') or (
                measurements and typ=='device' and goal=='measurement' and str(self.devices.get(key,{}).get('stage')) in stages
            ) or typ=='system':self.reopen(identity)

    def observe(self, call, result):
        bad=(result.get('ok') is False or 'gateway_error' in result or
             'declaration_error' in result or result.get('status')=='error')
        identity=self.identity(call)
        if bad and identity is not None and identity not in self.completed:
            key=(identity,self.cycles[identity]);self.failures[key]+=1
            event=dict(identity=list(identity),cycle=key[1],failures=self.failures[key],
                       tool=call['name'],skill_id=call.get('arguments',{}).get('skill_id'))
            self.failure_events.append(event)
            if self.local_failure_limit is not None and self.failures[key]>=self.local_failure_limit:
                self.terminal=event
        if not bad:
            name=call['name'];outer=call.get('arguments',{});args=outer.get('arguments',{})
            data=result.get('data',{})
            if name=='search_objects':
                for item in result.get('items',[]):
                    if outer.get('collection')=='devices' and 'stage' in item:
                        self.devices.setdefault(item['id'],{}).update(item)
                    elif outer.get('collection')=='services' and 'stage' in item:
                        self.services.setdefault(item['id'],{}).update(item)
                    elif outer.get('collection')=='artifacts' and 'stage' in item:
                        self.artifacts.setdefault(item['id'],{}).update(item)
            if name=='read_object':
                obj=result.get('object',{});key=outer.get('object_id')
                if outer.get('collection')=='devices' and 'stage' in obj:self.devices[key]=obj
                if outer.get('collection')=='services' and 'stage' in obj:self.services[key]=obj
                if outer.get('collection') in {'artifacts','isolations'}:
                    self.artifacts[key]=obj.get('data',obj)
            if result.get('artifact_id'):
                self.artifacts[result['artifact_id']]=data
            skill=outer.get('skill_id') if name in {'execute_step','run_skill_script'} else None
            if skill=='repair-device':
                self.reopen(('device',args['device_id'],'measurement'))
                stage=self.stage(args)
                # A repair replaces only this device's test, not its peers' tests.
                if stage:self.invalidate_stages([stage], measurements=False)
            if skill in {'restore-group','release-isolation'}:
                stage=self.stage(args)
                if stage:
                    self.reopen(('service',stage,'isolation'))
                    self.reopen(('service',stage,'feasible_plan'))
            if skill in {'test-device','retest-device'} and data.get('passed') is False:
                # A valid measurement is not K failure; it can expose ineffective repair.
                self.reopen(('device',args['device_id'],'repair'))
            complete=(skill in {'repair-device','isolate-devices','restore-group','commission-service','release-isolation','accept-system'} or
                skill=='plan-isolation' and outer.get('step_id')=='submit' or
                skill in {'test-device','retest-device'} and type(data.get('passed')) is bool)
            if complete and identity is not None:self.completed.add(identity)
            if skill=='commission-service':self.invalidate_stages(data.get('invalidated_service_stages',[]))
            for notice in result.get('notices',[]):
                if notice.get('type')=='supply_pressure_changed':
                    self.invalidate_stages(self.services.keys() | {str(d['stage']) for d in self.devices.values()})
                elif notice.get('type')=='local_device_fault':
                    self.reopen(('device',notice['device_id'],'repair'))
                    self.invalidate_stages(notice['affected_service_stages'])
                elif notice.get('type') in {'service_operating_point_changed','maintenance_reopened'}:
                    self.invalidate_stages(notice.get('affected_service_stages',[]))
        return dict(failed=bad,terminate=self.terminal is not None,local_failure=self.terminal)

    def public_instructions(self):
        local = ('Local failures are recorded per independent obligation and cycle, with no local failure stopping limit. '
                 if self.local_failure_limit is None else
                 f'Local K={self.local_failure_limit}: reaching this accumulated explicit rejection count for one '
                 'independently verifiable business obligation in its current cycle ends the entire task immediately. ')
        return ('Begin each skill execution instance with begin_skill, attach its operations using skill_run_id, '
            'and close it with finish_skill (completed/blocked/abandoned). Multiple instances may be interleaved. '
            'Public discovery queries may be unassigned. Declarations record your choices and local completion '
            'claims; they do not authorize operations or certify business success. '
            f'Execution budget B={self.B}. Each actual query, business, script or rejected attempt consumes one; '
            'begin_skill/finish_skill declarations consume zero. ' + local +
            'The independent goals are: a feasible submitted plan per service; open isolation per service; a current '
            'physical repair per device; a valid measurement report per device (test and retest share this goal; '
            'a valid passed=false measurement completes measurement, without proving restoration readiness); current '
            'restoration per service; service commissioning; safe release of each isolation; and final system acceptance. '
            'Plan draft/check/submit share the feasible-plan goal. Only public facts can identify an object. '
            'Changing parameters, artifacts, declarations, methods or fixing prerequisites does not reset unfinished '
            'failures. Actual completion closes that goal; real later invalidation opens a new cycle. Queries and '
            'accepted negative measurements or feasibility checks are observations, not K failures. The first explicit '
            'execution rejection pauses a batch; its unattempted suffix consumes no B/K and is not automatically resumed. '
            f'{self.protocol_recovery_limit} consecutive malformed action/final envelopes end the task as a separate '
            'protocol error. No total response or cumulative token cap; per-output limit8192; compaction at estimated '
            '200000 input tokens; '
            f"{public_limit(self.wall_time_seconds, getattr(self, 'time_accounting', LEGACY))}"
            'A successful final action at exactly B remains eligible for success.')

    def summary(self):
        return dict(K=self.local_failure_limit,B=self.B,formula='R+ceil(A/2)',revision='fieldops_independent_obligations_v1',
            boundary='record_only' if self.local_failure_limit is None else 'local_failure_ends_entire_task',
            local_failure_mode='record_only' if self.local_failure_limit is None else 'terminate',
            wall_time_seconds=self.wall_time_seconds, declarations_consume_B=self.declarations_consume_B,
            protocol_recovery_limit=self.protocol_recovery_limit,batch_first_failure_pause=True,
            failure_events=self.failure_events,terminal=self.terminal)
