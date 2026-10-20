"""Describe unusable provider replies without repairing or executing their text."""

class ResponseIssue(Exception):
    """An observed response failure, not a model JSON or business error."""
    def __init__(self, code, message, *, category=None):
        # Directly constructed output feedback must satisfy the same contract
        # as response_issue(); diagnostics must never interrupt its delivery.
        category = category or ('output_limit' if code == 'OUTPUT_LIMIT' else 'provider_response')
        self.detail = dict(code=code, message=message, category=category)
        super().__init__(code)


def response_issue(text, finish, response, *, allow_native=False):
    """Inspect provider evidence without executing, repairing or retrying it."""
    message = response['choices'][0]['message']
    native = bool(message.get('tool_calls') or message.get('function_call'))
    if finish == 'content_filter' or message.get('refusal'):
        code, cause = "PROVIDER_CONTENT_FILTER", "provider_filter_marker"
        message = ("The provider marked the response as filtered or refused. This is not a JSON syntax "
                   "or output-length diagnosis. No returned actions or final were accepted. "
                   "Do not infer, reconstruct, or execute filtered content.")
    elif finish == "native_calls_not_enabled" or native and not allow_native:
        code, cause = "TEXT_ACTIONS_REQUIRED", "native_calls_not_enabled"
        message = ("The response contained native tool_calls/function_call, but this request uses "
                   "the text JSON actions interface. No native calls were executed. "
                   "Use the listed tool's schema inside one text actions envelope.")
    elif finish == "length":
        code, cause = "INCOMPLETE_RESPONSE", "output_length"
        message = ("The provider reported finish_reason=length: generation was truncated. "
                   "No partial actions or final were accepted. Submit a shorter complete response; "
                   "the reported output usage includes any reported reasoning tokens.")
    elif finish not in ({'stop', 'tool_calls'} if native and allow_native else {'stop'}):
        code, cause = "INCOMPLETE_RESPONSE", "other_finish_reason"
        message = (f"The provider reported finish_reason={finish!r}, not a completed text response. "
                   "No actions or final were accepted. This flag alone does not establish a JSON "
                   "syntax error or output-length exhaustion.")
    elif not native and (not isinstance(text, str) or not text.strip()):
        code, cause = "EMPTY_PROVIDER_CONTENT", "empty_content"
        message = ("The provider returned no non-whitespace visible text with finish_reason=stop. "
                   "There is no JSON text to diagnose. No actions or final were accepted. "
                   "The returned usage does not establish what missing content, if any, contained.")
    else:
        return
    error = ResponseIssue(code, message)
    usage = response.get("usage") or {}
    parts = usage.get("completion_tokens_details") or {}
    category = {'native_calls_not_enabled': 'interface_mismatch',
                'output_length': 'output_truncation'}.get(cause, 'provider_response')
    error.detail.update(cause=cause, category=category, response_observation={
        "finish_reason": finish, "visible_text_chars": len(text) if isinstance(text, str) else 0,
        "reported_completion_tokens": usage.get("completion_tokens"),
        "reported_reasoning_tokens": parts.get("reasoning_tokens"),
        "reported_text_tokens": parts.get("text_tokens"),
    })
    return error


def validate_actor_response(text, finish, response):
    error = response_issue(text, finish, response)
    if error:
        raise error


def rejection_feedback(error, framing):
    if isinstance(error, ResponseIssue):
        return error.detail, ('Controller response feedback: ' + error.detail['code'] + '. '
            + error.detail['message'] + ' Earlier successful results persist. '
            'This feedback is not a user request to end the task. Existing budgets and time limits still apply.')
    detail, message = framing.feedback(error)
    return dict(detail, category='model_protocol'), message
