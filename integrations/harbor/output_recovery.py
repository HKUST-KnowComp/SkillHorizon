"""Explicit output-limit feedback; the actor owns bounded recovery counters."""
from .provider_guard import GuardError


INSTRUCTIONS = (
    'Each response has a total generation limit of 8192 tokens, including reasoning. '
    'Keep action batches small enough to return a complete envelope; prefer a separate '
    'response for each script call with large arguments. An output-limit rejection '
    'executes no actions. Use its public feedback to submit a revised response. '
    'Output-limit recovery obeys the public execution recovery limits and consumes '
    'execution time; actual tool attempts still consume B.'
)


class OutputLimitFeedback(GuardError):
    def __init__(self, raw, usage, maximum):
        super().__init__('OUTPUT_LIMIT_FEEDBACK')
        self.raw = raw
        self.usage = usage
        self.maximum = maximum

    def public_feedback(self, response, *, compact=False):
        details = (response.get('usage') or {}).get('completion_tokens_details') or {}
        reasoning = details.get('reasoning_tokens')
        information = (
            f'The provider reported {self.usage["completion_tokens"]} total output tokens '
            f'against the requested limit of {self.maximum}. '
        )
        if type(reasoning) is int and 0 <= reasoning <= self.usage['completion_tokens']:
            information += f'It reported {reasoning} of these as reasoning tokens. '
        if compact:
            return (information + 'The summary was not adopted. Produce a shorter complete '
                    'summary from the same public conversation; do not invent or omit critical state.')
        return (information + 'This response was rejected; none of its actions or final '
                'submissions were executed or accepted. The business state is unchanged. '
                'Return a revised complete response in the documented format. Reduce batch size for '
                'large arguments; prefer one large script call per response. If the visible '
                'action was already short, the reported reasoning or provider limit behavior '
                'caused the rejection, not a business tool failure. All elapsed time remains '
                'charged; no tool attempts were charged for this rejected response.')
