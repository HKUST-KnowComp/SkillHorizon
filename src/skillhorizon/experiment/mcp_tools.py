"""Translate public MCP schemas without importing historical benchmark engines."""

from copy import deepcopy
import jsonschema

REFERENCE_SCHEMA_POLICY = "all_reference_arguments_public_schema_v1"


def canonicalize_mcp_schema(schema):
    if not isinstance(schema, dict):
        raise ValueError("MCP parameter schema must be an object")
    maps = {"properties", "patternProperties", "dependentSchemas"}
    children = {"items", "additionalProperties", "not", "if", "then", "else", "contains",
                "propertyNames", "unevaluatedItems", "unevaluatedProperties"}
    alternatives = {"anyOf", "allOf", "oneOf", "prefixItems"}

    def visit(value, references=()):
        if not isinstance(value, dict):
            return deepcopy(value)
        if "$ref" in value:
            reference = value["$ref"]
            if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                raise ValueError("Only local MCP schema references are supported")
            if reference in references:
                raise ValueError("Recursive MCP schema cannot be flattened")
            target = schema
            try:
                for part in reference[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError) as exc:
                raise ValueError("Unresolved local MCP schema reference") from exc
            resolved = visit(target, (*references, reference))
            siblings = visit({k:v for k,v in value.items() if k != "$ref"}, references)
            return {"allOf": [resolved, siblings]} if siblings else resolved
        result = {}
        for key, item in value.items():
            if key in {"title", "$defs", "default"}:
                continue
            if key in maps and isinstance(item, dict):
                result[key] = {name: visit(child, references) for name, child in item.items()}
            elif key in children:
                result[key] = visit(item, references)
            elif key in alternatives and isinstance(item, list):
                result[key] = [visit(child, references) for child in item]
            else:
                # Literal enum/const values and property names are data, not schemas.
                result[key] = deepcopy(item)
        kind = result.get("type")
        if kind == "object" or isinstance(kind, list) and "object" in kind:
            result.setdefault("properties", {})
            result.setdefault("required", [])
        return result
    return visit(schema)


def validate_mcp_reference_actions(tools, actions):
    validators = {}
    for tool in tools:
        name, schema = tool["name"], tool["parameters"]
        if name in validators:
            raise ValueError("Duplicate public tool name")
        validator = jsonschema.validators.validator_for(schema)
        validator.check_schema(schema)
        validators[name] = validator(schema)
    for index, action in enumerate(actions, 1):
        name = action["skill_id"]
        if name not in validators:
            raise ValueError(f"Reference action {index} names an unknown public tool: {name}")
        if not validators[name].is_valid(action["arguments"]):
            raise ValueError(f"Reference action {index} violates the public schema: {name}")
    return {"passed": True, "policy": REFERENCE_SCHEMA_POLICY,
            "checked_actions": len(actions), "provider_calls": 0}


def strict_compatible_mcp_schema(schema):
    # Keep the full MCP contract, but do not promise constrained decoding for
    # JSON Schema keywords outside the provider's strict subset.
    supported = {"type", "description", "properties", "required", "additionalProperties", "items",
                 "enum", "anyOf", "minLength", "maxLength", "pattern", "format", "minimum",
                 "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems", "maxItems"}
    if not isinstance(schema, dict) or set(schema) - supported:
        return False
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if "object" in kinds:
        properties = schema.get("properties", {})
        if (not isinstance(properties, dict) or set(schema.get("required", [])) != set(properties)
                or schema.get("additionalProperties") is not False
                or not all(strict_compatible_mcp_schema(value) for value in properties.values())):
            return False
    if "anyOf" in schema and not all(strict_compatible_mcp_schema(value) for value in schema["anyOf"]):
        return False
    if "array" in kinds and not strict_compatible_mcp_schema(schema.get("items", {})):
        return False
    return True


def provider_tools_from_mcp_listing(listing):
    tools = []
    for tool in listing.tools:
        parameters = canonicalize_mcp_schema(tool.input_schema)
        tools.append({"type": "function", "name": tool.name, "description": tool.description or "",
                      "parameters": parameters, "strict": strict_compatible_mcp_schema(parameters)})
    return tools
