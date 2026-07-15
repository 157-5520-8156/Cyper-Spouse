"""Proof-backed, inert Fact source resolution.

This module deliberately stops before acceptance or materialization.  It turns
only exact observation events authenticated by ``SQLiteProofBackedObservationReader``
into the closed v2 Fact evidence and assertion shapes.  A later acceptance
adapter must still prove policy, revision, and transition authority.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from threading import RLock
from weakref import WeakKeyDictionary

from .fact_accepted_contracts import (
    FactAssertionBindingV2,
    FactCommitIntentV2,
    ResolvedFactEvidenceV2,
    canonical_fact_commit_intent_json,
)
from .ledger import HistoricalLedgerEvent, ObservationEventLocator
from .schemas import Observation
from .sqlite_ledger import (
    PinnedObservationHistoryHandle,
    ProofBackedObservationLookup,
    SQLiteProofBackedObservationReader,
)


class FactEvidenceResolutionError(ValueError):
    """A Fact source cannot be resolved from the exact pinned history."""


@dataclass(frozen=True, slots=True)
class _ResolvedFactCommitSourceMaterialV2:
    """Resolver-private source image for one exact canonical Fact intent."""

    intent_json: str
    evidence_refs: tuple[ResolvedFactEvidenceV2, ...]
    assertion_binding: FactAssertionBindingV2


class ResolvedFactCommitSourcesV2:
    """Opaque resolver-issued capability for one exact Fact intent.

    The evidence and assertion image deliberately do not live on this public
    object.  A freshly constructed instance has no resolver registry entry and
    therefore cannot be used to materialize a Fact change.  This still has no
    ledger-writing authority: it is only a proof that one exact resolver read
    the source observations at one of its reader-issued historical pins.
    """

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("resolved Fact source capabilities cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("resolved Fact source capabilities cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("resolved Fact source capabilities cannot be copied")


class ProofBackedFactEvidenceResolverV2:
    """Resolve Fact observation sources from one reader-issued historical pin.

    This is not a general ledger lookup and does not accept arbitrary evidence
    claims.  Every intent evidence reference must name exactly one supplied
    observation locator.  Authenticated absence remains a terminal failure;
    it must not be substituted with a current projection, another event kind,
    or a same-named observation.
    """

    __slots__ = ("__reader", "__sources", "__sources_lock")

    def __init__(self, *, reader: SQLiteProofBackedObservationReader) -> None:
        if type(reader) is not SQLiteProofBackedObservationReader:
            raise TypeError("Fact proof resolver requires an exact SQLite proof reader")
        self.__reader = reader
        self.__sources: WeakKeyDictionary[
            ResolvedFactCommitSourcesV2, _ResolvedFactCommitSourceMaterialV2
        ] = WeakKeyDictionary()
        self.__sources_lock = RLock()

    def resolve(
        self,
        *,
        handle: PinnedObservationHistoryHandle,
        intent: FactCommitIntentV2,
        locators: Sequence[ObservationEventLocator],
    ) -> ResolvedFactCommitSourcesV2:
        """Resolve all intent sources and derive its assertion binding.

        The handle remains opaque and is checked by the owning reader.  This
        resolver cannot select a cursor, read unpinned history, or turn a
        ``locator_missing`` proof into a weaker evidence form.
        """

        if type(intent) is not FactCommitIntentV2:
            raise TypeError("Fact proof resolver requires an exact FactCommitIntentV2")
        expected_refs = tuple(use.evidence_ref for use in intent.evidence_uses)
        supplied = _exact_locators_for_refs(locators=locators, expected_refs=expected_refs)
        lookups = self.__reader.read(handle=handle, locators=supplied)
        if len(lookups) != len(expected_refs):
            raise FactEvidenceResolutionError("proof reader did not return every requested locator")

        evidence_by_ref: dict[str, ResolvedFactEvidenceV2] = {}
        assertion_by_ref: dict[str, FactAssertionBindingV2] = {}
        for locator, lookup in zip(supplied, lookups, strict=True):
            if lookup.locator != locator:
                raise FactEvidenceResolutionError("proof reader returned a mismatched locator")
            if lookup.status == "locator_missing" or lookup.event is None:
                raise FactEvidenceResolutionError(
                    f"Fact evidence locator_missing: {locator.observation_id}"
                )
            purpose = next(
                use.purpose for use in intent.evidence_uses if use.evidence_ref == locator.observation_id
            )
            evidence, binding = _resolve_observation_lookup(
                lookup=lookup,
                purpose=purpose,
                asserted_subject_ref=intent.subject_ref,
            )
            if evidence.ref_id != locator.observation_id:
                raise FactEvidenceResolutionError("resolved Fact evidence ref mismatches its locator")
            evidence_by_ref[evidence.ref_id] = evidence
            assertion_by_ref[evidence.ref_id] = binding

        try:
            material = _ResolvedFactCommitSourceMaterialV2(
                intent_json=canonical_fact_commit_intent_json(intent),
                evidence_refs=tuple(evidence_by_ref[ref] for ref in expected_refs),
                assertion_binding=assertion_by_ref[intent.assertion_source_ref],
            )
        except KeyError as exc:
            raise FactEvidenceResolutionError(
                "Fact assertion source was not resolved from pinned history"
            ) from exc
        sources = ResolvedFactCommitSourcesV2()
        with self.__sources_lock:
            self.__sources[sources] = material
        return sources

    def _sealed_material(
        self,
        *,
        sources: ResolvedFactCommitSourcesV2,
        intent: FactCommitIntentV2,
    ) -> _ResolvedFactCommitSourceMaterialV2:
        """Return one issuer-verified source image for the sealed adapter.

        This deliberately remains module-private API.  The adapter passes the
        canonical intent it sealed from its proposal; a capability issued for a
        different assertion, purpose, or subject is rejected before any Fact
        payload is shaped.
        """

        if type(sources) is not ResolvedFactCommitSourcesV2:
            raise FactEvidenceResolutionError(
                "Fact sources must come from the proof-backed resolver result"
            )
        if type(intent) is not FactCommitIntentV2:
            raise FactEvidenceResolutionError("Fact source intent must use its exact v2 contract")
        with self.__sources_lock:
            material = self.__sources.get(sources)
        if material is None:
            raise FactEvidenceResolutionError("Fact source capability is not owned by this resolver")
        if material.intent_json != canonical_fact_commit_intent_json(intent):
            raise FactEvidenceResolutionError("Fact source capability belongs to another Fact intent")
        return material


def _exact_locators_for_refs(
    *, locators: Sequence[ObservationEventLocator], expected_refs: tuple[str, ...]
) -> tuple[ObservationEventLocator, ...]:
    if isinstance(locators, (str, bytes)):
        raise FactEvidenceResolutionError("Fact evidence locators must be a sequence")
    try:
        supplied = tuple(locators)
    except TypeError as exc:
        raise FactEvidenceResolutionError("Fact evidence locators must be iterable") from exc
    if any(type(locator) is not ObservationEventLocator for locator in supplied):
        raise FactEvidenceResolutionError("Fact evidence locators must be exact locator values")
    supplied_refs = tuple(locator.observation_id for locator in supplied)
    if supplied_refs != expected_refs:
        raise FactEvidenceResolutionError(
            "Fact evidence locators must exactly enumerate intent evidence refs"
        )
    canonical = tuple(
        sorted(
            supplied,
            key=lambda item: (item.observation_id, item.event_type, item.idempotency_key),
        )
    )
    if supplied != canonical or len(set(supplied)) != len(supplied):
        raise FactEvidenceResolutionError("Fact evidence locators must be canonical and unique")
    return supplied


def _resolve_observation_lookup(
    *,
    lookup: ProofBackedObservationLookup,
    purpose: str,
    asserted_subject_ref: str,
) -> tuple[ResolvedFactEvidenceV2, FactAssertionBindingV2]:
    historical = lookup.event
    if historical is None:
        raise FactEvidenceResolutionError("Fact evidence lookup lacks an authenticated event")
    event = historical.event
    if event.event_type == "ObservationRecorded":
        return _resolve_message_observation(
            historical=historical,
            locator=lookup.locator,
            purpose=purpose,
            asserted_subject_ref=asserted_subject_ref,
        )
    if event.event_type == "OperatorObservationRecorded":
        return _resolve_operator_observation(
            historical=historical,
            locator=lookup.locator,
            purpose=purpose,
            asserted_subject_ref=asserted_subject_ref,
        )
    raise FactEvidenceResolutionError("proof-backed event is not a Fact observation source")


def _resolve_message_observation(
    *,
    historical: HistoricalLedgerEvent,
    locator: ObservationEventLocator,
    purpose: str,
    asserted_subject_ref: str,
) -> tuple[ResolvedFactEvidenceV2, FactAssertionBindingV2]:
    event = historical.event
    raw = event.payload()
    if raw.get("observation_kind") != "message":
        raise FactEvidenceResolutionError("ObservationRecorded is not a retained message observation")
    try:
        observation = Observation.model_validate_json(event.payload_json)
    except Exception as exc:
        raise FactEvidenceResolutionError("message observation payload is not valid") from exc
    if (
        observation.observation_id != locator.observation_id
        or observation.world_id != event.world_id
        or observation.logical_time != event.logical_time
        or observation.created_at != event.created_at
        or observation.actor != event.actor
        or observation.source != event.source
        or observation.trace_id != event.trace_id
        or observation.causation_id != event.causation_id
        or observation.correlation_id != event.correlation_id
    ):
        raise FactEvidenceResolutionError("message observation conflicts with its event envelope")
    if observation.actor != asserted_subject_ref:
        raise FactEvidenceResolutionError(
            "message observation actor does not match the asserted Fact subject"
        )
    try:
        return (
            ResolvedFactEvidenceV2(
                ref_id=observation.observation_id,
                evidence_type="observed_message",
                claim_purpose=purpose,
                source_world_revision=historical.event_cursor.world_revision,
                immutable_hash=event.payload_hash,
            ),
            FactAssertionBindingV2(
                source_kind="observed_message",
                source_ref=observation.observation_id,
                asserted_subject_ref=asserted_subject_ref,
                actor_ref=observation.actor,
                channel=observation.channel,
                payload_ref=observation.payload_ref,
                content_payload_hash=observation.payload_hash,
            ),
        )
    except Exception as exc:
        raise FactEvidenceResolutionError("message observation cannot form Fact evidence") from exc


def _resolve_operator_observation(
    *,
    historical: HistoricalLedgerEvent,
    locator: ObservationEventLocator,
    purpose: str,
    asserted_subject_ref: str,
) -> tuple[ResolvedFactEvidenceV2, FactAssertionBindingV2]:
    event = historical.event
    raw = event.payload()
    observation_id = raw.get("observation_id")
    observation_hash = raw.get("observation_hash")
    if observation_id != locator.observation_id or type(observation_hash) is not str:
        raise FactEvidenceResolutionError("operator observation does not match its locator")
    try:
        return (
            ResolvedFactEvidenceV2(
                ref_id=observation_id,
                evidence_type="operator_observation",
                claim_purpose=purpose,
                source_world_revision=historical.event_cursor.world_revision,
                immutable_hash=observation_hash,
            ),
            FactAssertionBindingV2(
                source_kind="operator_observation",
                source_ref=observation_id,
                # Operator observations intentionally have no message envelope.
                # The proposed Fact subject supplies the asserted subject; a
                # future acceptance adapter must separately authorize that
                # operator-to-subject claim.
                asserted_subject_ref=asserted_subject_ref,
                actor_ref=None,
                channel=None,
                payload_ref=None,
                content_payload_hash=observation_hash,
            ),
        )
    except Exception as exc:
        raise FactEvidenceResolutionError("operator observation cannot form Fact evidence") from exc


__all__ = [
    "FactEvidenceResolutionError",
    "ProofBackedFactEvidenceResolverV2",
    "ResolvedFactCommitSourcesV2",
]
