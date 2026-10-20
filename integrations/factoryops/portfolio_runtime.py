"""Compact public projections; complete native business evidence is preserved."""
from copy import deepcopy
from .kernel import BusinessError, schema

REVISION='factoryops_portfolio150_v1'
RUN_FIELDS=('run_id','order_id','program_id','release_id','run_status','scheduled_start','scheduled_end',
    'rework','observed_at','output_lot_id','disposition_id','planned_cycle_count','setup_seconds')


def pick(value, fields):
    return {k:deepcopy(value[k]) for k in fields if k in value}


class PortfolioProduction:
    def __init__(self,task,runtime):
        super().__init__(task,runtime)
        if self.public.get('instance_revision')!=REVISION:return
        self.metadata['read_material_lot']={'description':'Read an explicitly named material lot and its current units. No implicit source selection.',
            'input_schema':schema({'lot_id':{'type':'string','minLength':1}})}
        self.metadata['poll_production_updates']['description']='Poll the public tooling update after all orders through its after_level have accepted outputs; only future executions are invalidated.'

    def dispatch(self,name,args):
        if self.public.get('instance_revision')!=REVISION:return super().dispatch(name,args)
        if name=='read_material_lot':
            lot=self.owned('lot',args['lot_id'])
            return {'lot':lot,'units':[self.owned('unit',i) for i in lot['unit_ids']]}
        if name=='inspect_program_detail' and not args.get('include_audit_summary',False):
            program=self.owned('program',args['program_id'])
            cell=self.owned('robot_cell',program['cell_id'])
            blockers,warnings=self.logic._readiness(self.sm,cell,program,strict_release_required=False)
            data={'program':program,'waypoints':sorted([self.owned('waypoint',i) for i in program['waypoint_ids']],key=lambda w:w['sequence_index']) if args.get('include_waypoints',True) else [],
                'active_tool':self.owned('robot_tool',program['active_tool_id']),
                'safety_zones':[self.owned('safety_zone',i) for i in program['safety_zone_ids']],
                'quality_profile':self.owned('quality_check_profile',program['quality_profile_id']),
                'readiness_diagnostics':blockers+warnings,'audit_summary':[]}
        else:
            data=super().dispatch(name,args)
        if name=='inspect_program_detail':
            # Repeating every unrelated compatible program on each inspection
            # makes output grow quadratically with the number of work orders.
            data['active_tool'].pop('compatible_program_ids',None)
            for zone in data['safety_zones']:zone.pop('linked_program_ids',None)
            return data
        if name in ('schedule_program_run','execute_program_run','cancel_program_run'):
            return pick(data,RUN_FIELDS)
        if name=='validate_production_program':
            return pick(data,('validation_id','program_id','checks','passed','validated_at'))
        if name=='publish_program_release':
            return pick(data,('release_id','program_id','validation_id','released_at'))
        if name=='update_tool_calibration':
            return pick(data,('tool_id','revision','calibration_id','calibration_status','compensation_units','last_calibrated_at'))
        if name=='collect_run_observations':
            return {'run':pick(data['run'],RUN_FIELDS), 'units':data['units'],
                'checks':[pick(c,('check_id','run_id','release_id','profile_id','unit_id','metric_name','measured_value','expected_min','expected_max')) for c in data['checks']]}
        if name=='finalize_program_run':
            return {k:v for k,v in data.items() if k!='quality_check_ids'}
        return data

    def poll_updates(self):
        if self.public.get('instance_revision')!=REVISION:return super().poll_updates()
        if self.state['applied_updates']:return {'status':'drained','events':[]}
        planned=self.state['scheduled_updates'][0]
        preceding=[o for o in self.state['orders'].values() if o['level']<=planned['after_level']]
        if not all(len(self.available_output(o['order_id']))==o['output_quantity'] for o in preceding):
            return {'status':'awaiting_declared_stage','events':[], 'prerequisites':self.update_prerequisites(planned)}
        tool=self.owned('robot_tool',planned['tool_id'])
        if self.busy(tool['cell_id']):
            raise BusinessError('CELL_OCCUPIED',self.cell_occupancy(tool['cell_id']))
        after=self.update('robot_tool',tool['tool_id'],{'revision':tool['revision']+1,'calibration_status':'due','calibration_id':None,'compensation_units':0})
        self.state['tool_biases'][tool['tool_id']]=planned['new_bias_units']
        released={**{k:v for k,v in planned.items() if k!='new_bias_units'},'tool_revision':after['revision'],'released_at':self.now()}
        self.state['applied_updates'].append(released)
        self.event('tool.drifted',tool['tool_id'],tool['cell_id'],{'update':released,'before':tool,'after':after},environment=True)
        return {'status':'update_released','events':[deepcopy(released)]}
