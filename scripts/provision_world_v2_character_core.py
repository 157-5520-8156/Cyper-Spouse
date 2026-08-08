#!/usr/bin/env python3
"""Provision the World v2 character core (CharacterCoreInitialized) for one world.

Seeds the character's personality core from configs/character.yaml by having
the DeepSeek character model translate the personality/values text into the
ledger's typed CharacterCoreValues. Falls back to a deterministic mapping if
model generation fails, so provisioning never deadlocks.

Usage (run from the repository root):

    .venv/bin/python scripts/provision_world_v2_character_core.py \
        --database data/companion.sqlite \
        --world-id world:companion-v2:qq-c2c:geoff

WORLD_V2_ROOT_SIGNING_KEY_HEX is read from .env (deployment root already
pinned in actor_authority_events.ROOT_PUBLIC_KEYS).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nacl.signing import SigningKey  # noqa: E402

from companion_daemon.llm import DeepSeekChatModel  # noqa: E402
from companion_daemon.world_v2.actor_authority_events import (  # noqa: E402
    ROOT_PUBLIC_KEYS,
    ROOT_KEYSET_DIGEST,
    ROOT_KEYSET_VERSION,
    actor_authority_mutation_hash,
    root_envelope_signature_message,
)
from companion_daemon.world_v2.actor_authority_reducers import (  # noqa: E402
    ACTOR_AUTHORITY_POLICY_DIGEST,
)
from companion_daemon.world_v2.character_core_events import (  # noqa: E402
    CharacterCoreChangedPayload,
    character_core_evidence_refs,
    character_core_mutation_hash,
)
from companion_daemon.world_v2.character_core_reducers import (  # noqa: E402
    CHARACTER_CORE_POLICY_DIGEST,
    CHARACTER_CORE_POLICY_REFS,
    CHARACTER_CORE_POLICY_VERSION,
    _canonical_hash,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key  # noqa: E402
from companion_daemon.world_v2.schemas import (  # noqa: E402
    CHARACTER_CORE_COORDINATE_CATALOG_DIGEST,
    CHARACTER_CORE_COORDINATE_CATALOG_VERSION,
    CharacterCoreAxis,
    CharacterCoreImmutableIdentity,
    CharacterCoreOperatorAuthorityBinding,
    CharacterCoreOperatorGoverned,
    CharacterCoreOrigin,
    CharacterCoreProjection,
    CharacterCoreProposalProjection,
    CharacterCoreProposedMutation,
    CharacterCoreSlowEvolving,
    CharacterCoreValuePriority,
    CharacterCoreValues,
    character_core_semantic_fingerprint,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger  # noqa: E402
from companion_daemon.world_v2.schemas import WorldEvent  # noqa: E402

NOW = datetime.now(UTC)

TRAIT_AXES = (
    "agreeableness",
    "assertiveness",
    "autonomy",
    "conscientiousness",
    "curiosity",
    "emotional_stability",
    "extraversion",
    "openness",
    "warmth",
)
VALUE_REFS = (
    "value:autonomy",
    "value:care",
    "value:growth",
    "value:honesty",
    "value:privacy",
    "value:reciprocity",
)
PREFERENCE_REFS = (
    "preference:direct_communication",
    "preference:independent_time",
    "preference:playful_banter",
    "preference:quiet_reflection",
    "preference:shared_routines",
)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


def default_values() -> dict[str, object]:
    """Deterministic mapping of character.yaml personality to typed values."""
    return {
        "slow_evolving": {
            "trait_axes": [
                {"axis_code": "agreeableness", "value_bp": 6200},
                {"axis_code": "assertiveness", "value_bp": 3800},
                {"axis_code": "autonomy", "value_bp": 6800},
                {"axis_code": "conscientiousness", "value_bp": 5600},
                {"axis_code": "curiosity", "value_bp": 6400},
                {"axis_code": "emotional_stability", "value_bp": 6000},
                {"axis_code": "extraversion", "value_bp": 3200},
                {"axis_code": "openness", "value_bp": 7000},
                {"axis_code": "warmth", "value_bp": 5600},
            ],
            "value_priorities": [
                {"value_ref": "value:autonomy", "priority_bp": 7500},
                {"value_ref": "value:honesty", "priority_bp": 7000},
                {"value_ref": "value:care", "priority_bp": 6000},
                {"value_ref": "value:privacy", "priority_bp": 6500},
                {"value_ref": "value:reciprocity", "priority_bp": 5500},
                {"value_ref": "value:growth", "priority_bp": 5000},
            ],
            "preference_refs": [
                "preference:quiet_reflection",
                "preference:independent_time",
                "preference:direct_communication",
            ],
            "autonomy_style": "self_directed",
            "attachment_tendency": "balanced",
            "conflict_style": "deliberative",
            "privacy_tendency": "selective",
        }
    }


_GENERATE_PROMPT = """你是角色核心设定助手。下面是一份角色的完整人格描述。请把它翻译成结构化的角色核心数值。

人格描述：
{personality}

请严格输出一个 JSON 对象（不要 Markdown、不要额外文字），结构如下：
{{
  "slow_evolving": {{
    "trait_axes": [
      {{"axis_code": "agreeableness", "value_bp": 0}},
      {{"axis_code": "assertiveness", "value_bp": 0}},
      {{"axis_code": "autonomy", "value_bp": 0}},
      {{"axis_code": "conscientiousness", "value_bp": 0}},
      {{"axis_code": "curiosity", "value_bp": 0}},
      {{"axis_code": "emotional_stability", "value_bp": 0}},
      {{"axis_code": "extraversion", "value_bp": 0}},
      {{"axis_code": "openness", "value_bp": 0}},
      {{"axis_code": "warmth", "value_bp": 0}}
    ],
    "value_priorities": [
      {{"value_ref": "value:autonomy", "priority_bp": 0}},
      {{"value_ref": "value:care", "priority_bp": 0}},
      {{"value_ref": "value:growth", "priority_bp": 0}},
      {{"value_ref": "value:honesty", "priority_bp": 0}},
      {{"value_ref": "value:privacy", "priority_bp": 0}},
      {{"value_ref": "value:reciprocity", "priority_bp": 0}}
    ],
    "preference_refs": ["preference:direct_communication", "preference:independent_time", "preference:playful_banter", "preference:quiet_reflection", "preference:shared_routines"],
    "autonomy_style": "self_directed",
    "attachment_tendency": "balanced",
    "conflict_style": "deliberative",
    "privacy_tendency": "selective"
  }}
}}

规则：
- value_bp 和 priority_bp 都是 0-10000 的整数，10000 表示极强
- trait_axes 必须恰好包含上述 9 个 axis_code，每个都要有值
- value_priorities 必须恰好包含上述 6 个 value_ref
- preference_refs 从给定 5 个候选中选择与角色最匹配的 2-4 个
- autonomy_style ∈ self_directed|interdependent; attachment_tendency ∈ balanced|slow_to_attach|anxious|avoidant; conflict_style ∈ deliberative|direct|avoidant|collaborative; privacy_tendency ∈ selective|open|guarded
- 严格按人格描述推断，不要臆造性格之外的东西"""


def generate_values(personality_text: str, api_key: str) -> dict[str, object] | None:
    try:
        model = DeepSeekChatModel(
            api_key=api_key,
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            thinking_enabled=False,
        )
        import asyncio

        async def _run() -> str:
            return await model.complete_json(
                [
                    {"role": "system", "content": "你是角色核心设定助手，输出严格 JSON。"},
                    {"role": "user", "content": _GENERATE_PROMPT.format(personality=personality_text)},
                ],
                temperature=0.4,
            )

        raw = asyncio.run(_run())
        parsed = json.loads(raw)
        if "slow_evolving" not in parsed:
            return None
        return parsed
    except Exception as exc:  # noqa: BLE001
        print(f"model generation failed ({type(exc).__name__}): {str(exc)[:120]}", file=sys.stderr)
        return None


def build_values(gen: dict[str, object] | None) -> CharacterCoreValues:
    se = (gen or {}).get("slow_evolving") or {}
    trait_bp: dict[str, int] = {}
    for item in se.get("trait_axes") or []:
        if isinstance(item, dict):
            code = item.get("axis_code")
            if code in TRAIT_AXES and isinstance(item.get("value_bp"), int):
                trait_bp[code] = item["value_bp"]
    for code in TRAIT_AXES:
        trait_bp.setdefault(code, 5000)
    priorities: dict[str, int] = {}
    for item in se.get("value_priorities") or []:
        if isinstance(item, dict):
            ref = item.get("value_ref")
            if ref in VALUE_REFS and isinstance(item.get("priority_bp"), int):
                priorities[ref] = item["priority_bp"]
    for ref in VALUE_REFS:
        priorities.setdefault(ref, 5000)
    prefs = [
        p for p in (se.get("preference_refs") or [])
        if p in PREFERENCE_REFS
    ]
    if not prefs:
        prefs = ["preference:quiet_reflection", "preference:independent_time"]

    def _pick(key: str, allowed: tuple[str, ...], default: str) -> str:
        value = se.get(key)
        return value if isinstance(value, str) and value in allowed else default

    values = CharacterCoreValues(
        immutable_identity=CharacterCoreImmutableIdentity(
            canonical_identity_refs=("identity:companion",),
            continuity_anchor_refs=("continuity:world",),
        ),
        operator_governed=CharacterCoreOperatorGoverned(
            role_refs=("role:virtual-companion",),
            non_negotiable_value_refs=("value:autonomy", "value:honesty"),
            hard_boundary_refs=("boundary:no-coercion",),
        ),
        slow_evolving=CharacterCoreSlowEvolving(
            coordinate_catalog_version=CHARACTER_CORE_COORDINATE_CATALOG_VERSION,
            coordinate_catalog_digest=CHARACTER_CORE_COORDINATE_CATALOG_DIGEST,
            trait_axes=tuple(
                CharacterCoreAxis(axis_code=code, value_bp=trait_bp[code])
                for code in TRAIT_AXES
            ),
            value_priorities=tuple(
                CharacterCoreValuePriority(value_ref=ref, priority_bp=priorities[ref])
                for ref in VALUE_REFS
            ),
            preference_refs=tuple(sorted(prefs)),
            autonomy_style=_pick(
                "autonomy_style", ("dependent", "collaborative", "self_directed"), "self_directed"
            ),
            attachment_tendency=_pick(
                "attachment_tendency", ("guarded", "balanced", "connection_seeking"), "balanced"
            ),
            conflict_style=_pick(
                "conflict_style", ("avoidant", "deliberative", "direct"), "deliberative"
            ),
            privacy_tendency=_pick(
                "privacy_tendency", ("open", "selective", "reserved"), "selective"
            ),
        ),
        privacy_class="private",
    )
    return values


def _ledger_event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    *,
    world_id: str,
    at: datetime,
    actor: str = "system:character-core-provisioning",
    source: str = "world-v2:character-core-provisioning",
) -> WorldEvent:
    identity = domain_idempotency_key(event_type=event_type, world_id=world_id, payload=payload)
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        event_type=event_type,
        world_id=world_id,
        logical_time=at,
        created_at=at,
        actor=actor,
        source=source,
        trace_id="trace:character-core-provisioning",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:character-core-provisioning",
        idempotency_key=identity,
        payload=payload,
    )


def _core_projection(
    values: CharacterCoreValues,
    *,
    revision: int,
    event_ref: str,
    at: datetime,
) -> CharacterCoreProjection:
    origin = CharacterCoreOrigin(
        change_id=f"change:core:{revision}",
        transition_id=f"transition:core:{revision}",
        policy_refs=CHARACTER_CORE_POLICY_REFS,
        accepted_event_ref=event_ref,
    )
    return CharacterCoreProjection(
        core_id="core:companion",
        actor_ref="agent:companion",
        entity_revision=revision,
        semantic_fingerprint=character_core_semantic_fingerprint(
            core_id="core:companion",
            actor_ref="agent:companion",
            values=values,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        created_at=at,
        updated_at=at,
    )


def _mutation(
    after: CharacterCoreProjection,
    *,
    operation: str,
    lane: str,
    changed: tuple[str, ...],
    operator: CharacterCoreOperatorAuthorityBinding,
    evaluated_world_revision: int,
) -> CharacterCoreChangedPayload:
    raw: dict[str, object] = {
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": (),
        "policy_refs": CHARACTER_CORE_POLICY_REFS,
        "acceptance_id": f"acceptance:{after.origin.transition_id}",
        "proposal_id": f"proposal:{after.origin.transition_id}",
        "evaluated_world_revision": evaluated_world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": operation,
        "authority_lane": lane,
        "changed_field_classes": changed,
        "core_before": None,
        "core_after": after,
        "evidence_window": None,
        "operator_authority": operator,
        "compensation_target": None,
        "policy_version": CHARACTER_CORE_POLICY_VERSION,
        "policy_digest": CHARACTER_CORE_POLICY_DIGEST,
    }
    raw["evidence_refs"] = character_core_evidence_refs(raw)
    raw["accepted_change_hash"] = character_core_mutation_hash(raw)
    return CharacterCoreChangedPayload.model_validate(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--character", default="configs/character.yaml")
    parser.add_argument("--no-generate", action="store_true", help="skip model generation")
    args = parser.parse_args()

    load_env(Path(".env"))
    signing_key_hex = os.environ.get("WORLD_V2_ROOT_SIGNING_KEY_HEX", "").strip()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not signing_key_hex:
        print("WORLD_V2_ROOT_SIGNING_KEY_HEX is required", file=sys.stderr)
        return 2

    ledger = SQLiteWorldLedger(path=Path(args.database), world_id=args.world_id)
    try:
        signing = SigningKey(bytes.fromhex(signing_key_hex))
        verify_hex = signing.verify_key.encode().hex()
        root_key_id = next(
            (kid for kid, pub in ROOT_PUBLIC_KEYS.items() if pub == verify_hex),
            None,
        )
        if root_key_id is None:
            raise ValueError("supplied signing key does not match any installed deployment root")
        projection = ledger.project()
        if projection.character_core is not None:
            print(f"character core already present: {projection.character_core.core_id}")
            return 0
        logical_time = projection.logical_time or NOW

        # 1) operator authority for character-core governance
        authorities = {a.authority_id for a in projection.actor_authorities}
        operator_binding: CharacterCoreOperatorAuthorityBinding | None = None
        if "authority:character-core" not in authorities:
            transition_id = "transition:authority:character-core"
            payload: dict[str, object] = {
                "world_id": args.world_id,
                "authority_id": "authority:character-core",
                "transition_id": transition_id,
                "operation": "bootstrap",
                "expected_entity_revision": 0,
                "values_before": None,
                "values_after": {
                    "principal_ref": "operator:character-core",
                    "principal_kind": "deployment_operator",
                    "credential_ref": "credential:character-core",
                    "allowed_operations": ["character_core_governance"],
                    "valid_from": logical_time.isoformat(),
                    "expires_at": None,
                    "status": "active",
                },
                "policy_version": "actor-authority-policy.1",
                "policy_digest": ACTOR_AUTHORITY_POLICY_DIGEST,
                "changed_at": logical_time.isoformat(),
                "compensates_transition_id": None,
                "root_proof": {
                    "keyset_version": ROOT_KEYSET_VERSION,
                    "keyset_digest": ROOT_KEYSET_DIGEST,
                    "root_key_id": root_key_id,
                    "nonce": "nonce:character-core:bootstrap",
                    "signed_mutation_hash": "0" * 64,
                    "signature_hex": "0" * 128,
                },
            }
            mutation_hash = actor_authority_mutation_hash(payload)
            payload["root_proof"]["signed_mutation_hash"] = mutation_hash
            event_id = "event:authority:character-core"
            identity = domain_idempotency_key(
                event_type="ActorAuthorityBootstrapped",
                world_id=args.world_id,
                payload=payload,
            )
            payload["root_proof"]["signature_hex"] = signing.sign(
                root_envelope_signature_message(
                    schema_version="world-v2.1",
                    world_id=args.world_id,
                    event_type="ActorAuthorityBootstrapped",
                    event_id=event_id,
                    actor="system:character-core-provisioning",
                    source="world-v2:character-core-provisioning",
                    logical_time=logical_time,
                    created_at=logical_time,
                    trace_id="trace:character-core-provisioning",
                    causation_id=f"cause:{event_id}",
                    correlation_id="correlation:character-core-provisioning",
                    idempotency_key=identity,
                    mutation_hash=mutation_hash,
                )
            ).signature.hex()
            ev = _ledger_event(
                event_id,
                "ActorAuthorityBootstrapped",
                payload,
                world_id=args.world_id,
                at=logical_time,
            )
            ledger.commit(
                (ev,),
                expected_world_revision=projection.world_revision,
                expected_deliberation_revision=projection.deliberation_revision,
            )
            projection = ledger.project()
            print("committed authority:authority:character-core")

        # operator binding from the authority projection (real committed ref)
        committed_refs = {
            ref.event_id: ref for ref in projection.committed_world_event_refs
        }
        authority_ref = committed_refs["event:authority:character-core"]
        core_authority = next(
            a for a in projection.actor_authorities
            if a.authority_id == "authority:character-core"
        )
        operator_binding = CharacterCoreOperatorAuthorityBinding(
            authority_id=core_authority.authority_id,
            authority_revision=core_authority.entity_revision,
            principal_ref=core_authority.values.principal_ref,
            authority_event_ref=authority_ref.event_id,
            authority_world_revision=authority_ref.world_revision,
            authority_payload_hash=authority_ref.payload_hash,
            authority_values_hash=_canonical_hash(core_authority.values),
            authority_policy_digest=core_authority.policy_digest,
            authorization_contract="deployment-actor-authority:character-core.1",
        )

        # 2) values: model-generated or deterministic fallback
        generated: dict[str, object] | None = None
        if not args.no_generate and api_key:
            character_path = Path(args.character)
            personality = ""
            if character_path.exists():
                text = character_path.read_text()
                for key in ("personality:", "values:", "speech:"):
                    idx = text.find(key)
                    if idx >= 0:
                        chunk = text[idx:].split("\n\n", 1)[0]
                        personality += chunk + "\n"
            generated = generate_values(personality or "温和、慢热、有独立判断。", api_key)
            if generated:
                print("character core values generated by model")
            else:
                print("model generation failed; using deterministic mapping", file=sys.stderr)
        else:
            print("using deterministic mapping (no-generate or missing key)")
        values = build_values(generated)

        # 3) initialize: proposal -> acceptance -> CharacterCoreInitialized
        event_ref = "event:core:ledger-initialize"
        initialized = _core_projection(values, revision=1, event_ref=event_ref, at=logical_time)
        payload = _mutation(
            initialized,
            operation="initialize",
            lane="operator_initialize",
            changed=("immutable_identity", "operator_governed", "privacy_class", "slow_evolving"),
            operator=operator_binding,
            evaluated_world_revision=projection.world_revision,
        )

        proposal_projection = CharacterCoreProposalProjection(
            proposal_id=payload.proposal_id,
            proposal_kind="character_core_revision",
            proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:character-core.1",
            transition_kind=payload.operation,
            change_id=payload.change_id,
            transition_id=payload.transition_id,
            evaluated_world_revision=payload.evaluated_world_revision,
            expected_entity_revision=payload.expected_entity_revision,
            proposed_change_hash=payload.accepted_change_hash,
            evidence_refs=payload.evidence_refs,
            policy_refs=payload.policy_refs,
            proposed_mutation=CharacterCoreProposedMutation(
                event_type="CharacterCoreInitialized",
                payload_json=json.dumps(
                    payload.model_dump(mode="json"),
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ),
            ),
        )
        proposal_ev = _ledger_event(
            f"event:{payload.proposal_id}",
            "ProposalRecorded",
            proposal_projection.model_dump(mode="json"),
            world_id=args.world_id,
            at=logical_time,
        )
        acceptance_ev = _ledger_event(
            f"event:{payload.acceptance_id}",
            "AcceptanceRecorded",
            {
                "acceptance_id": payload.acceptance_id,
                "status": "accepted",
                "proposal_id": payload.proposal_id,
                "evaluated_world_revision": payload.evaluated_world_revision,
                "accepted_change_id": payload.change_id,
                "accepted_change_hash": payload.accepted_change_hash,
            },
            world_id=args.world_id,
            at=logical_time,
        )
        core_ev = _ledger_event(
            payload.core_after.origin.accepted_event_ref,
            "CharacterCoreInitialized",
            payload.model_dump(mode="json"),
            world_id=args.world_id,
            at=logical_time,
        )
        ledger.commit(
            (proposal_ev,),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )
        projection = ledger.project()
        ledger.commit(
            (acceptance_ev, core_ev),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )
        projection = ledger.project()
        print(f"committed CharacterCoreInitialized: {projection.character_core.core_id} rev={projection.character_core.entity_revision}")
        return 0
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
