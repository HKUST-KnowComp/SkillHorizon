"""Initial business facts for capacity, funding, synthesis and measured waves."""
from collections import Counter
from copy import deepcopy
import json
import random

from .programs import CAPACITY, FUNDING, SYNTHESIS, MEASURED, SUPPLEMENTAL_PROGRAMS


def checks(profile, public, world, private):
    from .profile import compare, field, MISSING
    program, p = profile["environment_format"], profile["parameters"]
    if SUPPLEMENTAL_PROGRAMS.get(profile["environment_id"]) != program:
        return []
    rows = [compare("supplement.private.public", public, field(private, "public")),
            compare("supplement.private.initial_tables", field(world, "tables"), field(private, "initial_tables"))]
    pairs = ({"initial_state": "mail"} if program == FUNDING else
             {"initial_state": "library"} if program == SYNTHESIS else {})
    for name, source in pairs.items():
        rows.append(compare("supplement.private."+name, field(world, source), field(private, name)))
    if program == CAPACITY:
        rows.append(compare("supplement.private.scheduled_updates", field(world, "planning", "scheduled_updates"),
                            field(private, "scheduled_updates")))
    if program == MEASURED:
        for name in ("briefs", "response_profiles", "scheduled_updates"):
            rows.append(compare("supplement.private."+name, field(world, "social", name), field(private, name)))

    def fact(name, function):
        try:
            value = function()
        except KeyError:
            value = MISSING
        except (TypeError, ValueError, IndexError, StopIteration, ZeroDivisionError):
            value = False
        rows.append(compare("supplement."+name, True, value))

    if program == CAPACITY:
        def contracts():
            blocks = {b["block_id"]: b for b in world["tables"]["page_block"]}
            return {page["page_id"]: json.loads(blocks[page["requirements_block_id"]]["content"])
                    for page in world["tables"]["page"]}
        fact("page_grid", lambda: len(world["tables"]["page"]) == len(contracts()) == p["width"]*p["depth"] and
             Counter((r["capacity_policy"]["stage"], r["capacity_policy"]["team"]) for r in contracts().values()) ==
             Counter((stage, team) for stage in range(1,p["depth"]+1) for team in range(1,p["width"]+1)))
        def capacity():
            from integrations.internal_mcp.topology import parent_positions
            records = contracts()
            grid = {(r["capacity_policy"]["stage"]-1,r["capacity_policy"]["team"]-1): key for key,r in records.items()}
            for key, r in records.items():
                c = r["capacity_policy"]; stage,lane = c["stage"]-1,c["team"]-1
                parents = [grid[s,i] for s,i in parent_positions(stage,lane,p["width"],p["seed"],p["dependency_pattern"])]
                expected = [{"consumer_page_id": child,"activation_stage": v["capacity_policy"]["stage"],
                             "weight": 1+(v["capacity_policy"]["stage"]+v["capacity_policy"]["team"])%3,"max_units":400}
                            for child,v in records.items() if key in v["parent_page_ids"]]
                if (c["page_id"] != key or r["parent_page_ids"] != parents or c["consumers"] != expected
                        or c["local_capacity_units"] != (200+lane*40 if not stage else 48+p["seed"]%5*8+lane*16)
                        or r["local_limits"]["batch_size"] != 400+lane*100+stage*30):
                    return False
            return True
        fact("capacity_relationships", capacity)
        fact("relation_edges", lambda: Counter((r["source_page_id"],r["target_page_id"]) for r in world["tables"]["relation"])
             == Counter((key,parent) for key,r in contracts().items() for parent in r["parent_page_ids"]))
        fact("amendment_count", lambda: public["planned_change_windows"] == len(world["planning"]["scheduled_updates"])
             == int(p["amendment"]))
    elif program == FUNDING:
        def nodes(): return world["mail"]["nodes"]
        fact("node_grid", lambda: len(public["case_ids"]) == p["cases"] == len(set(public["case_ids"])) and
             len(nodes()) == p["cases"]*p["phases"] and public["nodes"] == list(nodes().values()) and
             Counter((n["phase"],n["case_id"]) for n in nodes().values()) ==
             Counter((phase,case) for phase in range(p["phases"]) for case in public["case_ids"]))
        def funding():
            from integrations.internal_mcp.topology import parent_positions
            all_nodes=nodes(); cases=public["case_ids"]
            grid={(n["phase"],cases.index(n["case_id"])):key for key,n in all_nodes.items()}
            for key,n in all_nodes.items():
                lane=cases.index(n["case_id"])
                parents=[grid[s,i] for s,i in parent_positions(n["phase"],lane,p["cases"],p["seed"],p["dependency_pattern"])]
                doc=world["mail"]["documents"][n["brief_document_id"]]
                content=doc["versions"]["1"]["content"]
                items=content["line_items"]; policy=content["funding_policy"]
                if (key!=n["node_id"] or doc["document_id"]!=n["brief_document_id"] or content["node_id"]!=key
                        or content["case_id"]!=n["case_id"] or content["phase"]!=n["phase"]
                        or n["parent_node_ids"]!=parents or content["parent_node_ids"]!=parents or len(items)!=p["line_items_per_brief"]
                        or len({i['item_id'] for i in items})!=len(items)
                        or any(type(i[k]) is not int or i[k]<=0 for i in items for k in ("quantity","unit_cost_cents","work_days"))):
                    return False
                cost=sum(i["quantity"]*i["unit_cost_cents"] for i in items)
                consumers=[{"node_id":child,"maturity_phase":v["phase"],"weight":1+index%3}
                           for index,(child,v) in enumerate(all_nodes.items()) if key in v["parent_node_ids"]]
                if (policy["local_grant_cents"]!=cost+40000+lane*10000 or policy["minimum_retained_cents"]!=5000
                        or policy["consumers"]!=consumers):
                    return False
            return True
        fact("funding_relationships", funding)
    elif program == SYNTHESIS:
        def batches(): return world["library"]["batches"]
        def value(work): return json.loads(next(p["text"] for p in work["versions"]["1"]["passages"] if p["passage_id"]=="results"))
        fact("acquisition_grid", lambda: len(public["sections"])==p["sections"]==len({s["section_id"] for s in public["sections"]})
             and len(batches())==p["rounds"]==public["rounds"] and [b["round"] for b in batches()]==list(range(p["rounds"]))
             and all(len(b["works"])==1+p["sections"]*(p["studies_per_section"]+p["distractors_per_section"]) for b in batches()))
        def identities():
            all_keys=[]
            for stage,b in enumerate(batches()):
                all_keys.extend(b["works"])
                if any(key!=w["source_key"] or w["acquisition_round"]!=stage or w["versions"]["1"]["metadata"]["doi"]!=key
                       for key,w in b["works"].items()): return False
                expected=Counter({key:1+(p["alias_copies"] if i in (0,1) else 0) for i,key in enumerate(b["works"])})
                if stage and p["alias_copies"]: expected[next(iter(batches()[stage-1]["works"]))]+=1
                if (Counter(r["source_key"] for r in b["records"])!=expected
                        or len({r["paper_id"] for r in b["records"]})!=len(b["records"])): return False
            return len(all_keys)==len(set(all_keys))
        fact("source_identities", identities)
        def synthesis():
            anchors=[next(iter(b["works"])) for b in batches()]
            rng=random.Random(p["seed"]+1101)
            for stage,b in enumerate(batches()):
                for number,(key,w) in enumerate(b["works"].items()):
                    v=value(w)
                    if number==0:
                        expected={"role":"calibration","metric":"allocation_score","population":"synthetic_items",
                                  "offset_points":rng.choice((-2,-1,1,2)),"origin_round":stage}
                    else:
                        unit=("points","fraction","basis_points")[(number+stage)%3]
                        score=rng.randint(30,85)
                        expected={"role":"study","metric":"allocation_score","population":"synthetic_items",
                            "sample_size":rng.randint(20,90),"value_numerator":score*(100 if unit=="basis_points" else 1),
                            "value_denominator":100 if unit=="fraction" else 1,"unit":unit,
                            "calibration_doi":anchors[max(0,stage-(1+number%3))],"origin_round":stage}
                        if w["versions"]["1"]["metadata"]["study_type"]=="commentary" or (stage==2 and number==len(b["works"])-2):
                            expected.update(metric="latency",unit="milliseconds")
                    if (v!=expected or any(type(v[k]) is not type(item) for k,item in expected.items())):
                        return False
                correction=b["correction"]
                if stage==1:
                    original=batches()[0]["works"][anchors[0]]["versions"]["1"]
                    expected=deepcopy(original)
                    expected["revision"]=2
                    expected["metadata"]["title"]+=" (corrected)"
                    expected["metadata"]["venue"]="Synthetic Proceedings: Corrected Edition"
                    passage=next(r for r in expected["passages"] if r["passage_id"]=="results")
                    corrected=json.loads(passage["text"]);corrected["offset_points"]+=2
                    passage["text"]=json.dumps(corrected,sort_keys=True)
                    if (correction["source_key"]!=anchors[0] or correction["version"]["revision"]!=2
                            or correction["version"]!=expected): return False
                elif correction is not None: return False
            return True
        fact("synthesis_origins", synthesis)
        fact("declared_batches", lambda: public["batches"]==[{"batch_id":b["batch_id"],"round":b["round"],
             "record_count":len(b["records"]),"has_publisher_correction":b["correction"] is not None} for b in batches()])
        fact("distractors", lambda: all(sum(w["versions"]["1"]["metadata"]["study_type"]=="commentary" for w in b["works"].values())
                                       ==p["sections"]*p["distractors_per_section"] for b in batches()))
    elif program == MEASURED:
        def briefs(): return world["social"]["briefs"]
        def campaigns(): return [c["campaign_id"] for c in world["tables"]["campaign"]]
        fact("campaign_brief_grid", lambda: len(campaigns())==p["campaigns"]==len(set(campaigns())) and
             public["brief_ids"]==list(briefs()) and Counter((b["wave"],b["campaign_id"]) for b in briefs().values())==
             Counter((wave,case) for wave in range(1,p["waves"]+1) for case in campaigns()))
        def measured():
            grid={(b["wave"],b["campaign_id"]):key for key,b in briefs().items()}
            posts=world["tables"]["content_post"]
            count=p["variants_per_brief"]
            if len(posts)!=len({r["post_id"] for r in posts}) or len(posts)!=len(briefs())*(count+1): return False
            for key,b in briefs().items():
                lane=campaigns().index(b["campaign_id"])
                parents=[] if b["wave"]==1 else [grid[b["wave"]-1,campaigns()[i]] for i in
                    (range(min(2,p["campaigns"])) if lane==p["campaigns"]-1 else (lane,))]
                owned=[r for r in posts if r["brief_id"]==key]
                if (key!=b["brief_id"] or b["parent_brief_ids"]!=parents
                        or any(r["campaign_id"]!=b["campaign_id"] for r in owned)
                        or Counter((r["role"],r["variant_index"]) for r in owned)!=Counter([*(('variant',i) for i in range(count)),('deployment',None)])
                        or b["variant_post_ids"]!=[r["post_id"] for r in owned if r["role"]=="variant"]
                        or [b["deployment_post_id"]]!=[r["post_id"] for r in owned if r["role"]=="deployment"]): return False
            return True
        fact("measured_parent_roles", measured)
        def configuration():
            count=p["variants_per_brief"]
            audiences=[r["segment_id"] for r in world["tables"]["audience_segment"]]
            if len(audiences)!=1+p["campaigns"]*count or len(set(audiences))!=len(audiences):return False
            for b in briefs().values():
                lane=campaigns().index(b["campaign_id"])
                expected=[f"{verb} package {lane+1} wave {b['wave']}" for verb in ("Explore","Compare","Review","Discover","Assess")[:count]]
                if (b["ctas"]!=expected or b["allowed_audience_ids"]!=audiences
                        or b["initial_variant_audiences"]!=[[audiences[0],audiences[1+lane*count+i]] for i in range(count)]):return False
            return (world["tables"]["workspace"][0]["active_post_count"]==len(world["tables"]["content_post"])
                    and all(c["post_count"]==(count+1)*p["waves"] for c in world["tables"]["campaign"]))
        fact("variant_configuration",configuration)
        def responses():
            response=world["social"]["response_profiles"]
            if set(response)!={key for b in briefs().values() for key in b["variant_post_ids"]}: return False
            for b in briefs().values():
                variants=[response[key] for key in b["variant_post_ids"]]
                if sorted(v["impressions"] for v in variants)!=sorted([1000,10000,*[5000+1000*i for i in range(p["variants_per_brief"]-2)]]): return False
                if any(type(v["clicks"]) is not int or type(v["impressions"]) is not int or not (60<=v["clicks"]<=95 if v["impressions"]==1000 else 250<=v["clicks"]<=450 if v["impressions"]==10000 else 100<=v["clicks"]<=600)
                       for v in variants): return False
            return True
        fact("measured_responses", responses)
        fact("rights_notice", lambda: len(world["social"]["scheduled_updates"])==1 and
             world["social"]["scheduled_updates"][0]["after_wave"]==1 and
             world["social"]["scheduled_updates"][0]["asset_id"]==next(a["asset_id"] for a in world["tables"]["asset"]
                 if a["campaign_id"]==campaigns()[0]))
    return rows
