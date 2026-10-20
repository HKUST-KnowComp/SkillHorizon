"""Public reference: consume compact facts, cache observations, refresh changed sources."""
from skillhorizon.dataflow_v5.witness import Witness as BusinessWitness
import json


class Witness(BusinessWitness):
    def skill(self, name, **arguments):
        if name != 'review-credit-note':
            return super().skill(name, **arguments)
        key = (arguments['source_id'], self.sources[arguments['source_id']]['versions']['notes'],
               json.dumps(arguments, sort_keys=True))
        if not hasattr(self, '_notes'):
            self._notes = {}
        if key not in self._notes:
            self._notes[key] = super().skill(name, **arguments)
        return self._notes[key]

    def inventory(self, stage):
        entries = super().inventory(stage)
        for entry in entries:
            if 'decision_metadata' in entry:
                self.sources[entry['id']] = entry['decision_metadata']
        return entries

    def solve(self, key, refresh=False):
        source = self.source(key, refresh)
        if source['kind'] != 'feed':
            return super().solve(key)
        identity, mapping, _ = self.identity_choice(source, accounts=())
        contract = next(c for c in source['contracts'] if c['format'] == source['format'])
        normalized = self.skill('normalize-source', source_id=key, contract_id=contract['id'],
            identity_source_id=identity['id'], identity_map_id=mapping['id'],
            conflict_policy=source['conflict_rule'])
        output = self.skill('aggregate-source', normalized_id=normalized['id'])
        self.identity_accounts[key] = {row['account'] for row in normalized['data']}
        self.identity_signatures[key] = self.identity_choice(source, self.identity_accounts[key])[2]
        self.outputs[key, 'default'] = output['id']
