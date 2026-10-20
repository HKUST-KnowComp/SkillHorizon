"""Public stopping confirmation; independent of hidden business scores."""

from .declarations import NAMES as DECLARATION_NAMES

CONFIRMATION = (
    'Controller confirmation: This checks your prior final request; '
    'it is not a new user instruction to stop.\n\n'
    'Are you sure you want to end the entire task and submit the current result?\n\n'
    'Confirming will submit the current state for evaluation, after which no '
    'further actions can be taken. To confirm, submit final again. To continue '
    'working, submit actions.\n\n'
    'After any actual non-declaration tool attempt, including a rejected attempt, '
    'a later final will require a new confirmation. Existing budgets and time '
    'limits still apply.'
)


class FinalConfirmation:
    initial_instructions = (
        'Stopping requires confirmation: submit final to request an end, then final again to confirm. '
        'Every actual non-declaration tool attempt, including a rejected attempt, cancels pending '
        'confirmation. Begin/finish declarations do not cancel it, regardless of their B charge. '
        'Invalid responses and compaction do not confirm or cancel it. Budgets and time do not reset.'
    )

    def __init__(self, *, pending=False, offered=0):
        self.pending = pending
        self.offered = offered
        self.confirmed = False
        self.resumptions = 0

    def on_final(self):
        if self.pending:
            self.confirmed = True
            return True
        self.pending = True
        self.offered += 1
        return False

    def on_tool_executed(self):
        if self.pending:
            self.resumptions += 1
        self.pending = False
        self.confirmed = False

    def on_tool_attempt(self, call, charged):
        # Ending confirmation follows action semantics, not its B charge.
        if call.get('name') not in DECLARATION_NAMES:
            self.on_tool_executed()

    def summary(self):
        return dict(mode='consecutive_final', pending=self.pending,
                    confirmed=self.confirmed, confirmations_offered=self.offered,
                    resumed_after_confirmation=self.resumptions)
