"""Independent scorer from public facts, not reference choices or runtime arithmetic."""
from collections import defaultdict
from fractions import Fraction

def source_answer(s,sources):
 if s['kind']=='feed':
  c=next(x for x in s['contracts'] if x['format']==s['format']);identity=sources[s['identity_source_id']];eligible=[m for m in identity['identity_maps'] if m['from_stage']<=s['stage']<=m['to_stage']];superseded={m['supersedes'] for m in eligible};mapping=next(m for m in eligible if m['id'] not in superseded)
  rows={}
  for raw in s['rows']:
   r={k:raw[v] for k,v in c['columns'].items()};old=rows.get(r['order'])
   if old is None or (s['conflict_rule']=='latest_revision' and r['kind']!='metadata' and int(r['version'])>int(old['version'])):rows[r['order']]=r
  out=defaultdict(int)
  for r in rows.values():
   if r['kind']=='withdrawal':continue
   x=Fraction(str(r['amount']))*(100 if c['unit']=='major' else 1)*Fraction(c['rate_numerator'],c['rate_denominator']);q,rem=divmod(x.numerator,x.denominator);out[mapping['entities'][r['account']]]+=q+int(2*rem>=x.denominator)
  return dict(out)
 if s['kind']=='maintenance':
  current=s['booked_cents']+sum(x['evidence']['settled_cents']-x['evidence']['booked_cents'] for x in s['cases'].values() if x['evidence']['status']=='confirmed')
  return {s['region']+'/'+view:s['signed_snapshot_cents'] if view=='historical' else current for view in s['consumer_views']}
 invoices={r['id']:r for key in s['invoice_sources'] for r in sources[key]['invoices']};advice={r['receipt_id']:r for r in s['advice']};seen=set()
 currencies={i['currency'] for i in s['invoices']}|{r['currency'] for r in s['receipts']}|{i['currency'] for i in invoices.values()}
 answer={kind+'/'+currency:0 for currency in currencies for kind in ['cash','suspense','receivable_delta']}
 for inv in s['invoices']:answer['receivable_delta/'+inv['currency']]+=inv['amount_cents']
 for r in s['receipts']:
  if r['transaction_id'] in seen:continue
  seen.add(r['transaction_id']);currency=r['currency'];answer['cash/'+currency]+=r['amount_cents'];answer['suspense/'+currency]+=r['amount_cents'];e=advice.get(r['id'])
  if e and e['evidence_customer']==r['payer'] and e['currency']==r['currency']:
   for a in e['invoice_allocations']:
    inv=invoices[a['invoice_id']]
    if inv['customer']==r['payer'] and inv['currency']==r['currency']:
     answer['suspense/'+currency]-=a['cents'];answer['receivable_delta/'+currency]-=a['cents']
 for note in s['notes']:
  if note['status']!='confirmed':continue
  currency=invoices[note['invoice_id']]['currency'];value=note['evidence_cents']
  if note['kind']=='credit':answer['receivable_delta/'+currency]-=value
  else:answer['cash/'+currency]-=value;answer['receivable_delta/'+currency]+=value
 return answer

def score(runtime,initial):
 p=runtime.p;runtime.memo={};errors=[];good=0;expected_total=defaultdict(int);expected_by_stage={};decisions=0
 for key,s in p['sources'].items():
  expected=source_answer(s,p['sources']);stage=s['stage'];local=expected_by_stage.setdefault(stage,defaultdict(int))
  for entity,cents in expected.items():local[entity]+=cents;expected_total[entity]+=cents
  decisions+=3 if s['kind']=='feed' else len(s['cases']) if s['kind']=='maintenance' else len(s['receipts'])+len(s['notes'])
  close=p['artifacts'].get(p['closes'].get(str(stage)),{});outputs=[p['artifacts'][k] for k in close.get('output_ids',[]) if p['artifacts'][k]['source_id']==key];output=outputs[0] if outputs else None
  actual={r['entity']:r['cents'] for out in outputs for r in out['data']}
  if output and all(runtime.current(o['id']) for o in outputs) and actual==expected:
   # Correct totals cannot hide wrong per-record identity or payment assignments.
   ok=True
   if s['kind']=='maintenance':
    for case,item in s['cases'].items():
     d=p['artifacts'].get(s['decisions'].get(case),{}).get('data',{});want=item['evidence']['settled_cents']-item['evidence']['booked_cents'] if item['evidence']['status']=='confirmed' else 0
     ok &= d.get('delta_cents')==want and d.get('disposition')==('apply' if item['evidence']['status']=='confirmed' else 'reject')
     policy=next((v for v in s['policies'] if v['id']==d.get('policy_id')),None)
     ok &= bool(policy and s['region'] in policy['regions'] and policy['from_stage']<=s['stage']<=policy['to_stage'] and abs(want)<=policy['max_delta_cents'])
    ok &= s['signed_snapshot_cents']==initial['sources'][key]['signed_snapshot_cents'] and s['contributions']=={c:(x['evidence']['settled_cents']-x['evidence']['booked_cents'] if x['evidence']['status']=='confirmed' else 0) for c,x in s['cases'].items()}
    ok &= {o.get('view') for o in outputs}==set(s['consumer_views']) and len(outputs)==len(s['consumer_views'])
    signed=s['signed_artifact_id'];ok &= p['artifacts'][signed]==initial['artifacts'][signed]
   elif s['kind']=='feed':
    choice=p['artifacts'][output['inputs'][0]['artifact']]['choices'];identity=p['sources'][s['identity_source_id']];eligible=[m for m in identity['identity_maps'] if m['from_stage']<=s['stage']<=m['to_stage']];blocked={m['supersedes'] for m in eligible};ok &= choice['identity_source_id']==identity['id'] and choice['identity_map_id'] in {m['id'] for m in eligible if m['id'] not in blocked}
    ok &= choice['conflict_policy']==s['conflict_rule'] and next(c for c in s['contracts'] if c['id']==choice['contract_id'])['format']==s['format']
   else:
    matching=p['artifacts'][output['inputs'][0]['artifact']]['data'];inv={r['id']:r for sid in s['invoice_sources'] for r in p['sources'][sid]['invoices']};rec={r['id']:r for r in s['receipts']};adv={r['receipt_id']:r for r in s['advice']};expected_alloc=defaultdict(int);actual_alloc=defaultdict(int);seen=set()
    for rid,r in rec.items():
     if r['transaction_id'] in seen:continue
     seen.add(r['transaction_id']);e=adv.get(rid)
     if e and e['evidence_customer']==r['payer'] and e['currency']==r['currency']:
      for row in e['invoice_allocations']:
       if inv[row['invoice_id']]['customer']==r['payer'] and inv[row['invoice_id']]['currency']==r['currency']:expected_alloc[r['transaction_id'],row['invoice_id']]+=row['cents']
    for row in matching:actual_alloc[rec[row['receipt_id']]['transaction_id'],row['invoice_id']]+=row['cents']
    ok &= dict(expected_alloc)==dict(actual_alloc)
    note_art=p['artifacts'][output['inputs'][1]['artifact']]['data'];notes={n['id']:n for n in s['notes']}
    for decision in note_art:
     n=notes[decision['note_id']];ok &= decision['disposition']==('apply' if n['status']=='confirmed' else 'reject') and decision['cents']==(n['evidence_cents'] if n['status']=='confirmed' else 0)
   if ok:good+=1
   else:errors.append({'source_id':key,'code':'SEMANTIC_BINDING'})
  else:errors.append({'source_id':key,'code':'MISSING_STALE_OR_WRONG_OUTPUT'})
 cumulative=defaultdict(int)
 for stage in range(1,p['stages']+1):
  for key,value in expected_by_stage[stage].items():cumulative[key]+=value
  close=p['artifacts'].get(p['closes'].get(str(stage)))
  if not close or not runtime.current(close['id']) or {r['entity']:r['cents'] for r in close['data']['cumulative']}!=dict(cumulative):errors.append({'stage':stage,'code':'STAGE_ACCOUNTING'})
 pub=p['artifacts'].get(p['publication']);published=bool(pub and runtime.current(pub['id']) and {r['entity']:r['cents'] for r in pub['data']}==dict(expected_total))
 if not published:errors.append({'code':'PUBLICATION'})
 if not p['input_closed'] or len(p['milestones'])!=p['stages']:errors.append({'code':'UNRELEASED_OBLIGATIONS'})
 return dict(task_success=not errors,completed_work_packages=good,total_work_packages=len(p['sources']),constructed_decision_sites=decisions,events_released=len(p['notices']),input_closed=p['input_closed'],errors=errors,scoring_basis='independent_public_facts_and_bindings_not_reference_actions',eight_axis_model_metrics='not_computed_without_model_trajectory_analysis')
