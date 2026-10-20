"""Explicit business-program routes, selected by the actual environment format."""
from copy import deepcopy
import importlib

from integrations.internal_mcp.qualify_topologies import DOMAINS

EXPOSURE = "skillhorizon.c06.exposure_network.v1"
REACH = "skillhorizon.c06.reach_network.v2"
TRANSFER = "skillhorizon.c09.transfer_network.v1"
WORKLOAD = "skillhorizon.c47.workload_network.v1"
ASSEMBLY = "skillhorizon.c50.assembly_network.v1"
SHORT_PROGRAMS = {"C09": TRANSFER, "C47": WORKLOAD, "C50": ASSEMBLY}
CAPACITY = "skillhorizon.c25.capacity_network.v1"
FUNDING = "skillhorizon.c01.funding_network.v1"
SYNTHESIS = "skillhorizon.c11.synthesis_network.v1"
MEASURED = "skillhorizon.c06.measured_waves.v1"
SUPPLEMENTAL_PROGRAMS = {"C25": CAPACITY, "C06": MEASURED, "C01": FUNDING, "C11": SYNTHESIS}


def binding(identifier, environment_format=None):
    if identifier not in DOMAINS:
        raise ValueError("Unknown internal environment")
    spec = deepcopy(DOMAINS[identifier])
    current = importlib.import_module(f'integrations.{spec["module"]}.{spec["builder"]}').FORMAT
    if environment_format in (None, current):
        return {**spec, "format": current}
    if identifier in SUPPLEMENTAL_PROGRAMS and environment_format == SUPPLEMENTAL_PROGRAMS[identifier]:
        variants = {
            "C25": ("capacity_profile_build", "capacity_build", {"width": 3, "depth": 2, "amendment": False, "dependency_pattern": "staggered"}),
            "C01": ("funding_profile_build", "funding_build", {"cases": 2, "phases": 2, "line_items_per_brief": 4, "dependency_pattern": "distributed"}),
            "C11": ("synthesis_profile_build", "synthesis_build", {"sections": 2, "rounds": 2, "studies_per_section": 1,
                                                                  "alias_copies": 0, "distractors_per_section": 0}),
            "C06": ("measured_profile_build", "build", {"campaigns": 2, "waves": 1, "variants_per_brief": 2})}
        builder, delegate, sizes = variants[identifier]
        parameters = {**sizes, "background": False, "runtime_identity": "task_local_v1"}
        package = "integrations." + spec["module"]
        consumers = [package + "." + delegate + ".build"]
        if delegate != "build":
            consumers.append(package + ".build.build")
        return {**spec, "format": environment_format, "builder": builder, "runtime": "runtime",
                "class": {"C25": "SkillPlanning", "C01": "SkillCorrespondence", "C11": "SkillLibrary", "C06": "SkillSocial"}[identifier],
                "witness": "witness", "evaluator": "evaluate", "parameters": deepcopy(parameters),
                "profile_parameters": parameters, "consumers": consumers}
    if identifier in SHORT_PROGRAMS and environment_format == SHORT_PROGRAMS[identifier]:
        variants = {
            "C09": {"builder": "transfer_profile_build", "runtime": "network_runtime",
                    "class": "TransferNetwork", "witness": "network_witness", "evaluator": "network_evaluate",
                    "sizes": {"cohorts": 2, "legs": 3}, "delegate": "network_build"},
            "C47": {"builder": "workload_profile_build", "runtime": "workload_runtime",
                    "class": "WorkloadFinOps", "witness": "witness", "evaluator": "evaluate",
                    "sizes": {"units": 2, "periods": 2, "resources_per_unit": 2}, "delegate": "workload_build"},
            "C50": {"builder": "assembly_profile_build", "runtime": "runtime",
                    "class": "SkillProduction", "witness": "witness", "evaluator": "evaluate",
                    "sizes": {"levels": 2, "terminal_quantity": 1, "terminal_lanes": 2, "supplier_lanes": 2}, "delegate": "assembly_build"}}
        variant = variants[identifier]
        parameters = {**variant["sizes"], "background": False,
                      "dependency_pattern": "distributed", "runtime_identity": "task_local_v1"}
        return {**spec, **{k: v for k, v in variant.items() if k not in {"sizes", "delegate"}},
                "format": environment_format, "parameters": deepcopy(parameters), "profile_parameters": parameters,
                "consumers": [f'integrations.{spec["module"]}.{variant["delegate"]}.build',
                              f'integrations.{spec["module"]}.build.build']}
    if identifier == "C06" and environment_format == EXPOSURE:
        parameters = {"campaigns": 2, "waves": 2, "background": False,
                      "dependency_pattern": "distributed", "runtime_identity": "task_local_v1"}
        return {**spec, "format": EXPOSURE, "builder": "exposure_profile_build",
                "runtime": "exposure_runtime", "class": "ExposureSocial", "witness": "witness",
                "evaluator": "evaluate", "parameters": deepcopy(parameters), "profile_parameters": parameters,
                "consumers": ["integrations.socialops.exposure_build.build", "integrations.socialops.build.build"]}
    raise ValueError("Unregistered or cross-domain business program")
