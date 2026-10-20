"""Same business operations; discovery exposes compact facts rather than requiring raw-row reads."""
from copy import deepcopy
from skillhorizon.dataflow_v5.runtime import Runtime as BusinessRuntime


class Runtime(BusinessRuntime):
    def search_objects(self, collection, offset, limit, stage=0):
        result = super().search_objects(collection, offset, limit, stage)
        if collection == 'sources':
            for row in result['items']:
                source = self.source(row['id'])
                if source['kind'] == 'feed':
                    row['decision_metadata'] = {k: deepcopy(source[k]) for k in
                        ('id', 'kind', 'stage', 'versions', 'format', 'conflict_rule', 'contracts',
                         'identity_source_id', 'identity_maps')}
                    row['decision_metadata_scope'] = 'complete_selection_facts; raw_rows_not_included'
        return result
