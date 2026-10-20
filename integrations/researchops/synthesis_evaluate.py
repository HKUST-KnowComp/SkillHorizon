"""Independent source-grounded normalization and overlapping-section accounting."""

from collections import Counter
from fractions import Fraction
import json


class MissingContribution(ValueError):
    """Well-formed submitted synthesis omitted a required compatible study."""


def verify(public, row, sources, annotations, papers):
    errors = []
    def check(value, code):
        if not value:
            errors.append(code)
    def number(value):
        return {"numerator": value.numerator, "denominator": value.denominator}
    def rational(value):
        if type(value["numerator"]) is not int or type(value["denominator"]) is not int:
            raise ValueError("Rational components must be integers")
        return number(Fraction(value["numerator"], value["denominator"]))
    def normalized_section(value):
        contributions = [{**item, "calibrated_points": rational(item["calibrated_points"])} for item in value["contributions"]]
        if "calibration_graph" in public["synthesis_policy"]:
            for item in contributions:
                item["effective_calibration"] = {k: rational(item["effective_calibration"][k]) for k in ("gain", "offset")}
                chain = item["calibration_chain"]
                if len({c["source_key"] for c in chain}) != len(chain):
                    raise ValueError("Duplicate ancestor binding")
                item["calibration_chain"] = sorted(chain, key=lambda c: c["source_key"])
        if len({item["source_key"] for item in contributions}) != len(contributions):
            raise ValueError("Duplicate empirical source")
        exclusions = value["exclusions"]
        if len({item["source_key"] for item in exclusions}) != len(exclusions):
            raise ValueError("Duplicate excluded source")
        return {**value, "contributions": sorted(contributions, key=lambda item: item["source_key"]),
            "exclusions": sorted(exclusions, key=lambda item: item["source_key"]),
            "weighted_sum_points": rational(value["weighted_sum_points"]), "mean_points": rational(value["mean_points"])}
    try:
        policy = public["synthesis_policy"]
        measurements = {key: json.loads(next(p["text"] for p in version["passages"] if p["passage_id"] == "results")) for key, version in sources.items()}
        maps, depths, ancestry, parent_joins = {}, {}, {}, {}
        if "calibration_graph" in policy:
            pending = {k: v for k, v in measurements.items() if v["role"] == "calibration"}
            while pending:
                ready = [k for k, v in pending.items() if all(p["source_key"] in maps for p in v["parent_calibrations"])]
                if not ready:
                    raise ValueError("Missing or cyclic calibration dependencies")
                for key in ready:
                    value = pending.pop(key)
                    if value.get("evidence_status", "active") != "active":
                        raise ValueError("Withdrawn calibration has no replacement")
                    parents = value["parent_calibrations"]
                    if len({p["source_key"] for p in parents}) != len(parents):
                        raise ValueError("Repeated calibration parent")
                    if any(type(value[k]) is not int or value[k] <= 0 for k in ("gain_numerator", "gain_denominator")):
                        raise ValueError("Invalid gain")
                    if value["metric"] != policy["metric"] or value["population"] != policy["population"]:
                        raise ValueError("Incompatible calibration map")
                    coefficient = Fraction(value["gain_numerator"], value["gain_denominator"])
                    upstream_g, upstream_b = Fraction(1), Fraction(0)
                    chain = {key}
                    if parents:
                        if any(type(p["weight"]) is not int or p["weight"] <= 0 or measurements[p["source_key"]]["origin_round"] >= value["origin_round"] for p in parents):
                            raise ValueError("Invalid calibration parent weight or chronology")
                        weight = sum(p["weight"] for p in parents)
                        upstream_g = sum((maps[p["source_key"]][0] * p["weight"] for p in parents), Fraction(0)) / weight
                        upstream_b = sum((maps[p["source_key"]][1] * p["weight"] for p in parents), Fraction(0)) / weight
                        for parent in parents:
                            chain.update(ancestry[parent["source_key"]])
                    maps[key] = coefficient * upstream_g, coefficient * upstream_b + value["offset_points"]
                    ancestry[key], parent_joins[key] = chain, len(parents)
                    depths[key] = 1 + max([0] + [depths[p["source_key"]] for p in parents])
        supplied = {s["section_id"]: s for s in row["sections"]}
        global_studies, means, appearances, ages, fanout = {}, {}, Counter(), Counter(), Counter()
        used_anchors, multi_parent_studies = set(), set()
        def valid_annotation(identifier, key):
            annotation = annotations[identifier]
            text = next(p["text"] for p in sources[key]["passages"] if p["passage_id"] == "results")
            return papers[annotation["paper_id"]]["status"] == "active" and papers[annotation["paper_id"]]["source_key"] == key and annotation["passage_id"] == "results" and annotation["content"] == text and annotation["start"] == 0 and annotation["end"] == len(text)
        for section in public["sections"]:
            criterion = section["criteria"]
            keys = sorted(key for key, version in sources.items() if version["metadata"]["year"] >= criterion["year_min"] and version["metadata"]["study_type"] == criterion["study_type"] and criterion["topic"] in version["metadata"]["keywords"])
            authored = supplied[section["section_id"]]["synthesis"]
            declared = {r["source_key"]: r for r in authored["contributions"]}
            contributors, excluded, weighted, total_n = [], [], Fraction(0), 0
            for key in keys:
                observation = measurements[key]
                if observation.get("evidence_status", "active") not in {"active", "retracted"}:
                    raise ValueError("Unknown evidence status")
                if observation.get("evidence_status") == "retracted":
                    if observation["role"] != "study":
                        raise ValueError("Withdrawn calibration has no replacement")
                    excluded.append({"source_key": key, "reason": "retracted_study"})
                    continue
                if observation["role"] == "calibration":
                    excluded.append({"source_key": key, "reason": "calibration_reference"})
                    continue
                if observation["metric"] != policy["metric"] or observation["population"] != policy["population"]:
                    excluded.append({"source_key": key, "reason": "incompatible_metric_or_population"})
                    continue
                anchor = observation["calibration_doi"]
                calibration = measurements[anchor]
                check(calibration["role"] == "calibration" and calibration["metric"] == policy["metric"] and calibration["population"] == policy["population"], "CALIBRATION_COMPATIBILITY")
                unit_n, unit_d = policy["unit_factors_to_points"][observation["unit"]]
                value = Fraction(observation["value_numerator"] * unit_n, observation["value_denominator"] * unit_d) - Fraction(calibration["offset_points"])
                if "calibration_graph" in policy:
                    g, b = maps[anchor]
                    value = (Fraction(observation["value_numerator"] * unit_n, observation["value_denominator"] * unit_d) - b) / g
                    used_anchors.add(anchor)
                    if parent_joins[anchor] > 1:
                        multi_parent_studies.add(key)
                n = observation["sample_size"]
                check(type(n) is int and n > 0, "STUDY_SAMPLE_SIZE")
                if key not in declared:
                    raise MissingContribution()
                contribution = declared[key]
                check(valid_annotation(contribution["annotation_id"], key) and valid_annotation(contribution["calibration_annotation_id"], anchor), "ACTUAL_STUDY_AND_CALIBRATION_EVIDENCE")
                contributors.append({"source_key": key, "annotation_id": contribution["annotation_id"], "calibration_source_key": anchor,
                    "calibration_annotation_id": contribution["calibration_annotation_id"], "sample_size": n, "calibrated_points": number(value)})
                if "calibration_graph" in policy:
                    bindings = contribution["calibration_chain"]
                    check(len(bindings) == len(ancestry[anchor]) and {c["source_key"] for c in bindings} == ancestry[anchor], "COMPLETE_CALIBRATION_ANCESTOR_JOIN")
                    check(all(valid_annotation(c["annotation_id"], c["source_key"]) for c in bindings), "CURRENT_ANCESTOR_CALIBRATION_EVIDENCE")
                    contributors[-1].update(effective_calibration={"gain": number(g), "offset": number(b)},
                        calibration_chain=sorted(bindings, key=lambda c: c["source_key"]))
                total_n += n
                weighted += n * value
                global_studies[key] = (n, value)
                appearances[key] += 1
                ages[observation["origin_round"] - calibration["origin_round"]] += 1
                fanout[anchor] += 1
            mean = weighted / total_n
            means[section["section_id"]] = mean
            expected = {"contributions": contributors, "exclusions": excluded, "total_sample_size": total_n,
                "weighted_sum_points": number(weighted), "mean_points": number(mean)}
            check(normalized_section(authored) == expected, "INDEPENDENT_SECTION_WEIGHTING_UNITS_AND_CALIBRATION")
        total = sum(n for n, _ in global_studies.values())
        weighted = sum((n * value for n, value in global_studies.values()), Fraction(0))
        names = sorted(means)
        contrasts = [{"left_section_id": left, "right_section_id": right, "mean_difference_points": number(means[left] - means[right])} for index, left in enumerate(names) for right in names[index + 1:]]
        expected_global = {"unique_study_source_keys": sorted(global_studies), "total_sample_size": total, "weighted_sum_points": number(weighted),
            "mean_points": number(weighted / total), "section_contrasts": contrasts}
        actual_global = row["cross_section_synthesis"]
        check(len(set(actual_global["unique_study_source_keys"])) == len(actual_global["unique_study_source_keys"]), "DUPLICATE_GLOBAL_STUDY")
        normalized_global = {**actual_global, "unique_study_source_keys": sorted(actual_global["unique_study_source_keys"]),
            "weighted_sum_points": rational(actual_global["weighted_sum_points"]), "mean_points": rational(actual_global["mean_points"]),
            "section_contrasts": sorted([{**item, "mean_difference_points": rational(item["mean_difference_points"])} for item in actual_global["section_contrasts"]],
                key=lambda item: (item["left_section_id"], item["right_section_id"]))}
        if public.get("instance_revision") == "researchops_portfolio150_v2":
            contrasts, pairs = [], set()
            for item in normalized_global["section_contrasts"]:
                left, right = item["left_section_id"], item["right_section_id"]
                pair = tuple(sorted((left, right)))
                if left == right or pair in pairs:
                    raise ValueError("Self contrast or duplicate unordered pair")
                pairs.add(pair)
                value = Fraction(item["mean_difference_points"]["numerator"], item["mean_difference_points"]["denominator"])
                contrasts.append({**item, "left_section_id": pair[0], "right_section_id": pair[1],
                    "mean_difference_points": number(-value if left > right else value)})
            normalized_global["section_contrasts"] = sorted(contrasts, key=lambda x: (x["left_section_id"], x["right_section_id"]))
        check(normalized_global == expected_global, "DEDUPLICATED_GLOBAL_STUDY_POOL_AND_SECTION_CONTRASTS")
        return errors, {"unique_studies": len(global_studies), "shared_studies_across_sections": sum(v > 1 for v in appearances.values()),
            "retracted_sources": sum(v.get("evidence_status") == "retracted" for v in measurements.values()),
            "incompatible_study_sources": sum(v["role"] == "study" and (v["metric"] != policy["metric"] or v["population"] != policy["population"]) for v in measurements.values()),
            "calibration_origin_age_histogram_with_section_appearances": dict(sorted(ages.items())), "max_calibration_fanout_with_section_appearances": max(fanout.values(), default=0),
            "global_sample_size": total, "global_mean_points": expected_global["mean_points"], "minimal_horizon_proven": False,
            **({"calibration_network": {"max_derived_map_depth": max(depths.values()), "max_parent_map_join": max(parent_joins.values()),
                "max_ancestor_evidence_count": max(map(len, ancestry.values())), "derived_maps": len(maps),
                "max_used_calibration_depth": max(depths[k] for k in used_anchors),
                "max_used_ancestor_evidence_count": max(len(ancestry[k]) for k in used_anchors),
                "multi_parent_anchored_studies": len(multi_parent_studies),
                "scope": "Synthetic affine coordinate maps; canonical-work deduplication does not establish participant independence"}} if maps else {})}
    except MissingContribution:
        return errors + ["MISSING_COMPATIBLE_STUDY_CONTRIBUTION"], {}
    except (KeyError, ValueError, TypeError, StopIteration, ZeroDivisionError):
        return errors + ["MALFORMED_OR_INCOMPLETE_QUANTITATIVE_SYNTHESIS"], {}
