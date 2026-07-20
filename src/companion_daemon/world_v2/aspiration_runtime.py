"""Aspiration layer: reviewed low-stakes wishes planted, reinforced, faded.

This is the quiet-wake sibling of ``NpcInitiativeRuntime`` for the inner
world.  It follows the same reviewed discipline:

* candidates come only from the reviewed ``aspiration_seeds`` catalog section
  (anti-fabrication) and only while their eligibility witness — a recently
  accepted plan of a listed activity kind — actually exists in the ledger;
* whether a wish sprouts is a recorded ``RandomAuthority`` draw over the
  eligible seeds *plus one always-legal "nothing" candidate* whose reviewed
  base masses keep planting a rare event (5-10% per check in production);
* a drawn seed still needs the bounded model's semantic confirmation using
  the life author's exact select/no_op JSON contract — "今天没有冒出什么念头"
  is a permanently legitimate answer;
* frequency is host-owned: each companion-local day has at most one check,
  with every identity encoding the local date so wakes of one day converge
  instead of re-rolling.

The same daily check also maintains existing wishes: an active aspiration
whose related material reappeared may be probabilistically reinforced, and
one untouched for ``fade_idle_days`` may quietly fade.  Both are recorded
draws plus ledger events; neither consults the model (fading is time
semantics, not a judgment call — the wish simply stops being thought of).
Crystallization into a real calendar plan is authority-level interface only
in phase one; no runtime path emits it yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Literal

import httpx
from pydantic import Field

from .aspiration_events import (
    ASPIRATION_POLICY_REF,
    AspirationCrystallizedPayload,
    AspirationFadedPayload,
    AspirationPlantedPayload,
    AspirationReinforcedPayload,
)
from .event_identity import domain_idempotency_key
from .life_author_runtime import (
    LifeAuthorModel,
    LifeAuthorModelFailure,
    LifeAvailabilitySnapshotRecordedPayload,
)
from .life_author_seed import ReviewedAspirationSeed, ReviewedLifeSeedCatalog
from .life_events import ActivityPlannedPayload
from .random_authority import RandomAuthority
from .schema_core import FrozenModel
from .schemas import (
    AspirationProjection,
    DueWindow,
    EvidenceRef,
    PlanStateProjection,
    ProjectionCursor,
    WorldEvent,
)

_POLICY = ASPIRATION_POLICY_REF

# One ledger-recorded "nothing sprouted" candidate shares the planting draw
# with every eligible seed, so a checked-but-quiet day replays exactly.
NOTHING_CANDIDATE_REF = "nothing:aspiration"

# Lived material older than this no longer counts as an eligibility or
# reinforcement witness: a wish grows out of what recently touched her life.
RECENT_MATERIAL_DAYS = 7


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AspirationSeedCandidate(FrozenModel):
    """One reviewed seed that may legally sprout today.

    The token deliberately excludes the wake event: one local day compiles one
    stable identity per seed, so the daily recorded draw and its bounded model
    decision replay instead of re-rolling.
    """

    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: ReviewedAspirationSeed
    # The exact committed plan-acceptance event that makes this seed eligible
    # (None only for seeds with no witness requirement).
    witness_event_ref: str | None = None


class AspirationResult(FrozenModel):
    status: Literal[
        "planted", "recovered", "no_op", "no_candidates", "slot_consumed", "blocked",
    ]
    reason_code: str
    check_event_ref: str | None = None
    draw_event_ref: str | None = None
    aspiration_id: str | None = None
    reinforced_aspiration_ids: tuple[str, ...] = ()
    faded_aspiration_ids: tuple[str, ...] = ()
    crystallized_aspiration_ids: tuple[str, ...] = ()


class AspirationWeightPolicy:
    """Replayable per-check planting mass for reviewed aspiration seeds.

    ``aspiration-seed-weight.1`` uses the reviewed ``base_chance_bp`` of each
    eligible seed inside a 10_000 probability space shared with the nothing
    candidate — no mood modulation in phase one: planting a wish is a rare
    background event, and any future affect tilt must bump this version so
    recorded draws replay under their original weights.
    """

    version = "aspiration-seed-weight.1"

    def compile(
        self, *, candidates: tuple[AspirationSeedCandidate, ...]
    ) -> dict[str, int]:
        weights: dict[str, int] = {}
        total = 0
        for candidate in candidates:
            weights[candidate.token] = candidate.seed.base_chance_bp
            total += candidate.seed.base_chance_bp
        weights[NOTHING_CANDIDATE_REF] = max(10_000 - total, 0)
        return weights


class AspirationRuntime:
    """Own the daily check budget, recorded draws, bounded confirmation, and
    the aspiration ledger events.

    The sole public operation accepts a committed clock ref, exactly like the
    life author lanes.  The caller provides no wish, probability, identity,
    or evidence.
    """

    maintenance_weight_policy_version = "aspiration-maintenance-weight.1"
    crystallization_weight_policy_version = "aspiration-crystallize-weight.1"

    def __init__(
        self,
        *,
        ledger,
        catalog: ReviewedLifeSeedCatalog,
        model: LifeAuthorModel,
        owner_actor_ref: str,
        actor: str = "worker:world-v2:aspiration",
        fade_idle_days: int = 14,
        fade_chance_bp: int = 1_000,
        reinforce_chance_bp: int = 2_500,
        crystallize_chance_bp: int = 1_500,
    ) -> None:
        if not owner_actor_ref or not actor:
            raise ValueError("aspiration runtime requires owner and worker actors")
        if not 1 <= fade_idle_days <= 365:
            raise ValueError("aspiration fade idle days must be in [1, 365]")
        if not 0 <= fade_chance_bp <= 10_000 or not 0 <= reinforce_chance_bp <= 10_000:
            raise ValueError("aspiration maintenance chances must be basis points")
        if not 0 <= crystallize_chance_bp <= 10_000:
            raise ValueError("aspiration crystallization chance must be basis points")
        self._ledger = ledger
        self._catalog = catalog
        self._model = model
        self._owner_actor_ref = owner_actor_ref
        self._actor = actor
        self._fade_idle_days = fade_idle_days
        self._fade_chance_bp = fade_chance_bp
        self._reinforce_chance_bp = reinforce_chance_bp
        self._crystallize_chance_bp = crystallize_chance_bp
        self._random = RandomAuthority(ledger=ledger, source="world-v2:aspiration-random")
        self._weight_policy = AspirationWeightPolicy()

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> AspirationResult:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        wake = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == wake_event_ref
            ),
            None,
        )
        transition = next(
            (
                item
                for item in projection.clock_transition_history
                if item.clock_event_ref == wake_event_ref
            ),
            None,
        )
        if (
            logical_time is None or wake is None or wake.event_type != "ClockAdvanced"
            or transition is None or transition.payload_hash != wake.payload_hash
            or transition.computed_world_revision != wake.world_revision
        ):
            return AspirationResult(
                status="blocked", reason_code="aspiration.wake_not_exact_clock"
            )
        local_date_iso = self._catalog.localize(wake.logical_time).date().isoformat()
        # Crystallization runs on every wake of the day before the planting
        # slot dedupe: its own per-aspiration daily check event converges
        # repeated wakes, and a model outage on the first wake may retry on a
        # later one even after the planting slot was consumed.
        crystallized = await self._maintain_crystallization(
            wake=wake, local_date_iso=local_date_iso,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        check_event_id = "event:aspiration:check:" + _digest({
            "world_id": self._ledger.world_id, "local_date": local_date_iso,
        })
        existing_check = self._check_event(check_event_id)
        if existing_check is not None:
            payload = existing_check.payload()
            if payload.get("decision") == "selected":
                # Crash between the recorded selection and the planted event:
                # recover the exact durable choice from the check payload.
                recovered = self._recover_planting(
                    check_event=existing_check,
                    wake=wake,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                if recovered is not None:
                    return recovered.model_copy(
                        update={"crystallized_aspiration_ids": crystallized}
                    )
            return AspirationResult(
                status="slot_consumed",
                reason_code="aspiration.daily_check_consumed",
                check_event_ref=existing_check.event_id,
                crystallized_aspiration_ids=crystallized,
            )

        # Maintenance shares the daily check: reinforcement first (fresh
        # material resets the fade clock), then quiet fading.  Every draw and
        # ledger event identity encodes the local date, so a wake replayed
        # after a partial crash converges instead of re-rolling.
        reinforced = self._maintain_reinforcement(
            wake=wake, local_date_iso=local_date_iso,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        faded = self._maintain_fade(
            wake=wake, local_date_iso=local_date_iso,
            trace_id=trace_id, correlation_id=correlation_id,
        )

        candidates = self._plant_candidates(local_date_iso=local_date_iso)
        if not candidates:
            # A world with nothing to wish about never consumes the check
            # slot: checks are chances against eligible material only.
            return AspirationResult(
                status="no_candidates", reason_code="aspiration.no_eligible_seed",
                reinforced_aspiration_ids=reinforced, faded_aspiration_ids=faded,
                crystallized_aspiration_ids=crystallized,
            )

        attempt_id = "attempt:aspiration:" + _digest({
            "world_id": self._ledger.world_id,
            "local_date": local_date_iso,
            "catalog_version": self._catalog.version,
            "catalog_hash": self._catalog.catalog_hash,
        })
        current = self._ledger.project()
        draw = self._random.draw(
            attempt_id=attempt_id,
            candidate_refs=(*(item.token for item in candidates), NOTHING_CANDIDATE_REF),
            catalog_version=self._catalog.version,
            logical_time=current.logical_time,
            seed_instant=wake.logical_time,
            actor=self._actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
            candidate_weights=self._weight_policy.compile(candidates=candidates),
            weight_policy_version=self._weight_policy.version,
        )
        draw_event_ref = "event:random-draw:" + draw.draw_id
        if draw.selected_candidate_ref == NOTHING_CANDIDATE_REF:
            check_event = self._record_check(
                check_event_id=check_event_id, local_date_iso=local_date_iso,
                decision="nothing", wake=wake, draw_event_ref=draw_event_ref,
                candidate=None, model="random-authority",
                raw_output=draw.selected_candidate_ref,
                trace_id=trace_id, correlation_id=correlation_id,
            )
            return AspirationResult(
                status="no_op", reason_code="aspiration.nothing_drawn",
                check_event_ref=check_event.event_id, draw_event_ref=draw_event_ref,
                reinforced_aspiration_ids=reinforced, faded_aspiration_ids=faded,
                crystallized_aspiration_ids=crystallized,
            )
        selected = next(
            item for item in candidates if item.token == draw.selected_candidate_ref
        )
        try:
            decision, raw = await self._deliberate(selected, wake=wake)
        except LifeAuthorModelFailure:
            # Model outage does not consume the check slot; a later wake of
            # the same day replays the identical durable draw and retries.
            return AspirationResult(
                status="blocked", reason_code="aspiration.model_unavailable",
                draw_event_ref=draw_event_ref,
                reinforced_aspiration_ids=reinforced, faded_aspiration_ids=faded,
                crystallized_aspiration_ids=crystallized,
            )
        if decision == "no_op":
            check_event = self._record_check(
                check_event_id=check_event_id, local_date_iso=local_date_iso,
                decision="no_op", wake=wake, draw_event_ref=draw_event_ref,
                candidate=selected, model=self._model_id(), raw_output=raw,
                trace_id=trace_id, correlation_id=correlation_id,
            )
            return AspirationResult(
                status="no_op", reason_code="aspiration.model_declined",
                check_event_ref=check_event.event_id, draw_event_ref=draw_event_ref,
                reinforced_aspiration_ids=reinforced, faded_aspiration_ids=faded,
                crystallized_aspiration_ids=crystallized,
            )
        check_event = self._record_check(
            check_event_id=check_event_id, local_date_iso=local_date_iso,
            decision="selected", wake=wake, draw_event_ref=draw_event_ref,
            candidate=selected, model=self._model_id(), raw_output=raw,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        aspiration_id = self._plant(
            seed=selected.seed, witness_event_ref=selected.witness_event_ref,
            wake=wake, check_event_ref=check_event.event_id,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        return AspirationResult(
            status="planted", reason_code="aspiration.planted",
            check_event_ref=check_event.event_id, draw_event_ref=draw_event_ref,
            aspiration_id=aspiration_id,
            reinforced_aspiration_ids=reinforced, faded_aspiration_ids=faded,
            crystallized_aspiration_ids=crystallized,
        )

    # -- planting -----------------------------------------------------------

    def _plant_candidates(
        self, *, local_date_iso: str
    ) -> tuple[AspirationSeedCandidate, ...]:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        planted_seeds = {
            (item.owner_actor_ref, item.seed_id) for item in projection.aspirations
        }
        candidates: list[AspirationSeedCandidate] = []
        for seed in self._catalog.reviewed_aspiration_seeds:
            if (self._owner_actor_ref, seed.id) in planted_seeds:
                continue
            witness_ref: str | None = None
            if seed.requires_recent_activity_kinds:
                witness = self._latest_witness(
                    projection,
                    kinds=set(seed.requires_recent_activity_kinds),
                    accepted_after=logical_time - timedelta(days=RECENT_MATERIAL_DAYS),
                    logical_time=logical_time,
                )
                if witness is None:
                    continue
                witness_ref = witness[0]
            candidates.append(AspirationSeedCandidate(
                token=_digest({
                    "catalog_version": self._catalog.version,
                    "catalog_hash": self._catalog.catalog_hash,
                    "seed_id": seed.id,
                    "local_date": local_date_iso,
                }),
                seed=seed,
                witness_event_ref=witness_ref,
            ))
        candidates.sort(key=lambda item: item.seed.id)
        return tuple(candidates)

    @staticmethod
    def _latest_witness(
        projection,
        *,
        kinds: set[str],
        accepted_after: datetime,
        logical_time: datetime,
    ) -> tuple[str, datetime] | None:
        """Newest committed plan acceptance of a listed kind inside the window."""

        best: tuple[str, datetime] | None = None
        for plan in projection.plans:
            origin = plan.authority_origin
            if (
                origin is None
                or plan.activity_kind not in kinds
                or origin.accepted_at <= accepted_after
                or origin.accepted_at > logical_time
            ):
                continue
            if best is None or origin.accepted_at > best[1]:
                best = (origin.accepted_event_ref, origin.accepted_at)
        return best

    def _plant(
        self, *, seed: ReviewedAspirationSeed, witness_event_ref: str | None,
        wake, check_event_ref: str, trace_id: str, correlation_id: str,
    ) -> str:
        projection = self._ledger.project()
        aspiration_id = "aspiration:" + _digest({
            "world_id": self._ledger.world_id, "seed_id": seed.id,
        })
        if any(item.aspiration_id == aspiration_id for item in projection.aspirations):
            return aspiration_id
        event_id = "event:aspiration:planted:" + _digest({
            "world_id": self._ledger.world_id, "aspiration_id": aspiration_id,
        })
        if self._ledger.lookup_event_commit(event_id) is not None:
            return aspiration_id
        evidence = [self._wake_evidence(wake)]
        source_event_ref = wake.event_id
        if witness_event_ref is not None:
            witness = next(
                item
                for item in projection.committed_world_event_refs
                if item.event_id == witness_event_ref
            )
            evidence.append(EvidenceRef(
                ref_id=witness.event_id,
                evidence_type="committed_world_event",
                claim_purpose="past_experience",
                source_world_revision=witness.world_revision,
                immutable_hash=witness.payload_hash,
            ))
            source_event_ref = witness.event_id
        suffix = aspiration_id.removeprefix("aspiration:")
        payload = AspirationPlantedPayload(
            change_id="change:aspiration:planted:" + suffix,
            transition_id="transition:aspiration:planted:" + suffix,
            expected_entity_revision=0,
            evidence_refs=tuple(evidence),
            policy_refs=(
                _POLICY,
                f"aspiration-seed:{seed.id}",
                f"policy:life-author-catalog:{self._catalog.version}",
            ),
            aspiration=AspirationProjection(
                aspiration_id=aspiration_id,
                entity_revision=1,
                owner_actor_ref=self._owner_actor_ref,
                seed_id=seed.id,
                text=seed.text,
                privacy_class=seed.privacy,
                status="active",
                planted_at=wake.logical_time,
                planted_event_ref=event_id,
                source_event_ref=source_event_ref,
            ),
        ).model_dump(mode="json")
        self._commit(
            event_id=event_id, event_type="AspirationPlanted", payload=payload,
            wake=wake, causation_id=check_event_ref,
            trace_id=trace_id, correlation_id=correlation_id,
            projection=projection,
        )
        return aspiration_id

    def _recover_planting(
        self, *, check_event: WorldEvent, wake, trace_id: str, correlation_id: str
    ) -> AspirationResult | None:
        payload = check_event.payload()
        seed_id = payload.get("seed_id")
        seed = next(
            (
                item
                for item in self._catalog.reviewed_aspiration_seeds
                if item.id == seed_id
            ),
            None,
        )
        if seed is None:
            return AspirationResult(
                status="blocked", reason_code="aspiration.selected_seed_stale",
                check_event_ref=check_event.event_id,
            )
        aspiration_id = "aspiration:" + _digest({
            "world_id": self._ledger.world_id, "seed_id": seed.id,
        })
        projection = self._ledger.project()
        if any(item.aspiration_id == aspiration_id for item in projection.aspirations):
            return None
        witness_ref = payload.get("witness_event_ref")
        self._plant(
            seed=seed,
            witness_event_ref=witness_ref if isinstance(witness_ref, str) else None,
            wake=wake, check_event_ref=check_event.event_id,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        return AspirationResult(
            status="recovered", reason_code="aspiration.planting_recovered",
            check_event_ref=check_event.event_id, aspiration_id=aspiration_id,
        )

    # -- crystallization ----------------------------------------------------

    async def _maintain_crystallization(
        self, *, wake, local_date_iso: str, trace_id: str, correlation_id: str
    ) -> tuple[str, ...]:
        """Rarely turn one supported active wish into a concrete future plan.

        The seam follows the future life author's daily discipline: one check
        per aspiration per companion-local day, a recorded crystallize/hold
        draw, the bounded select/no_op confirmation, then one atomic batch of
        availability snapshot + ``ActivityPlanned`` + ``AspirationCrystallized``
        with evidence pointing back at the wish's planting event.
        """

        projection = self._ledger.project()
        logical_time = projection.logical_time
        seeds = {item.id: item for item in self._catalog.reviewed_aspiration_seeds}
        crystallized: list[str] = []
        for aspiration in projection.aspirations:
            seed = seeds.get(aspiration.seed_id)
            if seed is None or seed.crystallizes_into is None:
                continue
            check_event_id = "event:aspiration:crystallize-check:" + _digest({
                "world_id": self._ledger.world_id,
                "aspiration_id": aspiration.aspiration_id,
                "local_date": local_date_iso,
            })
            existing_check = self._crystallize_check_event(check_event_id)
            if existing_check is not None:
                if (
                    existing_check.payload().get("decision") == "selected"
                    and aspiration.status == "active"
                ):
                    # Crash between the durable selection and the plan batch:
                    # recover the exact recorded slot instead of re-rolling.
                    if self._commit_crystallization_from_check(
                        check_event=existing_check, wake=wake,
                        trace_id=trace_id, correlation_id=correlation_id,
                    ):
                        crystallized.append(aspiration.aspiration_id)
                continue
            if aspiration.status != "active":
                continue
            # Lived-material support: fresh reinforcement or a recent witness
            # of the seed's listed activity kinds keeps the wish "touched".
            recent_floor = logical_time - timedelta(days=RECENT_MATERIAL_DAYS)
            witness_ref: str | None = None
            supported = (
                aspiration.last_reinforced_at is not None
                and aspiration.last_reinforced_at > recent_floor
            )
            if not supported and seed.requires_recent_activity_kinds:
                witness = self._latest_witness(
                    projection,
                    kinds=set(seed.requires_recent_activity_kinds),
                    accepted_after=recent_floor,
                    logical_time=logical_time,
                )
                if witness is not None:
                    supported, witness_ref = True, witness[0]
            if not supported and not seed.requires_recent_activity_kinds:
                supported = True
            if not supported:
                continue
            slot = self._crystallization_slot(projection, seed=seed, wake=wake)
            if slot is None:
                # No free reviewed calendar slot inside the horizon: the wish
                # simply stays a wish today.
                continue
            crystallize_ref = f"crystallize:{aspiration.aspiration_id}"
            hold_ref = f"hold:{aspiration.aspiration_id}"
            draw = self._random.draw(
                attempt_id="attempt:aspiration-crystallize:" + _digest({
                    "world_id": self._ledger.world_id,
                    "local_date": local_date_iso,
                    "aspiration_id": aspiration.aspiration_id,
                }),
                candidate_refs=(crystallize_ref, hold_ref),
                catalog_version=self._catalog.version,
                logical_time=self._ledger.project().logical_time,
                seed_instant=wake.logical_time,
                actor=self._actor,
                trace_id=trace_id,
                correlation_id=correlation_id,
                candidate_weights={
                    crystallize_ref: max(self._crystallize_chance_bp, 0),
                    hold_ref: max(10_000 - self._crystallize_chance_bp, 0),
                },
                weight_policy_version=self.crystallization_weight_policy_version,
            )
            draw_event_ref = "event:random-draw:" + draw.draw_id
            if draw.selected_candidate_ref != crystallize_ref:
                continue
            try:
                decision, raw = await self._deliberate_crystallization(
                    aspiration=aspiration, slot=slot, wake=wake
                )
            except LifeAuthorModelFailure:
                # A model outage leaves no check event: the durable draw
                # replays on a later wake of the same day and retries.
                continue
            check_event = self._record_crystallize_check(
                check_event_id=check_event_id,
                local_date_iso=local_date_iso,
                decision="selected" if decision == "select" else "no_op",
                aspiration=aspiration,
                slot=slot,
                witness_event_ref=witness_ref,
                wake=wake,
                draw_event_ref=draw_event_ref,
                raw_output=raw,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            if decision != "select":
                continue
            if self._commit_crystallization(
                aspiration_id=aspiration.aspiration_id, slot=slot, wake=wake,
                check_event_ref=check_event.event_id,
                trace_id=trace_id, correlation_id=correlation_id,
            ):
                crystallized.append(aspiration.aspiration_id)
        return tuple(crystallized)

    def _crystallization_slot(
        self, projection, *, seed: ReviewedAspirationSeed, wake
    ) -> dict | None:
        """Pick the earliest free reviewed slot of the seed's target opening.

        The slot is captured as plain replayable coordinates so the durable
        check event can recover the exact same plan after a crash.
        """

        owner_plans = tuple(
            plan for plan in projection.plans
            if plan.owner_actor_ref == self._owner_actor_ref
        )
        candidates = tuple(
            item
            for item in self._catalog.future_candidates_at(
                instant=wake.logical_time,
                plans=owner_plans,
                npcs=projection.npcs,
            )
            if item.opening.id == seed.crystallizes_into
        )
        if not candidates:
            return None
        selected = candidates[0]
        return {
            "opening_id": selected.opening.id,
            "activity_kind": selected.opening.activity_kind,
            "candidate_token": selected.token,
            "target_local_date": selected.target_local_date.isoformat(),
            "local_window": selected.local_window,
            "opens_at": selected.opens_at.isoformat(),
            "closes_at": selected.closes_at.isoformat(),
            "location_ref": selected.location_ref,
            "participant_ref": selected.participant_ref,
            "availability_hash": selected.availability_hash,
            "importance_bp": selected.opening.importance_bp,
            "duration_minutes": selected.opening.duration_minutes,
            "privacy": selected.opening.privacy,
            "policy_refs": list(
                selected.opening.policy_refs(catalog_version=self._catalog.version)
            ),
        }

    async def _deliberate_crystallization(
        self, *, aspiration: AspirationProjection, slot: dict, wake
    ) -> tuple[Literal["select", "no_op"], str]:
        """The life author's exact bounded select/no_op JSON contract."""

        try:
            raw = await self._model.complete(
                [
                    {"role": "system", "content": (
                        "You are the final semantic confirmation for crystallizing one long-held, "
                        "reviewed wish into one concrete future calendar plan. The host has already "
                        "verified the wish's committed planting event, its recent lived-material "
                        "support, the reviewed future opening, the free calendar slot, the daily "
                        "frequency budget, and the controlled-random crystallization draw. Select "
                        "when actually scheduling this wish now rings true; return no_op when it "
                        "does not — a wish quietly staying a wish is always a legitimate outcome. "
                        "Return exactly {\"decision\":\"no_op\"} or "
                        "{\"decision\":\"select\",\"candidate_token\":\"offered token\"}. "
                        "Do not invent an outcome, person, place, time, event id, or extra detail."
                    )},
                    {"role": "user", "content": json.dumps({
                        "authoritative_eligibility": {
                            "logical_time": wake.logical_time.isoformat(),
                            "planted_event_ref": aspiration.planted_event_ref,
                            "availability_hash": slot["availability_hash"],
                        },
                        "aspiration": {
                            "text": aspiration.text,
                            "held_since": aspiration.planted_at.isoformat(),
                            "reinforcement_count": aspiration.reinforcement_count,
                        },
                        "crystallization_candidate": {
                            "token": slot["candidate_token"],
                            "activity_kind": slot["activity_kind"],
                            "target_local_date": slot["target_local_date"],
                            "local_window": slot["local_window"],
                            "duration_minutes": slot["duration_minutes"],
                            "privacy": slot["privacy"],
                        },
                    }, ensure_ascii=False, separators=(",", ":"))},
                ],
                temperature=0.2,
            )
        except (TimeoutError, ConnectionError, OSError, httpx.HTTPError) as exc:
            raise LifeAuthorModelFailure("aspiration crystallization model is unavailable") from exc
        if not isinstance(raw, str) or len(raw.encode()) > 32_768:
            raise LifeAuthorModelFailure("crystallization model response is not bounded text")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LifeAuthorModelFailure("crystallization model response is not valid JSON") from exc
        if not isinstance(parsed, dict) or set(parsed) not in (
            {"decision"}, {"decision", "candidate_token"}
        ):
            raise LifeAuthorModelFailure("crystallization model returned an invalid decision")
        decision = parsed.get("decision")
        if decision == "no_op":
            if "candidate_token" in parsed:
                raise LifeAuthorModelFailure("crystallization no_op cannot select a candidate")
            return "no_op", raw
        if decision != "select" or parsed.get("candidate_token") != slot["candidate_token"]:
            raise LifeAuthorModelFailure("crystallization model selected an unoffered candidate")
        return "select", raw

    def _record_crystallize_check(
        self, *, check_event_id: str, local_date_iso: str,
        decision: Literal["no_op", "selected"], aspiration: AspirationProjection,
        slot: dict, witness_event_ref: str | None, wake, draw_event_ref: str,
        raw_output: str, trace_id: str, correlation_id: str,
    ) -> WorldEvent:
        projection = self._ledger.project()
        payload = {
            "proposal_id": "proposal:aspiration-crystallize:" + _digest({
                "world_id": self._ledger.world_id,
                "aspiration_id": aspiration.aspiration_id,
                "local_date": local_date_iso,
            }),
            "proposal_kind": "aspiration_crystallization",
            "decision": decision,
            "check_local_date": local_date_iso,
            "trigger_id": wake.event_id,
            "evaluated_world_revision": projection.world_revision,
            "wake_event_ref": wake.event_id,
            "wake_event_payload_hash": wake.payload_hash,
            "draw_event_ref": draw_event_ref,
            "aspiration_id": aspiration.aspiration_id,
            "seed_id": aspiration.seed_id,
            "witness_event_ref": witness_event_ref,
            "slot": slot,
            "catalog_version": self._catalog.version,
            "catalog_hash": self._catalog.catalog_hash,
            "model": self._model_id(),
            "raw_output_hash": "sha256:" + hashlib.sha256(raw_output.encode()).hexdigest(),
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=check_event_id,
            event_type="ProposalRecorded",
            world_id=self._ledger.world_id,
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:aspiration",
            trace_id=trace_id,
            causation_id=draw_event_ref,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload
            ) or "aspiration-crystallize-check:" + _digest(check_event_id),
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=_cursor(projection),
            commit_id="commit:aspiration:crystallize-check:" + _digest(check_event_id),
        )
        return event

    def _crystallize_check_event(self, check_event_id: str) -> WorldEvent | None:
        located = self._ledger.lookup_event_commit(check_event_id)
        if located is None or located[0].event_type != "ProposalRecorded":
            return None
        if located[0].payload().get("proposal_kind") != "aspiration_crystallization":
            return None
        return located[0]

    def _commit_crystallization_from_check(
        self, *, check_event: WorldEvent, wake, trace_id: str, correlation_id: str
    ) -> bool:
        payload = check_event.payload()
        aspiration_id = payload.get("aspiration_id")
        slot = payload.get("slot")
        if not isinstance(aspiration_id, str) or not isinstance(slot, dict):
            return False
        return self._commit_crystallization(
            aspiration_id=aspiration_id, slot=slot, wake=wake,
            check_event_ref=check_event.event_id,
            trace_id=trace_id, correlation_id=correlation_id,
        )

    def _commit_crystallization(
        self, *, aspiration_id: str, slot: dict, wake, check_event_ref: str,
        trace_id: str, correlation_id: str,
    ) -> bool:
        """One atomic batch: availability snapshot + plan + crystallized wish."""

        projection = self._ledger.project()
        current = next(
            (item for item in projection.aspirations if item.aspiration_id == aspiration_id),
            None,
        )
        if current is None or current.status != "active":
            return False
        opens_at = datetime.fromisoformat(slot["opens_at"])
        if projection.logical_time is not None and projection.logical_time >= opens_at:
            # The recorded slot is no longer in the future (long outage
            # between selection and recovery): leave the wish active.
            return False
        suffix = _digest({
            "world_id": self._ledger.world_id, "aspiration_id": aspiration_id,
        })
        plan_event_id = "event:aspiration-crystallize-plan:" + suffix
        if self._ledger.lookup_event_commit(plan_event_id) is not None:
            return False
        planted = next(
            item
            for item in projection.committed_world_event_refs
            if item.event_id == current.planted_event_ref
        )
        clock_evidence = self._wake_evidence(wake)
        planted_evidence = EvidenceRef(
            ref_id=planted.event_id,
            evidence_type="committed_world_event",
            claim_purpose="past_experience",
            source_world_revision=planted.world_revision,
            immutable_hash=planted.payload_hash,
        )
        participant_refs = (
            (slot["participant_ref"],) if slot.get("participant_ref") else ()
        )
        snapshot_payload = LifeAvailabilitySnapshotRecordedPayload(
            snapshot_id="availability:aspiration-crystallize:" + suffix,
            wake_event_ref=wake.event_id,
            wake_event_payload_hash=wake.payload_hash,
            wake_world_revision=wake.world_revision,
            candidate_token=slot["candidate_token"],
            catalog_version=self._catalog.version,
            catalog_hash=self._catalog.catalog_hash,
            owner_actor_ref=self._owner_actor_ref,
            location_ref=slot.get("location_ref"),
            participant_refs=participant_refs,
            availability_hash=slot["availability_hash"],
        )
        snapshot_event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:aspiration-crystallize-availability:" + suffix,
            world_id=self._ledger.world_id,
            event_type="LifeAvailabilitySnapshotRecorded",
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:aspiration",
            trace_id=trace_id,
            causation_id=check_event_ref,
            correlation_id=correlation_id,
            idempotency_key="aspiration-crystallize-availability:" + suffix,
            payload=snapshot_payload.model_dump(mode="json"),
        )
        snapshot_evidence = EvidenceRef(
            ref_id=snapshot_event.event_id,
            evidence_type="committed_world_event",
            claim_purpose="future_plan",
            source_world_revision=projection.world_revision + 1,
            immutable_hash=snapshot_event.payload_hash,
        )
        plan_evidence = (clock_evidence, snapshot_evidence, planted_evidence)
        # The aspiration reducer resolves ``plan_ref`` as "plan:" + plan_id,
        # so the plan identity itself deliberately carries no "plan:" prefix.
        plan = PlanStateProjection(
            plan_id="aspiration-crystallize:" + suffix,
            activity_id="activity:aspiration-crystallize:" + suffix,
            entity_revision=1,
            activity_kind=slot["activity_kind"],
            evidence_refs=plan_evidence,
            status="planned",
            importance_bp=int(slot["importance_bp"]),
            scheduled_window=DueWindow(
                opens_at=opens_at,
                closes_at=datetime.fromisoformat(slot["closes_at"]),
            ),
            participant_refs=participant_refs,
            location_ref=slot.get("location_ref"),
            privacy_class=slot["privacy"],
            owner_actor_ref=self._owner_actor_ref,
        )
        plan_payload = ActivityPlannedPayload(
            change_id="change:aspiration-crystallize:plan:" + suffix,
            transition_id="transition:aspiration-crystallize:plan:" + suffix,
            expected_entity_revision=0,
            evidence_refs=plan_evidence,
            policy_refs=tuple(sorted({
                *slot.get("policy_refs", ()),
                _POLICY,
                f"aspiration-seed:{current.seed_id}",
            })),
            plan=plan,
        ).model_dump(mode="json")
        plan_event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=plan_event_id,
            world_id=self._ledger.world_id,
            event_type="ActivityPlanned",
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:aspiration",
            trace_id=trace_id,
            causation_id=check_event_ref,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ActivityPlanned", world_id=self._ledger.world_id,
                payload=plan_payload,
            ) or "aspiration-crystallize-plan:" + suffix,
            payload=plan_payload,
        )
        crystallized_payload = AspirationCrystallizedPayload(
            change_id="change:aspiration:crystallized:" + suffix,
            transition_id="transition:aspiration:crystallized:" + suffix,
            expected_entity_revision=current.entity_revision,
            evidence_refs=(clock_evidence, planted_evidence),
            policy_refs=(_POLICY, f"aspiration-seed:{current.seed_id}"),
            aspiration_id=aspiration_id,
            crystallized_at=wake.logical_time,
            plan_ref="plan:" + plan.plan_id,
        ).model_dump(mode="json")
        crystallized_event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:aspiration:crystallized:" + suffix,
            world_id=self._ledger.world_id,
            event_type="AspirationCrystallized",
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:aspiration",
            trace_id=trace_id,
            causation_id=current.planted_event_ref,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="AspirationCrystallized", world_id=self._ledger.world_id,
                payload=crystallized_payload,
            ) or "aspiration-crystallized:" + suffix,
            payload=crystallized_payload,
        )
        self._ledger.commit_at_cursor(
            (snapshot_event, plan_event, crystallized_event),
            expected_cursor=_cursor(projection),
            commit_id="commit:aspiration:crystallize:" + suffix,
        )
        return True

    # -- maintenance --------------------------------------------------------

    def _maintain_reinforcement(
        self, *, wake, local_date_iso: str, trace_id: str, correlation_id: str
    ) -> tuple[str, ...]:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        seeds = {item.id: item for item in self._catalog.reviewed_aspiration_seeds}
        reinforced: list[str] = []
        for aspiration in projection.aspirations:
            if aspiration.status != "active":
                continue
            seed = seeds.get(aspiration.seed_id)
            if seed is None or not seed.requires_recent_activity_kinds:
                continue
            idle_since = aspiration.last_reinforced_at or aspiration.planted_at
            witness = self._latest_witness(
                projection,
                kinds=set(seed.requires_recent_activity_kinds),
                accepted_after=max(
                    idle_since, logical_time - timedelta(days=RECENT_MATERIAL_DAYS)
                ),
                logical_time=logical_time,
            )
            if witness is None:
                continue
            reinforce_ref = f"reinforce:{aspiration.aspiration_id}"
            hold_ref = f"hold:{aspiration.aspiration_id}"
            draw = self._random.draw(
                attempt_id="attempt:aspiration-reinforce:" + _digest({
                    "world_id": self._ledger.world_id,
                    "local_date": local_date_iso,
                    "aspiration_id": aspiration.aspiration_id,
                }),
                candidate_refs=(reinforce_ref, hold_ref),
                catalog_version=self._catalog.version,
                logical_time=self._ledger.project().logical_time,
                seed_instant=wake.logical_time,
                actor=self._actor,
                trace_id=trace_id,
                correlation_id=correlation_id,
                candidate_weights={
                    reinforce_ref: max(self._reinforce_chance_bp, 0),
                    hold_ref: max(10_000 - self._reinforce_chance_bp, 0),
                },
                weight_policy_version=self.maintenance_weight_policy_version,
            )
            if draw.selected_candidate_ref != reinforce_ref:
                continue
            if self._commit_reinforcement(
                aspiration=aspiration, witness_event_ref=witness[0], wake=wake,
                local_date_iso=local_date_iso, draw_id=draw.draw_id,
                trace_id=trace_id, correlation_id=correlation_id,
            ):
                reinforced.append(aspiration.aspiration_id)
        return tuple(reinforced)

    def _commit_reinforcement(
        self, *, aspiration: AspirationProjection, witness_event_ref: str,
        wake, local_date_iso: str, draw_id: str, trace_id: str, correlation_id: str,
    ) -> bool:
        projection = self._ledger.project()
        current = next(
            item
            for item in projection.aspirations
            if item.aspiration_id == aspiration.aspiration_id
        )
        if current.status != "active":
            return False
        event_id = "event:aspiration:reinforced:" + _digest({
            "world_id": self._ledger.world_id,
            "aspiration_id": aspiration.aspiration_id,
            "local_date": local_date_iso,
        })
        if self._ledger.lookup_event_commit(event_id) is not None:
            return False
        witness = next(
            item
            for item in projection.committed_world_event_refs
            if item.event_id == witness_event_ref
        )
        suffix = _digest({"event": event_id})
        payload = AspirationReinforcedPayload(
            change_id="change:aspiration:reinforced:" + suffix,
            transition_id="transition:aspiration:reinforced:" + suffix,
            expected_entity_revision=current.entity_revision,
            evidence_refs=(
                self._wake_evidence(wake),
                EvidenceRef(
                    ref_id=witness.event_id,
                    evidence_type="committed_world_event",
                    claim_purpose="past_experience",
                    source_world_revision=witness.world_revision,
                    immutable_hash=witness.payload_hash,
                ),
            ),
            policy_refs=(_POLICY, f"aspiration-seed:{current.seed_id}"),
            aspiration_id=current.aspiration_id,
            reinforced_at=wake.logical_time,
            reinforcement_evidence_ref=witness.event_id,
        ).model_dump(mode="json")
        self._commit(
            event_id=event_id, event_type="AspirationReinforced", payload=payload,
            wake=wake, causation_id="event:random-draw:" + draw_id,
            trace_id=trace_id, correlation_id=correlation_id,
            projection=projection,
        )
        return True

    def _maintain_fade(
        self, *, wake, local_date_iso: str, trace_id: str, correlation_id: str
    ) -> tuple[str, ...]:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        seeds = {item.id: item for item in self._catalog.reviewed_aspiration_seeds}
        faded: list[str] = []
        for aspiration in projection.aspirations:
            if aspiration.status != "active":
                continue
            idle_since = aspiration.last_reinforced_at or aspiration.planted_at
            if logical_time - idle_since < timedelta(days=self._fade_idle_days):
                continue
            # Fresh related material means the wish is still being touched by
            # her life; leave it to the reinforcement draw instead of letting
            # the same day both feed and bury it.
            seed = seeds.get(aspiration.seed_id)
            if seed is not None and seed.requires_recent_activity_kinds:
                witness = self._latest_witness(
                    projection,
                    kinds=set(seed.requires_recent_activity_kinds),
                    accepted_after=max(
                        idle_since, logical_time - timedelta(days=RECENT_MATERIAL_DAYS)
                    ),
                    logical_time=logical_time,
                )
                if witness is not None:
                    continue
            fade_ref = f"fade:{aspiration.aspiration_id}"
            keep_ref = f"keep:{aspiration.aspiration_id}"
            draw = self._random.draw(
                attempt_id="attempt:aspiration-fade:" + _digest({
                    "world_id": self._ledger.world_id,
                    "local_date": local_date_iso,
                    "aspiration_id": aspiration.aspiration_id,
                }),
                candidate_refs=(fade_ref, keep_ref),
                catalog_version=self._catalog.version,
                logical_time=self._ledger.project().logical_time,
                seed_instant=wake.logical_time,
                actor=self._actor,
                trace_id=trace_id,
                correlation_id=correlation_id,
                candidate_weights={
                    fade_ref: max(self._fade_chance_bp, 0),
                    keep_ref: max(10_000 - self._fade_chance_bp, 0),
                },
                weight_policy_version=self.maintenance_weight_policy_version,
            )
            if draw.selected_candidate_ref != fade_ref:
                continue
            if self._commit_fade(
                aspiration=aspiration, wake=wake, draw_id=draw.draw_id,
                trace_id=trace_id, correlation_id=correlation_id,
            ):
                faded.append(aspiration.aspiration_id)
        return tuple(faded)

    def _commit_fade(
        self, *, aspiration: AspirationProjection, wake, draw_id: str,
        trace_id: str, correlation_id: str,
    ) -> bool:
        projection = self._ledger.project()
        current = next(
            item
            for item in projection.aspirations
            if item.aspiration_id == aspiration.aspiration_id
        )
        if current.status != "active":
            return False
        event_id = "event:aspiration:faded:" + _digest({
            "world_id": self._ledger.world_id,
            "aspiration_id": aspiration.aspiration_id,
        })
        if self._ledger.lookup_event_commit(event_id) is not None:
            return False
        suffix = _digest({"event": event_id})
        payload = AspirationFadedPayload(
            change_id="change:aspiration:faded:" + suffix,
            transition_id="transition:aspiration:faded:" + suffix,
            expected_entity_revision=current.entity_revision,
            evidence_refs=(self._wake_evidence(wake),),
            policy_refs=(_POLICY, f"aspiration-seed:{current.seed_id}"),
            aspiration_id=current.aspiration_id,
            faded_at=wake.logical_time,
        ).model_dump(mode="json")
        self._commit(
            event_id=event_id, event_type="AspirationFaded", payload=payload,
            wake=wake, causation_id="event:random-draw:" + draw_id,
            trace_id=trace_id, correlation_id=correlation_id,
            projection=projection,
        )
        return True

    # -- shared plumbing ----------------------------------------------------

    async def _deliberate(
        self, candidate: AspirationSeedCandidate, *, wake
    ) -> tuple[Literal["select", "no_op"], str]:
        """The life author's exact bounded select/no_op JSON contract."""

        try:
            raw = await self._model.complete(
                [
                    {"role": "system", "content": (
                        "You are the final semantic confirmation for one reviewed aspiration seed — "
                        "a low-stakes wish with no deadline that the companion may quietly come to hold. "
                        "The host has already verified the reviewed catalog, the recent lived material "
                        "that makes this wish plausible, the daily frequency budget, and the "
                        "controlled-random planting draw. Select the offered wish when it rings true "
                        "as something she would privately start wanting now; return no_op when it does "
                        "not — no wish sprouting today is always a legitimate outcome. Return exactly "
                        "{\"decision\":\"no_op\"} or "
                        "{\"decision\":\"select\",\"candidate_token\":\"offered token\"}. "
                        "Do not invent a wish, plan, deadline, person, place, or additional detail."
                    )},
                    {"role": "user", "content": json.dumps({
                        "authoritative_eligibility": {
                            "logical_time": wake.logical_time.isoformat(),
                            "witness_event_ref": candidate.witness_event_ref,
                        },
                        "aspiration_candidate": {
                            "token": candidate.token,
                            "seed_id": candidate.seed.id,
                            "text": candidate.seed.text,
                            "privacy": candidate.seed.privacy,
                            "base_chance_bp": candidate.seed.base_chance_bp,
                        },
                    }, ensure_ascii=False, separators=(",", ":"))},
                ],
                temperature=0.2,
            )
        except (TimeoutError, ConnectionError, OSError, httpx.HTTPError) as exc:
            raise LifeAuthorModelFailure("aspiration model provider is unavailable") from exc
        if not isinstance(raw, str) or len(raw.encode()) > 32_768:
            raise LifeAuthorModelFailure("aspiration model response is not bounded text")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LifeAuthorModelFailure("aspiration model response is not valid JSON") from exc
        if not isinstance(parsed, dict) or set(parsed) not in (
            {"decision"}, {"decision", "candidate_token"}
        ):
            raise LifeAuthorModelFailure("aspiration model returned an invalid decision")
        decision = parsed.get("decision")
        if decision == "no_op":
            if "candidate_token" in parsed:
                raise LifeAuthorModelFailure("aspiration no_op cannot select a candidate")
            return "no_op", raw
        if decision != "select" or parsed.get("candidate_token") != candidate.token:
            raise LifeAuthorModelFailure("aspiration model selected an unoffered candidate")
        return "select", raw

    def _record_check(
        self, *, check_event_id: str, local_date_iso: str,
        decision: Literal["nothing", "no_op", "selected"], wake, draw_event_ref: str,
        candidate: AspirationSeedCandidate | None, model: str, raw_output: str,
        trace_id: str, correlation_id: str,
    ) -> WorldEvent:
        projection = self._ledger.project()
        payload = {
            "proposal_id": "proposal:aspiration:" + _digest({
                "world_id": self._ledger.world_id,
                "local_date": local_date_iso,
            }),
            "proposal_kind": "aspiration",
            "decision": decision,
            "check_local_date": local_date_iso,
            "trigger_id": wake.event_id,
            "evaluated_world_revision": projection.world_revision,
            "wake_event_ref": wake.event_id,
            "wake_event_payload_hash": wake.payload_hash,
            "draw_event_ref": draw_event_ref,
            "candidate_token": candidate.token if candidate else None,
            "seed_id": candidate.seed.id if candidate else None,
            "witness_event_ref": candidate.witness_event_ref if candidate else None,
            "catalog_version": self._catalog.version,
            "catalog_hash": self._catalog.catalog_hash,
            "model": model,
            "raw_output_hash": "sha256:" + hashlib.sha256(raw_output.encode()).hexdigest(),
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=check_event_id,
            event_type="ProposalRecorded",
            world_id=self._ledger.world_id,
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:aspiration",
            trace_id=trace_id,
            causation_id=draw_event_ref,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload
            ) or "aspiration-check:" + _digest(check_event_id),
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=_cursor(projection),
            commit_id="commit:aspiration:check:" + _digest(check_event_id),
        )
        return event

    def _commit(
        self, *, event_id: str, event_type: str, payload: dict, wake,
        causation_id: str, trace_id: str, correlation_id: str, projection,
    ) -> None:
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            event_type=event_type,
            world_id=self._ledger.world_id,
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:aspiration",
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type=event_type, world_id=self._ledger.world_id, payload=payload
            ) or "aspiration:" + _digest(event_id),
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=_cursor(projection),
            commit_id="commit:aspiration:" + _digest(event_id),
        )

    def _check_event(self, check_event_id: str) -> WorldEvent | None:
        located = self._ledger.lookup_event_commit(check_event_id)
        if located is None or located[0].event_type != "ProposalRecorded":
            return None
        if located[0].payload().get("proposal_kind") != "aspiration":
            return None
        return located[0]

    def _model_id(self) -> str:
        return str(getattr(self._model, "model", "")).strip() or type(self._model).__name__

    @staticmethod
    def _wake_evidence(wake) -> EvidenceRef:
        return EvidenceRef(
            ref_id=wake.event_id,
            evidence_type="committed_world_event",
            claim_purpose="current_fact",
            source_world_revision=wake.world_revision,
            immutable_hash=wake.payload_hash,
        )


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


__all__ = [
    "NOTHING_CANDIDATE_REF",
    "RECENT_MATERIAL_DAYS",
    "AspirationResult",
    "AspirationRuntime",
    "AspirationSeedCandidate",
    "AspirationWeightPolicy",
]
