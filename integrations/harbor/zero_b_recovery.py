"""Bound free actor loops without imposing a business-progress criterion."""
from copy import deepcopy
import json
from pathlib import Path

from .backend import write_record

VERSION = 'zero_b_recovery_v1'
STATE_FILE = 'ZERO_B_RECOVERY.json'
LIMITS = dict(zero_b_actor=32, warning_at=24, output_rejections=4, compact_rejections=4)


class ZeroBRecovery:
    def __init__(self, root, *, state=None, source=None):
        if state is not None and source is not None:
            raise ValueError('Choose one recovery-state source')
        if source is not None:
            path = Path(source) / STATE_FILE
            if not path.exists():
                raise ValueError('Missing zero-B checkpoint; explicit legacy migration is required')
            state = json.loads(path.read_text())
        self.path = Path(root) / STATE_FILE
        self.state = dict(version=VERSION, limits=dict(LIMITS), zero_b_actor=0,
                          peak_zero_b_actor=0, output_rejections=0, compact_rejections=0,
                          last_actor=0, last_compact=0, compact_feedback='')
        if state is not None:
            if state.get('version') != VERSION or state.get('limits') != LIMITS:
                raise ValueError('Recovery checkpoint policy differs')
            for key in self.state.keys() - {'version', 'limits', 'compact_feedback'}:
                if type(state.get(key)) is not int or state[key] < 0:
                    raise ValueError('Invalid recovery checkpoint counter')
            if not isinstance(state.get('compact_feedback'), str):
                raise ValueError('Invalid compaction recovery feedback')
            self.state = deepcopy(state)
        self.save()

    def summary(self):
        return deepcopy(self.state)

    def save(self):
        write_record(self.path, self.state)

    @staticmethod
    def instructions():
        return (
            f'Execution recovery policy {VERSION}: at most {LIMITS["zero_b_actor"]} consecutive '
            'actor responses may occur without a B-charged tool attempt. Declarations, '
            'invalid responses and an unconfirmed final do not reset this counter. '
            'Any actual B-charged attempt, including a query or rejected attempt, resets it; '
            'business success is not required. Compaction and transport retries neither '
            'increment nor reset it. Confirmed final ends the task normally. '
            f'After {LIMITS["output_rejections"]} consecutive output-limit/truncation rejections, '
            'actor output recovery stops; only an actor response within the output boundary '
            'resets that output counter. A single compaction has at most '
            f'{LIMITS["compact_rejections"]} output-limit rejections. '
            'These counters survive checkpoint continuation. No rejected output is executed '
            'or adopted as a summary. Existing B, protocol and time limits still apply.'
        )

    def actor_response(self, decision, *, output_rejected=False):
        if decision <= self.state['last_actor']:
            raise ValueError('Actor recovery boundary already settled')
        self.state['last_actor'] = decision
        self.state['zero_b_actor'] += 1
        self.state['output_rejections'] = self.state['output_rejections'] + 1 if output_rejected else 0
        self.save()

    def charged_attempt(self):
        if self.state['zero_b_actor']:
            self.state['zero_b_actor'] = 0
            self.save()

    def finish_actor(self):
        self.state['peak_zero_b_actor'] = max(self.state['peak_zero_b_actor'], self.state['zero_b_actor'])
        self.save()

    def compact_rejected(self, sequence, feedback):
        if sequence <= self.state['last_compact']:
            raise ValueError('Compaction recovery boundary already settled')
        self.state['last_compact'] = sequence
        self.state['compact_rejections'] += 1
        self.state['compact_feedback'] = feedback
        self.save()

    def compact_adopted(self):
        self.state['compact_rejections'] = 0
        self.state['compact_feedback'] = ''
        self.save()

    def stop_reason(self):
        if self.state['compact_rejections'] >= LIMITS['compact_rejections']:
            return 'compact_output_recovery_exhausted'
        if self.state['output_rejections'] >= LIMITS['output_rejections']:
            return 'actor_output_recovery_exhausted'
        if self.state['zero_b_actor'] >= LIMITS['zero_b_actor']:
            return 'zero_b_recovery_exhausted'
        return None

    def warning(self):
        if self.state['zero_b_actor'] != LIMITS['warning_at']:
            return ''
        return (f'Controller: {LIMITS["warning_at"]} consecutive actor responses have made no '
                f'B-charged tool attempt; the limit is {LIMITS["zero_b_actor"]}. '
                'Use the public interface to attempt tools or submit final to end the task. '
                'This notice does not assess business completion or suggest a business action.')
