"""Opt-in successor profiles. No mutation of historical checkpoints."""
from collections import Counter
from copy import deepcopy
import math
import random

PREFIX = 'fieldops_replacement_v1_'
STYLES = ('unequal_join', 'shared_feeder', 'multi_join', 'late_crosslink')


def pressure_requirements(devices, services):
    """Backward feasibility bounds using only public device/service facts."""
    need = {int(k): s['minimum_flow']+1 for k, s in services.items()}
    for key in sorted(services, key=int, reverse=True):
        s = services[key]
        inlet_need = max(max(need[int(key)], devices[d]['minimum_pressure'])
                         + max(0, devices[d]['severity']-max(devices[d]['repair_methods'].values()))
                         + devices[d]['minimum_test_load']//25 for d in s['device_ids'])
        inlet_need += sum(services[str(p)]['minimum_flow']//2 for p in s['remote_feed_stages'])
        for p in s['parent_stages']:
            need[p] = max(need[p], inlet_need + services[str(p)]['minimum_flow'])
    return need


def choose_method(device, inlet, required):
    for method in ('recalibrate', 'replace'):
        residual = max(0, device['severity'] - device['repair_methods'][method]) + device['minimum_test_load']//25
        if residual <= device['maximum_residual'] and min(device['rated_pressure'], inlet)-residual >= max(required, device['minimum_pressure']):
            return method, residual
    raise ValueError('No physically feasible public repair method')


def configure(p, seed, variant):
    successor = variant.startswith('fieldops_replacement_v2_')
    kind, style = variant[len(PREFIX):].split(':')
    if kind not in ('a', 'b') or style not in STYLES:
        raise ValueError('Unknown replacement profile')
    if p['business_scenario'] not in ('mesh_convergence', 'mesh_supply_shocks'):
        raise ValueError('Replacement profile requires a paired mesh scenario')
    recovery = p['business_scenario'] == 'mesh_supply_shocks'
    rng = random.Random(f'replacement-foundation-v{2 if successor else 1}:{kind}:{style}:{seed}')
    n = len(p['services'])
    # Variable layer widths and parent sets, with depth bounded by physical losses.
    roots = max(2, math.ceil(n/7)) if successor else max(2, round(math.sqrt(n)))
    levels = [list(range(1, roots+1))]
    remaining = list(range(roots+1, n+1))
    while remaining:
        width = min(len(remaining), rng.randint(max(2, roots-1), roots+2))
        levels.append(remaining[:width]); remaining = remaining[width:]
    layer_of = {s: i for i, layer in enumerate(levels) for s in layer}
    # Cap depth through additional inlet branches when business size grows.
    for stage, service in enumerate(p['services'].values(), 1):
        layer = layer_of[stage]
        if layer == 0 or layer > 7 and rng.random() < .45:
            parents = []
        else:
            previous = levels[layer-1]
            if style == 'unequal_join':
                pool = previous + (levels[max(0, layer-2)] if layer > 1 else [])
                parents = rng.sample(pool, min(len(pool), rng.choice((1, 1, 2))))
            elif style == 'shared_feeder':
                parents = [rng.choice(previous)]
                if layer > 1 and rng.random() < .7:
                    parents.append(rng.choice(levels[0]))
            elif style == 'multi_join':
                parents = rng.sample(previous, min(len(previous), rng.choice((1, 2, 3))))
            else:
                parents = [rng.choice(previous)]
                if layer >= 2:
                    parents.append(rng.choice(levels[rng.randrange(layer-1)]))
        service['parent_stages'] = sorted(set(parents))
        service['remote_feed_stages'] = [s for s in service['parent_stages'] if layer-layer_of[s] > 1]
    # Ensure a genuine late join and a target with a completed downstream chain.
    if n >= 6:
        p['services'][str(n)]['parent_stages'] = sorted(set([n-1, roots]))
        p['services'][str(n)]['remote_feed_stages'] = [roots]
    p['service_rules'].pop('subnetworks', None)
    p['service_rules'].update(topology=style, topology_layers=[layer for layer in levels],
                             construction_profile=variant, design_supply_pressure=112)
    if successor:
        p['service_rules']['allow_restored_rework'] = True
    # IDs are independently permuted after all graph and physical assignments.
    returns = {}
    for stage, service in enumerate(p['services'].values(), 1):
        inlet = min((returns[s] for s in service['parent_stages']), default=112)
        inlet -= sum(p['services'][str(s)]['minimum_flow']//2 for s in service['remote_feed_stages'])
        residuals = []
        for key in service['device_ids']:
            d = p['devices'][key]
            if kind == 'a':
                typ = rng.choice(('pump', 'valve', 'regulator'))
                severity = rng.randint(6, 13)
                d.update(type=typ, severity=severity, repair_methods={
                    'recalibrate': rng.randint(5, 12), 'replace': severity+rng.randint(0, 2)},
                    duration=rng.choice((1, 2, 3)), minimum_test_load=rng.choice((50, 60, 75)),
                    maximum_residual=rng.choice((3, 4, 5)))
            else:
                d.update(severity=rng.randint(6, 12), repair_methods={'recalibrate':9, 'replace':14})
            d.update(predecessors=[v for parent in service['parent_stages'] for v in p['services'][str(parent)]['device_ids']],
                     earliest_start=0, rated_pressure=200)
            best = d['minimum_test_load']//25
            d['minimum_pressure'] = inlet-best-rng.choice((0, 1, 2, 3))
            residuals.append(best)
        returns[stage] = inlet-max(residuals)-service['minimum_flow']
        if returns[stage] <= 10:
            raise ValueError('Profile exceeds physical pressure depth; change business topology')
    need = pressure_requirements(p['devices'], p['services'])
    returns, required_parts = {}, Counter()
    for stage, service in enumerate(p['services'].values(), 1):
        inlet = min((returns[s] for s in service['parent_stages']), default=112)
        inlet -= sum(p['services'][str(s)]['minimum_flow']//2 for s in service['remote_feed_stages'])
        residuals=[]
        for key in service['device_ids']:
            d=p['devices'][key]; method,residual=choose_method(d,inlet,need[stage]);residuals.append(residual)
            if method=='replace': required_parts[d['type']] += 1
        returns[stage]=inlet-max(residuals)-service['minimum_flow']
    types=sorted({d['type'] for d in p['devices'].values()})
    p['parts']={f'SP-{i+1}': {'compatible_type':typ, 'quantity':required_parts[typ]+1,
                'initial_quantity':required_parts[typ]+1} for i,typ in enumerate(types)}
    keys=list(p['devices']); renamed=keys[:];rng.shuffle(renamed);mapping=dict(zip(keys,renamed))
    devices={}
    for key,d in p['devices'].items():
        d['predecessors']=sorted(mapping[x] for x in d['predecessors']);devices[mapping[key]]=d
    p['devices']=dict(sorted(devices.items()))
    for key,s in p['services'].items():
        s['device_ids']=sorted(mapping[d] for d in s['device_ids'])
        p['device_layout']['stages'][key]={'device_ids':s['device_ids'], 'severity_roles':['public_physical_properties']*len(s['device_ids'])}
    p['device_layout']['revision']='heterogeneous_public_facts_v1'
    # Mix short, long and recovery-capable windows. Times are only learned by reading.
    windows={}
    for i in range(n*7):
        width=rng.choice((3,4,5,7,8)) if kind=='a' else rng.choice((4,6,8))
        windows[f'W{i+1}']={'start':i*10, 'end':i*10+width}
    p['windows']=windows
    p['scheduled_change']=None; p['scheduled_changes']=[]
    p['instance_variant']=variant;p['project_id']+='-'+variant.replace(':','-')
    p['physical_history']={key:{str(d['revision']):deepcopy(d)} for key,d in p['devices'].items()}
    if recovery and kind=='a':
        depths={}
        for key,s in p['services'].items():depths[int(key)]=1+max((depths[t] for t in s['parent_stages']),default=0)
        p['scheduled_changes']=[dict(kind='supply_pressure',trigger='active_service_progress',
            minimum_active_services=math.ceil(n*f),minimum_active_depth=max(2,math.ceil(max(depths.values())*f)),
            pressure=pressure,applied=False) for f,pressure in ((.5,116),(.8,112))]
    elif recovery:
        from .services import descendants
        candidates=[s for s in range(1,n+1) if 1<=len(descendants(p,s))<=n-3]
        if successor and n >= 32:
            candidates=[s for s in candidates if 2 <= len(descendants(p,s)) <= max(3,n//12)]
            if not candidates:
                raise ValueError('No nontrivial local dependency closure in this topology')
        source=rng.choice(candidates)
        target=rng.choice(p['services'][str(source)]['device_ids'])
        p['scheduled_changes']=[dict(kind='local_device_fault',target=target,minimum_active_services=math.ceil(n*.7),
                                    severity=p['devices'][target]['repair_methods']['recalibrate']+4,applied=False)]
    p['goal']=p['goal'].split(' This project has')[0].split(' This regional')[0].split(' More than one supply correction')[0]
    p['goal']+=' Read the actual parent_stages, remote_feed_stages, device facts and window times; IDs and list positions do not encode business choices. '
    p['goal']+='Different devices may require different methods, minimum loads and work durations. Spare stock is shared and finite. The public design supply is 112; retain enough pressure for downstream minimum-pressure obligations. '
    if kind=='b':
        p['goal']+='A local fault update may revise an already repaired device. A repair is reusable only when its device_revision equals the current device revision. Dependent certifications can become stale while their physical repairs remain valid; unrelated active services remain usable. Historical measurements retain their original physical conditions. After a local update, affected maintenance must start no earlier than the affected device effective_from. '
    if recovery and kind=='a':p['goal']+='Two progress-triggered supply corrections can occur, to 116 then 112. Renew affected certifications using current upstream evidence. '
    if successor:
        p['goal'] += ('A restored boundary may be isolated again for corrective work. Use a new feasible window '
            'after the latest completed work in that service and its descendants, with no affected open isolation. '
            'Re-isolation suspends that service and its downstream certifications while preserving physical repairs '
            'and unrelated services. A current repair can be reused by retest-device; repair-device may replace '
            'an inadequate method. Released reservations remain part of the historical schedule. ')
