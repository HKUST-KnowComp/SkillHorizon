"""Scaled synthetic evidence obligations; no model calls or private answer hints."""
from copy import deepcopy
import json
from pathlib import Path
import random

from integrations.internal_mcp.io import read_json, write_json
from .declaration_build import build as declaration_build
from .kernel import METADATA

REVISION = "researchops_portfolio150_v2"


def result(version):
    return json.loads(next(p['text'] for p in version['passages'] if p['passage_id']=='results'))


def set_result(version, value):
    next(p for p in version['passages'] if p['passage_id']=='results')['text']=json.dumps(value,sort_keys=True)


def build(source, output, *, seed, sections, rounds, studies_per_section, scenario,
          alias_copies=1, distractors_per_section=1, dependency_pattern='distributed', background=False, trim_last_round_studies=0):
    public=declaration_build(source,output,seed=seed,sections=sections,rounds=rounds,
        studies_per_section=studies_per_section,scenario=scenario,alias_copies=alias_copies,
        distractors_per_section=distractors_per_section,dependency_pattern=dependency_pattern,
        background=background,construction_profile='researchops150_v1')
    output=Path(output);world=read_json(output/'checkpoint/world.json')
    batches=world['library']['batches'];rng=random.Random(f'researchops-role-v1:{seed}')
    topics=[s['topic'] for s in public['sections']]
    # Fine business granularity: remove only terminal studies which no later
    # acquisition can cite, retaining at least one original study per topic.
    if type(trim_last_round_studies) is not int or trim_last_round_studies<0:
        raise ValueError('Invalid terminal-study adjustment')
    removable=[]
    terminal=batches[-1]
    for topic in topics:
        group=[k for k,w in terminal['works'].items() if result(w['versions']['1'])['role']=='study'
            and w['versions']['1']['metadata']['keywords']==[topic]
            and result(w['versions']['1'])['metric']=='allocation_score']
        removable.extend(group[1:])
    if trim_last_round_studies>len(removable):raise ValueError('Terminal adjustment would remove required topic evidence')
    removed=set(removable[:trim_last_round_studies])
    terminal['works']={k:w for k,w in terminal['works'].items() if k not in removed}
    terminal['records']=[r for r in terminal['records'] if r['source_key'] not in removed]
    originals={k:w['versions']['1'] for b in batches for k,w in b['works'].items()}
    anchors=[next(k for k,w in b['works'].items() if result(w['versions']['1'])['role']=='calibration') for b in batches]
    prior_keys=[]
    for stage,b in enumerate(batches):
        # Original section membership is a legitimate topic criterion, not a role code.
        studies=[k for k in b['works'] if result(originals[k])['role']=='study']
        # Independent sampling of aliases, chronology, units, author ordering and overlap.
        for k in studies:
            v=originals[k];value=result(v)
            base_topic=v['metadata']['keywords'][0]
            v['metadata']['keywords']=[base_topic]
            unit=rng.choice(['points','fraction','basis_points']);points=rng.randint(30,85)
            value.update(metric='allocation_score',population='synthetic_items',unit=unit,
                sample_size=rng.randint(20,120),value_numerator=points*(100 if unit=='basis_points' else 1),
                value_denominator=100 if unit=='fraction' else 1)
            value['calibration_doi']=rng.choice(anchors[:stage+1])
            set_result(v,value)
        for topic in topics:
            group=[k for k in studies if topic in originals[k]['metadata']['keywords']]
            incompatible=rng.sample(group,min(distractors_per_section,len(group)-1))
            for k in group:
                v=originals[k];value=result(v)
                # Selection and numerical eligibility are separate. All three families
                # have real exclusions; withdrawal families add a later public update.
                v['metadata'].update(study_type='empirical',year=rng.randint(2020,2025))
                if k in incompatible:
                    if rng.random()<.5:value.update(metric='latency',unit='milliseconds')
                    else:value.update(population='synthetic_adults')
                set_result(v,value)
        compatible=[k for k in studies if result(originals[k])['metric']=='allocation_score' and result(originals[k])['population']=='synthetic_items']
        for k in rng.sample(compatible,min(len(compatible),rng.randint(1,3))):
            originals[k]['metadata']['keywords']=rng.sample(topics,rng.randint(2,len(topics)))
        # At least one current study uses each newly introduced calibration.
        value=result(originals[compatible[0]]);value['calibration_doi']=anchors[stage];set_result(originals[compatible[0]],value)
        for k in b['works']:
            v=originals[k];value=result(v)
            v['metadata']['title']='Synthetic Evidence & Allocation: '+rng.choice(['cohort comparison','measurement study','allocation analysis'])+' '+str(rng.randint(1000,9999))
            v['metadata']['authors']=rng.sample(v['metadata']['authors'],len(v['metadata']['authors']))
            v['metadata']['arxiv_id']=f'2610.{rng.randint(10000,99999)}'
            v['metadata']['url']='https://synthetic.invalid/publication/'+f'{rng.getrandbits(40):010x}'
            v['metadata']['year']=rng.randint(2020,2025)
            if value['role']=='study':
                p=next(p for p in v['passages'] if p['passage_id']=='calibration-reference')
                p.update(text='This synthetic measurement uses calibration DOI '+value['calibration_doi']+'.',cited_dois=[value['calibration_doi']])
            # Narrative metadata does not reveal which empirical result is compatible.
            v['metadata']['abstract']='Synthetic evidence concerning '+', '.join(v['metadata']['keywords'])+'. This is a simulated publication, not real research evidence.'
            next(p for p in v['passages'] if p['passage_id']=='abstract')['text']=v['metadata']['abstract']
        templates={k:deepcopy(next(r for r in b['records'] if r['source_key']==k)) for k in b['works']}
        records=[];duplicated=set(rng.sample(list(b['works']),min(2,len(b['works']))))
        for k in b['works']:
            for copy in range(1+(alias_copies if k in duplicated else 0)):
                row=deepcopy(templates[k]);row.update(deepcopy(originals[k]['metadata']))
                row['paper_id']+=f'-portfolio{copy}';row['source_record_id']='record-'+row['paper_id']
                if copy:row['doi']='https://doi.org/'+k.upper()
                records.append(row)
        if prior_keys and alias_copies:
            k=rng.choice(prior_keys);row=deepcopy(next(r for p in batches[:stage] for r in p['records'] if r['source_key']==k))
            row['paper_id']+=f'-late{stage}';row['source_record_id']='record-'+row['paper_id'];row['doi']='doi:'+k.upper()
            records.append(row)
        b['records']=records;b['correction']=None;prior_keys.extend(b['works'])
    # Vary the actual update cut and target, always after an existing review.
    correction_stage=rng.randint(1,rounds-2) if scenario!='identity_compatibility' else rng.randint(1,rounds-1)
    target=anchors[rng.randrange(max(1,correction_stage-1))]
    revised=deepcopy(originals[target]);revised['revision']=2
    revised['metadata']['title']+=' (corrected)';revised['metadata']['venue']='Synthetic Proceedings: Corrected Edition'
    value=result(revised);value['offset_points']+=rng.choice([-3,-2,2,3]);set_result(revised,value)
    batches[correction_stage]['correction']={'source_key':target,'version':revised}
    if scenario!='identity_compatibility':
        withdrawal_stage=rng.randint(correction_stage+1,rounds-1)
        candidates=[k for b in batches[:withdrawal_stage] for k in b['works'] if result(originals[k])['role']=='study'
            and result(originals[k])['metric']=='allocation_score' and result(originals[k])['population']=='synthetic_items']
        key=rng.choice(candidates);revised=deepcopy(originals[key]);revised['revision']=2
        revised['metadata']['title']+=' (withdrawal notice)';value=result(revised)
        value.update(evidence_status='retracted',withdrawal_reason='Synthetic cohort provenance defect discovered after publication')
        set_result(revised,value);batches[withdrawal_stage]['correction']={'source_key':key,'version':revised}
    # Complete exact replacements also inside quoted JSON and citation passages.
    mapping={};taken=set()
    def identifier(old,prefix):
        if old not in mapping:
            while True:
                new=f'{prefix}-{rng.getrandbits(32):08x}'
                if new not in taken:break
            mapping[old]=new;taken.add(new)
    for kind,rows in world['tables'].items():
        for row in rows:
            key={'reading_list':'list_id'}.get(kind,kind+'_id')
            if key in row:identifier(row[key],kind[:3])
    for s in public['sections']:identifier(s['section_id'],'sec')
    for b in batches:
        identifier(b['batch_id'],'batch')
        for k in b['works']:identifier(k,'10.99999/r')
        for r in b['records']:
            identifier(r['paper_id'],'paper');identifier(r['source_record_id'],'record')
    mapping[public['task_id']]=f'researchops-{rng.getrandbits(48):012x}'
    replacements=sorted(mapping,key=len,reverse=True)
    def remap(v):
        if isinstance(v,str):
            if v in mapping:return mapping[v]
            # DOI forms and embedded prose preserve exact references; no seed/id roles.
            for old in replacements:
                v=v.replace(old,mapping[old]).replace(old.upper(),mapping[old].upper())
            return v
        if isinstance(v,list):return [remap(x) for x in v]
        if isinstance(v,dict):return {mapping.get(k,k):remap(x) for k,x in v.items()}
        return v
    public,world=remap(public),remap(world)
    for b in world['library']['batches']:
        rng.shuffle(b['records']);keys=list(b['works']);rng.shuffle(keys);b['works']={k:b['works'][k] for k in keys}
    rng.shuffle(public['sections'])
    for rows in world['tables'].values():rng.shuffle(rows)
    public['batches']=[{'batch_id':b['batch_id'],'round':b['round'],'record_count':len(b['records']),
        'has_publisher_correction':b['correction'] is not None} for b in world['library']['batches']]
    public['instance_revision']=REVISION
    # Construction seed/topology labels belong to controller metadata, not model hints.
    public.pop('dependency_construction',None);public.pop('scenario',None)
    public['policy']['public_cache_contract']='Fresh library has no task papers, memberships, annotations or citations. Acquisition returns all newly imported paper IDs and the full released correction. Unannounced sources do not change. Creation/update receipts and unchanged public evidence can be reused. A late alias does not require changing the existing canonical representative.'
    public['policy']['review_drafts']='Reviews may be assembled in bounded item chunks. Drafts store only caller-selected evidence and keys; the optional fixed synthesis calculator reads only those selected annotations. It does not search for missing works or certify completeness. Submit validates ordinary bindings; independent grading still checks every acquisition review.'
    for name in ('assemble-literature-handoff','build-versioned-bibliography'):
        p=output/'public/skills'/name/'SKILL.md'
        p.write_text(p.read_text()+'\n\n## Bounded review assembly\n\nFor large reviews, use start_review_draft, set_review_items (at most 20 items per call), calculate_review_synthesis with explicitly selected calibration annotation IDs, and submit_review_draft. The calculator uses only selected item results plus these supplied ancestors. It does not discover missing works. Draft changes invalidate prior calculations. create_draft_bibliography serializes exactly the draft union with caller-provided citation keys. read_review_draft provides bounded recovery reads. All seven business goals and independent final checks remain the same.\n')
    for folder in ('checkpoint','runtime'):write_json(output/folder/'world.json',world)
    write_json(output/'public/task.json',public)
    write_json(output/'private/initial_obligations.json',{'public':public,'initial_tables':world['tables'],'initial_state':world['library']})
    return public
