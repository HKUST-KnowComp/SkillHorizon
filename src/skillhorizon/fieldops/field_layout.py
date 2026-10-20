"""Explicit device work per service stage, independent of the service graph."""

REVISION = "field_device_layout_v1"
PROGRAMS = {"pressure_services_v2", "pressure_branches_v3"}
SERVICE_SCALE = "extended_services_v1"
PARALLEL_SERVICE_SCALE = "parallel_services_v2"
REGIONAL_SERVICE_SCALE = "regional_services_v3"
HIGHBAND_SERVICE_SCALE = "highband_services_v4"
SERVICE_LIMITS = {SERVICE_SCALE: (5, 24), PARALLEL_SERVICE_SCALE: (16, 40),
                  REGIONAL_SERVICE_SCALE: (16, 384), HIGHBAND_SERVICE_SCALE: (16, 768)}


def normalize_counts(domain, program, groups, counts, service_scale=None):
    if service_scale is not None and (service_scale not in SERVICE_LIMITS or
            (domain != "fieldops" or program != "pressure_branches_v3")):
        raise ValueError("Service scale applies only to FieldOps pressure_branches_v3")
    if domain != "fieldops" or program not in PROGRAMS:
        if counts is not None:
            raise ValueError("device_counts requires a FieldOps service program")
        return None
    low, high = (6, 12) if program == "pressure_services_v2" else (8, 16)
    if service_scale:
        low, high = SERVICE_LIMITS[service_scale]
    if type(groups) is not int or not low <= groups <= high:
        raise ValueError("Device layout requires a valid service-stage count")
    if counts is None:
        return [2] * groups
    if (type(counts) is not list or len(counts) != groups
            or any(type(n) is not int or n not in (1, 2) for n in counts)):
        raise ValueError("device_counts must contain one integer 1 or 2 per stage")
    return list(counts)


def stage_layout(counts):
    stages, ordinal = {}, 1
    for index, count in enumerate(counts):
        roles = ["low", "high"] if count == 2 else ["low" if index % 2 == 0 else "high"]
        stages[str(index + 1)] = {
            "device_ids": [f"D{n:02d}" for n in range(ordinal, ordinal + count)],
            "severity_roles": roles,
        }
        ordinal += count
    return stages
