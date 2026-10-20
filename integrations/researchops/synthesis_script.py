"""Optional calculator: explicit source quotes only, no files or evaluation access."""

from fractions import Fraction
import json
import sys


class PublicInputError(ValueError):
    def __init__(self, code, paths=(), missing_source_keys=()):
        self.diagnostic = {"code": code, "paths": list(paths)[:30],
                           "missing_source_keys": sorted(missing_source_keys)[:30]}
        super().__init__(code)


def validate_public_input(data):
    """Dependencies come only from submitted sections and submitted source quotes."""
    if not isinstance(data, dict):
        raise PublicInputError("OBJECT_REQUIRED", ["inputs"])
    missing = [key for key in ("policy", "sections", "evidence") if key not in data]
    if missing:
        raise PublicInputError("MISSING_FIELDS", missing)
    if not isinstance(data["policy"], dict) or not isinstance(data["sections"], list) or not isinstance(data["evidence"], list):
        raise PublicInputError("INVALID_FIELD_TYPES", ["policy", "sections", "evidence"])
    for key in ("metric", "population", "unit_factors_to_points"):
        if key not in data["policy"]:
            raise PublicInputError("MISSING_FIELDS", ["policy." + key])
    if not data["sections"]:
        raise PublicInputError("EMPTY_SECTIONS", ["sections"])
    parsed, evidence, section_ids, required = {}, {}, set(), set()
    for index, row in enumerate(data["evidence"]):
        path = f"evidence[{index}]"
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k] for k in ("source_key", "annotation_id", "quote")):
            raise PublicInputError("MISSING_OR_INVALID_EVIDENCE_FIELDS", [path])
        key = row["source_key"]
        if key in parsed:
            raise PublicInputError("DUPLICATE_CANONICAL_EVIDENCE", [path + ".source_key"])
        try:
            value = json.loads(row["quote"])
        except (TypeError, ValueError):
            raise PublicInputError("INVALID_QUOTE_JSON", [path + ".quote"]) from None
        if not isinstance(value, dict) or value.get("role") not in {"study", "calibration"}:
            raise PublicInputError("INVALID_SOURCE_ROLE", [path + ".quote.role"])
        parsed[key], evidence[key] = value, row
    for index, section in enumerate(data["sections"]):
        path = f"sections[{index}]"
        if (not isinstance(section, dict) or not isinstance(section.get("section_id"), str)
                or not isinstance(section.get("source_keys"), list)
                or any(not isinstance(k, str) or not k for k in section["source_keys"])):
            raise PublicInputError("INVALID_SECTION_FIELDS", [path])
        if section["section_id"] in section_ids or len(set(section["source_keys"])) != len(section["source_keys"]):
            raise PublicInputError("DUPLICATE_SECTION_OR_SOURCE", [path])
        section_ids.add(section["section_id"])
        required.update(section["source_keys"])
    pending = [key for key in required if key in parsed and parsed[key]["role"] == "study"]
    visited = set()
    while pending:
        key = pending.pop()
        if key in visited or key not in parsed:
            continue
        visited.add(key)
        row = parsed[key]
        if (row.get("evidence_status", "active") != "active" or row.get("metric") != data["policy"]["metric"]
                or row.get("population") != data["policy"]["population"]):
            continue
        links = [row.get("calibration_doi")] if row["role"] == "study" else (
            [p.get("source_key") if isinstance(p, dict) else None for p in row.get("parent_calibrations", [])]
            if "calibration_graph" in data["policy"] else [])
        if any(not isinstance(link, str) or not link for link in links):
            raise PublicInputError("INVALID_CALIBRATION_REFERENCE", ["submitted_quote.calibration_reference"])
        required.update(links)
        pending.extend(links)
    missing = required - parsed.keys()
    if missing:
        raise PublicInputError("MISSING_PUBLIC_EVIDENCE", ["evidence"], missing)
    return evidence, parsed


def rational(value):
    return {"numerator": value.numerator, "denominator": value.denominator}


def resolve_calibration(key, parsed, policy, cache, visiting):
    if key in cache:
        return cache[key]
    if key in visiting:
        raise ValueError("Cyclic calibration graph")
    row = parsed[key]
    if row.get("evidence_status", "active") != "active":
        raise ValueError("Calibration withdrawal requires a replacement map")
    if row["role"] != "calibration" or row["metric"] != policy["metric"] or row["population"] != policy["population"]:
        raise ValueError("Incompatible calibration ancestor")
    parents = row["parent_calibrations"]
    if len({p["source_key"] for p in parents}) != len(parents) or any(type(p["weight"]) is not int or p["weight"] <= 0 for p in parents):
        raise ValueError("Distinct positive-weight parents required")
    if any(type(row[k]) is not int or row[k] <= 0 for k in ("gain_numerator", "gain_denominator")):
        raise ValueError("Positive rational local gain required")
    visiting.add(key)
    gain, offset, chain = Fraction(1), Fraction(0), {key}
    if parents:
        gain, weight = Fraction(0), sum(p["weight"] for p in parents)
        for parent in parents:
            if parsed[parent["source_key"]]["origin_round"] >= row["origin_round"]:
                raise ValueError("Calibration parent must originate earlier")
            pg, pb, pc = resolve_calibration(parent["source_key"], parsed, policy, cache, visiting)
            gain += pg * parent["weight"] / weight
            offset += pb * parent["weight"] / weight
            chain |= pc
    local = Fraction(row["gain_numerator"], row["gain_denominator"])
    result = local * gain, local * offset + row["offset_points"], chain
    visiting.remove(key)
    cache[key] = result
    return result


def calculate(data):
    evidence, parsed = validate_public_input(data)
    policy = data["policy"]
    sections, unique, means, calibration_cache = {}, {}, {}, {}
    for section in data["sections"]:
        contributions, exclusions = [], []
        for key in sorted(section["source_keys"]):
            row = parsed[key]
            if row.get("evidence_status", "active") not in {"active", "retracted"}:
                raise ValueError("Unknown evidence status")
            if row.get("evidence_status") == "retracted":
                if row["role"] != "study":
                    raise ValueError("Calibration withdrawal requires a replacement map")
                exclusions.append({"source_key": key, "reason": "retracted_study"})
                continue
            if row["role"] == "calibration":
                exclusions.append({"source_key": key, "reason": "calibration_reference"})
                continue
            if row["metric"] != policy["metric"] or row["population"] != policy["population"]:
                exclusions.append({"source_key": key, "reason": "incompatible_metric_or_population"})
                continue
            anchor = row["calibration_doi"]
            calibration = parsed[anchor]
            if calibration["role"] != "calibration" or calibration["metric"] != row["metric"] or calibration["population"] != row["population"]:
                raise ValueError("Named calibration incompatible")
            normalized = Fraction(row["value_numerator"], row["value_denominator"]) * Fraction(*policy["unit_factors_to_points"][row["unit"]])
            if "calibration_graph" in policy:
                gain, offset, chain = resolve_calibration(anchor, parsed, policy, calibration_cache, set())
                points = (normalized - offset) / gain
            else:
                points = normalized - calibration["offset_points"]
            if type(row["sample_size"]) is not int or row["sample_size"] <= 0:
                raise ValueError("Positive sample size required")
            contribution = {"source_key": key, "annotation_id": evidence[key]["annotation_id"], "calibration_source_key": anchor,
                "calibration_annotation_id": evidence[anchor]["annotation_id"], "sample_size": row["sample_size"], "calibrated_points": rational(points)}
            if "calibration_graph" in policy:
                contribution.update(effective_calibration={"gain": rational(gain), "offset": rational(offset)},
                    calibration_chain=[{"source_key": k, "annotation_id": evidence[k]["annotation_id"]} for k in sorted(chain)])
            contributions.append(contribution)
            unique[key] = contribution
        total = sum(r["sample_size"] for r in contributions)
        weighted = sum((Fraction(r["calibrated_points"]["numerator"], r["calibrated_points"]["denominator"]) * r["sample_size"] for r in contributions), Fraction(0))
        if total == 0:
            raise ValueError("No compatible measured studies")
        means[section["section_id"]] = weighted / total
        sections[section["section_id"]] = {"contributions": contributions, "exclusions": exclusions, "total_sample_size": total,
            "weighted_sum_points": rational(weighted), "mean_points": rational(weighted / total)}
    total = sum(r["sample_size"] for r in unique.values())
    weighted = sum((Fraction(r["calibrated_points"]["numerator"], r["calibrated_points"]["denominator"]) * r["sample_size"] for r in unique.values()), Fraction(0))
    ids = sorted(means)
    contrasts = [{"left_section_id": left, "right_section_id": right, "mean_difference_points": rational(means[left] - means[right])} for index, left in enumerate(ids) for right in ids[index + 1:]]
    return {"sections": sections, "global": {"unique_study_source_keys": sorted(unique), "total_sample_size": total,
        "weighted_sum_points": rational(weighted), "mean_points": rational(weighted / total), "section_contrasts": contrasts}}


if __name__ == "__main__":
    try:
        print(json.dumps(calculate(json.load(sys.stdin)), allow_nan=False))
    except PublicInputError as error:
        print(json.dumps({"script_input_error": error.diagnostic}, allow_nan=False))
        sys.exit(2)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        print(json.dumps({"script_input_error": {"code": "INVALID_PUBLIC_VALUE", "paths": [], "missing_source_keys": []}}))
        sys.exit(2)
