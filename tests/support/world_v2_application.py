"""Explicit fixture composition for the unified Character Interior.

This module is deliberately not a compatibility adapter.  Tests either pass a
fully composed :class:`CharacterInterior` to the application builder or build
one from a single inbound-cognition Faculty plus explicitly named purpose
Faculties.  There is no translation from retired protagonist model arguments.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from companion_daemon.world_v2.character_interior import CharacterInterior
from companion_daemon.world_v2.character_interior.authority import (
    _DeferredInteriorAuthority,
)
from companion_daemon.world_v2.character_interior.author_identity import (
    character_semantic_author_identity,
    supplied_semantic_author_id,
)
from companion_daemon.world_v2.character_interior.inbound_turn import (
    InboundTurnFaculty,
)
from companion_daemon.world_v2.character_interior.production import (
    _DeferredProjection,
)
from companion_daemon.world_v2.character_interior.ports import _RoleResultContractError
from companion_daemon.world_v2.character_interior.structured_role import (
    StructuredCharacterRoleFaculty,
)
from companion_daemon.world_v2.fact_memory_draft import materialize_fact_memory_draft
from companion_daemon.world_v2.memory_withdrawal_review import (
    materialize_memory_withdrawal_review_draft,
)
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplication,
    build_sqlite_world_v2_turn_application,
)


def _hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class _FixtureIdentityMappedInboundAuthor:
    """Attach test-only identity evidence without weakening production checks."""

    def __init__(self, *, author: object, identity: dict[str, str]) -> None:
        self._author = author
        self._identity = dict(identity)

    @property
    def author_identity(self) -> dict[str, str]:
        return dict(self._identity)

    def __getattr__(self, name: str) -> object:
        return getattr(self._author, name)


def _fixture_identity_mapped_inbound_author(author: object) -> object:
    """Give fixture authors a stable production-shaped semantic identity.

    This conversion exists only in ``tests/support``.  The production binder
    still rejects authors that do not themselves expose verified identity
    evidence.  Fixtures without model metadata receive an identity namespaced
    to their test class; that mapping is never installed on a live author.
    """

    supplied = getattr(author, "author_identity", None)
    if callable(supplied):
        supplied = supplied()
    if isinstance(supplied, Mapping) and supplied_semantic_author_id(supplied) is not None:
        return author

    nested_model = getattr(author, "_model", None)
    model_id = str(
        getattr(author, "_model_id", "")
        or getattr(author, "model", "")
        or getattr(nested_model, "model", "")
    ).strip()
    if not model_id:
        author_type = type(author)
        model_id = (
            "fixture-test-author:"
            f"{author_type.__module__}.{author_type.__qualname__}"
        )
    model_version = str(
        getattr(author, "VERSION", "")
        or getattr(author, "model_version", "")
        or getattr(nested_model, "model_version", "")
        or "fixture-author.1"
    ).strip()
    identity = character_semantic_author_identity(
        model_id=model_id,
        model_version=model_version,
    )
    return _FixtureIdentityMappedInboundAuthor(author=author, identity=identity)


class _FixturePrimaryRoleModel:
    """Explicit test-only terminal for unused CharacterInterior purposes."""

    model = "fixture-character-role"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        request = json.loads(messages[-1]["content"])
        phase = request["inner_turn"]["phase"]
        return json.dumps(
            {
                "status": "no_change" if phase == "experience" else "silent",
                "summary": (
                    "Fixture character chose no private transition."
                    if phase == "experience"
                    else "Fixture character chose silence."
                ),
                "attended_source_refs": [],
                "decision": None,
                "recall_query": None,
                "proposals": [],
            },
            ensure_ascii=False,
        )


class _FixtureStructuredCharacterRole(StructuredCharacterRoleFaculty):
    """One production-shaped role with explicit test-only purpose delegates.

    Production now registers every reviewed purpose on the sole
    :class:`StructuredCharacterRoleFaculty`.  Test doubles therefore cannot be
    installed as sibling purpose Faculties without recreating duplicate
    semantic owners.  Keep them behind this one role boundary and dispatch
    only the exact purpose each fixture declared.
    """

    def __init__(
        self,
        *,
        purpose_faculties: tuple[object, ...],
        semantic_author_identity: object,
    ) -> None:
        delegates: dict[str, object] = {}
        for faculty in purpose_faculties:
            purposes = tuple(getattr(faculty, "purposes", ()))
            if not purposes:
                raise ValueError("fixture purpose faculty must declare a purpose")
            for purpose in purposes:
                normalized = str(purpose).strip()
                if not normalized:
                    raise ValueError("fixture purpose faculty contains an empty purpose")
                if normalized in delegates:
                    raise ValueError(
                        f"duplicate fixture character purpose delegate: {normalized}"
                    )
                delegates[normalized] = faculty
        super().__init__(
            model=_FixturePrimaryRoleModel(),
            model_id=_FixturePrimaryRoleModel.model,
        )
        if not isinstance(semantic_author_identity, dict):
            raise ValueError("fixture inbound author identity is unavailable")
        semantic_author_id = semantic_author_identity.get("semantic_author_id")
        if not isinstance(semantic_author_id, str) or not semantic_author_id:
            raise ValueError("fixture inbound author identity is not production-shaped")
        self._fixture_semantic_author_identity = dict(semantic_author_identity)
        unknown = set(delegates) - set(self.purposes)
        if unknown:
            raise ValueError(
                "fixture purpose delegate lacks a production contract: "
                + ", ".join(sorted(unknown))
            )
        self._fixture_delegates = delegates

    @property
    def author_identity(self) -> object:
        return {
            **self._fixture_semantic_author_identity,
            "name": self.name,
            "version": self.VERSION,
        }

    async def experience(self, request: object) -> object:
        delegate = self._fixture_delegates.get(str(request.purpose))
        if delegate is not None:
            return await delegate.experience(request)  # type: ignore[attr-defined]
        return await super().experience(request)  # type: ignore[arg-type]

    async def consider(self, request: object) -> object:
        delegate = self._fixture_delegates.get(str(request.purpose))
        if delegate is not None:
            return await delegate.consider(request)  # type: ignore[attr-defined]
        return await super().consider(request)  # type: ignore[arg-type]


class _MediaSelectionPurposeFixtureFaculty:
    """Test provider hosted behind the Interior media-selection purpose."""

    name = "fixture-media-selection-author"
    purposes = ("media_selection",)

    def __init__(self, provider: object) -> None:
        self._provider = provider

    async def experience(self, request: object) -> dict[str, object]:
        return {
            "status": "no_change",
            "summary": "fixture media capability observed",
            "attended_source_refs": (),
        }

    async def consider(self, request: object) -> dict[str, object]:
        manifest = request.capability_manifest
        assert manifest is not None
        payload = dict(manifest.payload)
        request_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw = await self._provider.complete(  # type: ignore[attr-defined]
            [
                {"role": "system", "content": "fixture media selection"},
                {"role": "user", "content": request_json},
            ],
            temperature=0.2,
        )
        value = json.loads(raw)
        payload: dict[str, object] = {
            "contract": "character-interior-media-selection-decision.1",
            "decision": value["decision"],
        }
        if value.get("decision") == "select":
            payload["selected_token"] = value["token"]
        decision: dict[str, object] = {
            "contract": "character-interior-purpose-decision.1",
            "purpose": "media_selection",
            "source_refs": list(manifest.source_refs),
            "capability_ref": manifest.capability_ref,
            "capability_payload_hash": manifest.payload_hash,
            "payload": payload,
        }
        request_hash = _hash_text(request_json)
        call_id = (
            "fixture-media-selection-call:"
            + request.inner_turn_id
            + f":{request.correction_ordinal}"
        )
        return {
            "status": "decision",
            "summary": "fixture character media choice",
            "attended_source_refs": (),
            "decision": decision,
            "author_lineage": {
                "contract": "character-interior-author-lineage.1",
                "model_id": str(
                    getattr(self._provider, "model", "fixture-media-selection")
                ),
                "model_version": "fixture.1",
                "model_call_id": call_id,
                "request_hash": request_hash,
                "response_hash": _hash_text(raw),
                "attempt_ordinal": request.correction_ordinal,
                "parent_model_call_id": (
                    None
                    if request.correction_ordinal == 0
                    else "fixture-media-selection-call:" + request.inner_turn_id + ":0"
                ),
            },
        }


class _StructuredPurposeFixtureFaculty:
    """Test provider hosted behind one explicit Interior purpose boundary."""

    requires_author_lineage = True

    def __init__(self, *, purpose: str, provider: object) -> None:
        self.name = f"fixture-character-author:{purpose}"
        self.purposes = (purpose,)
        self._purpose = purpose
        self._provider = provider

    async def experience(self, request: object) -> dict[str, object]:
        del request
        return {"status": "no_change", "summary": "fixture capability observed"}

    async def consider(self, request: object) -> dict[str, object]:
        manifest = request.capability_manifest
        assert manifest is not None
        capability = dict(manifest.payload)
        system_content = (
            "Classify whether this lived Experience from your own life should become "
            "a source-bound retrieval memory. Return exactly one JSON object."
            if self._purpose == "experience_memory_retention"
            else "Fixture character purpose author. Return exactly one JSON object."
        )
        messages = [
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": json.dumps(capability, ensure_ascii=False, sort_keys=True),
            },
        ]
        try:
            complete_json = getattr(self._provider, "complete_json", None)
            if callable(complete_json):
                raw = await complete_json(messages, temperature=0.15)
            else:
                raw = await self._provider.complete(messages, temperature=0.15)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("fixture purpose result must be an object")
            if self._purpose in {
                "fact_memory_retention",
                "experience_memory_retention",
            }:
                materialize_fact_memory_draft(raw)
                payload = value
            elif self._purpose == "memory_withdrawal_review":
                reviewed = materialize_memory_withdrawal_review_draft(raw)
                if reviewed.disposition not in capability["offered_tokens"]:
                    raise ValueError("fixture selected an unavailable disposition")
                payload = {"selected_token": reviewed.disposition}
            else:
                payload = {"completion": value}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _RoleResultContractError(
                "fixture_purpose_result_invalid",
                detail=str(exc),
                response_hash=(_hash_text(raw) if isinstance(locals().get("raw"), str) else None),
            ) from exc
        payload_contract = {
            "life_development_choice": "character-interior-life-development-choice.1",
            "fact_memory_retention": "character-interior-fact-memory-retention.1",
            "experience_memory_retention": (
                "character-interior-experience-memory-retention.1"
            ),
            "memory_withdrawal_review": (
                "character-interior-memory-withdrawal-review.1"
            ),
        }[self._purpose]
        return {
            "status": "decision",
            "summary": f"fixture character decision for {self._purpose}",
            "attended_source_refs": (),
            "decision": {
                "contract": "character-interior-purpose-decision.1",
                "purpose": self._purpose,
                "source_refs": list(manifest.source_refs),
                "capability_ref": manifest.capability_ref,
                "capability_payload_hash": manifest.payload_hash,
                "payload": {"contract": payload_contract, **payload},
            },
            "author_lineage": {
                "contract": "character-interior-author-lineage.1",
                "model_id": str(getattr(self._provider, "model", "fixture-purpose")),
                "model_version": "fixture.1",
                "model_call_id": f"model-call:fixture:{request.inner_turn_id}:{self._purpose}",
                "request_hash": _hash_text(json.dumps(messages)),
                "response_hash": _hash_text(raw),
                "attempt_ordinal": 0,
                "parent_model_call_id": None,
            },
        }


class _StructuredRolePurposeFixtureFaculty:
    """Expose one production role contract without registering unrelated lanes."""

    requires_author_lineage = True

    def __init__(self, *, purpose: str, provider: object) -> None:
        self.name = f"fixture-structured-character-author:{purpose}"
        self.purposes = (purpose,)
        model_id = str(getattr(provider, "model", f"fixture-{purpose}"))
        self._delegate = StructuredCharacterRoleFaculty(
            model=provider,  # type: ignore[arg-type]
            model_id=model_id,
        )

    @property
    def author_identity(self) -> object:
        return self._delegate.author_identity

    async def experience(self, request: object) -> object:
        return await self._delegate.experience(request)  # type: ignore[arg-type]

    async def consider(self, request: object) -> object:
        return await self._delegate.consider(request)  # type: ignore[arg-type]


def compose_fixture_character_purpose(
    *,
    purpose: str,
    provider: object,
) -> object:
    """Compose one explicitly named Character-Interior purpose Faculty."""

    if purpose == "media_selection":
        return _MediaSelectionPurposeFixtureFaculty(provider)
    if purpose in {
        "proactive_contact",
        "activity_lifecycle_choice",
        "outcome_selection",
    }:
        return _StructuredRolePurposeFixtureFaculty(
            purpose=purpose,
            provider=provider,
        )
    if purpose not in {
        "life_development_choice",
        "fact_memory_retention",
        "experience_memory_retention",
        "memory_withdrawal_review",
    }:
        raise ValueError(f"unsupported fixture CharacterInterior purpose: {purpose}")
    return _StructuredPurposeFixtureFaculty(purpose=purpose, provider=provider)


def compose_fixture_character_interior(
    *,
    inbound_author: object,
    purpose_faculties: tuple[object, ...] = (),
) -> CharacterInterior:
    """Compose one provider-free fixture Interior around its sole inbound author.

    The fixture has the same one-author topology as production.  Purpose
    Faculties may add bounded capabilities, but cannot create another inbound
    author.
    """

    mapped_inbound_author = _fixture_identity_mapped_inbound_author(inbound_author)
    inbound = InboundTurnFaculty(author=mapped_inbound_author)
    return CharacterInterior(
        projection=_DeferredProjection(),
        role=_FixtureStructuredCharacterRole(
            purpose_faculties=purpose_faculties,
            semantic_author_identity=inbound.author_identity,
        ),
        faculties=(inbound,),
        authority=_DeferredInteriorAuthority(),
    )


def build_sqlite_world_v2_test_application(
    *,
    character_interior: CharacterInterior,
    **builder_kwargs: Any,
) -> WorldV2TurnApplication:
    """Build a test application through the production Interior-only seam."""

    return build_sqlite_world_v2_turn_application(
        character_interior=character_interior,
        **builder_kwargs,
    )


__all__ = [
    "build_sqlite_world_v2_test_application",
    "compose_fixture_character_interior",
    "compose_fixture_character_purpose",
]
