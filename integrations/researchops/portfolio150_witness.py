"""Incremental public reference. Caches observations; never reads runtime/evaluator."""
from copy import deepcopy
from .witness import normalize


async def run(call):
    task=await call('read_task')
    for s in task['skills']:await call('read_skill_file',skill_id=s['name'])
    canonical={};sources={};annotations={};citations={};section_members={s['section_id']:set() for s in task['sections']}
    previous_items={s['section_id']:{} for s in task['sections']};prior=None;prior_draft=None;citation_keys={}
    for stage,batch in enumerate(task['batches']):
        args={'batch_id':batch['batch_id']}
        if prior:args['prior_review_artifact_id']=prior
        acquired=await call('acquire_library_batch',**args)
        records=[await call('read_source_record',paper_id=p) for p in acquired['paper_ids']]
        for record in records:
            key=normalize(record['doi'])
            if key in canonical:
                await call('merge_duplicate_papers',primary_paper_id=canonical[key]['paper_id'],secondary_paper_id=record['paper_id'])
            else:
                canonical[key]=deepcopy(record)
                citation_keys[key]='Work'+str(len(citation_keys)+1)
                if record['doi']!=key:
                    canonical[key]=await call('apply_source_revision',paper_id=record['paper_id'],source_revision=record['source_revision'])
        correction=acquired.get('publisher_correction')
        if correction:
            key=correction['source_key'];v=correction['version']
            canonical[key]=await call('apply_source_revision',paper_id=canonical[key]['paper_id'],source_revision=v['revision'])
            sources[key]=deepcopy(v)
        selections={s['section_id']:[k for k,p in canonical.items() if p['year']>=s['criteria']['year_min']
            and p['study_type']==s['criteria']['study_type'] and s['criteria']['topic'] in p['keywords']] for s in task['sections']}
        needed={k for keys in selections.values() for k in keys}
        for key in canonical:
            if key not in needed:continue
            paper=canonical[key]
            if key not in sources:sources[key]=await call('read_source_text',paper_id=paper['paper_id'])
            source=sources[key];known=annotations.setdefault(key,{})
            for passage in source['passages']:
                if passage['passage_id']=='abstract':continue
                old=known.get(passage['passage_id'])
                if old and old['content']==passage['text']:continue
                known[passage['passage_id']]=await call('add_annotation',paper_id=paper['paper_id'],source_revision=source['revision'],passage_id=passage['passage_id'])
            wanted={(p['passage_id'],normalize(target),known[p['passage_id']]['annotation_id'])
                for p in source['passages'] for target in p['cited_dois']}
            saved=citations.setdefault(key,{})
            for signature in list(saved):
                if signature not in wanted:
                    await call('remove_citation',citation_id=saved.pop(signature))
            for signature in sorted(wanted):
                if signature in saved:continue
                _,target,annotation=signature
                saved[signature]=(await call('add_citation',annotation_id=annotation,cited_paper_id=canonical[target]['paper_id']))['citation_id']
        for s in task['sections']:
            sid=s['section_id'];wanted=set(selections[sid]);known=section_members[sid]
            for key in sorted(wanted-known):
                pid=canonical[key]['paper_id']
                await call('add_paper_to_collection',collection_id=s['collection_id'],paper_id=pid)
                await call('add_paper_to_reading_list',list_id=s['reading_list_id'],paper_id=pid)
                await call('assign_tag_to_paper',tag_id=s['tag_id'],paper_id=pid)
            for key in sorted(known-wanted):
                pid=canonical[key]['paper_id']
                await call('remove_paper_from_collection',collection_id=s['collection_id'],paper_id=pid)
                await call('remove_paper_from_reading_list',list_id=s['reading_list_id'],paper_id=pid)
                await call('remove_tag_from_paper',tag_id=s['tag_id'],paper_id=pid)
            section_members[sid]=wanted
        args={'sections':[{'section_id':s['section_id'],'title':s['title']} for s in task['sections']]}
        if prior_draft:args['base_draft_id']=prior_draft
        draft=(await call('start_review_draft',**args))['draft_id']
        for s in task['sections']:
            sid=s['section_id'];items={key:{'paper_id':canonical[key]['paper_id'],'annotation_id':annotations[key]['results']['annotation_id'],
                'citation_ids':list(citations[key].values()),'citation_key':citation_keys[key]} for key in selections[sid]}
            changed=[item for key,item in items.items() if previous_items[sid].get(key)!=item]
            removed=[v['paper_id'] for key,v in previous_items[sid].items() if key not in items]
            for offset in range(0,max(len(changed),len(removed),1),20):
                if changed[offset:offset+20] or removed[offset:offset+20]:
                    await call('set_review_items',draft_id=draft,section_id=sid,items=changed[offset:offset+20],remove_paper_ids=removed[offset:offset+20])
            previous_items[sid]=items
        await call('create_draft_bibliography',draft_id=draft)
        # All calibration sources are themselves covered in this construction;
        # supplying them explicitly also works when a future section excludes one.
        import json
        ancestors=[annotations[k]['results']['annotation_id'] for k in needed
            if json.loads(annotations[k]['results']['content'])['role']=='calibration']
        await call('calculate_review_synthesis',draft_id=draft,calibration_annotation_ids=ancestors)
        prior=(await call('submit_review_draft',draft_id=draft,include_content=False))['artifact_id']
        prior_draft=draft
    return prior
