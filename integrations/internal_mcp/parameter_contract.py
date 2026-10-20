"""Reviewed public spellings for finite backend parameters, not business answers."""

from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from .io import read_json


REVISION = "public_static_enums_v1"


@lru_cache(maxsize=1)
def contracts():
    return read_json(Path(__file__).with_name("public_parameter_enums.json"))


def apply_public_enums(domain, metadata):
    for name, fields in contracts().get(domain, {}).items():
        if name not in metadata:
            continue
        properties = metadata[name]["input_schema"]["properties"]
        for parameter, values in fields.items():
            if parameter not in properties:
                raise ValueError(f"Public parameter contract drift: {domain}.{name}.{parameter}")
            # Domain wrappers may deliberately admit a stricter subset.
            if "enum" not in properties[parameter]:
                properties[parameter] = {**deepcopy(properties[parameter]), "enum": deepcopy(values)}
