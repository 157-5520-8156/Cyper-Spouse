from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.life_content_store import life_content_payload_hash
from companion_daemon.world_v2.life_development_draft import (
    LifeDevelopmentVisualEvidenceDraft,
)
from companion_daemon.world_v2.life_development_runtime import (
    LifeDevelopmentPlanMaterial,
    LifeDevelopmentReadableOutcome,
)
from companion_daemon.world_v2.media_evidence_snapshot import (
    MediaEvidenceCompileRequest,
    MediaEvidenceSnapshotCompiler,
)
from companion_daemon.world_v2.media_v2 import PhotoCandidate
from companion_daemon.world_v2.life_visual_evidence_author import (
    LifeVisualEvidenceAuthor,
    VisualEvidenceAuthorPolicy,
)
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    OutcomeCandidateDescriptor,
    ProjectionCursor,
    WorldEvent,
)


NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
WORLD = "world:visual-evidence-author"
CHARACTER = "agent:companion"
RECIPIENT = "user:geoff"

_SEED = dedent(
    """
    biographical_lifecycle:
      version: biography.test.1
      birth_date: 2005-04-12
      baseline_context_tags: []
      residence:
        before_enrollment: residence:family_home_jiaxing
        term: residence:campus_dorm
        winter_break: residence:family_home_jiaxing
        summer_break: residence:family_home_jiaxing
        graduated: residence:shanghai_home
      academic:
        enrolled_on: 2024-09-01
        expected_graduation_on: 2028-06-30
        term_windows:
          - {opens_on: "09-01", closes_on: "01-15"}
          - {opens_on: "02-20", closes_on: "06-29"}
        winter_break_windows:
          - {opens_on: "01-16", closes_on: "02-19"}
        summer_break_windows:
          - {opens_on: "06-30", closes_on: "08-31"}
        enrollment_context_tags: []
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


def _timeline_event() -> WorldEvent:
    return _event(
        event_id="event:biographical-timeline:test",
        event_type="BiographicalTimelineConfigured",
        at=NOW - timedelta(days=100),
    )


class _Ledger:
    world_id = WORLD
    blocks_event_loop = False

    def __init__(
        self, *events: WorldEvent, plans=(), occurrences=(), affect_episodes=(),
        relationship_states=(), life_arcs=(),
    ) -> None:
        self._events: dict[str, WorldEvent] = {}
        self._refs: list[CommittedWorldEventRef] = []
        self._revision = 0
        self.plans = tuple(plans)
        self.occurrences = tuple(occurrences)
        self.affect_episodes = tuple(affect_episodes)
        self.relationship_states = tuple(relationship_states)
        self.life_arcs = tuple(life_arcs)
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
            life_arcs=self.life_arcs,
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
    life_arcs = tuple(overrides.pop("life_arcs", ()))
    arc_events = tuple(
        _event(
            event_id=arc.accepted_event_ref,
            event_type="LifeArcChanged",
            at=arc.started_at,
        )
        for arc in life_arcs
    )
    ledger = _Ledger(
        _timeline_event(), *arc_events, settlement,
        plans=(plan,), occurrences=(occurrence,), life_arcs=life_arcs,
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
        _timeline_event(), settlement, plans=(plan,), occurrences=(occurrence,),
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


class _OpenLifeProposalReader:
    def __init__(
        self, visual_evidence: LifeDevelopmentVisualEvidenceDraft | None
    ) -> None:
        self._visual_evidence = visual_evidence

    def read_for_plan(self, *, plan_id: str) -> LifeDevelopmentPlanMaterial:
        descriptor = OutcomeCandidateDescriptor(
            candidate_result_ref="candidate:open-life:1",
            result_id="result:open-life:1",
            result_payload_ref="content:open-life:1",
            result_payload_hash="a" * 64,
            privacy_class="shareable",
        )
        alternate = OutcomeCandidateDescriptor(
            candidate_result_ref="candidate:open-life:2",
            result_id="result:open-life:2",
            result_payload_ref="content:open-life:2",
            result_payload_hash="b" * 64,
            privacy_class="shareable",
        )
        return LifeDevelopmentPlanMaterial(
            plan_id=plan_id,
            proposal_event_ref="event:proposal:open-life",
            causal_authority="character_choice",
            premise="暑假实习下班后临时去了校外的河边市集。",
            claim_declarations=(),
            outcomes=(
                LifeDevelopmentReadableOutcome(
                    descriptor=descriptor,
                    text="她在河边市集逛了一会儿，买到一束向日葵。",
                    visual_evidence=self._visual_evidence,
                ),
                LifeDevelopmentReadableOutcome(
                    descriptor=alternate,
                    text="她到河边才发现市集临时取消，很快就回去了。",
                    visual_evidence=None,
                ),
            ),
            character_intention="下班以后想换换空气。",
        )


def _open_life_world() -> tuple[_Ledger, WorldEvent]:
    settlement = _event(
        event_id="event:settlement:open-life",
        event_type="WorldOccurrenceSettled",
        at=NOW - timedelta(hours=1),
    )
    text = "她在河边市集逛了一会儿，买到一束向日葵。"
    occurrence = SimpleNamespace(
        occurrence_id="occurrence:open-life",
        status="settled",
        settled_at=NOW - timedelta(hours=1),
        settlement_event_ref=settlement.event_id,
        settled_outcome_ref="candidate:open-life:1",
        trigger_ref="plan:open-life",
        visibility="shareable",
        result_payload_ref="content:open-life-result",
        result_payload_hash=life_content_payload_hash(text),
    )
    plan = SimpleNamespace(
        plan_id="plan:open-life",
        activity_kind="open_life.market_after_internship",
    )
    return (
        _Ledger(
            _timeline_event(),
            settlement,
            plans=(plan,),
            occurrences=(occurrence,),
        ),
        settlement,
    )


def test_open_life_world_author_visual_claim_enters_the_existing_media_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, settlement = _open_life_world()
    _force_bucket(monkeypatch, 0)
    visual = LifeDevelopmentVisualEvidenceDraft(
        claim_refs=("local:claim:market-visit",),
        activity_description="实习下班后在河边市集闲逛",
        location={
            "location_ref": "location:off-campus-riverside-market",
            "kind": "outdoor_market",
            "city": "上海",
            "publicness": "public",
        },
        environment={
            "light": "summer sunset",
            "structure": "riverside stalls outside the campus",
        },
        objects=(
            {
                "local_ref": "local:object:sunflowers",
                "kind": "flowers",
                "description": "刚买的一束向日葵",
            },
        ),
    )
    author = LifeVisualEvidenceAuthor(
        ledger=ledger,
        catalog=_catalog(tmp_path),
        content_store=_ContentStore("她在河边市集逛了一会儿，买到一束向日葵。"),
        character_ref=CHARACTER,
        recipient_ref=RECIPIENT,
        life_development_proposals=_OpenLifeProposalReader(visual),
    )

    result = author.advance_once(
        wake_event_ref=settlement.event_id,
        trace_id="trace",
        correlation_id="corr",
    )

    assert result.status == "declared"
    evidence = ledger.events_of_type("ImageEvidenceDeclared")[0].payload()[
        "image_evidence"
    ]
    assert evidence["activity"]["description"] == "实习下班后在河边市集闲逛"
    assert evidence["location"]["id"] == "location:off-campus-riverside-market"
    assert evidence["objects"][0]["description"] == "刚买的一束向日葵"
    assert evidence["situational_context"]["academic_phase"] == "summer_break"
    declaration = ledger.events_of_type("ImageEvidenceDeclared")[0]
    projection = ledger.project()
    compiled = MediaEvidenceSnapshotCompiler(ledger=ledger).compile(
        MediaEvidenceCompileRequest(
            candidate=PhotoCandidate(
                candidate_id="candidate:open-life",
                source_event_refs=tuple(
                    sorted((settlement.event_id, declaration.event_id))
                ),
                family="life_share",
                privacy_ceiling="shareable",
            ),
            category="activity_result",
            cursor=ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            ),
        )
    )
    snapshot = compiled.snapshot.image_event_snapshot
    assert snapshot is not None
    assert snapshot.location["id"] == "location:off-campus-riverside-market"
    assert snapshot.situational_context is not None
    assert snapshot.situational_context["season"] == "summer"


def test_open_life_outcome_may_choose_to_supply_no_visual_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, settlement = _open_life_world()
    _force_bucket(monkeypatch, 0)
    author = LifeVisualEvidenceAuthor(
        ledger=ledger,
        catalog=_catalog(tmp_path),
        content_store=_ContentStore("她在河边市集逛了一会儿，买到一束向日葵。"),
        character_ref=CHARACTER,
        recipient_ref=RECIPIENT,
        life_development_proposals=_OpenLifeProposalReader(None),
    )

    result = author.advance_once(
        wake_event_ref=settlement.event_id,
        trace_id="trace",
        correlation_id="corr",
    )

    assert result.status == "idle"
    assert ledger.events_of_type("ImageEvidenceDeclared") == ()


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


def test_author_copies_current_season_calendar_residence_and_life_arc_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_arc = SimpleNamespace(
        arc_id="life-arc:summer-internship",
        accepted_event_ref="event:life-arc:summer-internship",
        privacy_class="shareable",
        status="active",
        started_at=NOW - timedelta(days=10),
        ends_at=NOW + timedelta(days=20),
        context_tags=(
            "life_arc:summer_internship",
            "residence:temporary_internship_flat",
        ),
    )
    ledger, settlement = _walk_world(life_arcs=(active_arc,))
    _force_bucket(monkeypatch, 0)

    result = _author(ledger, tmp_path).advance_once(
        wake_event_ref=settlement.event_id,
        trace_id="trace",
        correlation_id="corr",
    )

    assert result.status == "declared"
    evidence = ledger.events_of_type("ImageEvidenceDeclared")[0].payload()[
        "image_evidence"
    ]
    assert evidence["situational_context"] == {
        "season": "summer",
        "academic_phase": "summer_break",
        "academic_year": 2,
        "calendar_context_tags": ["academic:enrolled", "calendar:summer_break"],
        "current_residence_context_tags": [
            "residence:temporary_internship_flat"
        ],
        "life_arc_context_tags": ["life_arc:summer_internship"],
        "active_life_arc_ids": ["life-arc:summer-internship"],
        "source_event_refs": [
            "event:biographical-timeline:test",
            "event:life-arc:summer-internship",
        ],
    }


def test_public_visual_context_does_not_leak_a_private_active_life_arc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_arc = SimpleNamespace(
        arc_id="life-arc:private-trip",
        accepted_event_ref="event:life-arc:private-trip",
        privacy_class="private",
        status="active",
        started_at=NOW - timedelta(days=2),
        ends_at=NOW + timedelta(days=2),
        context_tags=(
            "travel:private_trip",
            "residence:private_temporary_stay",
        ),
    )
    ledger, settlement = _walk_world(life_arcs=(private_arc,))
    _force_bucket(monkeypatch, 0)

    result = _author(ledger, tmp_path).advance_once(
        wake_event_ref=settlement.event_id,
        trace_id="trace",
        correlation_id="corr",
    )

    assert result.status == "declared"
    evidence = ledger.events_of_type("ImageEvidenceDeclared")[0].payload()[
        "image_evidence"
    ]
    assert evidence["situational_context"] is None


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
