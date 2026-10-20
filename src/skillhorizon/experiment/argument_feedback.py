"""Bounded structural feedback from submitted JSON and public schemas only."""

from itertools import islice
import json
import math
import re


MAX_ISSUES = 8
MAX_LABEL = 160


def pointer(parts):
    return ("/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts))[:MAX_LABEL]


class ObjectPairs(list):
    pass


def parse_arguments(raw):
    """Keep duplicate keys visible until checked; never repair ambiguous input."""
    issues = []
    issue_count = 0

    def issue(path, rule, **details):
        nonlocal issue_count
        issue_count += 1
        if len(issues) < MAX_ISSUES:
            issues.append({"path": pointer(path), "rule": rule, **details})

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non_finite_number")
        return number

    def convert(value, path):
        if isinstance(value, ObjectPairs):
            result = {}
            for key, item in value:
                converted = convert(item, (*path, key))
                if key in result:
                    issue((*path, key), "duplicate_key")
                else:
                    result[key] = converted
            return result
        if isinstance(value, list):
            return [convert(item, (*path, i)) for i, item in enumerate(value)]
        return value

    try:
        value = convert(json.loads(raw, object_pairs_hook=ObjectPairs,
            parse_float=finite, parse_constant=finite), ("arguments",))
        if not isinstance(value, dict):
            issue(("arguments",), "type", expected="object")
    except json.JSONDecodeError as exc:
        value = None
        issue(("arguments",), "json_syntax", line=exc.lineno, column=exc.colno)
    except (ValueError, RecursionError):
        value = None
        issue(("arguments",), "non_finite_number_or_excessive_nesting")
    return (None if issues else value), {"issues": issues, "truncated": issue_count > len(issues)}


def schema_rejection(validator, arguments):
    # Do not expose ValidationError.message: it can echo entire submitted objects.
    problems = list(islice(validator.iter_errors(arguments), MAX_ISSUES + 1))
    if not problems:
        return None
    issues = []
    truncated = len(problems) > MAX_ISSUES
    for error in problems[:MAX_ISSUES]:
        entry = {"path": pointer(("arguments", *error.absolute_path)), "rule": error.validator}
        if error.validator == "required":
            names = [key for key in error.validator_value if key not in error.instance]
            entry["missing_fields"] = [key[:MAX_LABEL] for key in names[:MAX_ISSUES]]
            truncated |= len(names) > MAX_ISSUES
        elif error.validator == "additionalProperties" and error.validator_value is False:
            names = sorted(key for key in error.instance if key not in error.schema.get("properties", {})
                and not any(re.search(pattern, key) for pattern in error.schema.get("patternProperties", {})))
            entry["unexpected_fields"] = [key[:MAX_LABEL] for key in names[:MAX_ISSUES]]
            truncated |= len(names) > MAX_ISSUES
        elif error.validator == "type":
            expected = error.validator_value
            entry["expected"] = expected if isinstance(expected, str) else expected[:MAX_ISSUES]
        elif error.validator == "enum":
            values = error.validator_value
            entry["allowed_values"] = [v[:MAX_LABEL] if isinstance(v, str) else v
                for v in values[:MAX_ISSUES] if v is None or isinstance(v, (str, int, float, bool))]
            truncated |= len(values) > MAX_ISSUES
        elif error.validator in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                "minItems", "maxItems", "minLength", "maxLength", "multipleOf"}:
            entry["constraint"] = error.validator_value
        elif error.validator in {"anyOf", "oneOf", "allOf"}:
            entry["schema_path"] = pointer(error.absolute_schema_path)
        if entry not in issues:
            issues.append(entry)
    return {"ok": False, "error": {"code": "ARGUMENT_SCHEMA",
        "message": "Arguments were not executed. Correct the listed paths against the public tool schema; no fields were moved or repaired.",
        "feedback_revision": "public_structure_v1", "issues": issues, "truncated": truncated}}
