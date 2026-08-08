from __future__ import annotations

import ast
from datetime import UTC, datetime
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.character_interior import CharacterInterior, InnerDecision
from companion_daemon.world_v2.character_interior.snapshot_compiler import (
    compile_inner_life_snapshot,
)
from companion_daemon.world_v2.character_interior.structured_role import (
    StructuredCharacterRoleFaculty,
)
from companion_daemon.world_v2.external_world_perception.contracts import (
    AuditedLiveCharacterAttentionResult,
    CharacterAttentionContext,
    CharacterAttentionRequest,
    CharacterAttentionTechnicalFailure,
    LicensedEvidenceView,
    LiveCharacterAttentionContext,
    LiveCharacterAttentionRequest,
    LivePerceptionWindow,
    PerceptionChannelProof,
    PerceptionDossier,
    PerceptionWindow,
)
from companion_daemon.world_v2.external_perception_events import (
    FrozenExternalSignalSnapshot,
    canonical_external_perception_json,
)
from companion_daemon.world_v2.external_world_perception.deployment import (
    build_external_world_perception_deployment,
)
from companion_daemon.world_v2.schemas import LedgerProjection, ProjectionCursor


_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


_CURSOR = ProjectionCursor(world_revision=42, deliberation_revision=7, ledger_sequence=84)


def _inner_life_context() -> dict[str, object]:
    return {
        "world_id": "world:test",
        "actor_ref": "character:zhizhi",
        "world_revision": _CURSOR.world_revision,
        "deliberation_revision": _CURSOR.deliberation_revision,
        "ledger_sequence": _CURSOR.ledger_sequence,
        "logical_time": _NOW.isoformat(),
        "consumer_scope": "deliberation_internal",
        "slices": {
            "character_core": {
                "availability": "available",
                "items": [
                    {
                        "item_ref": "character-core:test:1",
                        "value": {
                            "values": {"slow_evolving": {"self_description": "有自己的判断"}}
                        },
                    },
                ],
            },
            "current_situation": {
                "availability": "available",
                "items": [
                    {
                        "item_ref": "capability:test",
                        "value": {
                            "logical_time": _NOW.isoformat(),
                            "activity_slices": ["public_information_read"],
                        },
                    }
                ],
            },
        },
    }


def _inner_life_snapshot():  # type: ignore[no-untyped-def]
    return compile_inner_life_snapshot(_inner_life_context())


def _inner_life_view() -> dict[str, object]:
    return _inner_life_snapshot().model_view()


class _CanonicalProjection:
    async def project(self, *, subject):  # type: ignore[no-untyped-def]
        assert subject.cursor == _CURSOR
        return _inner_life_snapshot()


class _OneResponseAuthor:
    model = "fixture-character-author"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = []

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        self.calls.append((messages, temperature))
        return self.response


class _CapturingInterior:
    def __init__(
        self,
        *,
        technical_failure: str | None = None,
    ) -> None:
        self.opportunities = []
        self.technical_failure = technical_failure

    async def consider(self, opportunity):  # type: ignore[no-untyped-def]
        self.opportunities.append(opportunity)
        view = _inner_life_view()
        if self.technical_failure is not None:
            return InnerDecision(
                inner_turn_id="character-inner-turn:test",
                opportunity_ref=opportunity.opportunity_ref,
                actor_ref=opportunity.actor_ref,
                cursor=opportunity.cursor,
                status="technical_failure",
                failure_code=self.technical_failure,
            )
        summary = "她看过这次机会后决定不关注其中任何一条。"
        author = {
            "model_id": "fixture-character-author",
            "model_version": "1",
            "model_call_id": "model-call:character-interior:test:1",
            "request_hash": "sha256:" + "a" * 64,
            "response_hash": "sha256:" + "b" * 64,
            "attempt_ordinal": 0,
        }
        return InnerDecision(
            inner_turn_id="character-inner-turn:test",
            opportunity_ref=opportunity.opportunity_ref,
            actor_ref=opportunity.actor_ref,
            cursor=opportunity.cursor,
            snapshot_id=view["snapshot_id"],
            snapshot_hash=view["snapshot_hash"],
            status="decided",
            summary=summary,
            instant_private_self={"summary": summary},
            private_self_lineage={
                "relation": "single_pass",
                "initial_private_self": {"summary": summary},
                "initial_snapshot_id": view["snapshot_id"],
                "initial_snapshot_hash": view["snapshot_hash"],
                "initial_author_lineage": author,
                "final_private_self": {"summary": summary},
                "final_snapshot_id": view["snapshot_id"],
                "final_snapshot_hash": view["snapshot_hash"],
                "final_author_lineage": author,
            },
            decision={
                "contract": "character-interior-purpose-decision.1",
                "purpose": "external_perception_attention",
                "source_refs": ["character-core:test:1"],
                "capability_ref": opportunity.capability_manifest.capability_ref,
                "capability_payload_hash": (opportunity.capability_manifest.payload_hash),
                "payload": {
                    "contract": "external-perception-attention-decision.1",
                    "selections": (),
                },
            },
            author_lineage=author,
        )


class _LedgerAuthorityFixture:
    world_id = "world:test"
    blocks_event_loop = False

    def __init__(self) -> None:
        self.lookups: list[str] = []
        self._projection = LedgerProjection(
            world_id=self.world_id,
            world_revision=_CURSOR.world_revision,
            deliberation_revision=_CURSOR.deliberation_revision,
            ledger_sequence=_CURSOR.ledger_sequence,
            logical_time=_NOW,
            semantic_hash="f" * 64,
        )

    def project_at(self, cursor: ProjectionCursor) -> LedgerProjection:
        assert cursor == _CURSOR
        return self._projection

    def lookup_event_commit(self, source_ref: str):  # type: ignore[no-untyped-def]
        self.lookups.append(source_ref)
        if source_ref != "capability:test":
            return None
        return (
            SimpleNamespace(
                world_id=self.world_id,
                event_type="CapabilityGranted",
                payload_hash="sha256:" + "e" * 64,
            ),
            SimpleNamespace(
                world_revision=1,
                deliberation_revision=0,
                ledger_sequence=1,
            ),
        )


class _CapsuleFixture:
    def compile(self, query):  # type: ignore[no-untyped-def]
        assert query.world_id == "world:test"
        assert query.world_revision == _CURSOR.world_revision
        return SimpleNamespace(
            model_content_json=json.dumps(_inner_life_context(), ensure_ascii=False)
        )


def _shadow_request() -> CharacterAttentionRequest:
    channel = PerceptionChannelProof(
        channel_ref="channel:test",
        channel_kind="public_feed",
        evidence_refs=("capability:test",),
        accessible_source_ids=("source:test",),
        valid_until=datetime(2026, 8, 4, 13, 0, tzinfo=UTC),
    )
    dossier = PerceptionDossier(
        candidate_ref="candidate:test:1",
        exact_signal_revisions=("signal-revision:test:1",),
        accessible_channels=(channel,),
        model_visible_material=(
            LicensedEvidenceView(
                signal_revision_ref="signal-revision:test:1",
                source_id="source:test",
                upstream_publisher_ref="publisher:test",
                signal_kind="local_report",
                headline="A source-bound test report",
                observed_at=_NOW,
                expires_at=datetime(2026, 8, 4, 13, 0, tzinfo=UTC),
            ),
        ),
        evidence_digest="b" * 64,
    )
    return CharacterAttentionRequest(
        attention_attempt_id="attention-attempt:test:1",
        retry_ordinal=0,
        selection_ordinal=0,
        window=PerceptionWindow(
            window_id="window:test:1",
            attention_attempt_id="attention-attempt:test:1",
            opportunity_id="opportunity:test:1",
            world_id="world:test",
            actor_ref="character:zhizhi",
            pinned_world_cursor="projection-cursor:"
            + json.dumps(
                _CURSOR.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
            attention_policy_revision="attention-policy:test:1",
            deployment_mode_revision="shadow:test:1",
            generated_at=_NOW,
            expires_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
            candidate_set_hash="a" * 64,
            candidates=(dossier,),
            exposure_draw_ref="random-draw:test:1",
        ),
        current_context=CharacterAttentionContext(
            world_id="world:test",
            actor_ref="character:zhizhi",
            pinned_world_cursor="projection-cursor:"
            + json.dumps(
                _CURSOR.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
            world_logical_time=_NOW,
            situation=(),
            relevant_context=(),
            available_channels=(),
        ),
    )


def _live_request() -> LiveCharacterAttentionRequest:
    dossier = _shadow_request().window.candidates[0]
    material_json = canonical_external_perception_json(
        dossier.model_visible_material[0].model_dump(mode="json")
    )
    context = LiveCharacterAttentionContext(
        world_id="world:test",
        actor_ref="character:zhizhi",
        pinned_world_cursor=_CURSOR,
        world_logical_time=_NOW,
        situation=(),
        relevant_context=(),
        available_channels=(),
    )
    window = LivePerceptionWindow(
        window_id="window:live:test:1",
        attention_attempt_id="attention-attempt:live:test:1",
        opportunity_id="opportunity:live:test:1",
        world_id=context.world_id,
        actor_ref=context.actor_ref,
        pinned_world_cursor=_CURSOR,
        attention_policy_revision="attention-policy:live:test:1",
        deployment_mode="live",
        deployment_mode_revision="live:test:1",
        generated_at=_NOW,
        expires_at=datetime(2026, 8, 4, 13, 0, tzinfo=UTC),
        candidates=(dossier,),
        durable_snapshots=(
            FrozenExternalSignalSnapshot(
                snapshot_ref="external-snapshot:test:1",
                signal_revision_ref="signal-revision:test:1",
                source_id="source:test",
                upstream_publisher_ref="publisher:test",
                upstream_item_id="item:test:1",
                source_policy_revision="source-policy:test:1",
                source_payload_hash="d" * 64,
                normalized_hash="e" * 64,
                headline="A source-bound test report",
                observed_at=_NOW,
                expires_at=datetime(2026, 8, 4, 13, 0, tzinfo=UTC),
                model_visible_material_json=material_json,
                model_visible_material_hash=hashlib.sha256(material_json.encode()).hexdigest(),
                may_expose_to_character_model=True,
                may_quote=True,
                may_freeze_durable_snapshot=True,
            ),
        ),
        candidate_set_hash="c" * 64,
        exposure_draw_ref="random-draw:live:test:1",
    )
    return LiveCharacterAttentionRequest(
        attention_attempt_id=window.attention_attempt_id,
        retry_ordinal=0,
        selection_ordinal=0,
        window=window,
        current_context=context,
    )


def test_deployment_accepts_only_character_interior_for_character_attention() -> None:
    parameters = inspect.signature(build_external_world_perception_deployment).parameters

    assert "character_interior" in parameters
    assert "model" not in parameters
    assert "background_model" not in parameters


def test_qq_and_http_hosts_have_no_direct_external_attention_model_lane() -> None:
    for relative in (
        "src/companion_daemon/world_v2/qq_c2c_host.py",
        "src/companion_daemon/world_v2/http_capture_host.py",
    ):
        path = _ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name != "build_external_world_perception_deployment":
                continue
            keywords = {item.arg for item in node.keywords}
            assert "character_interior" in keywords
            assert "model" not in keywords
            assert "background_model" not in keywords


@pytest.mark.asyncio
async def test_external_attention_is_one_source_bound_character_interior_opportunity() -> None:
    from companion_daemon.world_v2.character_interior.external_perception import (
        character_interior_shadow_attention_port,
    )

    interior = _CapturingInterior()
    request = _shadow_request()

    model = character_interior_shadow_attention_port(interior)  # type: ignore[arg-type]
    result = await model.consider_attention(request)

    assert result.selections == ()
    assert len(interior.opportunities) == 1
    opportunity = interior.opportunities[0]
    assert opportunity.purpose == "external_perception_attention"
    assert opportunity.cursor == _CURSOR
    assert opportunity.inner_turn_ref.endswith(":retry:0")
    assert opportunity.capability_manifest is not None
    payload = json.loads(opportunity.capability_manifest.payload_json)
    assert payload["candidate_set_hash"] == "a" * 64
    assert payload["candidates"][0]["candidate_token"] == "candidate:test:1"
    assert "inner_life_binding" not in payload
    assert payload["pinned_cursor"] == _CURSOR.model_dump(mode="json")
    # Only the committed channel authority crosses into the production
    # projection's ledger evidence closure.  Sidecar draw/revision identities
    # remain content-bound inside the manifest payload.
    assert opportunity.capability_manifest.source_refs == ("capability:test",)
    assert payload["exposure_draw_ref"] == "random-draw:test:1"
    assert payload["candidates"][0]["exact_signal_revisions"] == ["signal-revision:test:1"]


@pytest.mark.asyncio
async def test_production_projection_closes_only_ledger_authority_not_sidecar_ids() -> None:
    from companion_daemon.world_v2.character_interior.external_perception import (
        character_interior_shadow_attention_port,
    )
    from companion_daemon.world_v2.character_interior.production import (
        _LedgerCapsuleInteriorProjection,
    )

    capture = _CapturingInterior()
    await character_interior_shadow_attention_port(capture).consider_attention(_shadow_request())
    opportunity = capture.opportunities[0]
    ledger = _LedgerAuthorityFixture()
    projection = _LedgerCapsuleInteriorProjection(
        ledger=ledger,  # type: ignore[arg-type]
        capsules=_CapsuleFixture(),  # type: ignore[arg-type]
        companion_actor_ref="character:zhizhi",
    )

    snapshot = await projection.project(subject=opportunity)

    # The same committed capability is already present in this fixture's
    # canonical Capsule, so projection reuses it without a second lookup.
    assert ledger.lookups == []
    assert "capability:test" in snapshot.source_refs
    assert "signal-revision:test:1" not in snapshot.source_refs
    assert "random-draw:test:1" not in snapshot.source_refs
    assert "external-snapshot:test:1" not in snapshot.source_refs
    assert "capability_evidence" not in snapshot.materials


@pytest.mark.asyncio
async def test_character_interior_technical_failure_cannot_become_no_selection() -> None:
    from companion_daemon.world_v2.character_interior.external_perception import (
        character_interior_shadow_attention_port,
    )

    interior = _CapturingInterior(technical_failure="role_faculty_unavailable")

    with pytest.raises(CharacterAttentionTechnicalFailure, match="role_faculty_unavailable"):
        await character_interior_shadow_attention_port(  # type: ignore[arg-type]
            interior
        ).consider_attention(_shadow_request())

    assert len(interior.opportunities) == 1


@pytest.mark.asyncio
async def test_real_structured_character_interior_owns_external_attention_choice() -> None:
    from companion_daemon.world_v2.character_interior.external_perception import (
        character_interior_shadow_attention_port,
    )

    response = json.dumps(
        {
            "status": "decision",
            "summary": "她对这一条产生了兴趣。",
            "attended_source_refs": ["character-core:test:1"],
            "decision": {
                "source_refs": [
                    "character-core:test:1",
                    "capability:test",
                ],
                "payload": {
                    "selections": [
                        {
                            "candidate_ref": "candidate:test:1",
                            "exact_signal_revision_refs": ["signal-revision:test:1"],
                            "selected_channel_ref": "channel:test",
                            "subjective_summary": "我刚好留意到了这条本地消息。",
                            "epistemic_notes": "",
                            "attended_context_refs": ["character-core:test:1"],
                        }
                    ]
                },
            },
            "recall_query": None,
            "proposals": [],
        },
        ensure_ascii=False,
    )
    author = _OneResponseAuthor(response)
    interior = CharacterInterior(
        projection=_CanonicalProjection(),
        role=StructuredCharacterRoleFaculty(
            model=author,
            model_id=author.model,
        ),
    )

    result = await character_interior_shadow_attention_port(interior).consider_attention(
        _shadow_request()
    )

    assert len(author.calls) == 1
    assert len(result.selections) == 1
    assert result.selections[0].candidate_ref == "candidate:test:1"
    provider_request = json.loads(author.calls[0][0][1]["content"])
    assert provider_request["inner_turn"]["purpose"] == ("external_perception_attention")
    assert (
        provider_request["capability_manifest"]["payload"]["candidates"][0]["candidate_token"]
        == "candidate:test:1"
    )


@pytest.mark.asyncio
async def test_live_attention_consumes_character_interior_decision_and_exact_model_audit() -> None:
    from companion_daemon.world_v2.character_interior.external_perception import (
        character_interior_live_attention_port,
    )

    request = _live_request()
    interior = _CapturingInterior()

    result = await character_interior_live_attention_port(  # type: ignore[arg-type]
        interior
    ).consider_attention(request)

    assert isinstance(result, AuditedLiveCharacterAttentionResult)
    assert result.decision.selections == ()
    assert result.model_result.attempt_id == request.attention_attempt_id
    assert result.model_result.capsule_id == request.window.candidate_set_hash
    assert result.model_result.audit_contract == "model-result-audit.7"
    audit = json.loads(result.model_result.audit_json)
    assert audit["model_id"] == "fixture-character-author"
    assert audit["request_hash"] == "a" * 64
    assert audit["response_hash"] == "b" * 64
    assert (
        audit["character_interior_lineage"]["snapshot_hash"]
        == (_inner_life_view()["snapshot_hash"])
    )
    assert len(interior.opportunities) == 1
    manifest = interior.opportunities[0].capability_manifest
    assert manifest.source_refs == ("capability:test",)
    payload = json.loads(manifest.payload_json)
    assert payload["durable_snapshots"][0]["snapshot_ref"] == ("external-snapshot:test:1")
    assert payload["durable_snapshots"][0]["model_visible_material_hash"] == (
        request.window.durable_snapshots[0].model_visible_material_hash
    )


@pytest.mark.asyncio
async def test_legacy_coordinator_reselection_cannot_open_a_second_interior_choice() -> None:
    from companion_daemon.world_v2.character_interior.external_perception import (
        character_interior_shadow_attention_port,
    )

    interior = _CapturingInterior()
    request = _shadow_request().model_copy(
        update={
            "selection_ordinal": 1,
            "validation_failure_codes": ("candidate_not_offered",),
            "rejected_result_json": '{"selections":[]}',
        }
    )

    with pytest.raises(
        CharacterAttentionTechnicalFailure,
        match="reselection_owned_by_character_interior",
    ):
        await character_interior_shadow_attention_port(  # type: ignore[arg-type]
            interior
        ).consider_attention(request)

    assert interior.opportunities == []


def test_deployment_has_no_legacy_attention_chat_adapter_or_raw_author_extraction() -> None:
    deployment = _ROOT / "src/companion_daemon/world_v2/external_world_perception/deployment.py"
    source = deployment.read_text(encoding="utf-8")

    assert "ChatCompletionLiveAttentionModel" not in source
    assert "ChatCompletionShadowAttentionModel" not in source
    assert "character_interior_live_attention_port" in source
    assert "character_interior_shadow_attention_port" in source
    bridge = (
        _ROOT / "src/companion_daemon/world_v2/character_interior/external_perception.py"
    ).read_text(encoding="utf-8")
    assert "_runtime_faculty" not in bridge
    assert "ChatCompletionLiveAttentionModel" not in bridge
    assert "ChatCompletionShadowAttentionModel" not in bridge


def test_external_perception_public_package_does_not_reexport_old_role_models() -> None:
    import companion_daemon.world_v2.external_world_perception as package

    assert "ChatCompletionLiveAttentionModel" not in package.__all__
    assert "ChatCompletionShadowAttentionModel" not in package.__all__
    assert "ProductionAttentionModelTrace" not in package.__all__
