"""Classify unusable replies without executing, repairing or retrying them."""
from collections import Counter

VERSION = 'classified_response_feedback_v1'
LEGACY_COUNT_DEFINITION = 'legacy cumulative recovery-error count under batch rules; use response_diagnostics for attribution'


class ResponseDiagnostics:
    """New observations only; inherited aggregate errors are never reclassified."""
    def __init__(self):
        self.counts = Counter()
        self.categories = Counter()
        self.last = None

    def record(self, detail, role):
        self.counts[(role, detail['code'])] += 1
        self.categories[(role, detail['category'])] += 1
        self.last = dict(role=role, **detail)

    def summary(self):
        return dict(version=VERSION, scope='current_invocation',
            by_code={role: {code: n for (r, code), n in self.counts.items() if r == role}
                     for role in ('actor', 'compact')},
            by_category={role: {kind: n for (r, kind), n in self.categories.items() if r == role}
                         for role in ('actor', 'compact')}, last_rejection=self.last)
