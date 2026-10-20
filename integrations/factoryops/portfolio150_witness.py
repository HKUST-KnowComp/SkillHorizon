"""Public-only, cached reference; no runtime, private inputs or grader imports."""
from datetime import datetime, timedelta


async def run(call):
    task=await call('read_task');actor=task['actor']
    for skill in task['skills']:await call('read_skill_file',skill_id=skill['name'])
    orders={o['order_id']:o for o in task['orders']}
    stock={};deviations={};dispositions={};completed=set();updates=set()
    now=task['origin']
    def date(s):return datetime.fromisoformat(s.replace('Z','+00:00'))
    def slot(order,duration):
        candidate=date(now)
        for a,b in sorted((date(r['scheduled_start']),date(r['scheduled_end'])) for r in task['equipment_calendar']
                if any(r[k]==order[k] for k in ('cell_id','tool_id','fixture_id'))):
            if candidate+timedelta(seconds=duration)<=a:break
            if candidate<b:candidate=b
        return candidate.isoformat().replace('+00:00','Z')
    for order in sorted(orders.values(),key=lambda o:(o['level'],o['order_id'])):
        detail=await call('inspect_program_detail',program_id=order['program_id'])
        program=detail['program'];tool=detail['active_tool'];profile=detail['quality_profile']
        if program['process_offset_units']!=0:
            program=await call('update_program_metadata',program_id=program['program_id'],process_offset_units=0,actor=actor)
        maximum=min(z['max_speed_mm_s'] for z in detail['safety_zones'])
        for point in detail['waypoints']:
            if point['speed_mm_s']>maximum:
                await call('update_waypoint_pose',waypoint_id=point['waypoint_id'],speed_mm_s=maximum,actor=actor)
        if tool['calibration_status']!='valid':
            measurement=await call('measure_tool_calibration',tool_id=tool['tool_id'])
            await call('update_tool_calibration',tool_id=tool['tool_id'],calibration_id=measurement['calibration_id'],compensation_units=-measurement['observed_bias_units'],actor=actor)
        validation=await call('validate_production_program',program_id=program['program_id'])
        if not validation['passed']:raise ValueError('Reference program failed public validation')
        release=await call('publish_program_release',program_id=program['program_id'],validation_id=validation['validation_id'],actor=actor)
        if order['level']==0:
            lot=await call('read_material_lot',lot_id=order['raw_lot_ids'][0])
            groups=[[u['unit_id']] for u in lot['units'] if u['status']=='available']
        else:
            groups=[[] for _ in range(order['output_quantity'])]
            for part in order['bom']:
                n=part['units_per_output'];parent=stock[part['parent_order_id']]
                for group in groups:
                    if len(parent)<n:raise ValueError('Insufficient observed supplier stock')
                    group.extend(parent[:n]);del parent[:n]
        accepted=[];rework=False;attempts=0
        while groups:
            attempts+=1
            if attempts>20:raise ValueError('Construction reference failed to recover; not a model local limit')
            pressure=[];shims=[]
            for group in groups:
                fit=sum(deviations.get(i,{}).get('fit_units',0) for i in group)
                torque=sum(deviations.get(i,{}).get('torque_units',0) for i in group)
                p=-(fit+torque)//2;s=-fit-p
                pressure.append(p);shims.append(s)
            policy=task['coupled_material_policy']
            duration=len(groups)*program['cycle_time_sec']+sum(abs(p)*policy['pressure_seconds_per_unit']+abs(s)*policy['shim_seconds_per_unit'] for p,s in zip(pressure,shims))
            start=slot(order,duration)
            attempt=await call('schedule_program_run',order_id=order['order_id'],release_id=release['release_id'],start_at=start,
                input_groups=groups,rework=rework,operator_name=actor,pressure_units=pressure,shim_units=shims)
            if now!=attempt['scheduled_start']:
                now=(await call('advance_production_clock',to_time=attempt['scheduled_start']))['now']
            await call('execute_program_run',run_id=attempt['run_id'],actor=actor)
            now=(await call('advance_production_clock',to_time=attempt['scheduled_end']))['now']
            observed=await call('collect_run_observations',run_id=attempt['run_id'])
            targets={m['metric_name']:m['target'] for m in profile['metric_definitions']}
            failed=set()
            for c in observed['checks']:
                deviations.setdefault(c['unit_id'],{})[c['metric_name']]=c['measured_value']-targets[c['metric_name']]
                if not c['expected_min']<=c['measured_value']<=c['expected_max']:failed.add(c['unit_id'])
            uids=[u['unit_id'] for u in observed['units']];passed=[i for i in uids if i not in failed]
            disposition=await call('finalize_program_run',run_id=attempt['run_id'],accepted_unit_ids=passed,quarantined_unit_ids=sorted(failed),operator_name=actor)
            accepted.extend(passed)
            for uid in passed:dispositions[uid]=disposition['disposition_id']
            groups=[[i] for i in uids if i in failed];rework=True
        if len(accepted)!=order['output_quantity']:raise ValueError('Reference accepted quantity mismatch')
        stock[order['order_id']]=accepted;completed.add(order['order_id'])
        for event in task['tooling_updates']:
            if event['event_id'] not in updates and all(o['order_id'] in completed for o in orders.values() if o['level']<=event['after_level']):
                update=await call('poll_production_updates')
                updates.update(e['event_id'] for e in update['events'])
    terminal=[u for oid in task['terminal_order_ids'] for u in stock[oid]]
    return await call('reconcile_production_handoff',unit_ids=terminal,disposition_ids=sorted({dispositions[u] for u in terminal}))
