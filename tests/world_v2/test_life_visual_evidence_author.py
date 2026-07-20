from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.life_content_store import life_content_payload_hash
from companion_daemon.world_v2.life_visual_evidence_author import (
    LifeVisualEvidenceAuthor,
    VisualEvidenceAuthorPolicy,
)
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, WorldEvent


NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
WORLD = "world:visual-evidence-author"
CHARACTER = "agent:companion"
RECIPIENT = "user:geoff"

_SEED = dedent(
    """
    life_author_catalog:
      version: reviewed-test-visual.1
      locations:
        - id: campus-path
          location_ref: location:campus-path
          privacy: shareable
          local_windows: ["06:00-23:00"]
          weekdays: [0, 1, 2, 3, 4, 5, 6]
        - id: dorm-room
          location_ref: location:dorm-room
          privacy: private
          local_windows: ["00:00-23:59"]
          weekdays: [0, 1, 2, 3, 4, 5, 6]
      openings:
        - id: short-walk
          activity_kind: commute.short_walk
          source: environmental_opportunity
          domain: commute_walk
          social_shape: alone
          deviation: impulse
          visual_potential: place
          privacy: shareable
          location_id: campus-path
          local_windows: ["06:00-23:00"]
          weekdays: [0, 1, 2, 3, 4, 5, 6]
          duration_minutes: 30
          importance_bp: 3900
          outcomes:
            - {id: walk-found-light, text: 沿校园走了一圈，光线很好。, privacy: shareable}
            - {id: walk-cut-short, text: 提前绕回来了。, privacy: shareable}
          visual_evidence:
            activity_description: 傍晚沿校园林荫道散步
            location: {id: location:campus-path, kind: campus_path, publicness: public}
            environment: {light: dusk light, structure: tree-lined path}
            self_capture: [character_front_camera]
        - id: prepare-for-bed
          activity_kind: sleep.prepare_for_bed
          source: routine
          domain: sleep_wake
          social_shape: alone
          deviation: persist
          visual_potential: private_transition
          privacy: private
          location_id: dorm-room
          local_windows: ["00:00-23:59"]
          weekdays: [0, 1, 2, 3, 4, 5, 6]
          duration_minutes: 35
          importance_bp: 4200
          outcomes:
            - {id: bedtime-settled, text: 收拾好准备休息了。, privacy: private}
            - {id: bedtime-delayed, text: 磨蹭了一会儿才躺下。, privacy: private}
          visual_evidence:
            activity_description: 睡前在宿舍收拾东西准备休息
            location: {id: location:dorm-room, kind: dorm_room, publicness: private, mirror_available: true}
            environment: {light: warm dim lamp, structure: small dorm room}
            self_capture: [character_front_camera, mirror]
    """
).strip()


def _catalog(tmp_path: Path) -> ReviewedLifeSeedCatalog:
    seed = tmp_path / "seed.yaml"
    seed.write_text(_SEED, encoding="utf-8")
    return ReviewedLifeSeedCatalog.from_yaml(
        path=seed, chronology=LocalChronology("Asia/Shanghai")
    )


def _event(*, event_id: str, event_type: str, at: datetime = NOW) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, event_type=event_type,
        world_id=WORLD, logical_time=at, created_at=at, actor=CHARACTER,
        source="test", trace_id="trace:visual", causation_id="cause:visual",
        correlation_id="correlation:visual", idempotency_key=event_id, payload={},
    )


class _Ledger:
    world_id = WORLD
    blocks_event_loop = False

    def __init__(
        self, *events: WorldEvent, plans=(), occurrences=(), affect_episodes=(),
        relationship_states=(),
    ) -> None:
        self._events: dict[str, WorldEvent] = {}
        self._refs: list[CommittedWorldEventRef] = []
        self._revision = 0
        self.plans = tuple(plans)
        self.occurrences = tuple(occurrences)
        self.affect_episodes = tuple(affect_episodes)
        self.relationship_states = tuple(relationship_states)
        for event in events:
            self._append(event)

    def _append(self, event: WorldEvent) -> None:
        self._revision += 1
        self._events[event.event_id] = event
        self._refs.append(CommittedWorldEventRef(
            event_id=event.event_id, event_type=event.event_type,
            world_revision=self._revision, payload_hash=event.payload_hash,
            logical_time=event.logical_time,
        ))

    def _projection(self) -> SimpleNamespace:
        return SimpleNamespace(
            world_revision=self._revision, deliberation_revision=0,
            ledger_sequence=self._revision, logical_time=NOW,
            committed_world_event_refs=tuple(self._refs),
            plans=self.plans, world_occurrences=self.occurrences,
            affect_episodes=self.affect_episodes,
            relationship_states=self.relationship_states,
            photo_candidates=(), experiences=(), facts=(),
        )

    def project(self) -> SimpleNamespace:
        return self._projection()

    def project_at(self, cursor) -> SimpleNamespace:  # type: ignore[no-untyped-def]
        return self._projection()

    def lookup_event_commit(self, event_id: str):  # type: ignore[no-untyped-def]
        event = self._events.get(event_id)
        if event is None:
            return None
        return event, SimpleNamespace(
            world_revision=self._revision, event_ids=(event_id,),
        )

    def commit_at_cursor(self, events, *, expected_cursor, commit_id):  # type: ignore[no-untyped-def]
        for event in events:
            self._append(event)
        return SimpleNamespace(
            event_ids=tuple(event.event_id for event in events),
            world_revision=self._revision,
        )

    def events_of_type(self, event_type: str) -> tuple[WorldEvent, ...]:
        return tuple(
            event for event in self._events.values() if event.event_type == event_type
        )


class _ContentStore:
    def __init__(self, text: str = "沿校园走了一圈，光线很好。") -> None:
        self._text = text

    def read_exact(self, *, content_ref: str):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            content_payload_hash=life_content_payload_hash(self._text), text=self._text
        )


def _walk_world(**overrides):  # type: ignore[no-untyped-def]
    settlement = _event(
        event_id="event:settlement:walk", event_type="WorldOccurrenceSettled",
        at=NOW - timedelta(hours=1),
    )
    text = "沿校园走了一圈，光线很好。"
    occurrence = SimpleNamespace(
        occurrence_id="occurrence:walk", status="settled",
        settled_at=NOW - timedelta(hours=1),
        settlement_event_ref=settlement.event_id, trigger_ref="plan:walk",
        visibility="shareable",
        result_payload_ref="content:walk-result",
        result_payload_hash=life_content_payload_hash(text),
    )
    plan = SimpleNamespace(plan_id="plan:walk", activity_kind="commute.short_walk")
    ledger = _Ledger(
        settlement, plans=(plan,), occurrences=(occurrence,),
        **overrides,
    )
    return ledger, settlement


def _bed_world(*, relationship_states=()):  # type: ignore[no-untyped-def]
    settlement = _event(
        event_id="event:settlement:bed", event_type="WorldOccurrenceSettled",
        at=NOW - timedelta(hours=1),
    )
    text = "收拾好准备休息了。"
    occurrence = SimpleNamespace(
        occurrence_id="occurrence:bed", status="settled",
        settled_at=NOW - timedelta(hours=1),
        settlement_event_ref=settlement.event_id, trigger_ref="plan:bed",
        visibility="private",
        result_payload_ref="content:bed-result",
        result_payload_hash=life_content_payload_hash(text),
    )
    plan = SimpleNamespace(plan_id="plan:bed", activity_kind="sleep.prepare_for_bed")
    ledger = _Ledger(
        settlement, plans=(plan,), occurrences=(occurrence,),
        relationship_states=relationship_states,
    )
    return ledger, settlement, text


def _author(ledger: _Ledger, tmp_path: Path, *, content_text: str | None = None) -> LifeVisualEvidenceAuthor:
    return LifeVisualEvidenceAuthor(
        ledger=ledger,
        catalog=_catalog(tmp_path),
        content_store=_ContentStore(content_text) if content_text else _ContentStore(),
        character_ref=CHARACTER,
        recipient_ref=RECIPIENT,
    )


def _force_bucket(monkeypatch: pytest.MonkeyPatch, bucket: int) -> None:
    monkeypatch.setattr(
        LifeVisualEvidenceAuthor, "_chance_bucket",
        lambda self, **kwargs: bucket,
    )


def test_author_declares_public_evidence_and_opens_character_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, settlement = _walk_world()
    _force_bucket(monkeypatch, 0)

    result = _author(ledger, tmp_path).advance_once(
        wake_event_ref=settlement.event_id, trace_id="trace", correlation_id="corr",
    )

    assert result.status == "declared"
    assert result.lane == "public"
    assert result.declared_source_ref == settlement.event_id
    declarations = ledger.events_of_type("ImageEvidenceDeclared")
    assert len(declarations) == 1
    payload = declarations[0].payload()
    assert payload["source_event_ref"] == settlement.event_id
    evidence = payload["image_evidence"]
    assert evidence["visibility"] == "shareable"
    assert evidence["summary"] == "沿校园走了一圈，光线很好。"
    assert evidence["activity"]["description"] == "傍晚沿校园林荫道散步"
    assert evidence["location"]["publicness"] == "public"
    assert evidence["character_media"]["capture_capabilities"] == ["character_front_camera"]
    # The declaration wake immediately opened the fact-bound selfie candidate.
    opened = ledger.events_of_type("PhotoCandidateOpened")
    assert len(opened) == 1
    candidate = opened[0].payload()["candidate"]
    assert candidate["family"] == "character_media"
    assert candidate["character_media_contract"]["kind"] == "selfie"
    assert result.opened_candidate_ids == (candidate["candidate_id"],)


def test_author_keeps_the_moment_quiet_when_the_ticket_sits_above_the_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, settlement = _walk_world()
    _force_bucket(monkeypatch, 39)

    result = _author(ledger, tmp_path).advance_once(
        wake_event_ref=settlement.event_id, trace_id="trace", correlation_id="corr",
    )

    assert result.status == "idle"
    assert result.reason_code == "visual_evidence.nothing_selected"
    assert ledger.events_of_type("ImageEvidenceDeclared") == ()


def test_a_heavy_mood_holds_the_same_ticket_back_and_a_brighter_wake_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bucket 14 sits at 3_625bp: below the neutral place threshold (4_500bp)
    # but above the heavily-sad threshold (4_500 * 0.5 = 2_250bp).
    heavy = SimpleNamespace(
        status="active",
        components=(SimpleNamespace(dimension="sadness", intensity_bp=10_000),),
    )
    ledger, settlement = _walk_world(affect_episodes=(heavy,))
    _force_bucket(monkeypatch, 14)

    held = _author(ledger, tmp_path).advance_once(
        wake_event_ref=settlement.event_id, trace_id="trace", correlation_id="corr",
    )
    assert held.status == "idle"
    assert ledger.events_of_type("ImageEvidenceDeclared") == ()

    ledger.affect_episodes = ()
    released = _author(ledger, tmp_path).advance_once(
        wake_event_ref=settlement.event_id, trace_id="trace", correlation_id="corr",
    )
    assert released.status == "declared"
    assert len(ledger.events_of_type("ImageEvidenceDeclared")) == 1


def test_daily_budget_and_minimum_gap_suppress_further_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, settlement = _walk_world()
    _force_bucket(monkeypatch, 0)
    recent = _event(
        event_id="event:existing-declaration", event_type="ImageEvidenceDeclared",
        at=NOW - timedelta(minutes=30),
    )
    ledger._append(recent)

    gapped = _author(ledger, tmp_path).advance_once(
        wake_event_ref=settlement.event_id, trace_id="trace", correlation_id="corr",
    )
    assert gapped.status == "idle"
    assert gapped.reason_code == "visual_evidence.min_gap_not_elapsed"

    for index in range(3):
        ledger._append(_event(
            event_id=f"event:existing-declaration:{index}",
            event_type="ImageEvidenceDeclared",
            at=NOW - timedelta(hours=3 + index),
        ))
    budgeted = _author(ledger, tmp_path).advance_once(
        wake_event_ref=settlement.event_id, trace_id="trace", correlation_id="corr",
    )
    assert budgeted.status == "idle"
    assert budgeted.reason_code == "visual_evidence.daily_budget_exhausted"


def test_private_transition_declares_recipient_scoped_only_at_close_friend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_bucket(monkeypatch, 0)

    stranger_ledger, settlement, _text = _bed_world(
        relationship_states=(SimpleNamespace(subject_ref=RECIPIENT, stage="stranger"),),
    )
    held = _author(stranger_ledger, tmp_path, content_text="收拾好准备休息了。").advance_once(
        wake_event_ref=settlement.event_id, trace_id="trace", correlation_id="corr",
    )
    assert held.status == "idle"
    assert stranger_ledger.events_of_type("RecipientScopedImageEvidenceDeclared") == ()

    close_ledger, settlement, text = _bed_world(
        relationship_states=(SimpleNamespace(subject_ref=RECIPIENT, stage="close_friend"),),
    )
    declared = _author(close_ledger, tmp_path, content_text=text).advance_once(
        wake_event_ref=settlement.event_id, trace_id="trace", correlation_id="corr",
    )

    assert declared.status == "declared"
    assert declared.lane == "private"
    declarations = close_ledger.events_of_type("RecipientScopedImageEvidenceDeclared")
    assert len(declarations) == 1
    payload = declarations[0].payload()
    assert payload["recipient_ref"] == RECIPIENT
    evidence = payload["image_evidence"]
    assert evidence["visibility"] == "private"
    assert evidence["activity"]["private_transition"] is True
    assert evidence["location"]["mirror_available"] is True
    kinds = {
        opened.payload()["candidate"]["character_media_contract"]["kind"]
        for opened in close_ledger.events_of_type("PhotoCandidateOpened")
    }
    assert kinds == {"selfie", "mirror"}


def test_a_declared_source_is_never_redeclared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, settlement = _walk_world()
    _force_bucket(monkeypatch, 0)
    author = _author(ledger, tmp_path)

    first = author.advance_once(
        wake_event_ref=settlement.event_id, trace_id="trace", correlation_id="corr",
    )
    assert first.status == "declared"

    # The follow-up wake sees the source already declared and, with the daily
    # rhythm caps relaxed, still finds nothing new to declare.
    relaxed = LifeVisualEvidenceAuthor(
        ledger=ledger, catalog=_catalog(tmp_path), content_store=_ContentStore(),
        character_ref=CHARACTER, recipient_ref=RECIPIENT,
        policy=VisualEvidenceAuthorPolicy(min_gap=timedelta(0), max_declarations_per_day=10),
    )
    second = relaxed.advance_once(
        wake_event_ref=settlement.event_id, trace_id="trace", correlation_id="corr",
    )
    assert second.status == "idle"
    assert second.reason_code == "visual_evidence.no_eligible_settled_occurrence"
    assert len(ledger.events_of_type("ImageEvidenceDeclared")) == 1


def test_the_chance_ticket_is_recorded_once_and_stays_stable(tmp_path: Path) -> None:
    ledger, _settlement = _walk_world()
    author = _author(ledger, tmp_path)
    occurrence = ledger.occurrences[0]

    first = author._chance_bucket(
        occurrence=occurrence, logical_time=NOW, trace_id="trace", correlation_id="corr",
    )
    second = author._chance_bucket(
        occurrence=occurrence, logical_time=NOW, trace_id="trace", correlation_id="corr",
    )

    assert first == second
    assert 0 <= first < 40
    assert len(ledger.events_of_type("RandomDrawRecorded")) == 1


def test_catalog_rejects_visual_evidence_on_a_visually_silent_opening(tmp_path: Path) -> None:
    bad = _SEED.replace("visual_potential: place", "visual_potential: none")
    seed = tmp_path / "bad-seed.yaml"
    seed.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError, match="visually silent"):
        ReviewedLifeSeedCatalog.from_yaml(
            path=seed, chronology=LocalChronology("Asia/Shanghai")
        )
