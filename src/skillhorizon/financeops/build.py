"""CPU8 construction, public witness, independent score and exact trace measurement."""
from copy import deepcopy
import json
from pathlib import Path
import time
from . import FORMAT
from .construction import make_project
from .runtime import Runtime
from skillhorizon.dataflow_v5.verification import score
from .witness import Witness
from skillhorizon.dataflow_v5.skills import SKILLS,documentation,tools
from skillhorizon.financeops.diagnostics import describe

def write(path,data):
 path=Path(path);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)

def attributes(initial):
 sources=initial['sources'];spans=[];policies={};dual=shared=conflicts=partial=0;currencies=set();policy_deltas={}
 for s in sources.values():
  if s['kind']=='feed':spans.append(s['stage']-sources[s['identity_source_id']]['stage'])
  elif s['kind']=='maintenance':
   dual+=len(s['consumer_views'])>1
   components=[c['component'] for c in s['cases'].values()];shared+=len(components)>len(set(components))
   for policy in s['policies']:
    if s['region'] in policy['regions'] and policy['from_stage']<=s['stage']<=policy['to_stage']:
     policies[policy['id']]=policies.get(policy['id'],0)+1
     policy_deltas.setdefault(policy['id'],[]).extend(c['evidence']['settled_cents']-c['evidence']['booked_cents'] for c in s['cases'].values())
  else:
   currencies.update(i['currency'] for i in s['invoices'])
   spans.extend(s['stage']-sources[k]['stage'] for k in s['invoice_sources'])
   invoices={i['id']:i for k in s['invoice_sources'] for i in sources[k]['invoices']};receipts={r['id']:r for r in s['receipts']};seen=set()
   for claim in s['advice']:
    r=receipts[claim['receipt_id']]
    if r['transaction_id'] in seen:continue
    seen.add(r['transaction_id'])
    valid=[a for a in claim['invoice_allocations'] if claim['evidence_customer']==r['payer'] and claim['currency']==r['currency'] and invoices[a['invoice_id']]['customer']==r['payer'] and invoices[a['invoice_id']]['currency']==r['currency']]
    conflicts+=len(valid)<len(claim['invoice_allocations']);partial+=0<len(valid)<len(claim['invoice_allocations'])
 return dict(logical_stage_count=initial['stages'],max_public_evidence_stage_span=max(spans,default=0),max_event_source_stage_span=max((e['after_stage']-sources[e['source_id']]['stage'] for e in initial['events']),default=0),applicable_policy_counts=policies,dual_consumer_sources=dual,shared_component_sources=shared,advice_transactions_requiring_adjustment=conflicts,partially_valid_advice_transactions=partial,environment_signed_artifacts=len(initial['artifacts']),event_types=sorted(set(e['kind'] for e in initial['events'])),causal_decision_depth='not_measured',accounting_currencies=sorted(currencies),applicable_policy_delta_ranges={k:[min(v),max(v)] for k,v in policy_deltas.items()},invoice_id_semantics='opaque_equality_only',identity_dependency_scope='selected_mapping_and_used_account_values')

def build(spec,output):
 root=Path(output);root.mkdir(parents=True,exist_ok=False);started=time.monotonic();write(root/'STATUS.json',{'status':'running','id':spec['id'],'provider_calls':0})
 try:
  initial=make_project(**spec['parameters']);initial['task_id']+='-'+spec['id'];(root/'initial').mkdir();write(root/'initial/project.json',initial)
  # Each task ships only public observations, generic schemas and actual skill files.
  (root/'reference').mkdir();write(root/'reference/project.json',initial)
  runtime=Runtime(root/'reference',persist=False);brief=runtime.read_project()
  task=dict(format=FORMAT,task_id=initial['task_id'],config=spec['parameters'],execution_mode='procedural',skill_protocol='explicit_skill_step_arguments_v1',checkpoint_action_count=0,history_source='environment_origin_not_agent_history',messages=[{'role':'user','content':json.dumps(brief,ensure_ascii=False)}],tools=tools(),skill_ids=list(SKILLS),runtime_entrypoint='skillhorizon.financeops.runtime.Runtime',reference_policy='public_decision_metadata_v6',parent_task=spec.get('parent_task'),model_interface_qualification='not_run',private_reference_directory='controller_only_do_not_export')
  write(root/'task.json',task)
  for name in SKILLS:
   directory=root/'initial/skills'/name;directory.mkdir(parents=True);(directory/'SKILL.md').write_text(documentation(name))
   if SKILLS[name][2]:
    (directory/'scripts').mkdir();(directory/'scripts/arithmetic.py').write_text((Path(__file__).parent.parent/'dataflow_v5/arithmetic.py').read_text())
  policy=Witness(runtime.call);policy.run();runtime.save();result=score(runtime,initial);write(root/'SCORE.json',result)
  if not result['task_success']:raise ValueError('Reference failed independent business scoring: '+json.dumps(result['errors'][:3]))
  from skillhorizon.experiment.history_measurement import tokens
  call_tokens=result_tokens=read_tokens=repeat_tokens=0;seen=set();calls=0
  query_collections={};first_business_seen=False;initial_query_calls=initial_query_tokens=0
  for line in (root/'reference/actions.jsonl').open():
   r=json.loads(line);calls+=1;call_tokens+=tokens({'type':'call','name':r['skill_id'],'arguments':r['arguments']});nt=tokens({'type':'result','result':r['result']});result_tokens+=nt
   if r['skill_id'] in ['read_project','search_objects','read_object','read_skill_file']:
    read_tokens+=nt;k=json.dumps([r['skill_id'],r['arguments'],r['result']],sort_keys=True,separators=(',',':'))
    label=r['skill_id']+'/'+r['arguments'].get('collection','')
    entry=query_collections.setdefault(label,dict(calls=0,result_tokens=0));entry['calls']+=1;entry['result_tokens']+=nt
    if not first_business_seen:initial_query_calls+=1;initial_query_tokens+=nt
    if k in seen:repeat_tokens+=nt
    seen.add(k)
   else:first_business_seen=True
  total=call_tokens+result_tokens;target=spec.get('target');accepted=not target or target[0]<=total<=target[1]
  row=dict(id=spec['id'],status='passed' if accepted else 'length_miss',provider_calls=0,task_id=task['task_id'],task_root=str(root),parameters=spec['parameters'],band=spec.get('band'),variant=spec['parameters']['variant'],family=spec['parameters']['family'],L_business_ref=total,call_tokens=call_tokens,result_tokens=result_tokens,read_result_tokens=read_tokens,identical_repeat_read_result_tokens=repeat_tokens,reference_calls=calls,business_calls=policy.business_calls,business_success=True,events_released=result['events_released'],repairs=policy.repairs,work_packages=result['total_work_packages'],decision_sites=result['constructed_decision_sites'],structure=attributes(initial),shortcut_diagnostics=describe(initial),parallel_width=spec['parameters']['width'],elapsed_seconds=round(time.monotonic()-started,3),model_interface_qualified=False,reference_policy='public_decision_metadata_v6',parent_task=spec.get('parent_task'))
  row['query_measurement']=dict(before_first_business_calls=initial_query_calls,before_first_business_result_tokens=initial_query_tokens,by_tool_collection=query_collections)
  write(root/'STATUS.json',row);return row
 except Exception as e:
  row=dict(id=spec['id'],status='failed',provider_calls=0,error_type=type(e).__name__,error=str(e)[:1500],elapsed_seconds=round(time.monotonic()-started,3));write(root/'STATUS.json',row);return row
