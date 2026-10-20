"""Bounded business drafts, with caller-selected evidence and fixed arithmetic only."""
from copy import deepcopy
from .kernel import schema


class PortfolioReviewDrafts:
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.portfolio=self.public.get('instance_revision') in {'researchops_portfolio150_v1','researchops_portfolio150_v2'}
        if not self.portfolio:return
        self.script_input_limit=2*1024*1024
        self.state.setdefault('review_drafts',{})
        text={'type':'string','minLength':1}
        ids={'type':'array','items':text,'uniqueItems':True,'maxItems':64}
        item=schema({'paper_id':text,'annotation_id':text,'citation_ids':ids,'citation_key':text})
        definitions={
            'start_review_draft':('Start a current-round business review draft with explicitly named sections.',schema({'sections':{'type':'array','minItems':1,'maxItems':4,'items':schema({'section_id':text,'title':text})},'base_draft_id':text},['sections'])),
            'set_review_items':('Upsert up to 20 caller-selected items in a draft section; remove named items if requested. Invalidates draft bibliography and calculation; does not select relevant works.',schema({'draft_id':text,'section_id':text,'items':{'type':'array','maxItems':20,'items':item},'remove_paper_ids':ids},['draft_id','section_id','items'])),
            'create_draft_bibliography':('Serialize exactly the draft paper union using caller-provided keys as a real bibliography. No relevance or completeness checking.',schema({'draft_id':text})),
            'calculate_review_synthesis':('Run the fixed synthesis script on draft item results and explicitly supplied calibration annotations. Does not discover omitted evidence or repair stale selections. Stores numerical result for submission.',schema({'draft_id':text,'calibration_annotation_ids':ids})),
            'submit_review_draft':('Submit the prepared draft as an ordinary immutable review. Requires bibliography and calculation; independent scoring determines completeness.',schema({'draft_id':text,'include_content':{'type':'boolean'}},['draft_id'])),
            'read_review_draft':('Read draft status and a bounded page of explicitly selected items for recovery.',schema({'draft_id':text,'section_id':text,'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':20}},['draft_id'])),
        }
        self.metadata.update({n:{'description':d,'input_schema':s} for n,(d,s) in definitions.items()})
        # Old direct APIs remain legal; chunked calls avoid an oversized action.
        self.metadata['generate_bibtex']['input_schema']['properties']['entries']['maxItems']=2048
        self.metadata['compose_literature_review']['input_schema']['properties']['sections']['items']['properties']['items']['maxItems']=2048

    def draft(self,identifier,current=True):
        self.require(identifier in self.state['review_drafts'],'DRAFT_NOT_FOUND')
        row=self.state['review_drafts'][identifier]
        if current:self.require(row['round']==self.state['round'],'DRAFT_WRONG_ROUND')
        return row

    def dispatch(self,name,args):
        if not getattr(self,'portfolio',False):return super().dispatch(name,args)
        if name=='read_source_text' and self.public.get('instance_revision')=='researchops_portfolio150_v2':
            value=super().dispatch(name,args)
            value.pop('text',None)  # Exact passages already contain every character.
            return value
        if name=='read_library_workspace':
            value=super().dispatch(name,args)
            value['review_drafts']=[{'draft_id':d['draft_id'],'round':d['round']} for d in self.state['review_drafts'].values()]
            return value
        if name=='start_review_draft':
            self.require(self.state['round']>=0,'NO_ACQUISITION')
            sections=args['sections'];self.require(len({s['section_id'] for s in sections})==len(sections),'DUPLICATE_DRAFT_SECTION')
            for section in sections:self.owned('section',section['section_id'])
            identifier=self.next_id('draft')
            base=self.draft(args['base_draft_id'],current=False) if args.get('base_draft_id') else {'sections':[]}
            existing={s['section_id']:s['items'] for s in base['sections']}
            self.state['review_drafts'][identifier]={'draft_id':identifier,'round':self.state['round'],
                'sections':[{**deepcopy(s),'items':deepcopy(existing.get(s['section_id'],[]))} for s in sections]}
            return {'draft_id':identifier,'round':self.state['round']}
        if name=='set_review_items':
            d=self.draft(args['draft_id']);matched=[s for s in d['sections'] if s['section_id']==args['section_id']]
            self.require(len(matched)==1,'DRAFT_SECTION_NOT_FOUND');s=matched[0]
            self.require(len({i['paper_id'] for i in args['items']})==len(args['items']),'DUPLICATE_DRAFT_ITEM')
            items={i['paper_id']:i for i in s['items']}
            for identifier in args.get('remove_paper_ids',[]):items.pop(identifier,None)
            for item in args['items']:
                self.paper(item['paper_id']);self.owned('annotation',item['annotation_id'])
                items[item['paper_id']]=deepcopy(item)
            s['items']=list(items.values());d.pop('synthesis',None);d.pop('bibliography_artifact_id',None)
            return {'draft_id':d['draft_id'],'section_id':s['section_id'],'item_count':len(items)}
        if name=='create_draft_bibliography':
            d=self.draft(args['draft_id']);entries={}
            for s in d['sections']:
                for i in s['items']:
                    entry={'paper_id':i['paper_id'],'citation_key':i['citation_key']}
                    self.require(i['paper_id'] not in entries or entries[i['paper_id']]==entry,'INCONSISTENT_CITATION_KEY')
                    entries[i['paper_id']]=entry
            self.require(bool(entries),'EMPTY_BIBLIOGRAPHY')
            b=self.bibliography(list(entries.values()));d['bibliography_artifact_id']=b['artifact_id']
            return {'artifact_id':b['artifact_id'],'kind':'bibliography','entry_count':len(entries),'round':b['round']}
        if name=='calculate_review_synthesis':
            d=self.draft(args['draft_id']);evidence={};sections=[]
            for s in d['sections']:
                keys=[]
                for i in s['items']:
                    a=self.owned('annotation',i['annotation_id'])
                    self.require(a['paper_id']==self.paper(i['paper_id'])['paper_id'],'DRAFT_EVIDENCE_OWNERSHIP')
                    self.require(a['source_key'] not in evidence or evidence[a['source_key']]['annotation_id']==a['annotation_id'],'AMBIGUOUS_DRAFT_EVIDENCE')
                    evidence[a['source_key']]={'source_key':a['source_key'],'annotation_id':a['annotation_id'],'quote':a['content']};keys.append(a['source_key'])
                sections.append({'section_id':s['section_id'],'source_keys':keys})
            for identifier in args['calibration_annotation_ids']:
                a=self.owned('annotation',identifier)
                self.require(a['source_key'] not in evidence or evidence[a['source_key']]['annotation_id']==identifier,'AMBIGUOUS_DRAFT_EVIDENCE')
                evidence[a['source_key']]={'source_key':a['source_key'],'annotation_id':identifier,'quote':a['content']}
            d['synthesis']=super().dispatch('run_skill_script',{'skill_id':'assemble-literature-handoff','inputs':{
                'policy':deepcopy(self.public['synthesis_policy']),'sections':sections,'evidence':list(evidence.values())}})
            return {'draft_id':d['draft_id'],'supplied_evidence_count':len(evidence),
                'sections':{k:{f:v[f] for f in ('total_sample_size','mean_points')} for k,v in d['synthesis']['sections'].items()},
                'global':{f:d['synthesis']['global'][f] for f in ('total_sample_size','mean_points')}}
        if name=='submit_review_draft':
            d=self.draft(args['draft_id']);self.require('synthesis' in d and 'bibliography_artifact_id' in d,'DRAFT_NOT_PREPARED')
            sections=[{**deepcopy(s),'synthesis':deepcopy(d['synthesis']['sections'][s['section_id']])} for s in d['sections']]
            return super().dispatch('compose_literature_review',{'bibliography_artifact_id':d['bibliography_artifact_id'],
                'sections':sections,'cross_section_synthesis':deepcopy(d['synthesis']['global']),'include_content':args.get('include_content',False)})
        if name=='read_review_draft':
            d=self.draft(args['draft_id'],current=False);offset=args.get('offset',0);limit=args.get('limit',20)
            return {'draft_id':d['draft_id'],'round':d['round'],'bibliography_artifact_id':d.get('bibliography_artifact_id'),
                'calculated':'synthesis' in d,'sections':[{'section_id':s['section_id'],'title':s['title'],'item_count':len(s['items']),
                    'items':deepcopy(s['items'][offset:offset+limit])} for s in d['sections'] if args.get('section_id',s['section_id'])==s['section_id']]}
        return super().dispatch(name,args)
