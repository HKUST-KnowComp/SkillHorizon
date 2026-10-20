"""Fresh public business facts. Private events are never exported as observations."""
import random
from copy import deepcopy
from .skills import SKILLS
FAMILIES={
 'heterogeneous_integration':['branch_specific_transforms','temporal_identity_mapping','conflict_and_late_correction'],
 'release_maintenance':['local_component_correction','shared_source_concurrent_corrections','policy_scope_and_consumer_fork'],
 'receivables_reconciliation':['partial_and_many_to_many_payment','credit_note_refund_and_duplicate','delayed_evidence_and_reversal']}

def make_project(family,variant,seed,stages,width,*,micro=None):
 if family not in FAMILIES or variant not in FAMILIES[family] or not 1<=stages<=32 or not 1<=width<=512:raise ValueError('Unsupported construction profile')
 rng=random.Random(seed);vi=FAMILIES[family].index(variant)
 policy_schedule=([False,True]*((stages*width+1)//2))[:stages*width];random.Random(str(seed)+':policy').shuffle(policy_schedule)
 p=dict(task_id=f'DATAOPS-V5-{family}-{variant}-s{seed}-p{stages}-w{width}',family=family,variant=variant,stages=stages,released_stage=1,input_closed=False,publication=None,sources={},artifacts={},journal=[],events=[],notices=[],closes={},milestones=[],origin='fresh_business_world; existing signed snapshots are environment-origin')
 p['goal']=('Resolve every released work package using its documented public contracts. Close all logical stages in order, preserve immutable signed historical values, then review and publish the correct complete release. Later stage closures may release source-only corrections. Rebuild affected outputs and subsequent closes; retain unaffected valid work. Input closure means no further external changes, not correct completion. Reads never release new stages. ' +
 {'heterogeneous_integration':'Choose format contracts, period-applicable identity mappings and conflict rules for each branch; aggregate canonical entity totals.',
 'release_maintenance':'Apply only confirmed evidence, use component settled-minus-booked rather than the ticket claim, and reject withdrawn/disputed requests. Distinct component cases add; the requested consumer view determines current or signed historical value.',
 'receivables_reconciliation':'Match payments using original invoice, payer, currency and advice. Partial allocation is legal, duplicates are credited once, unsupported money remains suspense. Apply only confirmed credits/refunds. Record remaining receivables, including legitimate future obligations.'}[family])
 for stage in range(1,stages+1):
  for lane in range(1,width+1):
   key=f'work-{stage:02d}-{lane:03d}';region=f'R{lane:03d}';s=dict(id=key,stage=stage,region=region,versions={'data':1,'invoices':1})
   if family=='heterogeneous_integration':
    alternate=(stage+lane+seed)%2;fmt='minor_feed' if alternate else 'major_feed';cols={'order':'sale_ref' if alternate else 'order_id','account':'account','amount':'minor_amount' if alternate else 'amount','version':'edition','kind':'revision_kind'}
    s.update(kind='feed',format=fmt,conflict_rule='first' if vi==0 and lane%2 else 'latest_revision',rows=[],contracts=[],identity_maps=[])
    for n in range(4):
     cents=rng.randrange(500,25000);row={cols['order']:f'O-{stage}-{lane}-{n}',cols['account']:f'AC{n%2}',cols['amount']:cents if alternate else f'{cents//100}.{cents%100:02d}',cols['version']:1,cols['kind']:'sale'};s['rows'].append(row)
    # A conflicting revision is real business data, not a copied padding row.
    changed=dict(s['rows'][0]);changed[cols['version']]=2;changed[cols['kind']]=rng.choice(['correction','metadata','withdrawal']) if vi==2 else 'correction';old=changed[cols['amount']];changed[cols['amount']]=old+137 if alternate else f'{float(old)+1.37:.2f}';s['rows'].append(changed)
    for unit,tag in [('minor','minor_feed'),('major','major_feed')]:
     s['contracts'].append(dict(id=f'contract-{unit}',format=tag,columns=cols,unit=unit,rate_numerator=107+seed%5,rate_denominator=100))
    active=stages+1 if vi==0 else (stage if (stage+lane+seed)%2==0 else stage+1)
    if micro is not None:active=micro.get('mapping_effective_from',active)
    s['identity_maps']=[dict(id='legacy',from_stage=1,to_stage=stages+2,entities={'AC0':f'Alpha-{lane}','AC1':f'Beta-{lane}'},supersedes=None),dict(id='amendment',from_stage=active,to_stage=stages+2,entities={'AC0':f'Beta-{lane}','AC1':f'Alpha-{lane}'},supersedes='legacy')]
    s['versions']['identity-rules']=1;s['versions']['records']=1
    for mapping in s['identity_maps']:
     for account in mapping['entities']:s['versions']['identity-value/'+mapping['id']+'/'+account]=1
    s['identity_source_id']=key if vi==0 else f'work-01-{lane:03d}'
    if vi==2 and stage==max(2,stages//2) and lane==1 and stage<stages:
     s['rows'][-1][cols['kind']]='correction'
     row=s['rows'][-1];value=row[cols['amount']]+251 if alternate else f'{float(row[cols["amount"]])+2.51:.2f}'
     p['events'].append(dict(kind='feed_correction',after_stage=min(stages,stage+2),source_id=key,row=len(s['rows'])-1,column=cols['amount'],value=value))
   elif family=='release_maintenance':
    s.update(kind='maintenance',booked_cents=10000+rng.randrange(1000),contributions={},decisions={},committed_plans={},cases={},policies=[],consumer_views=['current','historical'] if vi==2 else ['current'])
    s['signed_snapshot_cents']=s['booked_cents']
    s['versions']['signed']=1
    use_exception=policy_schedule[(stage-1)*width+lane-1];cutover=stage if use_exception else stage+1
    statuses=['confirmed','confirmed',rng.choice(['withdrawn','disputed'])];rng.shuffle(statuses)
    cases=[]
    for j in range(3):
     case=f'change-{j}';delta=125+rng.randrange(350);status=statuses[j]
     if micro is not None and j==0:status=micro.get('evidence_status',status)
     component=f'component-{0 if vi==1 and j<2 else j}'
     cases.append(dict(id=case,component=component,claim_delta_cents=delta+rng.choice([-90,37,200] if j==0 else [-90,0,37,200]),evidence={'status':status,'booked_cents':1000,'settled_cents':1000+delta}))
     s['versions']['case/'+case]=1;s['versions']['decision/'+case]=1;s['versions']['component/'+component]=1
    rng.shuffle(cases);s['cases']={c['id']:c for c in cases}
    s['policies']=[dict(id='standing',regions=[region],from_stage=1,to_stage=cutover-1,max_delta_cents=500),dict(id='newer-exception',regions=[region],from_stage=cutover,to_stage=stages+2,max_delta_cents=1500)]
    rng.shuffle(s['policies'])
    if vi in [1,2] and stage in [1,max(2,stages//2)] and lane==1 and stage<stages:
     affected=next(c['id'] for c in cases if c['evidence']['status']=='confirmed')
     p['events'].append(dict(kind='evidence_withdrawal',after_stage=min(stages,stage+max(2,stages//3)),source_id=key,case_id=affected))
   else:
    total=10000+rng.randrange(2000);s.update(kind='receivables',invoices=[dict(id=f'I-{stage}-{lane}-{j}',customer=f'{region}-{c}',amount_cents=total if j<2 else total*3//5,currency='EUR',origin_stage=stage) for j,c in enumerate(['Alpha','Beta','Alpha'])],receipts=[],notes=[],invoice_sources=[],advice=[])
    primary,other=rng.sample(['EUR','USD','GBP'],2)
    for inv in s['invoices']:inv['currency']=primary
    s['invoices'].append(dict(id=f'I-{stage}-{lane}-3',customer=f'{region}-Alpha',amount_cents=total,currency=other,origin_stage=stage))
    s['versions'].update(remittance=1,notes=1,invoice_balance=1)
    gap=1 if vi==0 else 1+(stage+lane+seed)%min(4,max(1,stages-1))
    if vi==2 and stage<=stages//2:gap=max(1,min(stages-stage,min(12,max(2,stages//2))-(stage+lane+seed)%3))
    if stages==1:gap=0
    for inv in s['invoices']:inv.update(due_stage=stage+gap,obligation='in_horizon' if stage+gap<=stages else 'future')
   p['sources'][key]=s
 if family=='heterogeneous_integration' and vi in [1,2]:
  for lane in range(1,width+1):
   registry=p['sources'][f'work-01-{lane:03d}'];registry['identity_maps'][1]['from_stage']=max(2,stages//2)
  if vi==2 and stages>1:p['events'].append(dict(kind='identity_correction',after_stage=stages,source_id='work-01-001',mapping_id='legacy',account='AC0',entity='Corrected-Alpha-1'))
 if family=='receivables_reconciliation':
  for s in p['sources'].values():
   lane=int(s['region'][1:]);stage=s['stage']
   # Each origin batch matures exactly once; delays vary by origin and variant.
   for origin in range(1,stage+1):
    original=p['sources'][f'work-{origin:02d}-{lane:03d}'];invs=original['invoices'];s['invoice_sources'].append(original['id']);total=invs[0]['amount_cents'];partial=total//5
    if invs[0]['due_stage']!=stage:
     s['invoice_sources'].pop();continue
    for suffix,customer,amount,alloc in [('beta',invs[1]['customer'],total,[(invs[1]['id'],total)]),('alpha',invs[0]['customer'],total+partial,[(invs[0]['id'],total),(invs[2]['id'],partial)]),('other',invs[3]['customer'],total,[(invs[3]['id'],total)])]:
     rid=f'R-{origin}-{lane}-{suffix}';payer=customer
     currency=invs[3]['currency'] if suffix=='other' else invs[0]['currency']
     if micro is not None and suffix=='beta' and micro.get('payer_mismatch'):payer=f'{s["region"]}-Gamma'
     s['receipts'].append(dict(id=rid,transaction_id=rid,payer=payer,amount_cents=amount,currency=currency))
     evidence=dict(receipt_id=rid,invoice_allocations=[dict(invoice_id=k,cents=v) for k,v in alloc],evidence_customer=customer,currency=currency)
     # Public conflicting claims are mixed with valid instructions. No private
     # scenario flag or correct allocation is exposed to the actor.
     mode=rng.randrange(5)
     if mode==1:s['receipts'][-1]['payer']=f'{s["region"]}-Gamma'
     elif mode==2:evidence['currency']=next(c for c in ['EUR','USD','GBP'] if c!=currency)
     elif mode==3:evidence['invoice_allocations'][0]['invoice_id']=invs[0 if suffix=='beta' else 1]['id']
     elif mode==4:
      # Same customer and amount, different invoice currency; identity alone
      # cannot resolve this claim. Beta has a customer-mismatch alternative.
      evidence['invoice_allocations'][0]['invoice_id']=invs[0 if suffix=='other' else 3]['id']
     s['advice'].append(evidence)
    if vi>=1:
     duplicate=dict(s['receipts'][-1]);duplicate['id']+='-duplicate';s['receipts'].append(duplicate)
     duplicate_advice=deepcopy(s['advice'][-1]);duplicate_advice['receipt_id']=duplicate['id'];s['advice'].append(duplicate_advice)
    credit=total//10
    s['notes'].append(dict(id=f'credit-{origin}-{lane}',kind='credit',invoice_id=invs[2]['id'],status='confirmed',evidence_cents=credit,requested_cents=credit+100))
    if vi==1:s['notes'].append(dict(id=f'refund-{origin}-{lane}',kind='refund',invoice_id=invs[1]['id'],status='confirmed' if origin%2 else 'withdrawn',evidence_cents=50,requested_cents=150))
  if vi==2:
   # Schedule against actual evidence, so a random maturity delay cannot
   # accidentally remove the recovery mechanism from this variant.
   eligible=[s for s in p['sources'].values() if s['notes'] and s['stage']<stages]
   if eligible:
    selected=[eligible[0]]
    later=[s for s in eligible if s['stage']>=max(3,stages//2) and s['id']!=selected[0]['id']]
    if later:selected.append(later[0])
    if selected:
     s=selected[0];p['events'].append(dict(kind='note_withdrawal',after_stage=min(stages,s['stage']+2),source_id=s['id'],note_id=s['notes'][0]['id']))
    # Revise an early invoice after a later payment has already consumed it.
    candidates=[]
    for batch in p['sources'].values():
     if batch['stage']>=stages:continue
     receipts={r['id']:r for r in batch['receipts']}
     for advice in batch['advice']:
      receipt=receipts[advice['receipt_id']]
      if receipt['payer']!=advice['evidence_customer'] or receipt['currency']!=advice['currency']:continue
      for allocation in advice['invoice_allocations']:
       for sid in batch['invoice_sources']:
        inv=next((i for i in p['sources'][sid]['invoices'] if i['id']==allocation['invoice_id']),None)
        if inv and inv['customer']==receipt['payer']:candidates.append((batch,sid,inv));break
    if candidates:
     batch,sid,inv=max(candidates,key=lambda c:c[0]['stage']-c[2]['origin_stage'])
     p['events'].append(dict(kind='invoice_party_correction',after_stage=min(stages,batch['stage']+max(2,stages//3)),source_id=sid,invoice_id=inv['id'],customer=p['sources'][sid]['region']+'-Corrected'))
  opaque_receivable_references(p,seed)
  for s in p['sources'].values():rng.shuffle(s['invoices']);rng.shuffle(s['receipts']);rng.shuffle(s['advice']);rng.shuffle(s['notes'])
 if family=='release_maintenance':
  for s in p['sources'].values():
   key='signed-'+s['id'];s['signed_artifact_id']=key
   p['artifacts'][key]=dict(id=key,kind='work_output',source_id=s['id'],view='historical',origin='environment',data=[dict(entity=s['region']+'/historical',cents=s['signed_snapshot_cents'])],inputs=[dict(source=s['id'],scope='signed',revision=1)])
 return p

def opaque_receivable_references(project,seed):
 """Identifiers carry equality/joins only, never customer/currency/role labels."""
 rng=random.Random(str(seed)+':opaque-identifiers');sources=project['sources'].values()
 def names(values,prefix):
  values=sorted(set(values));numbers=rng.sample(range(100000000,1000000000),len(values))
  return {old:prefix+str(n) for old,n in zip(values,numbers)}
 invoices=names((i['id'] for s in sources for i in s['invoices']),'I-')
 receipts=names((r['id'] for s in sources for r in s['receipts']),'R-')
 transactions=names((r['transaction_id'] for s in sources for r in s['receipts']),'T-')
 notes=names((n['id'] for s in sources for n in s['notes']),'N-')
 customers=names([i['customer'] for s in sources for i in s['invoices']]+[r['payer'] for s in sources for r in s['receipts']]+[a['evidence_customer'] for s in sources for a in s['advice']]+[e['customer'] for e in project['events'] if 'customer' in e],'C-')
 for s in sources:
  for i in s['invoices']:i['id']=invoices[i['id']];i['customer']=customers[i['customer']]
  for r in s['receipts']:r['id']=receipts[r['id']];r['transaction_id']=transactions[r['transaction_id']];r['payer']=customers[r['payer']]
  for a in s['advice']:
   a['receipt_id']=receipts[a['receipt_id']];a['evidence_customer']=customers[a['evidence_customer']]
   for row in a['invoice_allocations']:row['invoice_id']=invoices[row['invoice_id']]
  for n in s['notes']:n['id']=notes[n['id']];n['invoice_id']=invoices[n['invoice_id']]
 for e in project['events']:
  if 'invoice_id' in e:e['invoice_id']=invoices[e['invoice_id']]
  if 'note_id' in e:e['note_id']=notes[e['note_id']]
  if 'customer' in e:e['customer']=customers[e['customer']]
