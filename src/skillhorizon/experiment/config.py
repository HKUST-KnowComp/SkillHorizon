from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RunConfig:
    model: str = "gpt-5.5"
    reasoning_effort: str = "low"
    context_window: int = 480000
    context_policy: str = "summary"
    compact_at: int = 200000
    compact_keep_tokens: int = 20000
    compact_summary_tokens: int = 12000
    compact_max_output_tokens: int | None = None
    compact_reasoning_effort: str | None = None
    max_compactions: int = 32
    max_output_tokens: int = 12288
    max_turns: int | None = None
    max_tool_calls: int | None = None
    parallel_tool_calls: bool = False
    max_provider_requests: int | None = None
    max_infrastructure_failures: int | None = None
    max_total_tokens: int | None = None
    max_no_call_turns: int = 3
    max_identical_failures: int = 8
    retries: int = 0
    timeout_seconds: int = 600
    provider_policy: str = "strict"
    provider_instruction_template: str | None = None
    provider_contract_retries: int = 3
    provider_contract_retry_scope: str = "settings_and_attribution"
    retry_unrequested_linkage: bool = False
    prompt_rejection_retries: int = 3
    rate_limit_retries: int = 0
    transport_retries: int = 0
    canonical_history: bool = True

    def __post_init__(self) -> None:
        if self.context_policy not in {"raw", "summary"}:
            raise ValueError("Unknown context policy")
        for name in ("compact_at", "compact_keep_tokens", "compact_summary_tokens", "max_compactions"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.context_policy == "summary" and not (
            self.compact_keep_tokens + self.compact_summary_tokens < self.compact_at < self.max_input_tokens
        ):
            raise ValueError("Summary plus recent history must fit below compact_at, below the input limit")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be non-empty")
        if self.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("Unknown reasoning effort")
        if self.compact_reasoning_effort is not None and (
            not isinstance(self.compact_reasoning_effort, str) or self.compact_reasoning_effort not in {
                "none", "minimal", "low", "medium", "high", "xhigh"
            }
        ):
            raise ValueError("Unknown compaction reasoning effort")
        validate_provider_policy(self.provider_policy, self.provider_instruction_template)
        if type(self.provider_contract_retries) is not int or not 0 <= self.provider_contract_retries <= 3:
            raise ValueError("provider_contract_retries must be an integer from 0 to 3")
        if self.provider_contract_retry_scope not in {"configuration_only", "settings_and_attribution",
                                                       "settings_attribution_and_output_limit"}:
            raise ValueError("Unknown provider contract retry scope")
        if type(self.retry_unrequested_linkage) is not bool:
            raise ValueError("retry_unrequested_linkage must be boolean")
        if type(self.prompt_rejection_retries) is not int or not 0 <= self.prompt_rejection_retries <= 3:
            raise ValueError("prompt_rejection_retries must be an integer from 0 to 3")
        if type(self.rate_limit_retries) is not int or not 0 <= self.rate_limit_retries <= 3:
            raise ValueError("rate_limit_retries must be an integer from 0 to 3")
        if type(self.transport_retries) is not int or not 0 <= self.transport_retries <= 3:
            raise ValueError("transport_retries must be an integer from 0 to 3")
        if type(self.canonical_history) is not bool:
            raise ValueError("canonical_history must be boolean")
        if type(self.parallel_tool_calls) is not bool:
            raise ValueError("parallel_tool_calls must be boolean")
        if self.provider_policy == "openlux_declared_image_serial" and self.parallel_tool_calls:
            raise ValueError("The declared-image serial policy requires parallel_tool_calls=false")
        if self.provider_policy == "openlux_declared_image_ordered_batch" and not self.parallel_tool_calls:
            raise ValueError("The declared-image ordered-batch policy requires parallel_tool_calls=true")
        for key in (
            "context_window",
            "max_output_tokens",
            "max_no_call_turns",
            "max_identical_failures",
            "timeout_seconds",
        ):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if not self.max_output_tokens < self.context_window <= 524288:
            raise ValueError("Output reserve must fit within a window <= 524288")
        if self.compact_max_output_tokens is not None:
            if (type(self.compact_max_output_tokens) is not int
                    or not self.compact_summary_tokens <= self.compact_max_output_tokens < self.context_window):
                raise ValueError("Compaction output reserve must cover its text budget and fit within the window")
            if (self.context_policy == "summary"
                    and self.compact_at >= self.context_window - self.compact_max_output_tokens):
                raise ValueError("Compaction trigger must fit below its independent input limit")
        for key in ("max_turns", "max_tool_calls", "max_provider_requests", "max_total_tokens", "max_infrastructure_failures"):
            value = getattr(self, key)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{key} must be a positive integer or null")
        if type(self.retries) is not int or self.retries != 0:
            raise ValueError("Hidden HTTP retries are disabled; use the bounded recorded retry policies")

    @property
    def max_input_tokens(self) -> int:
        return self.context_window - self.max_output_tokens

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.provider_instruction_template is None:
            del result["provider_instruction_template"]
        if self.compact_reasoning_effort is None:
            del result["compact_reasoning_effort"]
        if self.compact_max_output_tokens is None:
            del result["compact_max_output_tokens"]
        return result

    @classmethod
    def from_record(cls, value):
        # Old recordings explicitly remain raw; never silently enable a new policy.
        return cls(**{"context_policy": "raw", **value})


def validate_provider_policy(policy, template):
    route_policies = {"openlux_declared_image_serial", "openlux_declared_image_ordered_batch"}
    if policy not in {"strict", "observed_serial", "fixed_instruction_template", "allowlisted_instruction_template", *route_policies}:
        raise ValueError("Unknown provider policy")
    if policy in route_policies and template is None:
        return
    if policy in {"fixed_instruction_template", "allowlisted_instruction_template", *route_policies}:
        if not isinstance(template, str) or not template.strip():
            raise ValueError("Fixed instruction policy requires a frozen nonempty template")
    elif template is not None:
        raise ValueError("Instruction template requires the fixed instruction policy")


def validate_generation_settings(model: str, effort: str) -> None:
    if model == "gpt-5.5" or model.startswith("gpt-5.5-202"):
        if effort not in {"none", "low", "medium", "high", "xhigh"}:
            raise ValueError("gpt-5.5 does not support reasoning.effort=minimal; choose an explicit supported effort")


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Provider URL must be absolute and contain no credentials or query"
        )
    return value.rstrip("/")
