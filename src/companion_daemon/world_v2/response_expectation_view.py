"""Deterministic, model-safe view of one pending declared response expectation.

When she speaks, the expression contract may freeze a model-declared
``ResponseExpectationAuthority`` ("I hope they come back and tell me how it
went").  Until now that authority only drove behaviour (the response-gap
follow-up lane).  This module gives the feeling lanes the same committed
fact: being left waiting after asking for comfort and being left waiting
after an idle remark are different experiences, and the appraisal model can
only weigh that difference if it knows what she hoped for.

Everything here is a pure read over one pinned projection.  The exported
view carries only semantic values (hoped response, coarse pressure and
importance, how long she has been waiting) and never IDs, hashes or
authority references; the advisory helper binds the committed sources in
the ordinary Inner-Advisory envelope instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Literal

from pydantic import Field

from .context_capsule import InnerAdvisoryCandidate, InnerAdvisoryProjection
from .schema_core import FrozenModel


RESPONSE_EXPECTATION_ADVISORY_VERSION = "response-expectation-view.1"

# Mirrors the silence-anchor discipline: a receipt in these states means she
# visibly said it from her own point of view.  Behavioural follow-up applies
# the stricter ``delivery_requirement`` gate; the feeling side only needs
# "the invitation left her hands".
_ANSWERABLE_RECEIPT_STATES = frozenset({"provider_accepted", "delivered"})

ExpectationTier = Literal["low", "medium", "high"]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _tier(basis_points: int) -> ExpectationTier:
    # Presentation binning only: the model receives a coarse strength label
    # instead of raw basis points, so prose material stays human-shaped.
    if basis_points <= 3_333:
        return "low"
    if basis_points <= 6_666:
        return "medium"
    return "high"


class PendingResponseExpectationView(FrozenModel):
    """What she hoped for, in semantic values only: no IDs, hashes or refs."""

    hoped_response: str = Field(min_length=1, max_length=128)
    pressure: ExpectationTier
    importance: ExpectationTier
    # Seconds since the inviting expression was visibly delivered.  Delivery
    # is the declaration instant a projection can testify to; the freeze-time
    # wait/expiry offsets are not separately recoverable from the authority.
    declared_seconds_ago: int = Field(ge=0)


def pending_response_expectation(
    projection,
    *,
    anchor_event_ref: str | None = None,
    before_world_revision: int | None = None,
) -> PendingResponseExpectationView | None:
    """Resolve the pending declared expectation from committed projection state.

    With ``anchor_event_ref`` (a committed ``ExecutionReceiptRecorded`` event
    of her own visible message, e.g. the silence anchor) the chain is exact:
    receipt → action → manifest beat → that manifest's frozen expectation.

    Without an anchor, the most recently delivered, still-unexpired
    expectation wins; ``before_world_revision`` optionally restricts the
    search to expectations delivered strictly before that revision, so an
    inbound message is never explained by a hope she declared after it.
    """

    logical_time = projection.logical_time
    if logical_time is None:
        return None
    receipt_refs = tuple(
        item
        for item in projection.committed_world_event_refs
        if item.event_type == "ExecutionReceiptRecorded"
    )
    # Each ``ExecutionReceiptRecorded`` reduction appends exactly one receipt,
    # so the committed refs of that type align positionally with the receipt
    # projection (same invariant as the silence-anchor derivation).
    if len(receipt_refs) != len(projection.execution_receipts):
        raise ValueError("execution receipt projection does not align with committed refs")
    pairs = tuple(zip(receipt_refs, projection.execution_receipts, strict=True))

    if anchor_event_ref is not None:
        anchor = next((pair for pair in pairs if pair[0].event_id == anchor_event_ref), None)
        if anchor is None:
            return None
        anchor_ref, anchor_receipt = anchor
        if anchor_receipt.observed_state not in _ANSWERABLE_RECEIPT_STATES:
            return None
        # The anchor is her last visible message; the invitation may live on
        # an earlier beat of the same accepted plan, so the manifest is bound
        # through any beat whose action produced this receipt.
        manifest = next(
            (
                item
                for item in projection.expression_plan_manifests
                if item.response_expectation is not None
                and any(
                    beat.action.action_id == anchor_receipt.action_id for beat in item.beats
                )
            ),
            None,
        )
        if manifest is None:
            return None
        expectation = manifest.response_expectation
        if logical_time >= expectation.expires_at:
            return None
        return _view(
            expectation,
            declared_seconds_ago=int((logical_time - anchor_ref.logical_time).total_seconds()),
        )

    delivered_by_action: dict[str, object] = {}
    for ref, receipt in pairs:
        if receipt.observed_state not in _ANSWERABLE_RECEIPT_STATES:
            continue
        existing = delivered_by_action.get(receipt.action_id)
        if existing is None or ref.world_revision > existing.world_revision:
            delivered_by_action[receipt.action_id] = ref
    candidates = []
    for manifest in projection.expression_plan_manifests:
        expectation = manifest.response_expectation
        if expectation is None or logical_time >= expectation.expires_at:
            continue
        beat = next(
            (item for item in manifest.beats if item.beat_id == expectation.source_beat_id),
            None,
        )
        if beat is None:
            continue
        delivered_ref = delivered_by_action.get(beat.action.action_id)
        if delivered_ref is None:
            continue
        if (
            before_world_revision is not None
            and delivered_ref.world_revision >= before_world_revision
        ):
            continue
        candidates.append((delivered_ref, manifest))
    if not candidates:
        return None
    delivered_ref, manifest = max(
        candidates, key=lambda item: (item[0].world_revision, item[1].acceptance_event_ref)
    )
    return _view(
        manifest.response_expectation,
        declared_seconds_ago=max(
            0, int((logical_time - delivered_ref.logical_time).total_seconds())
        ),
    )


def _view(expectation, *, declared_seconds_ago: int) -> PendingResponseExpectationView:
    return PendingResponseExpectationView(
        hoped_response=expectation.hoped_response,
        pressure=_tier(expectation.pressure_bp),
        importance=_tier(expectation.importance_bp),
        declared_seconds_ago=max(0, declared_seconds_ago),
    )


def _expectation_summary(view: PendingResponseExpectationView) -> str:
    """Compress the pending expectation into one bounded read-only hint.

    Every value is copied from the frozen authority; nothing is inferred.
    The bound is the advisory candidate's 256-character contract.
    """

    minutes = view.declared_seconds_ago // 60
    waited = f"declared about {minutes} minutes ago" if minutes else "declared moments ago"
    return (
        f"When she last spoke she hoped for: {view.hoped_response}"
        f"; pressure {view.pressure}; importance {view.importance}; {waited}"
    )[:256]


def response_expectation_advisory(
    view: PendingResponseExpectationView,
    *,
    source_ref: str,
    logical_time: datetime,
) -> InnerAdvisoryProjection:
    """Wrap the view in the ordinary non-authoritative advisory envelope."""

    return InnerAdvisoryProjection(
        advisory_id="advisory:response-expectation:" + _digest(source_ref),
        kind="response_expectation",
        source_refs=(source_ref,),
        candidate_refs=("response-expectation:" + _digest(source_ref),),
        candidates=(
            InnerAdvisoryCandidate(
                candidate_ref="response-expectation:" + _digest(source_ref),
                value=_expectation_summary(view),
                weight_bp=10_000,
                confidence_bp=10_000,
            ),
        ),
        confidence_bp=10_000,
        # A short-lived deliberation aid anchored to the pinned durable head,
        # not wall-clock process time.
        expiry=logical_time + timedelta(days=1),
        producer_version=RESPONSE_EXPECTATION_ADVISORY_VERSION,
    )


__all__ = [
    "RESPONSE_EXPECTATION_ADVISORY_VERSION",
    "PendingResponseExpectationView",
    "pending_response_expectation",
    "response_expectation_advisory",
]
