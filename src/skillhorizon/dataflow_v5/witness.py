"""Feasible public-tool policy. No scorer, initial filesystem or private schedule access."""
class Witness:
 def __init__(self,invoke):self.invoke=invoke;self.docs=set();self.sources={};self.outputs={};self.closes={};self.calls=0;self.business_calls=0;self.decisions={};self.repairs=0;self.matchings={};self.identity_signatures={};self.identity_accounts={}
 def call(self,name,**args):
  self.calls+=1
  if self.calls>100000:raise ValueError('Reference construction call bound exhausted')
  r=self.invoke(name,args)
  if not r['ok']:raise ValueError(f"Public reference rejected at {name}: {r['error']}")
  return r
 def skill(self,name,**args):
  if name not in self.docs:self.call('read_skill_file',skill_id=name,path='SKILL.md');self.docs.add(name)
  self.business_calls+=1
  return self.call('execute_step',skill_id=name,step_id='compute',arguments=args)
 def source(self,key,refresh=False):
  if refresh or key not in self.sources:self.sources[key]=self.call('read_object',collection='sources',object_id=key)['object']
  return self.sources[key]
 def inventory(self,stage):
  offset=0;out=[]
  while offset is not None:
   p=self.call('search_objects',collection='sources',offset=offset,limit=200,stage=stage);out+=p['items'];offset=p['next_offset']
  return out
 def identity_choice(self,s,accounts=None):
  identity=self.source(s['identity_source_id']);maps=[m for m in identity['identity_maps'] if m['from_stage']<=s['stage']<=m['to_stage']];obsolete={m['supersedes'] for m in maps};mapping=next(m for m in maps if m['id'] not in obsolete)
  contract=next(c for c in s['contracts'] if c['format']==s['format'])
  if accounts is None:accounts={r[contract['columns']['account']] for r in s['rows']}
  signature=(identity['id'],mapping['id'],tuple((account,mapping['entities'][account]) for account in sorted(accounts)))
  return identity,mapping,signature
 def solve(self,key,refresh=False):
  s=self.source(key,refresh)
  if s['kind']=='feed':
   identity,mapping,signature=self.identity_choice(s);contract=next(c for c in s['contracts'] if c['format']==s['format'])
   norm=self.skill('normalize-source',source_id=key,contract_id=contract['id'],identity_source_id=identity['id'],identity_map_id=mapping['id'],conflict_policy=s['conflict_rule']);out=self.skill('aggregate-source',normalized_id=norm['id'])
   self.identity_accounts[key]={row['account'] for row in norm['data']};self.identity_signatures[key]=self.identity_choice(s,self.identity_accounts[key])[2]
  elif s['kind']=='maintenance':
   policy=next(p for p in s['policies'] if s['region'] in p['regions'] and p['from_stage']<=s['stage']<=p['to_stage'])
   for case,c in s['cases'].items():
    e=c['evidence'];old=self.decisions.get((key,case))
    if old==e:continue
    applied=e['status']=='confirmed';delta=e['settled_cents']-e['booked_cents'] if applied else 0
    d=self.skill('review-adjustment',source_id=key,case_id=case,policy_id=policy['id'],disposition='apply' if applied else 'reject',delta_cents=delta)
    plan=self.skill('prepare-adjustment',decision_id=d['id']);self.skill('commit-adjustment',plan_id=plan['id']);self.decisions[key,case]=dict(e)
   for view in s['consumer_views']:
    if view=='historical':self.outputs[key,view]=s['signed_artifact_id']
    else:self.outputs[key,view]=self.skill('snapshot-component',source_id=key,view=view)['id']
   return
  else:
   invoices={v['id']:v for sid in s['invoice_sources'] for v in self.source(sid)['invoices']};adv={x['receipt_id']:x for x in s['advice']};allocations=[];seen=set()
   for receipt in s['receipts']:
    if receipt['transaction_id'] in seen:continue
    seen.add(receipt['transaction_id']);e=adv.get(receipt['id'])
    if e is None or e['evidence_customer']!=receipt['payer'] or e['currency']!=receipt['currency']:continue
    for choice in e['invoice_allocations']:
     invoice=invoices[choice['invoice_id']]
     if invoice['customer']==receipt['payer'] and invoice['currency']==receipt['currency']:allocations.append(dict(receipt_id=receipt['id'],**choice))
   signature=(s['versions']['remittance'],tuple((sid,self.source(sid)['versions']['invoices']) for sid in s['invoice_sources']))
   old=self.matchings.get(key)
   matching=old[1] if old and old[0]==signature else self.skill('match-remittance',source_id=key,allocations=allocations)
   self.matchings[key]=(signature,matching)
   notes=self.skill('review-credit-note',source_id=key,decisions=[dict(note_id=n['id'],disposition='apply' if n['status']=='confirmed' else 'reject',cents=n['evidence_cents'] if n['status']=='confirmed' else 0) for n in s['notes']])
   out=self.skill('post-reconciliation',matching_id=matching['id'],notes_id=notes['id'])
  self.outputs[key,'default']=out['id']
 def close(self,stage):
  ids=[v for (key,view),v in self.outputs.items() if self.sources[key]['stage']==stage]
  r=self.skill('close-stage',stage=stage,output_ids=ids,previous_close_id=self.closes.get(stage-1,''));self.closes[stage]=r['id'];return r
 def run(self,reverse=False):
  project=self.call('read_project')
  for stage in range(1,project['stages']+1):
   entries=[r for r in self.inventory(stage) if r['stage']==stage]
   for row in (reversed(entries) if reverse else entries):self.solve(row['id'])
   closed=self.close(stage)
   if closed['notices']:
    earliest=stage;affected=set()
    for notice in closed['notices']:
     source=self.source(notice['source_id'],True)
     if notice['type']=='invoice_party_correction':affected.update(key for key,s in self.sources.items() if source['id'] in s.get('invoice_sources',[]))
     elif notice['type']=='identity_correction':affected.update(key for key,s in self.sources.items() if s.get('identity_source_id')==source['id'] and self.identity_signatures.get(key)!=self.identity_choice(s,self.identity_accounts.get(key))[2])
     else:affected.add(source['id'])
    for key in sorted(affected):
     earliest=min(earliest,self.sources[key]['stage']);self.solve(key);self.repairs+=1
    for previous in range(earliest,stage+1):
     again=self.close(previous)
     if again['notices']:raise ValueError('A repeated stage unexpectedly emitted new events')
  review=self.skill('review-release',close_ids=[self.closes[i] for i in range(1,project['stages']+1)])
  self.skill('publish-release',review_id=review['id']);self.call('read_project')
