"""Public authored-result shape; no source selection or numerical answer access."""


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def schemas(*, calibration_graph):
    text = {"type": "string", "minLength": 1}
    integer = {"type": "integer"}
    rational = {**object_schema({"numerator": integer, "denominator": integer}),
                "description": "Exact fraction as integer numerator and nonzero integer denominator; equivalent unreduced fractions are allowed. Not a string or unevaluated arithmetic expression."}
    contribution = {"source_key": text, "annotation_id": text, "calibration_source_key": text,
                    "calibration_annotation_id": text, "sample_size": integer, "calibrated_points": rational}
    if calibration_graph:
        contribution.update(effective_calibration=object_schema({"gain": rational, "offset": rational}),
            calibration_chain={"type": "array", "items": object_schema({"source_key": text, "annotation_id": text}),
                               "description": "Current evidence bindings for the complete actual ancestor set, including the named calibration."})
    section = object_schema({
        "contributions": {"type": "array", "items": object_schema(contribution)},
        "exclusions": {"type": "array", "items": object_schema({"source_key": text, "reason": {"enum": [
            "retracted_study", "calibration_reference", "incompatible_metric_or_population"]}})},
        "total_sample_size": integer, "weighted_sum_points": rational, "mean_points": rational})
    cross_section = object_schema({
        "unique_study_source_keys": {"type": "array", "items": text},
        "total_sample_size": integer, "weighted_sum_points": rational, "mean_points": rational,
        "section_contrasts": {"type": "array", "items": object_schema({"left_section_id": text,
            "right_section_id": text, "mean_difference_points": rational})}})
    return section, cross_section
