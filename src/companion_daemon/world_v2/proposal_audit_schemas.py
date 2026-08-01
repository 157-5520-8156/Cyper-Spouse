"""Immutable Phase-4A model/proposal audit contracts.

These records are deliberation authority only.  They never authorize a domain
mutation or Action and intentionally have no dependency on Acceptance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from .proposal_envelope import ProposalInput
from .recall_audit import (
    CharacterRecallRequest,
    PrefetchPresentationAudit,
    RecallAuditTrace,
)
from .recall_index import RecallCursor
from .schema_core import FrozenModel


_HASH = r"^[0-9a-f]{64}$"
_PROPOSAL_HASH = r"^sha256:[0-9a-f]{64}$"
_MAX_PROPOSAL_BYTES = 262_144
# audit.5 can retain up to four bounded prefetch presentation traces.  The
# immutable bytes are the source proof for what each role-author call actually
# saw, so truncating them would be less safe than using a larger explicit cap.
_MAX_AUDIT_BYTES = 262_144
_PROPOSAL_ADAPTER = TypeAdapter(ProposalInput)


class RecordedModelRoute(FrozenModel):
    tier: Literal["flash", "thinking"]
    reason_code: str = Field(min_length=1, max_length=128)
    router_version: str = Field(min_length=1, max_length=128)


class RecordedModelUsage(FrozenModel):
    """Provider-attested metering bound to one recorded model result.

    This deliberately lives in the immutable model-result audit rather than a
    mutable metrics table.  A replay can therefore distinguish an old audit
    with no metering authority from a provider-reported call without relying
    on deployment configuration at read time.
    """

    usage_contract: Literal["model-usage.1"] = "model-usage.1"
    route_class: Literal[
        "chat", "expressive", "world_action", "deep_deliberation", "quick_recovery"
    ]
    input_tokens: int = Field(ge=0, le=10_000_000)
    output_tokens: int = Field(ge=0, le=10_000_000)
    thinking_tokens: int = Field(ge=0, le=10_000_000)
    token_provenance: Literal["provider_reported", "offline_estimated"]
    transport: Literal["provider_api", "offline_fixture"]
    provider: str = Field(min_length=1, max_length=128)
    provider_usage_ref: str = Field(min_length=1, max_length=256)
    provider_usage_hash: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def provider_usage_hash_binds_metering_fields(self) -> Self:
        material = self.model_dump(mode="json", exclude={"provider_usage_hash"})
        if self.provider_usage_hash != sha256(canonical_json(material)):
            raise ValueError("provider usage hash is not bound to metering fields")
        return self


class RecordedModelDecisionContext(FrozenModel):
    """Exact decision subject and ledger prefix used by one model attempt chain."""

    context_contract: Literal["model-decision-context.1"] = "model-decision-context.1"
    decision_subject_hash: str = Field(pattern=_HASH)
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)


class RecordedModelResponseStorage(FrozenModel):
    """Bound one model response to bounded internal diagnostic persistence."""

    storage_contract: Literal["model-response-storage.1"] = "model-response-storage.1"
    content_kind: Literal["raw_model_result"] = "raw_model_result"
    disposition: Literal[
        "stored_exact",
        "omitted_oversize",
        "store_unavailable",
    ]
    original_response_hash: str = Field(pattern=_HASH)
    original_utf8_bytes: int = Field(ge=0, le=1_000_000_000)
    original_characters: int = Field(ge=0, le=1_000_000_000)
    truncated: bool
    content_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        exclude_if=lambda value: value is None,
    )
    content_payload_hash: str | None = Field(
        default=None,
        pattern=_HASH,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def storage_disposition_is_complete(self) -> Self:
        stored = self.disposition == "stored_exact"
        has_binding = self.content_ref is not None and self.content_payload_hash is not None
        if stored != has_binding:
            raise ValueError("model response storage binding is incomplete")
        if stored:
            if self.truncated:
                raise ValueError("exact model response storage cannot be truncated")
            if self.content_payload_hash != self.original_response_hash:
                raise ValueError("exact model response storage changed the response bytes")
        elif not self.truncated:
            raise ValueError("omitted model response storage must be marked truncated")
        return self


class RecordedModelResultAudit(FrozenModel):
    model_call_id: str = Field(min_length=1, max_length=256)
    parent_model_call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        exclude_if=lambda value: value is None,
    )
    semantic_stream_part: Literal["head", "tail"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    usage_status: Literal["provider_reported", "unresolved", "cancelled"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    semantic_model_call_ids: tuple[str, ...] = Field(
        default=(),
        max_length=2,
        exclude_if=lambda value: not value,
    )
    model_result_ref: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    route: RecordedModelRoute
    model_id: str | None = Field(default=None, max_length=256)
    model_version: str | None = Field(default=None, max_length=256)
    attempted_model_id: str | None = Field(
        default=None,
        max_length=256,
        exclude_if=lambda value: value is None,
    )
    attempted_model_version: str | None = Field(
        default=None,
        max_length=256,
        exclude_if=lambda value: value is None,
    )
    request_hash: str = Field(pattern=_HASH)
    response_hash: str | None = Field(default=None, pattern=_HASH)
    decision_context: RecordedModelDecisionContext | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    response_storage: RecordedModelResponseStorage | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    status: Literal[
        "proposal_validated",
        "candidate_returned",
        "main_timeout",
        "main_invalid",
        "main_exception",
        "main_timeout_recovered",
        "main_invalid_recovered",
        "main_exception_recovered",
        "recovery_failed",
        "provider_completed",
        "provider_cancelled",
        "provider_unresolved",
    ]
    failure_code: str | None = Field(default=None, max_length=64)
    slot: Literal["primary", "backup", "corrective"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    outcome: (
        Literal[
            "winner",
            "returned",
            "invalid",
            "timeout",
            "exception",
            "hedge_cancelled",
            "hedge_lost",
            "budget_exhausted",
            "completed",
            "cancelled",
            "unresolved",
        ]
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    input_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    usage: RecordedModelUsage | None = Field(default=None, exclude_if=lambda value: value is None)
    recall_trace: RecallAuditTrace | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    prefetch_trace: RecallAuditTrace | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    presented_prefetch_traces: tuple[PrefetchPresentationAudit, ...] = Field(
        default=(),
        max_length=4,
        exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def output_and_failure_are_consistent(self) -> Self:
        if self.parent_model_call_id == self.model_call_id:
            raise ValueError("model result cannot be its own provider parent")
        if self.semantic_stream_part is not None and self.parent_model_call_id is None:
            raise ValueError("stream semantic audit requires its physical provider parent")
        physical_status = self.status.startswith("provider_")
        if physical_status != bool(self.semantic_model_call_ids):
            raise ValueError("physical provider terminal must bind its semantic lineage")
        if physical_status:
            if self.parent_model_call_id is not None or self.semantic_stream_part is not None:
                raise ValueError("physical provider terminal cannot have a provider parent")
            if self.model_call_id in self.semantic_model_call_ids or len(
                set(self.semantic_model_call_ids)
            ) != len(self.semantic_model_call_ids):
                raise ValueError("physical provider semantic lineage is invalid")
            expected_outcome = {
                "provider_completed": "completed",
                "provider_cancelled": "cancelled",
                "provider_unresolved": "unresolved",
            }[self.status]
            if self.outcome != expected_outcome or self.slot != "primary":
                raise ValueError("physical provider terminal has an invalid outcome")
            if (self.usage_status == "provider_reported") != (self.usage is not None):
                raise ValueError("physical provider usage status is not truthful")
        elif self.usage_status is not None:
            raise ValueError("usage status is reserved for physical provider terminals")
        encoded = canonical_json(
            {"model_call_id": self.model_call_id, "response_hash": self.response_hash}
        )
        expected_ref = f"model-result:{sha256(encoded)}"
        if self.model_result_ref != expected_ref:
            raise ValueError("model result ref is not bound to its call")
        identity = (self.model_id, self.model_version, self.response_hash)
        has_output = all(value is not None for value in identity)
        if not has_output and any(value is not None for value in identity):
            raise ValueError("model output audit identity is partial")
        if self.response_storage is not None and (
            not has_output or self.response_storage.original_response_hash != self.response_hash
        ):
            raise ValueError("model response storage changed the audited response")
        attempted_identity = (self.attempted_model_id, self.attempted_model_version)
        has_attempted_identity = all(value is not None for value in attempted_identity)
        if not has_attempted_identity and any(value is not None for value in attempted_identity):
            raise ValueError("attempted model audit identity is partial")
        if (
            not has_output
            and not has_attempted_identity
            and (self.input_tokens is not None or self.output_tokens is not None)
        ):
            raise ValueError("model token counts require an output or attempted identity")
        if self.recall_trace is not None and not has_output:
            raise ValueError("recall trace requires a model output identity")
        if self.prefetch_trace is not None and not has_output:
            raise ValueError("prefetch trace requires a model output identity")
        if self.presented_prefetch_traces and not has_output:
            raise ValueError("prefetch presentations require a model output identity")
        if self.prefetch_trace is not None and self.presented_prefetch_traces:
            raise ValueError("ordered prefetch presentations supersede the legacy singular trace")
        if self.usage is not None:
            if not has_output and not has_attempted_identity:
                raise ValueError("model usage requires an output or attempted identity")
            if (self.input_tokens, self.output_tokens) != (
                self.usage.input_tokens,
                self.usage.output_tokens,
            ):
                raise ValueError("model usage tokens do not match audit tokens")
            # A recorded route is the requested role-author tier; provider
            # usage is observed evidence and can include hidden reasoning from
            # a recovery or source-review subcall. Replay preserves the real
            # token count instead of rejecting the successful result.
        if physical_status:
            if self.status == "provider_completed":
                if not has_output or self.failure_code is not None:
                    raise ValueError("completed physical provider audit lacks response identity")
            elif has_output or not has_attempted_identity or self.failure_code is None:
                raise ValueError("incomplete physical provider audit has invalid identity")
            return self
        required = {
            "main_timeout": {
                "main_timeout",
                "primary_timeout",
                "corrective_timeout",
                "source_review_timeout",
                "authored_subcall_timeout",
            },
            "main_invalid": {
                "main_invalid_output",
                "primary_invalid",
                "corrective_invalid",
            },
            "main_exception": {
                "main_exception",
                "primary_exception",
                "source_review_exception",
                "authored_subcall_exception",
                "recall_choice_reselection_invalid",
                "authored_expression_reselection_invalid",
                "affect_target_reselection_invalid",
                "recall_exception",
                "inventory_invalid",
                "coverage_invalid",
                "stream_superseded_by_newer_input",
                "stream_tail_cancelled",
                "stream_tail_unresolved",
            },
            "main_timeout_recovered": {
                "main_timeout",
                "primary_timeout",
                "corrective_timeout",
                "authored_subcall_timeout",
            },
            "main_invalid_recovered": {
                "main_invalid_output",
                "primary_invalid",
                "corrective_invalid",
            },
            "main_exception_recovered": {
                "main_exception",
                "primary_exception",
                "authored_subcall_exception",
            },
        }.get(self.status)
        provider_failure_type, separator, provider_failure_detail = (
            (self.failure_code or "").partition(":")
        )
        typed_provider_subcall_failure = (
            self.route.router_version == "provider-subcall-audit.1"
            and self.outcome in {"timeout", "exception"}
            and (
                (
                    self.outcome == "timeout"
                    and self.failure_code in {"provider_timeout", "caller_cancelled"}
                )
                or (
                    self.outcome == "exception"
                    and provider_failure_type.replace("_", "").isalnum()
                    and (
                        (separator == "" and provider_failure_detail == "")
                        or (
                            separator == ":"
                            and provider_failure_detail.startswith("http_")
                            and provider_failure_detail.removeprefix("http_").isdigit()
                            and 100
                            <= int(provider_failure_detail.removeprefix("http_"))
                            <= 599
                        )
                    )
                )
            )
        )
        if self.status == "proposal_validated":
            if not has_output or self.failure_code is not None:
                raise ValueError("validated audit requires output and no failure")
        elif self.status == "candidate_returned":
            if (
                not has_output
                or self.failure_code is not None
                or self.outcome != "returned"
            ):
                raise ValueError(
                    "returned candidate audit requires output without semantic acceptance"
                )
        elif self.status in {"main_timeout", "main_exception"}:
            if has_output or (
                self.failure_code not in (required or set())
                and not typed_provider_subcall_failure
            ):
                raise ValueError("terminal main audit has invalid lineage")
        elif self.status == "main_invalid":
            if self.failure_code not in (required or set()):
                raise ValueError("invalid main audit has invalid lineage")
        elif self.status == "recovery_failed":
            if not (
                (self.failure_code or "").startswith("quick_")
                or (self.failure_code or "").startswith("backup_")
                or (self.failure_code or "").startswith("corrective_")
            ):
                raise ValueError("failed recovery audit has invalid lineage")
        elif not has_output or self.failure_code not in (required or set()):
            raise ValueError("recovered audit has invalid lineage")
        if (self.slot is None) != (self.outcome is None):
            raise ValueError("slot and outcome audit metadata must appear together")
        return self


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def model_audit_json(audit: RecordedModelResultAudit) -> str:
    """Canonical bytes while preserving v1 audit bytes exactly.

    ``usage`` was added after audit.1.  Omitting it when absent keeps old
    ledger events replayable; new metered records contain it and are bound by
    the same audit hash.
    """

    payload = audit.model_dump(mode="json")
    if audit.usage is None:
        payload.pop("usage", None)
    if audit.slot is None:
        payload.pop("slot", None)
        payload.pop("outcome", None)
    return canonical_json(payload)


class LifeDevelopmentRecallResultRecordedPayload(FrozenModel):
    """Durable read-only result between Character recall and final choice."""

    result_contract: Literal["life-development-character-recall-result.1"] = (
        "life-development-character-recall-result.1"
    )
    result_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    trigger_ref: str = Field(min_length=1, max_length=512)
    evaluated_world_revision: int = Field(ge=0)
    decision_subject_hash: str = Field(pattern=_HASH)
    context_cursor: RecallCursor
    request_model_result_event_ref: str = Field(min_length=1, max_length=512)
    request_model_result_event_hash: str = Field(pattern=_HASH)
    request_model_result_ref: str = Field(min_length=1, max_length=256)
    request_deliberation_result_id: str = Field(min_length=1, max_length=256)
    request_response_hash: str = Field(pattern=_HASH)
    recall_request: CharacterRecallRequest
    recall_request_hash: str = Field(pattern=_HASH)
    status: Literal["returned", "technical_failure"]
    recall_trace: RecallAuditTrace | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    failure_code: Literal[
        "recall_timeout",
        "recall_exception",
        "recall_context_unavailable",
    ] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def result_is_request_and_context_bound(self) -> Self:
        expected_request_hash = sha256(
            canonical_json(self.recall_request.model_dump(mode="json"))
        )
        if self.recall_request_hash != expected_request_hash:
            raise ValueError("life recall result changed the Character request")
        expected_result_id = "life-recall-result:" + sha256(
            canonical_json(
                {
                    "proposal_id": self.proposal_id,
                    "request_model_result_ref": self.request_model_result_ref,
                    "recall_request_hash": self.recall_request_hash,
                    "trigger_ref": self.trigger_ref,
                }
            )
        )
        if self.result_id != expected_result_id:
            raise ValueError("life recall result identity is not deterministic")
        if self.context_cursor.world_revision != self.evaluated_world_revision:
            raise ValueError("life recall result changed its evaluated World revision")
        if self.status == "returned":
            trace = self.recall_trace
            if trace is None or self.failure_code is not None:
                raise ValueError("successful life recall result lacks its replay trace")
            evaluated = trace.evaluated_cursor or trace.index_cursor
            if (
                trace.mode != "character_pull"
                or trace.reuse_contract != "same_context"
                or trace.trigger_ref != self.trigger_ref
                or trace.request != self.recall_request
                or trace.index_cursor != self.context_cursor
                or evaluated != self.context_cursor
            ):
                raise ValueError("life recall trace changed request or pinned Context")
        elif self.recall_trace is not None or self.failure_code is None:
            raise ValueError("failed life recall result lacks its technical cause")
        return self


class ModelResultRecordedPayload(FrozenModel):
    audit_contract: Literal[
        "model-result-audit.1",
        "model-result-audit.2",
        "model-result-audit.3",
        "model-result-audit.4",
        "model-result-audit.5",
        "model-result-audit.6",
    ] = "model-result-audit.1"
    model_result_ref: str = Field(min_length=1, max_length=256)
    deliberation_result_id: str = Field(min_length=1, max_length=256)
    proposal_hash: str | None = Field(default=None, pattern=_PROPOSAL_HASH)
    model_call_id: str = Field(min_length=1, max_length=256)
    parent_model_call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        exclude_if=lambda value: value is None,
    )
    attempt_id: str = Field(min_length=1, max_length=256)
    capsule_id: str = Field(pattern=_HASH)
    trigger_ref: str = Field(min_length=1, max_length=512)
    evaluated_world_revision: int = Field(ge=0)
    attempt_index: int = Field(ge=0, le=1)
    attempt_count: int = Field(ge=1, le=2)
    audit_json: str = Field(min_length=2, max_length=_MAX_AUDIT_BYTES)
    audit_hash: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def audit_bytes_are_canonical_and_bound(self) -> Self:
        if self.attempt_index >= self.attempt_count:
            raise ValueError("model attempt index is out of bounds")
        if len(self.audit_json.encode("utf-8")) > _MAX_AUDIT_BYTES:
            raise ValueError("model result audit exceeds byte limit")
        audit = RecordedModelResultAudit.model_validate_json(self.audit_json)
        canonical = model_audit_json(audit)
        if canonical != self.audit_json or sha256(canonical) != self.audit_hash:
            raise ValueError("model result audit bytes/hash are not canonical")
        if (
            audit.model_call_id != self.model_call_id
            or audit.parent_model_call_id != self.parent_model_call_id
            or audit.model_result_ref != self.model_result_ref
            or audit.attempt_id != self.attempt_id
        ):
            raise ValueError("model result lineage does not match its audit bytes")
        if self.audit_contract == "model-result-audit.2" and audit.usage is None:
            raise ValueError("metered model result requires usage provenance")
        if self.audit_contract == "model-result-audit.1" and audit.usage is not None:
            raise ValueError("usage provenance requires model-result-audit.2")
        if self.audit_contract == "model-result-audit.3" and audit.slot is None:
            raise ValueError("hedged model result requires slot outcome metadata")
        if (
            self.audit_contract
            not in {
                "model-result-audit.3",
                "model-result-audit.4",
                "model-result-audit.5",
                "model-result-audit.6",
            }
            and audit.slot is not None
        ):
            raise ValueError("slot outcome metadata requires model-result-audit.3")
        has_recall_audit = (
            audit.recall_trace is not None
            or audit.prefetch_trace is not None
            or bool(audit.presented_prefetch_traces)
        )
        if (
            self.audit_contract
            in {
                "model-result-audit.4",
                "model-result-audit.5",
            }
            and not has_recall_audit
        ):
            raise ValueError("recall model result requires a replay trace")
        if (
            self.audit_contract
            not in {
                "model-result-audit.4",
                "model-result-audit.5",
                "model-result-audit.6",
            }
            and has_recall_audit
        ):
            raise ValueError("recall trace requires model-result-audit.4")
        if self.audit_contract != "model-result-audit.6" and (
            (self.audit_contract == "model-result-audit.5")
            != bool(audit.presented_prefetch_traces)
        ):
            raise ValueError("prefetch presentation sequence requires model-result-audit.5")
        is_stream_audit = (
            audit.semantic_stream_part is not None
            or audit.status.startswith("provider_")
        )
        if (self.audit_contract == "model-result-audit.6") != is_stream_audit:
            raise ValueError("stream lineage requires model-result-audit.6")
        if (
            audit.route.router_version == "life-development-router.2"
            and audit.recall_trace is not None
        ):
            decision_context = audit.decision_context
            trace = audit.recall_trace
            expected_cursor = (
                RecallCursor(
                    world_revision=decision_context.world_revision,
                    deliberation_revision=decision_context.deliberation_revision,
                    ledger_sequence=decision_context.ledger_sequence,
                )
                if decision_context is not None
                else None
            )
            evaluated_cursor = trace.evaluated_cursor or trace.index_cursor
            if (
                audit.route.reason_code != "life_development.character_model"
                or decision_context is None
                or self.evaluated_world_revision != decision_context.world_revision
                or trace.mode != "character_pull"
                or trace.reuse_contract != "same_context"
                or trace.trigger_ref != self.trigger_ref
                or trace.index_cursor != expected_cursor
                or evaluated_cursor != expected_cursor
            ):
                raise ValueError(
                    "life recall trace does not match its outer trigger and cursor"
                )
        return self


def validate_recorded_attempt_lineage(
    audits: tuple[RecordedModelResultAudit, ...],
    *,
    capsule_id: str,
    proposal_hash: str | None,
    deliberation_result_id: str,
) -> None:
    if not 1 <= len(audits) <= 2:
        raise ValueError("model attempt audit count is out of bounds")
    if len({audit.model_call_id for audit in audits}) != len(audits):
        raise ValueError("model attempts require distinct call identities")
    provider_subcall = (
        len(audits) == 1
        and audits[0].route.router_version == "provider-subcall-audit.1"
    )
    authored_candidate = (
        len(audits) == 1
        and audits[0].route.router_version == "authored-candidate-audit.1"
    )
    physical_provider = (
        len(audits) == 1
        and audits[0].route.router_version == "physical-provider-audit.1"
    )
    if len(audits) == 1:
        if physical_provider:
            terminal = audits[0]
            if proposal_hash is not None or terminal.status not in {
                "provider_completed",
                "provider_cancelled",
                "provider_unresolved",
            }:
                raise ValueError("physical provider terminal cannot claim a proposal")
        elif provider_subcall:
            if proposal_hash is not None:
                raise ValueError("provider subcall cannot claim a proposal")
            if audits[0].status == "proposal_validated":
                if audits[0].outcome != "winner":
                    raise ValueError("successful provider subcall lacks a winner")
            elif audits[0].outcome not in {"timeout", "exception"}:
                raise ValueError("failed provider subcall lacks a terminal outcome")
        elif authored_candidate and audits[0].status == "candidate_returned":
            if proposal_hash is not None or audits[0].outcome != "returned":
                raise ValueError("unresolved authored candidate cannot claim acceptance")
        elif proposal_hash is None:
            if (
                audits[0].semantic_stream_part == "tail"
                and audits[0].status == "candidate_returned"
                and audits[0].outcome == "returned"
            ):
                pass
            elif audits[0].outcome not in {
                "invalid",
                "timeout",
                "exception",
                "budget_exhausted",
            }:
                raise ValueError("single failed attempt lacks a terminal outcome")
        elif audits[0].status != "proposal_validated":
            raise ValueError("single successful attempt must validate a proposal")
    else:
        main, quick = audits
        expected: tuple[set[str], str] | None = None
        character_recall_followup = (
            main.status == "candidate_returned"
            and main.recall_trace is not None
            and main.slot == "primary"
            and main.outcome == "returned"
        )
        if character_recall_followup:
            if main.attempt_id != quick.attempt_id or main.route != quick.route:
                raise ValueError("character recall lineage changed identity or route")
            if proposal_hash is not None:
                if (
                    quick.status != "proposal_validated"
                    or quick.failure_code is not None
                    or quick.slot is not None
                    or quick.outcome is not None
                ):
                    raise ValueError("character recall follow-up did not validate its proposal")
            elif (
                quick.status != "recovery_failed"
                or not (quick.failure_code or "").startswith("corrective_")
                or quick.slot != "corrective"
                or quick.outcome not in {"invalid", "timeout", "exception"}
            ):
                raise ValueError("failed character recall follow-up is not terminal")
        primary_won_race = (
            proposal_hash is not None
            and quick.status == "proposal_validated"
            and main.status == "recovery_failed"
            and main.failure_code in {"backup_cancelled", "backup_lost"}
        )
        if character_recall_followup:
            pass
        elif primary_won_race:
            expected = None
        else:
            expected = {
                "main_timeout": (
                    {
                        "main_timeout",
                        "primary_timeout",
                        "corrective_timeout",
                        "authored_subcall_timeout",
                    },
                    "main_timeout_recovered",
                ),
                "main_invalid": (
                    {"main_invalid_output", "primary_invalid", "corrective_invalid"},
                    "main_invalid_recovered",
                ),
                "main_exception": (
                    {
                        "main_exception",
                        "primary_exception",
                        "authored_subcall_exception",
                    },
                    "main_exception_recovered",
                ),
            }.get(main.status)
        if (
            not character_recall_followup
            and not primary_won_race
            and (expected is None or main.failure_code not in expected[0])
        ):
            raise ValueError("recovery lineage has an invalid main audit")
        if character_recall_followup:
            pass
        elif primary_won_race:
            pass
        elif quick.status == "recovery_failed":
            if proposal_hash is not None or not (quick.failure_code or "").startswith("quick_"):
                if not (
                    (quick.failure_code or "").startswith("backup_")
                    or (quick.failure_code or "").startswith("corrective_")
                ):
                    raise ValueError("failed recovery cannot claim a proposal")
        elif (
            quick.status != expected[1]
            or quick.failure_code != main.failure_code
            or proposal_hash is None
        ):
            raise ValueError("successful recovery lineage is invalid")
        if (
            not character_recall_followup
            and (main.attempt_id != quick.attempt_id or main.route != quick.route)
        ):
            raise ValueError("model attempt lineage changed identity or route")
    identity = {
        "capsule_id": capsule_id,
        "proposal_hash": proposal_hash,
        "attempt_audits": [json.loads(model_audit_json(audit)) for audit in audits],
    }
    if deliberation_result_id != f"deliberation:{sha256(canonical_json(identity))}":
        raise ValueError("deliberation result identity is invalid")


class ProposalRecordedV2Payload(FrozenModel):
    audit_contract: Literal["proposal-envelope-audit.1"] = "proposal-envelope-audit.1"
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_kind: Literal["decision", "continuation", "minimal"]
    model_result_ref: str = Field(min_length=1, max_length=256)
    deliberation_result_id: str = Field(min_length=1, max_length=256)
    model_call_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    capsule_id: str = Field(pattern=_HASH)
    trigger_ref: str = Field(min_length=1, max_length=512)
    evaluated_world_revision: int = Field(ge=0)
    proposal_json: str = Field(min_length=2, max_length=_MAX_PROPOSAL_BYTES)
    proposal_hash: str = Field(pattern=_PROPOSAL_HASH)

    @model_validator(mode="after")
    def proposal_bytes_are_canonical_and_bound(self) -> Self:
        if len(self.proposal_json.encode("utf-8")) > _MAX_PROPOSAL_BYTES:
            raise ValueError("proposal audit exceeds byte limit")
        try:
            proposal = _PROPOSAL_ADAPTER.validate_json(self.proposal_json, strict=True)
        except (ValueError, RecursionError) as exc:
            raise ValueError("proposal audit must contain a valid ProposalEnvelope") from exc
        canonical = canonical_json(proposal.model_dump(mode="json"))
        if canonical != self.proposal_json or proposal.proposal_hash != self.proposal_hash:
            raise ValueError("proposal audit bytes/hash are not canonical")
        if (
            proposal.proposal_id != self.proposal_id
            or proposal.proposal_kind != self.proposal_kind
            or proposal.trigger_ref != self.trigger_ref
            or proposal.evaluated_world_revision != self.evaluated_world_revision
        ):
            raise ValueError("proposal audit lineage does not match its envelope")
        if (
            proposal.proposal_kind == "minimal"
            and proposal.source_model_result != self.model_result_ref
        ):
            raise ValueError("minimal proposal is not bound to the final model result")
        return self


class ModelResultAuditProjection(ModelResultRecordedPayload):
    event_ref: str = Field(min_length=1)
    event_payload_hash: str = Field(pattern=_HASH)


class ProposalAuditProjection(ProposalRecordedV2Payload):
    event_ref: str = Field(min_length=1)
    event_payload_hash: str = Field(pattern=_HASH)


__all__ = [
    "LifeDevelopmentRecallResultRecordedPayload",
    "ModelResultAuditProjection",
    "RecordedModelUsage",
    "ModelResultRecordedPayload",
    "ProposalAuditProjection",
    "ProposalRecordedV2Payload",
    "canonical_json",
    "model_audit_json",
    "sha256",
    "validate_recorded_attempt_lineage",
]
