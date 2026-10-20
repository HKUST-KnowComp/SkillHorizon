"""FactoryOps portfolio: real material DAGs with compact task-local identities."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import random

from integrations.internal_mcp.io import read_json, write_json
from skillhorizon.construction_provenance import construction_entry, observe_call
from .declaration_build import build as base_build
from .build import KEYS

REVISION = 'factoryops_portfolio150_v1'
FAMILIES = ('equipment_windows', 'selective_rework', 'constrained_rework')


@construction_entry(source_recording='path_only')
def build(source, output, *, seed, levels=3, width=2, terminal_quantity=1,
          scenario='constrained_rework', dependency_pattern='mixed_age', background=False):
    if not (type(width) is int and 2 <= width <= 100 and 2 <= levels <= 5 and 1 <= terminal_quantity <= 2):
        raise ValueError('Use width 2..100, levels 2..5 and terminal quantity 1..2')
    if scenario not in FAMILIES or dependency_pattern not in ('adjacent_join','mixed_age','shared_root'):
        raise ValueError('Unknown FactoryOps construction condition')
    public=observe_call(base_build,source,output,seed=seed,levels=2,terminal_quantity=1,
        scenario='constrained_rework',dependency_pattern='distributed',background=background)
    output=Path(output); old=read_json(output/'checkpoint/world.json')
    templates={k:deepcopy(v[0]) if v else {} for k,v in old['tables'].items()}
    rng=random.Random(seed); identities=random.Random(seed+738191)
    used=set()
    def ident(kind):
        while True:
            value=kind+'-'+format(identities.getrandbits(40),'010x')
            if value not in used: used.add(value);return value
    task_id=ident('factoryops'); origin=public['origin']; actor=public['actor']
    tables={k:[] for k in KEYS}; state={k:deepcopy(v) for k,v in old['production'].items()}
    for k in ('orders','lots','units','response_profiles','tool_biases','calibrations','validations','releases','dispositions','first_pass_deviations'):
        state[k]={}
    state.update(next_id=1,dispatch=None,scheduled_updates=[],applied_updates=[],events=[])
    def add(kind, **values):
        row={**deepcopy(templates[kind]),**values,'task_id':task_id,'created_at':origin,'updated_at':origin}
        tables[kind].append(row);return row
    equipment=[]
    cell_count=min(width,rng.choice((2,3,4)))
    for n in range(cell_count):
        cell,tool,fixture,zone=(ident(k) for k in ('cell','tool','fixture','zone'))
        add('robot_cell',cell_id=cell,line_name='Work center '+str(n+1),controller_serial=ident('controller'),active_fixture_id=fixture,current_program_id=None)
        add('fixture',fixture_id=fixture,cell_id=cell,name='Assembly fixture '+str(n+1))
        add('robot_tool',tool_id=tool,cell_id=cell,name='Assembly tool '+str(n+1),compatible_program_ids=[],calibration_status='due',calibration_id=None,compensation_units=0,last_calibrated_at=None)
        add('safety_zone',zone_id=zone,cell_id=cell,name='Published operating envelope',max_speed_mm_s=rng.choice((180,220,260,300)),linked_program_ids=[])
        state['tool_biases'][tool]=rng.choice((-4,-3,-2,2,3,4))
        equipment.append((cell,tool,fixture,zone))
    layers=[]
    for level in range(levels):
        layer=[]
        for lane in range(width):
            # IDs and physical resources are independent of stage, lane and role.
            oid,pid,qid=ident('order'),ident('program'),ident('profile')
            cell,tool,fixture,zone=rng.choice(equipment)
            order=dict(order_id=oid,name='Manufacturing order '+ident('work'),level=level,lane=lane,
                cell_id=cell,tool_id=tool,fixture_id=fixture,program_id=pid,profile_id=qid,
                output_quantity=terminal_quantity if level==levels-1 else 0,
                parent_order_ids=[],raw_lot_ids=[],components_per_output=1,max_attempts=None,
                terminal=level==levels-1,bom=[])
            state['orders'][oid]=order;layer.append(order)
            targets={'fit_units':rng.randint(80,140),'torque_units':rng.randint(35,75)}
            metrics=[dict(metric_name=k,unit='synthetic_unit',target=v,expected_min=v-2,expected_max=v+2) for k,v in targets.items()]
            add('quality_check_profile',profile_id=qid,cell_id=cell,program_id=pid,name='Mandatory fit and torque',metric_definitions=metrics)
            wpids=[]; maxspeed=next(z['max_speed_mm_s'] for z in tables['safety_zone'] if z['zone_id']==zone)
            for i in range(1,rng.choice((3,4,5))+1):
                wid=ident('waypoint');wpids.append(wid)
                pose=deepcopy(templates['waypoint']['pose']);pose.update(x_mm=rng.randint(-250,250),y_mm=rng.randint(-200,200),z_mm=rng.randint(40,180))
                add('waypoint',waypoint_id=wid,cell_id=cell,program_id=pid,sequence_index=i,name='Operation point '+str(i),pose=pose,
                    speed_mm_s=rng.randint(maxspeed+1,maxspeed+90) if rng.random()<.15 else rng.randint(70,maxspeed),locked=False)
            add('program',program_id=pid,cell_id=cell,name=order['name'],active_tool_id=tool,quality_profile_id=qid,
                cycle_time_sec=rng.randint(7,19),waypoint_ids=wpids,safety_zone_ids=[zone],order_id=oid,
                process_offset_units=rng.choice((-2,-1,0,0,1,2)),status='draft',validation_id=None,release_id=None)
            next(t for t in tables['robot_tool'] if t['tool_id']==tool)['compatible_program_ids'].append(pid)
            next(z for z in tables['safety_zone'] if z['zone_id']==zone)['linked_program_ids'].append(pid)
        layers.append(layer)
    for level in range(1,levels):
        primary=rng.sample(layers[level-1],width)
        secondary=rng.sample(layers[level-1],width)
        for pos,order in enumerate(layers[level]):
            # A permutation edge makes every preceding order an actual supplier.
            parents=[primary[pos]]
            pool=layers[0] if dependency_pattern=='shared_root' and level>1 else (
                [o for layer in layers[:level] for o in layer] if dependency_pattern=='mixed_age' else layers[level-1])
            candidates=[o for o in pool if o['order_id']!=parents[0]['order_id']]
            if candidates:
                candidate=secondary[pos]
                parents.append(candidate if candidate in candidates and dependency_pattern=='adjacent_join' else rng.choice(candidates))
            order['bom']=[{'parent_order_id':o['order_id'],'units_per_output':1} for o in parents]
            order['parent_order_ids']=[o['order_id'] for o in parents];order['components_per_output']=len(parents)
    for layer in reversed(layers):
        for order in layer:
            for part in order['bom']:
                state['orders'][part['parent_order_id']]['output_quantity']+=order['output_quantity']*part['units_per_output']
    if max(o['output_quantity'] for o in state['orders'].values())>128:
        raise ValueError('Real order exceeds 128-unit public batch capacity')
    for order in state['orders'].values():
        q=order['output_quantity'];oid=order['order_id']
        state['response_profiles'][oid]={str(slot):{k:rng.choice((-1,0,1)) for k in ('fit_units','torque_units')} for slot in range(q)}
        if order['level']==0:
            lid=ident('raw');order['raw_lot_ids']=[lid];uids=[]
            for slot in range(q):
                uid=ident('stock');uids.append(uid)
                state['units'][uid]=dict(unit_id=uid,lot_id=lid,order_id=None,level=-1,output_slot=slot,parent_unit_ids=[],produced_run_id=None,
                    status='available',quality_accepted=True,reserved_by=None,consumed_by=None)
            state['lots'][lid]=dict(lot_id=lid,order_id=None,level=-1,lane=order['lane'],unit_ids=uids,produced_run_id=None,status='accepted',version=1)
    if scenario in ('selective_rework','constrained_rework'):
        for layer in layers:
            selected=rng.sample(layer,max(1,len(layer)//4))
            for order in selected:
                count=max(1,order['output_quantity']//6)
                slots=rng.sample(range(order['output_quantity']),count)
                state['first_pass_deviations'][order['order_id']]={str(s):{'fit_units':rng.choice((-5,5)),'torque_units':rng.choice((-5,5))} for s in slots}
    # Future-only drift; it does not invalidate physical work already completed.
    trigger=rng.randrange(levels-1)
    later={o['tool_id'] for layer in layers[trigger+1:] for o in layer}
    tool=rng.choice(sorted(later))
    state['scheduled_updates']=[dict(event_id=ident('update'),tool_id=tool,after_level=trigger,new_bias_units=rng.choice((-5,-3,3,5)),
        reason='Scheduled tooling drift after the declared manufacturing stage; future executions require current calibration and release.')]
    calendar=[];origin_dt=datetime.fromisoformat(origin.replace('Z','+00:00'))
    if scenario in ('equipment_windows','constrained_rework'):
        for eq in equipment:
            for start in (0,rng.randint(350,750),rng.randint(1500,2500)):
                length=rng.randint(70,180)
                calendar.append(dict(reservation_id=ident('maintenance'),kind='maintenance',cell_id=eq[0],tool_id=eq[1],fixture_id=eq[2],
                    scheduled_start=(origin_dt+timedelta(seconds=start)).isoformat().replace('+00:00','Z'),
                    scheduled_end=(origin_dt+timedelta(seconds=start+length)).isoformat().replace('+00:00','Z')))
    # Public order enumeration carries no solution order or construction seed.
    orders=list(state['orders'].values());rng.shuffle(orders)
    for key in ('scenario','topology','construction_seed','dependency_pattern','topology_seed','dependency_construction'):
        public.pop(key,None)
    public.update(task_id=task_id,instance_revision=REVISION,environment_name='FactoryOps',levels=levels,
        cell_ids=[e[0] for e in equipment],orders=deepcopy(orders),order_ids=[o['order_id'] for o in orders],
        terminal_order_ids=[o['order_id'] for o in orders if o['terminal']],equipment_calendar=calendar,
        tooling_updates=[{k:v for k,v in u.items() if k!='new_bias_units'} for u in state['scheduled_updates']],
        goal='Produce and dispatch every requested terminal unit through the exact public material BOM. Maintain valid programs, calibrations and releases; respect equipment occupancy and maintenance; measure all mandatory fit and torque values; selectively rework defective units without discarding accepted siblings. Conserve every material unit and its actual lineage. Poll scheduled tooling drift once the declared preceding stages are complete. This is a synthetic discrete manufacturing workflow, not physical robot dynamics.')
    public['policy'].update(maximum_clock_seconds=604800,work_order_attempt_limit=None,
        first_pass_variation='A transient disturbance may occur on primary batches throughout production; affected units are identifiable only from actual QC. It does not recur during rework, whose actual input retains its measured deviation.',
        public_return_version='compact_factoryops_v1',cache_contract='Reuse returned observations, measured calibrations, releases, dispositions and known material state. No mandatory readback; query details only when missing or changed.')
    world={**old,'task_id':task_id,'tables':tables,'production':state,'sequence':0,'clock':origin}
    for name in ('checkpoint','runtime'):write_json(output/name/'world.json',world)
    write_json(output/'public/task.json',public)
    write_json(output/'private/initial_obligations.json',dict(public=public,initial_tables=tables,initial_state=state))
    for skill in public['skills']:
        path=output/'public/skills'/skill['name']/'SKILL.md';text=path.read_text()
        text=text.replace('after both root orders have enough accepted output','after the publicly declared stage has enough accepted output')
        text=text.replace('bounded rework','rework within the global execution budget')
        text+='\n\n## Cached public evidence\nReuse data already returned by creation, measurement and disposition. Full details remain queryable; immediate readback is not required. `read_material_lot` retrieves a named raw lot without scanning unrelated stock. Primary batches cover a complete work order; rework may group any quarantined units of that order. There is no separate local attempt cap.\n'
        path.write_text(text)
    return public
