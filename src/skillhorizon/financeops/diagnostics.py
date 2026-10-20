"""Descriptive construction attributes, not pass/fail gates."""
from collections import Counter, defaultdict


def describe(project):
    sources = list(project['sources'].values())
    result = dict(source_count=len(sources), source_ids_opaque=all(s['id'].startswith('source-') for s in sources))
    if project['family'] == 'heterogeneous_integration':
        groups = defaultdict(Counter)
        for s in sources:
            groups[int(s['id'].split('-')[-1]) % 2][s['format']] += 1
        # Best constant prediction in each identifier-parity class, described rather than thresholded.
        result.update(formats=dict(Counter(s['format'] for s in sources)),
            conflict_rules=dict(Counter(s['conflict_rule'] for s in sources)),
            best_id_parity_format_accuracy=sum(max(c.values()) for c in groups.values())/len(sources),
            authority_sources=len({s['identity_source_id'] for s in sources}),
            compact_selection_facts_available=True)
    elif project['family'] == 'receivables_reconciliation':
        for kind in ['credit', 'refund']:
            notes = [n for s in sources for n in s['notes'] if n['kind'] == kind]
            result[kind+'_statuses'] = dict(Counter(n['status'] for n in notes))
            result[kind+'_distinct_amounts'] = len({n['evidence_cents'] for n in notes})
    else:
        result['confirmed_case_counts'] = dict(Counter(sum(c['evidence']['status'] == 'confirmed'
            for c in s['cases'].values()) for s in sources))
    return result
