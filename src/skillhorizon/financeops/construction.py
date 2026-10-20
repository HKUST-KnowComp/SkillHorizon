"""Remove identifier/position rules without restricting legal tool use or caching."""
from copy import deepcopy
from decimal import Decimal
import random

from skillhorizon.dataflow_v5.construction import make_project as legacy_project


def remap(value, names):
    if isinstance(value, str):
        return names.get(value, value)
    if isinstance(value, list):
        return [remap(v, names) for v in value]
    if isinstance(value, dict):
        return {names.get(k, k): remap(v, names) for k, v in value.items()}
    return value


def feed_facts(project, rng):
    sources = list(project['sources'].values())
    registries = [s for s in sources if s['stage'] == 1]
    schemas = [dict(order='order_id', account='account', amount='amount', version='revision', kind='event'),
               dict(order='sale_ref', account='client_ref', amount='gross_value', version='edition', kind='revision_kind'),
               dict(order='line_ref', account='party', amount='net_value', version='sequence', kind='record_type')]
    for s in sources:
        old = next(c for c in s['contracts'] if c['format'] == s['format'])
        rows = [{k: r[v] for k, v in old['columns'].items()} for r in s['rows']]
        minor = rng.choice([True, False])
        s['format'] = 'minor_feed' if minor else 'major_feed'
        cols = deepcopy(rng.choice(schemas))
        s['rows'] = []
        for row in rows:
            cents = int(Decimal(str(row['amount'])) * (100 if old['unit'] == 'major' else 1))
            row['amount'] = cents if minor else f'{cents // 100}.{cents % 100:02d}'
            s['rows'].append({cols[k]: v for k, v in row.items()})
        s['contracts'] = [dict(id='profile-'+str(n), format=unit+'_feed', columns=deepcopy(cols),
                               unit=unit, rate_numerator=rng.randint(97, 113), rate_denominator=100)
                          for unit, n in zip(['minor', 'major'], rng.sample(range(100000, 1000000), 2))]
        rng.shuffle(s['contracts'])
        # Conflict cases genuinely exercise the chosen rule; correction events remain meaningful.
        s['conflict_rule'] = rng.choice(['first', 'latest_revision'])
        if project['variant'] == 'branch_specific_transforms':
            s['identity_source_id'] = s['id']
        else:
            s['identity_source_id'] = s['id'] if s['stage'] == 1 else rng.choice(registries)['id']
        s['identity_maps'][1]['from_stage'] = (project['stages'] + 1
            if project['variant'] == 'branch_specific_transforms' else rng.randint(2, max(2, project['stages'])))
    for event in project['events']:
        if event['kind'] == 'feed_correction':
            old_stage = project['sources'][event['source_id']]['stage']
            candidates = [s for s in sources if s['stage'] == old_stage]
            s = rng.choice(candidates)
            s['conflict_rule'] = 'latest_revision'
            c = next(c for c in s['contracts'] if c['format'] == s['format'])
            s['rows'][-1][c['columns']['kind']] = 'correction'
            col = c['columns']['amount']; old = s['rows'][-1][col]
            change = rng.randint(101, 499)
            value = old + change if c['unit'] == 'minor' else str(Decimal(old) + Decimal(change)/100)
            event.update(source_id=s['id'], row=len(s['rows'])-1, column=col, value=value)
        elif event['kind'] == 'identity_correction':
            s = rng.choice(registries)
            event.update(source_id=s['id'], mapping_id='legacy', account=rng.choice(['AC0', 'AC1']))


def maintenance_facts(project, rng):
    for s in project['sources'].values():
        for case in s['cases'].values():
            case['evidence']['status'] = rng.choice(['confirmed', 'confirmed', 'withdrawn', 'disputed'])
            booked = rng.randint(700, 1700)
            delta = rng.randint(-450, 475)
            case['evidence'].update(booked_cents=booked, settled_cents=booked+delta)
            case['claim_delta_cents'] = delta + rng.choice([-90, 0, 37, 200])
    for event in project['events']:
        if event['kind'] != 'evidence_withdrawal':
            continue
        old_stage = project['sources'][event['source_id']]['stage']
        candidates = [(s, c) for s in project['sources'].values() if s['stage'] == old_stage
                      for c in s['cases'].values() if c['evidence']['status'] == 'confirmed']
        if not candidates:
            s = rng.choice([s for s in project['sources'].values() if s['stage'] == old_stage])
            c = rng.choice(list(s['cases'].values())); c['evidence']['status'] = 'confirmed'
        else:
            s, c = rng.choice(candidates)
        event.update(source_id=s['id'], case_id=c['id'])


def receivable_facts(project, rng):
    sources = project['sources']
    for s in sources.values():
        invoices = {i['id']: i for k in s['invoice_sources'] for i in sources[k]['invoices']}
        for note in s['notes']:
            note['status'] = rng.choice(['confirmed', 'confirmed', 'withdrawn', 'disputed'])
            maximum = max(1, invoices[note['invoice_id']]['amount_cents'] // 3)
            note['evidence_cents'] = rng.randint(1, maximum)
            note['requested_cents'] = note['evidence_cents'] + rng.choice([0, 37, 90, 200])
    for event in project['events']:
        if event['kind'] == 'note_withdrawal':
            old_stage = sources[event['source_id']]['stage']
            candidates = [(s, n) for s in sources.values() if s['stage'] == old_stage
                          for n in s['notes'] if n['status'] == 'confirmed']
            if not candidates:
                s = sources[event['source_id']]
                n = next(n for n in s['notes'] if n['id'] == event['note_id']); n['status'] = 'confirmed'
            else:
                s, n = rng.choice(candidates)
            event.update(source_id=s['id'], note_id=n['id'])
        elif event['kind'] == 'invoice_party_correction':
            old_stage = sources[event['source_id']]['stage']
            candidates = []
            for s in sources.values():
                if s['stage'] >= event['after_stage']:
                    continue
                receipts = {r['id']: r for r in s['receipts']}
                invoices = {i['id']: (k, i) for k in s['invoice_sources'] for i in sources[k]['invoices']}
                for advice in s['advice']:
                    r = receipts[advice['receipt_id']]
                    if advice['evidence_customer'] != r['payer'] or advice['currency'] != r['currency']:
                        continue
                    for allocation in advice['invoice_allocations']:
                        key, invoice = invoices[allocation['invoice_id']]
                        if (sources[key]['stage'] == old_stage and invoice['customer'] == r['payer']
                                and invoice['currency'] == r['currency']):
                            candidates.append((key, invoice))
            if candidates:
                key, invoice = rng.choice(candidates)
                event.update(source_id=key, invoice_id=invoice['id'])


def make_project(family, variant, seed, stages, width, **kwargs):
    p = legacy_project(family, variant, seed, stages, width, **kwargs)
    rng = random.Random(str(seed) + ':independent-business-facts-v6')
    {'heterogeneous_integration': feed_facts, 'release_maintenance': maintenance_facts,
     'receivables_reconciliation': receivable_facts}[family](p, rng)
    # Randomize identifiers and enumeration after assigning facts. Stage remains explicit metadata.
    keys = list(p['sources']); numbers = rng.sample(range(100000000, 1000000000), len(keys))
    names = {key: 'source-' + str(n) for key, n in zip(keys, numbers)}
    names.update({'signed-' + key: 'signed-' + names[key] for key in keys})
    p = remap(p, names)
    sources = list(p['sources'].items()); rng.shuffle(sources); p['sources'] = dict(sources)
    p['task_id'] = p['task_id'].replace('DATAOPS-V5-', 'DATAOPS-V6-', 1)
    p['construction_profile'] = 'independent_business_facts_v6'
    p['goal'] += (' Source identifiers and list positions encode no business roles. Use explicit public '
                  'metadata and authoritative references. Feed discovery provides decision_metadata '
                  'without raw rows; reuse it when sufficient. Other source details remain available.')
    return p
