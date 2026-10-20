"""Reference cross-skill data depth, excluding intra-skill draft/check edges."""


def business_data_structure(project, stages, actions):
    owner = {action["result"]["artifact_id"]: index for index, stage in enumerate(stages)
             for action in actions[stage["action_start"]:stage["action_end"]] if action["result"].get("artifact_id")}
    parents = {index: set() for index in range(len(stages))}
    internal = external = 0
    for identifier, artifact in project["artifacts"].items():
        stage = owner[identifier]
        for value in artifact["inputs"]:
            if isinstance(value, dict) and value["type"] != "artifact":
                continue
            parent = value if isinstance(value, str) else value["id"]
            if owner[parent] == stage:
                internal += 1
            else:
                external += 1
                parents[stage].add(owner[parent])
    depths = {}
    for stage, dependencies in parents.items():
        if any(parent >= stage for parent in dependencies):
            raise ValueError("Reference business dependencies are not chronological")
        depths[stage] = 1 + max((depths[parent] for parent in dependencies), default=0)
    return {"reference_business_skill_data_depth": max(depths.values(), default=0),
            "intra_skill_artifact_edges": internal, "cross_skill_artifact_edges": external,
            "cross_skill_join_stages": sum(len(values) > 1 for values in parents.values()),
            "scope": "executed_reference_skills_not_minimum_plan; resources_and_time_not_counted_as_data_edges"}
