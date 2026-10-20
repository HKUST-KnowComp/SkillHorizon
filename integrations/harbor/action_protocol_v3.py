"""Text action framing and actionable, public-only diagnostics for new runs."""

import json
import re

from jsonschema import Draft202012Validator

from .declarations import strict_json

PROTOCOL = "leading_prose_actions_v3"
FEEDBACK_VERSION = "explicit_envelope_feedback_v2"
MAX_PREFIX_CHARS = 1024
INSTRUCTIONS = """The listed public tools are available through this text-JSON interface.
The controller executes your actions and returns a Public tool results message;
after receiving it, you can issue another actions object in the next response.
There is no need for a separate native function-call interface. A protocol or
argument error does not remove the tools or undo earlier successful operations.
Use the returned error to correct your next call. Do not invent a tool outage.
Each call's arguments object must match that tool's public schema. Do not infer
one tool's argument layout from another tool or environment. Keep skill_run_id,
if used, alongside name and the outer arguments, not inside business inputs.
The actions object is the sole executable instruction. Prefer JSON only.
Up to 1024 characters of leading commentary may precede one complete actions
object, including balanced inline backticks, quotes and brackets. Commentary is
non-operative. No code fences, multiple JSON values, trailing prose, or prefixed
final envelopes. Wait for actual results before issuing dependent calls.
Before choosing final, compare the user's goal with the public results you have
actually observed. State remaining work or an observed blocker accurately.
"""

def tool_argument_hint(tool):
    """Describe the supplied public schema without assuming an environment."""
    schema = tool.get("parameters", {})
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    hint = (f"For {tool['name']}, put the required fields "
            + json.dumps(required) + " directly inside this call's arguments object. ")
    objects = [key for key in required if properties.get(key, {}).get("type") == "object"]
    if objects:
        hint += "Object-valued fields: " + json.dumps(objects) + ". "
    return hint + "Follow this tool's public schema; tools remain available."


def tool_instructions(tools):
    """Render examples only for exposed tools and schema-valid placeholders."""
    sections = []
    for tool in tools:
        if tool["name"] not in {"execute_step", "run_skill_script"}:
            continue
        schema = tool.get("parameters", {})
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        if not required:
            continue
        sections.append(tool_argument_hint(tool))
        arguments = {}
        for key in required:
            kind = properties.get(key, {}).get("type")
            if kind not in {"string", "object"}:
                break
            arguments[key] = {} if kind == "object" else f"<{key}>"
        else:
            if Draft202012Validator(schema).is_valid(arguments):
                example = {"type": "actions", "actions": [
                    {"name": tool["name"], "arguments": arguments}]}
                sections.append("Layout example only; replace placeholders and empty objects with "
                                "actual public IDs and business inputs:\n"
                                + json.dumps(example, separators=(",", ":")))
    return "\n" + "\n".join(sections) + "\n" if sections else ""


ACTION_SCHEMA = {
    "type": "object", "required": ["type", "actions"], "additionalProperties": False,
    "properties": {"type": {"const": "actions"}, "actions": {
        "type": "array", "minItems": 1, "maxItems": 32, "items": {
            "type": "object", "required": ["name", "arguments"], "additionalProperties": False,
            "properties": {"name": {"type": "string"}, "arguments": {"type": "object"},
                           "skill_run_id": {"type": "string"}}}}}}
FINAL_SCHEMA = {
    "type": "object", "required": ["type", "text"], "additionalProperties": False,
    "properties": {"type": {"const": "final"}, "text": {"type": "string"}}}


class ProtocolError(ValueError):
    def __init__(self, code, message, path="$"):
        self.detail = {"code": code, "path": path, "message": message[:500]}
        super().__init__(f"{code} at {path}: {self.detail['message']}")


def schema_detail(error, *, path="$", code="PUBLIC_SCHEMA"):
    # JSON Pointer escaping keeps arbitrary public argument names unambiguous.
    suffix = "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in error.path)
    # jsonschema's default maxItems message starts with the entire rejected
    # array. Truncating it can remove the actual constraint and repair advice.
    if error.validator in {"maxItems", "minItems"}:
        bound = "at most" if error.validator == "maxItems" else "at least"
        message = f"Expected {bound} {error.validator_value} items; received {len(error.instance)}."
        if error.validator == "maxItems" and code == "ACTION_SCHEMA" and path + suffix == "$/actions":
            message += " Submit a smaller batch; do not assume any actions in the rejected response executed."
    elif error.validator == "required":
        missing = [key for key in error.validator_value if key not in error.instance]
        message = "Missing required properties: " + json.dumps(missing, ensure_ascii=False)
    elif error.validator == "type":
        message = f"Expected JSON type {error.validator_value}; received {type(error.instance).__name__}."
    else:
        message = error.message
    return {"code": code, "path": path + suffix, "message": message[:500]}


def delimiter_hint(text, stop):
    """Locate an actual bracket mismatch without repairing or executing output."""
    stack=[]; quoted=False; escaped=False
    for index,char in enumerate(text[:stop+1]):
        if quoted:
            if escaped: escaped=False
            elif char == "\\": escaped=True
            elif char == '"': quoted=False
            continue
        if char == '"': quoted=True
        elif char in "{[": stack.append((char,index))
        elif char in "}]":
            if not stack:
                return f" Extra closing {char} at character {index}; no container is open."
            opening,position=stack[-1]; expected="}" if opening=="{" else "]"
            if char!=expected:
                order = ""
                if len(stack) <= 64 and all(c.isspace() or c in "}]" for c in text[index:]):
                    closers = ''.join('}' if item[0] == '{' else ']' for item in reversed(stack))
                    order = (" The remaining open containers close, inner to outer, as "
                             + json.dumps(closers) + "; verify and resubmit the complete envelope."
                             " The controller has not edited or executed it.")
                return f" Bracket mismatch at character {index}: found {char}, but the container opened with {opening} at character {position} requires {expected}." + order + " Check nested tool arguments and actions-array closure."
            stack.pop()
    if stop >= len(text) and stack and not quoted:
        opening, position = stack[-1]
        expected = "}" if opening == "{" else "]"
        hint = (f" At end of output, {len(stack)} containers remain open; "
                f"the innermost {opening} at character {position} requires {expected}.")
        if len(stack) <= 64:
            closers = ''.join('}' if item[0] == '{' else ']' for item in reversed(stack))
            hint += " Their closing order, inner to outer, is " + json.dumps(closers) + "."
        return (hint + " Complete any missing value before closing containers and resubmit "
                "the complete envelope. The controller has not edited or executed it.")
    return ""


def json_error(error):
    if isinstance(error, json.JSONDecodeError):
        code = "TRAILING_CONTENT" if error.msg == "Extra data" else "JSON_SYNTAX"
        message = f"{error.msg}; line {error.lineno}, column {error.colno}." + delimiter_hint(error.doc,error.pos)
        if re.match(r'\s*\{\s*"type"\s*:\s*"actions"\s*:', error.doc):
            message += (' After "type":"actions", use a comma and a separate "actions" key: '
                        '{"type":"actions","actions":[...]}. Do not write {"type":"actions":[...]}.')
        excerpt = error.doc[max(0, error.pos - 40):error.pos + 80]
        message += " Nearby rejected text: " + json.dumps(excerpt, ensure_ascii=False)
        return ProtocolError(code, message)
    return ProtocolError("INVALID_JSON", str(error) or type(error).__name__)


def is_integer_limit_error(error):
    return type(error) is ValueError and re.match(
        r"Exceeds the limit \([0-9]+ digits\) for integer string conversion:", str(error)
    ) is not None


def parse_json(text):
    """Translate only recognized input failures at the strict JSON boundary."""
    if not isinstance(text, str):
        raise ProtocolError("INVALID_JSON", "Expected a string containing one JSON envelope.")
    try:
        return strict_json(text)
    except (json.JSONDecodeError, UnicodeEncodeError, RecursionError) as error:
        raise json_error(error) from error
    except ValueError as error:
        if type(error) is not ValueError or not (str(error) in {
            "Duplicate JSON key", "Non-finite JSON number",
            "JSON nesting exceeds transport limit",
        } or is_integer_limit_error(error)):
            raise
        raise json_error(error) from error


def check_prefix(prefix):
    if not prefix or len(prefix) > MAX_PREFIX_CHARS:
        raise ProtocolError("ACTION_PREFIX", "Leading commentary must contain 1 to 1024 characters.")
    # Accept inline formatting, but never extract an action from an open quote,
    # inline-code span, array, or fenced example. Do not search for a later object.
    code_span, quoted, brackets = None, False, 0
    for token in re.findall(r"\\.|`+|\"|\[|\]|.", prefix, flags=re.S):
        if token.startswith("`") and len(token) >= 3:
            raise ProtocolError("ACTION_PREFIX", "Code fences are not executable action envelopes.")
        if code_span:
            if token == code_span:
                code_span = None
        elif quoted:
            if token == '"':
                quoted = False
        elif token.startswith("`"):
            code_span = token
        elif token == '"':
            quoted = True
        elif token == "[":
            brackets += 1
        elif token == "]":
            brackets -= 1
            if brackets < 0:
                break
    if code_span or quoted or brackets:
        raise ProtocolError("ACTION_PREFIX", "Close inline quotes, backticks and brackets before the actions object.")


def parse(text, *, defer_action_validation=False):
    prefix_length = 0
    try:
        value = parse_json(text)
    except ProtocolError as error:
        if (not isinstance(error.__cause__, json.JSONDecodeError)
                or not text.strip() or text.lstrip().startswith(('{', '[', '"'))):
            raise
        # Reject a leading JSON scalar followed by another value as well.
        try:
            json.JSONDecoder().raw_decode(text.lstrip())
        except json.JSONDecodeError:
            pass
        else:
            raise ProtocolError("TRAILING_CONTENT", "Return one JSON envelope, not multiple JSON values.")
        start = text.find("{")
        if start < 0:
            raise
        check_prefix(text[:start].strip())
        value = parse_json(text[start:])
        prefix_length = start
    final = isinstance(value, dict) and value.get("type") == "final"
    schema = FINAL_SCHEMA if final else ACTION_SCHEMA
    if not final and defer_action_validation:
        schema = {**ACTION_SCHEMA, 'properties': {**ACTION_SCHEMA['properties'],
            'actions': {**ACTION_SCHEMA['properties']['actions'], 'items': {}}}}
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error:
        detail = schema_detail(error, code="FINAL_SCHEMA" if final else "ACTION_SCHEMA")
        hint = (" Put tool parameters inside the call's arguments object, using that tool's "
                "public schema; skill_run_id belongs alongside name and arguments.") if not final else ""
        raise ProtocolError(detail["code"], detail["message"] + hint, detail["path"])
    if final and prefix_length:
        raise ProtocolError("PREFIXED_FINAL", "Return final as standalone JSON without leading commentary.")
    return value, prefix_length


def feedback(error):
    detail = error.detail if isinstance(error, ProtocolError) else json_error(error).detail
    if detail["code"] in {"PREFIXED_FINAL", "FINAL_SCHEMA"}:
        correction = (
            "Correct the indicated error and return one standalone final envelope.\n"
            'Final format: {"type":"final","text":"your final answer"}. '
            "Use final only to request termination of the entire task. ")
    else:
        correction = (
            "Correct the indicated error and return one complete actions envelope.\n"
            'Actions format: {"type":"actions","actions":[{"name":"TOOL_NAME","arguments":{}}]}. '
            "Replace TOOL_NAME and arguments with a listed public tool and its required inputs; "
            "submit 1 to 32 calls. The value of type is a string; actions is a separate array property. ")
    message = ("Protocol error: " + json.dumps(detail, ensure_ascii=False) +
               "\nNo actions from this response were executed. Earlier successful results persist. "
               "The public tools remain available through the same JSON actions interface. "
               + correction +
               "Do not include simulated tool results or additional turns.")
    return detail, message
