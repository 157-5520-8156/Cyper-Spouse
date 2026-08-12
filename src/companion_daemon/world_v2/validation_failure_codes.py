"""Installed technical-failure vocabulary shared by runtime and audit replay.

Only these bounded codes may cross the private CharacterInterior faculty
boundary.  They carry no provider text, model output, exception message, or
semantic character content.  Keeping the vocabulary here prevents the live
Deliberation classifier and immutable audit validator from drifting apart.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal, cast, get_args


ValidationTechnicalFailureCode = Literal[
    "source_review_timeout",
    "source_review_exception",
    "authored_subcall_timeout",
    "authored_subcall_exception",
    "role_faculty_unavailable",
    "required_tool_choice_unsupported",
    "recall_choice_reselection_invalid",
    "authored_expression_reselection_invalid",
    "proactive_claim_binding_invalid",
    "affect_target_reselection_invalid",
    "inventory_invalid",
    "coverage_invalid",
    "appraisal_reselection_invalid",
    "appraisal_reselection_unavailable",
    "appraisal_result_missing",
    "contextual_failsafe_unavailable",
    "inbound_character_author_unavailable",
    "inbound_character_turn_requires_verified_observation",
    "paired_expression_materialization_changed",
    "paired_expression_missing",
    "paired_expression_origin_changed",
    "paired_expression_reselection_invalid",
    "proactive_source_closure_reviewer_unavailable",
]

VALIDATION_TECHNICAL_FAILURE_CODES = frozenset(
    get_args(ValidationTechnicalFailureCode)
)
VALIDATION_MAIN_TIMEOUT_FAILURE_CODES = frozenset(
    {
        "source_review_timeout",
        "authored_subcall_timeout",
    }
)
VALIDATION_MAIN_EXCEPTION_FAILURE_CODES = (
    VALIDATION_TECHNICAL_FAILURE_CODES - VALIDATION_MAIN_TIMEOUT_FAILURE_CODES
)

PHYSICAL_PROVIDER_FAILURE_CODES = frozenset(
    {
        "stream_reselected",
        "stream_reselection_unresolved",
        "stream_superseded_by_newer_input",
        "stream_tail_cancelled",
        "stream_tail_unresolved",
        "stream_tail_unresolved_after_bounded_cancellation",
        "stream_provider_unresolved",
    }
)
_V55_RECORDED_PHYSICAL_VALIDATION_FAILURE_CODES = frozenset(
    {
        "source_review_timeout",
        "source_review_exception",
        "authored_subcall_timeout",
        "authored_subcall_exception",
        "role_faculty_unavailable",
        "required_tool_choice_unsupported",
        "recall_choice_reselection_invalid",
        "authored_expression_reselection_invalid",
        "proactive_claim_binding_invalid",
        "affect_target_reselection_invalid",
        "inventory_invalid",
        "coverage_invalid",
    }
)
_RECORDED_PHYSICAL_PROVIDER_FAILURE_OUTCOMES = MappingProxyType(
    {
        **{
            code: frozenset({"unresolved"})
            for code in _V55_RECORDED_PHYSICAL_VALIDATION_FAILURE_CODES
        },
        # Exact aliases emitted by the v55 candidate/stream producers. New
        # writers collapse these through the installed sanitizer instead of
        # extending this historical recorded-reader language.
        "timeout": frozenset({"unresolved"}),
        "invalid": frozenset({"unresolved"}),
        "cancelled": frozenset({"unresolved"}),
        "exception": frozenset({"unresolved"}),
        "missing_output": frozenset({"unresolved"}),
        "stream_author_identity_changed": frozenset({"unresolved"}),
        "stream_reselected": frozenset({"cancelled"}),
        "stream_reselection_unresolved": frozenset({"unresolved"}),
        "stream_superseded_by_newer_input": frozenset({"cancelled"}),
        "stream_tail_cancelled": frozenset({"cancelled"}),
        "stream_tail_unresolved": frozenset({"unresolved"}),
        "stream_tail_unresolved_after_bounded_cancellation": frozenset(
            {"unresolved"}
        ),
        # Current installed physical-terminal codes remain accepted only for
        # the outcomes their producer or fail-closed writer can emit.
        "stream_provider_unresolved": frozenset({"cancelled", "unresolved"}),
    }
)

_LEGACY_PROVIDER_EXCEPTION_TYPES = frozenset(
    {
        "APIError",
        "AuthenticationError",
        "BadRequestError",
        "CancelledError",
        "ConnectError",
        "ConnectionError",
        "HTTPStatusError",
        "InternalServerError",
        "JSONDecodeError",
        "ProviderError",
        "RateLimitError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "ValidationError",
        "ValueError",
    }
)


def sanitize_validation_technical_failure_code(
    value: object,
) -> ValidationTechnicalFailureCode | None:
    """Return only an installed content-free technical category."""

    if not isinstance(value, str) or value not in VALIDATION_TECHNICAL_FAILURE_CODES:
        return None
    return cast(ValidationTechnicalFailureCode, value)


def sanitize_provider_subcall_failure_code(
    value: object,
    *,
    outcome: str,
) -> str | None:
    """Collapse nested provider failures to an installed content-free code."""

    if outcome == "winner":
        return None
    if outcome == "timeout":
        return "provider_timeout"
    installed = sanitize_validation_technical_failure_code(value)
    if installed is not None:
        return installed
    if isinstance(value, str):
        if value.startswith("provider_http_"):
            status = value.removeprefix("provider_http_")
        elif ":http_" in value:
            status = value.rsplit(":http_", 1)[-1]
        else:
            status = ""
        if len(status) == 3 and status.isdigit() and 100 <= int(status) <= 599:
            return f"provider_http_{status}"
    return "provider_exception"


def provider_subcall_failure_code_is_content_free(
    value: object,
    *,
    outcome: str,
) -> bool:
    """Accept new normalized codes and a bounded legacy replay vocabulary."""

    if outcome == "winner":
        return value is None
    if not isinstance(value, str) or not value:
        return False
    if value == sanitize_provider_subcall_failure_code(value, outcome=outcome):
        return True
    if outcome == "timeout" and value == "caller_cancelled":
        return True
    failure_type, separator, detail = value.partition(":")
    return (
        outcome == "exception"
        and failure_type in _LEGACY_PROVIDER_EXCEPTION_TYPES
        and (
            (separator == "" and detail == "")
            or (
                separator == ":"
                and detail.startswith("http_")
                and detail.removeprefix("http_").isdigit()
                and 100 <= int(detail.removeprefix("http_")) <= 599
            )
        )
    )


def sanitize_physical_provider_failure_code(
    value: object,
    *,
    outcome: str,
) -> str | None:
    """Keep only installed physical-stream terminal categories."""

    if outcome == "completed":
        return None
    if isinstance(value, str) and value in PHYSICAL_PROVIDER_FAILURE_CODES:
        return value
    return "stream_provider_unresolved"


def physical_provider_failure_code_is_content_free(
    value: object,
    *,
    outcome: str,
) -> bool:
    """Accept only the frozen producer token/outcome pairs from recorded audits."""

    if outcome == "completed":
        return value is None
    if not isinstance(value, str):
        return False
    return outcome in _RECORDED_PHYSICAL_PROVIDER_FAILURE_OUTCOMES.get(
        value,
        frozenset(),
    )


__all__ = [
    "VALIDATION_MAIN_EXCEPTION_FAILURE_CODES",
    "VALIDATION_MAIN_TIMEOUT_FAILURE_CODES",
    "VALIDATION_TECHNICAL_FAILURE_CODES",
    "PHYSICAL_PROVIDER_FAILURE_CODES",
    "ValidationTechnicalFailureCode",
    "provider_subcall_failure_code_is_content_free",
    "physical_provider_failure_code_is_content_free",
    "sanitize_physical_provider_failure_code",
    "sanitize_provider_subcall_failure_code",
    "sanitize_validation_technical_failure_code",
]
