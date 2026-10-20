"""Public stateful runtime for the three new DataOps families."""
from copy import deepcopy
import json
from pathlib import Path
from .skills import SKILLS, documentation
from . import arithmetic

class Rejection(ValueError):
 def __init__(self,code,message):self.code=code;super().__init__(message)

def require(ok,code,message):
 if not ok:raise Rejection(code,message)

class Runtime:
 def __init__(self,root,*,persist=True,record_calls=True):
  self.root=Path(root);self.p=json.loads((self.root/'project.json').read_text());self.persist=persist;self.record_calls=record_calls;self.memo={}
 def save(self):
  path=self.root/'project.json';temp=path.with_suffix('.tmp');temp.write_text(json.dumps(self.p,ensure_ascii=False));temp.replace(path)
 def source(self,key):
  s=self.p['sources'].get(key);require(s is not None and s['stage']<=self.p['released_stage'],'SOURCE_SCOPE','Source is not released');return s
 def source_ref(self,key,scope='data'):
  s=self.source(key);return {'source':key,'scope':scope,'revision':s['versions'][scope]}
 def current(self,key):
  if key in self.memo:return self.memo[key]
  a=self.p['artifacts'].get(key)
  if a is None:return False
  value=all(self.p['sources'][r['source']]['versions'][r['scope']]==r['revision'] if 'source' in r else self.current(r['artifact']) for r in a['inputs'])
  self.memo[key]=value;return value
 def artifact(self,key,kind=None,*,require_current=True):
  a=self.p['artifacts'].get(key);require(a is not None,'UNKNOWN_ARTIFACT','Unknown artifact')
  if 'source_id' in a:self.source(a['source_id'])
  require(kind is None or a['kind']==kind,'ARTIFACT_KIND','Incompatible artifact kind')
  if require_current:require(self.current(key),'STALE_ARTIFACT','Inspect changed source versions and current dependencies')
  return a
 def commit(self,kind,data,refs,**metadata):
  key=f"artifact-{len(self.p['artifacts'])+1:06d}"
  a={'id':key,'kind':kind,'data':deepcopy(data),'inputs':deepcopy(refs),**metadata};self.p['artifacts'][key]=a
  return {**deepcopy(a),'current':True}
 def read_project(self):
  return {k:deepcopy(self.p[k]) for k in ['task_id','family','variant','goal','stages','released_stage','input_closed','publication']} | {'skill_catalog':[dict(skill_id=k,description=v[0],script_available=v[2],entrypoint='SKILL.md',files=['SKILL.md']+(['scripts/arithmetic.py'] if v[2] else [])) for k,v in SKILLS.items()],'support':{'notice_policy':'source_only','current_means':'source_revision_freshness_not_semantic_correctness','historical_state':'environment_origin_not_model_history'}}
 def search_objects(self,collection,offset,limit,stage=0):
  require(type(offset)is int and type(limit)is int and offset>=0 and 1<=limit<=200,'PAGINATION','Use offset>=0 and limit 1..200')
  require(collection in ['sources','artifacts','notices'],'COLLECTION','Unknown collection')
  if collection=='sources':rows=[{'id':s['id'],'stage':s['stage'],'kind':s['kind'],'revision':s['versions']['data']} for s in self.p['sources'].values() if s['stage']<=self.p['released_stage'] and (stage==0 or s['stage']==stage)]
  elif collection=='artifacts':rows=[{k:a[k] for k in ('id','kind','source_id','stage','view') if k in a} | {'current':self.current(a['id'])} for a in self.p['artifacts'].values() if 'source_id' not in a or self.p['sources'][a['source_id']]['stage']<=self.p['released_stage']]
  else:rows=self.p['notices']
  return {'items':deepcopy(rows[offset:offset+limit]),'next_offset':offset+limit if offset+limit<len(rows) else None}
 def read_object(self,collection,object_id):
  if collection=='sources':return {'object':deepcopy(self.source(object_id))}
  require(collection=='artifacts' and object_id in self.p['artifacts'],'OBJECT','Unknown object')
  if 'source_id' in self.p['artifacts'][object_id]:self.source(self.p['artifacts'][object_id]['source_id'])
  return {'object':deepcopy(self.p['artifacts'][object_id]) | {'current':self.current(object_id)}}
 def read_skill_file(self,skill_id,path):
  require(skill_id in SKILLS,'SKILL','Unknown skill');require(path in ['SKILL.md','scripts/arithmetic.py'],'PATH','Unsupported file')
  if path=='SKILL.md':return {'text':documentation(skill_id)}
  require(SKILLS[skill_id][2],'SCRIPT','No script for this operation')
  return {'text':(Path(__file__).parent/'arithmetic.py').read_text(),'scope':'fixed_arithmetic_only; reconciliation posting uses the fixed runtime posting implementation'}
 def execute_step(self,skill_id,step_id,arguments):
  require(skill_id in SKILLS and step_id=='compute','STEP','Use a documented skill with compute step')
  require(isinstance(arguments,dict),'ARGUMENTS','Business arguments must be an object');return self.execute(skill_id,arguments)
 def run_skill_script(self,skill_id,step_id,arguments):
  require(skill_id in SKILLS and SKILLS[skill_id][2],'SCRIPT','Only documented arithmetic skills support a fixed script')
  return self.execute_step(skill_id,step_id,arguments)
 def fields(self,a,*names):require(set(a)==set(names),'ARGUMENTS','Required exact fields: '+', '.join(names))
 def execute(self,name,a):
  if name=='normalize-source':
   self.fields(a,'source_id','contract_id','identity_source_id','identity_map_id','conflict_policy');s=self.source(a['source_id']);identity=self.source(a['identity_source_id'])
   require(s['kind']=='feed' and identity['kind']=='feed','SOURCE_KIND','Expected feed/identity registry');contracts={x['id']:x for x in s['contracts']};maps={x['id']:x for x in identity['identity_maps']}
   require(a['contract_id'] in contracts and a['identity_map_id'] in maps and a['conflict_policy'] in ['first','latest_revision'],'CHOICE','Unknown normalization choice')
   rows=arithmetic.normalize(s,contracts[a['contract_id']],maps[a['identity_map_id']],a['conflict_policy'])
   refs=[self.source_ref(s['id'],'records'),self.source_ref(identity['id'],'identity-rules')]
   refs.extend(self.source_ref(identity['id'],'identity-value/'+a['identity_map_id']+'/'+account) for account in sorted({r['account'] for r in rows}))
   return self.commit('normalized',rows,refs,source_id=s['id'],choices=deepcopy(a))
  if name=='aggregate-source':
   self.fields(a,'normalized_id');v=self.artifact(a['normalized_id'],'normalized')
   return self.commit('work_output',arithmetic.aggregate(v['data']),[{'artifact':v['id']}],source_id=v['source_id'])
  if name=='review-adjustment':
   self.fields(a,'source_id','case_id','policy_id','disposition','delta_cents');s=self.source(a['source_id'])
   require(s['kind']=='maintenance' and a['case_id'] in s['cases'],'CASE','Unknown adjustment')
   require(a['policy_id'] in {x['id'] for x in s['policies']} and a['disposition'] in ['apply','reject'] and type(a['delta_cents']) is int,'DECISION','Invalid decision')
   require(a['disposition']=='apply' or a['delta_cents']==0,'REJECT_DELTA','Rejected requests cannot adjust a balance')
   return self.commit('decision',a,[self.source_ref(s['id'],'case/'+a['case_id'])],source_id=s['id'])
  if name=='prepare-adjustment':
   self.fields(a,'decision_id');d=self.artifact(a['decision_id'],'decision');s=self.source(d['source_id'])
   component=s['cases'][d['data']['case_id']]['component']
   return self.commit('plan',dict(decision_id=d['id'],component=component,component_revision=s['versions']['component/'+component]),[{'artifact':d['id']}],source_id=s['id'])
  if name=='commit-adjustment':
   self.fields(a,'plan_id');plan=self.artifact(a['plan_id'],'plan',require_current=False);s=self.source(plan['source_id'])
   if a['plan_id'] in s['committed_plans']:
    receipt=self.p['artifacts'][s['committed_plans'][a['plan_id']]]
    return {**deepcopy(receipt),'current':self.current(receipt['id']),'replayed':True}
   self.artifact(plan['id'],'plan');d=self.artifact(plan['data']['decision_id'],'decision');choice=d['data'];case=choice['case_id']
   component=plan['data']['component'];scope='component/'+component
   require(plan['data']['component_revision']==s['versions'][scope],'STALE_PLAN','This component changed; prepare a current decision again')
   policy=next(x for x in s['policies'] if x['id']==choice['policy_id'])
   require(s['region'] in policy['regions'] and policy['from_stage']<=s['stage']<=policy['to_stage'],'POLICY_SCOPE','Policy is outside its scope')
   require(abs(choice['delta_cents'])<=policy['max_delta_cents'],'POLICY_LIMIT','Adjustment exceeds selected policy limit')
   old=s['contributions'].get(case,0);new=choice['delta_cents'];previous=s['decisions'].get(case)
   s['contributions'][case]=new;s['decisions'][case]=d['id']
   if previous!=d['id']:s['versions']['decision/'+case]+=1
   if old!=new:
    s['versions'][scope]+=1;self.p['journal'].append(dict(source_id=s['id'],case_id=case,component=component,old=old,new=new,delta=new-old,decision_id=d['id']))
   self.memo={}
   receipt=self.commit('adjustment_receipt',dict(case_id=case,applied_delta_cents=new,source_balance_after_commit_cents=s['booked_cents']+sum(s['contributions'].values()),balance_semantics='historical_after_this_commit',component_revision=s['versions'][scope]),[{'artifact':d['id']},self.source_ref(s['id'],scope)],source_id=s['id'])
   s['committed_plans'][a['plan_id']]=receipt['id'];return receipt
  if name=='snapshot-component':
   self.fields(a,'source_id','view');s=self.source(a['source_id']);require(s['kind']=='maintenance','SOURCE_KIND','Expected maintenance source')
   require(a['view'] in ['current','historical'],'VIEW','Unknown consumer view')
   if a['view']=='current':require(set(s['decisions'])==set(s['cases']) and all(self.current(x) for x in s['decisions'].values()),'UNRESOLVED_CHANGES','Resolve all current evidence before current snapshot')
   amount=s['signed_snapshot_cents'] if a['view']=='historical' else s['booked_cents']+sum(s['contributions'].values())
   refs=[self.source_ref(s['id'],'signed')] if a['view']=='historical' else [self.source_ref(s['id'],scope) for scope in s['versions'] if scope.startswith(('component/','decision/'))]+[{'artifact':x} for x in s['decisions'].values()]
   return self.commit('work_output',[dict(entity=s['region']+'/'+a['view'],cents=amount)],refs,source_id=s['id'],view=a['view'])
  if name=='match-remittance':
   self.fields(a,'source_id','allocations');s=self.source(a['source_id']);require(s['kind']=='receivables','SOURCE_KIND','Expected receivables')
   require(isinstance(a['allocations'],list),'ALLOCATIONS','Expected allocation rows');receipts={r['id']:r for r in s['receipts']};invoices={}
   for key in s['invoice_sources']:
    invoices.update({r['id']:r for r in self.source(key)['invoices']})
   totals={};inv_totals={};transactions={}
   for row in a['allocations']:
    self.fields(row,'receipt_id','invoice_id','cents');rid,iid=row['receipt_id'],row['invoice_id']
    require(rid in receipts and iid in invoices and type(row['cents'])is int and row['cents']>0,'ALLOCATION','Unknown receipt/invoice or invalid amount')
    r=receipts[rid];totals[rid]=totals.get(rid,0)+row['cents'];inv_totals[iid]=inv_totals.get(iid,0)+row['cents'];tx=r['transaction_id']
    require(tx not in transactions or transactions[tx]==rid,'DOUBLE_COLLECTION','Credit a payment transaction once');transactions[tx]=rid
    require(totals[rid]<=r['amount_cents'] and inv_totals[iid]<=invoices[iid]['amount_cents'],'MONEY_CONSERVATION','Allocation exceeds receipt or invoice')
   return self.commit('matching',a['allocations'],[self.source_ref(s['id'],'remittance')]+[self.source_ref(k,'invoices') for k in s['invoice_sources']],source_id=s['id'])
  if name=='review-credit-note':
   self.fields(a,'source_id','decisions');s=self.source(a['source_id']);require(s['kind']=='receivables','SOURCE_KIND','Expected receivables');notes={r['id']:r for r in s['notes']}
   require(isinstance(a['decisions'],list) and len(a['decisions'])==len(notes) and {r['note_id'] for r in a['decisions']}==set(notes),'NOTE_COVERAGE','Supply exactly one decision per note')
   for r in a['decisions']:
    self.fields(r,'note_id','disposition','cents');require(r['disposition'] in ['apply','reject'] and type(r['cents'])is int and r['cents']>=0 and (r['disposition']=='apply' or r['cents']==0),'NOTE_DECISION','Invalid note decision: disposition must be apply or reject; cents must be a nonnegative integer; reject requires cents=0')
   return self.commit('notes',a['decisions'],[self.source_ref(s['id'],'notes')],source_id=s['id'])
  if name=='post-reconciliation':
   self.fields(a,'matching_id','notes_id');m=self.artifact(a['matching_id'],'matching');n=self.artifact(a['notes_id'],'notes');require(m['source_id']==n['source_id'],'SCOPE','Inputs describe different batches');s=self.source(m['source_id'])
   receipts={r['id']:r for r in s['receipts']};invoices={i['id']:i for sid in s['invoice_sources'] for i in self.source(sid)['invoices']};notes={x['id']:x for x in s['notes']}
   currencies={i['currency'] for i in s['invoices']}|{r['currency'] for r in receipts.values()}|{i['currency'] for i in invoices.values()}
   ledger={c:dict(cash=0,suspense=0,receivable_delta=0) for c in currencies}
   for i in s['invoices']:ledger[i['currency']]['receivable_delta']+=i['amount_cents']
   seen=set()
   for receipt in receipts.values():
    if receipt['transaction_id'] in seen:continue
    seen.add(receipt['transaction_id']);entry=ledger[receipt['currency']];entry['cash']+=receipt['amount_cents'];entry['suspense']+=receipt['amount_cents']
   for allocation in m['data']:
    ledger[receipts[allocation['receipt_id']]['currency']]['suspense']-=allocation['cents']
    ledger[invoices[allocation['invoice_id']]['currency']]['receivable_delta']-=allocation['cents']
   for decision in n['data']:
    if decision['disposition']!='apply':continue
    note=notes[decision['note_id']];entry=ledger[invoices[note['invoice_id']]['currency']]
    if note['kind']=='credit':entry['receivable_delta']-=decision['cents']
    else:entry['cash']-=decision['cents'];entry['receivable_delta']+=decision['cents']
   data=[dict(entity=kind+'/'+currency,cents=value) for currency,entries in sorted(ledger.items()) for kind,value in sorted(entries.items())]
   old=s.get('posted');new={r['entity']:r['cents'] for r in data}
   if old!=new:self.p['journal'].append(dict(source_id=s['id'],before=deepcopy(old),after=new));s['posted']=new
   return self.commit('work_output',data,[{'artifact':m['id']},{'artifact':n['id']},self.source_ref(s['id'],'invoice_balance')],source_id=s['id'])
  if name=='close-stage':
   self.fields(a,'stage','output_ids','previous_close_id');stage=a['stage'];require(type(stage)is int and 1<=stage<=self.p['released_stage'],'STAGE','Stage is not released')
   require(isinstance(a['output_ids'],list) and len(a['output_ids'])==len(set(a['output_ids'])),'DUPLICATE_INPUT','Unique output IDs required')
   outs=[self.artifact(k,'work_output') for k in a['output_ids']];expected={(s['id'],view) for s in self.p['sources'].values() if s['stage']==stage for view in s.get('consumer_views',['default'])}
   require(len(outs)==len(expected) and {(x['source_id'],x.get('view','default')) for x in outs}==expected,'COVERAGE','Exactly one output per required source and consumer view')
   rows=[r for x in outs for r in x['data']];refs=[{'artifact':x['id']} for x in outs]
   if stage>1:
    prev=self.artifact(a['previous_close_id'],'stage_close');require(prev['stage']==stage-1,'PREDECESSOR','Wrong preceding stage');rows+=prev['data']['cumulative'];refs.append({'artifact':prev['id']})
   else:require(a['previous_close_id']=='','PREDECESSOR','Stage one has no predecessor')
   out=self.commit('stage_close',{'local':arithmetic.aggregate([r for x in outs for r in x['data']]),'cumulative':arithmetic.aggregate(rows)},refs,stage=stage,output_ids=a['output_ids'])
   self.p['closes'][str(stage)]=out['id'];notices=[]
   if stage not in self.p['milestones']:
    self.p['milestones'].append(stage)
    for e in self.p['events']:
     if e['after_stage']!=stage:continue
     s=self.p['sources'][e['source_id']]
     if e['kind']=='feed_correction':s['rows'][e['row']][e['column']]=e['value'];s['versions']['records']+=1
     elif e['kind']=='identity_correction':
      next(m for m in s['identity_maps'] if m['id']==e['mapping_id'])['entities'][e['account']]=e['entity'];s['versions']['identity-value/'+e['mapping_id']+'/'+e['account']]+=1
     elif e['kind']=='evidence_withdrawal':s['cases'][e['case_id']]['evidence']['status']='withdrawn';s['versions']['case/'+e['case_id']]+=1
     elif e['kind']=='note_withdrawal':
      next(n for n in s['notes'] if n['id']==e['note_id'])['status']='withdrawn';s['versions']['notes']+=1
     elif e['kind']=='invoice_party_correction':
      next(i for i in s['invoices'] if i['id']==e['invoice_id'])['customer']=e['customer'];s['versions']['invoices']+=1
     s['versions']['data']+=1
     notice=dict(id=f"notice-{len(self.p['notices'])+1}",type=e['kind'],source_id=s['id'],revision=s['versions']['data']);self.p['notices'].append(notice);notices.append(notice)
    self.p['released_stage']=min(self.p['stages'],stage+1)
    if stage==self.p['stages']:self.p['input_closed']=True
   self.memo={}
   return {**out,'current':self.current(out['id']),'notices':notices,'input_closed':self.p['input_closed']}
  if name=='review-release':
   self.fields(a,'close_ids');require(self.p['input_closed'],'INPUT_OPEN','Not all finite business stages have closed')
   require(isinstance(a['close_ids'],list) and len(a['close_ids'])==self.p['stages'],'COVERAGE','One close per stage required');closes=[self.artifact(k,'stage_close') for k in a['close_ids']]
   require({v['stage'] for v in closes}==set(range(1,self.p['stages']+1)),'COVERAGE','Stage coverage incomplete')
   return self.commit('review',{'close_ids':a['close_ids']},[{'artifact':v['id']} for v in closes])
  if name=='publish-release':
   self.fields(a,'review_id');v=self.artifact(a['review_id'],'review');closes=[self.artifact(k,'stage_close') for k in v['data']['close_ids']];last=max(closes,key=lambda x:x['stage']);out=self.commit('publication',last['data']['cumulative'],[{'artifact':v['id']}]);self.p['publication']=out['id'];return out
  raise Rejection('SKILL','Unknown business skill')
 def call(self,name,arguments=None):
  arguments={} if arguments is None else deepcopy(arguments);self.memo={}
  try:
   require(name in ['read_project','search_objects','read_object','read_skill_file','execute_step','run_skill_script'],'TOOL','Unknown public tool')
   result={'ok':True,**getattr(self,name)(**arguments)}
  except Rejection as e:result={'ok':False,'error':{'code':e.code,'message':str(e)}}
  except (KeyError,TypeError,ValueError,IndexError) as e:result={'ok':False,'error':{'code':'INVALID_INPUT','message':str(e)[:250]}}
  self.memo={}
  if self.persist:self.save()
  if self.record_calls:
   with (self.root/'actions.jsonl').open('a') as f:f.write(json.dumps({'skill_id':name,'arguments':arguments,'result':result},ensure_ascii=False,separators=(',',':'))+'\n')
  return result
