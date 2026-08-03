"""Fail-closed production composition for the external perception Hub."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from pathlib import Path
from typing import Callable, Literal

import httpx

from companion_daemon.config import Settings

from ..external_perception_acceptance import (
    ExternalPerceptionAcceptanceRuntime,
    ExternalPerceptionDeliveryProducer,
)
from ..ledger_context_resolver import context_capsule_compiler_from_ledger
from ..sqlite_ledger import SQLiteWorldLedger
from .authorized_search import AcceptedWebSearchResultAdapter
from .contracts import (
    LiveAttentionRuntime,
    ShadowAttentionRuntime,
    SourceProfile,
    WorldPerceptionHub,
)
from .hub import SQLiteWorldPerceptionHub
from .live_acceptance import (
    LifeEcologyWakePort,
    LifeWakingExternalPerceptionAcceptance,
    ProducerBackedExternalPerceptionAcceptance,
)
from .production_attention import (
    CapsuleBackedLiveAttentionContextPort,
    CapsuleBackedShadowAttentionContextPort,
    ChatCompletionLiveAttentionModel,
    ChatCompletionShadowAttentionModel,
    LedgerPublicInformationChannelPort,
    LiveAttentionChannelPort,
)
from .registry import (
    build_production_source_profiles,
    external_perception_registry_health,
    load_external_perception_source_registry,
)


DeploymentStatus = Literal["disabled", "ready"]
DeploymentReason = Literal[
    "mode_off",
    "registry_not_configured",
    "no_enabled_sources",
    "channel_not_configured",
    "ready",
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _model_id(model: object) -> str:
    for attribute in ("model_id", "model", "MODEL"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()[:256]
    return type(model).__name__[:256]


class _OwnedWorldPerceptionHub:
    """Delegate the deep Hub seam while closing all deployment-owned resources."""

    def __init__(
        self,
        *,
        hub: SQLiteWorldPerceptionHub,
        http_client: httpx.AsyncClient | None,
        world_ledger: SQLiteWorldLedger | None,
    ) -> None:
        self._hub = hub
        self._http_client = http_client
        self._world_ledger = world_ledger
        self._closed = False

    async def advance_once(self, *, observed_at: datetime):
        return await self._hub.advance_once(observed_at=observed_at)

    def health_snapshot(self):
        return self._hub.health_snapshot()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._hub.aclose()
        finally:
            try:
                if self._world_ledger is not None:
                    self._world_ledger.close()
            finally:
                if self._http_client is not None:
                    await self._http_client.aclose()


@dataclass(frozen=True, slots=True)
class ExternalPerceptionDeployment:
    status: DeploymentStatus
    reason: DeploymentReason
    hub: WorldPerceptionHub | None = None
    registry_revision: str | None = None
    registry_content_hash: str | None = None
    registry_health: dict[str, object] | None = None


def build_external_world_perception_deployment(
    *,
    settings: Settings,
    world_id: str,
    actor_ref: str,
    model: object,
    life: LifeEcologyWakePort,
    channel_port: LiveAttentionChannelPort | None = None,
    authorized_search_profile: SourceProfile | None = None,
    wall_clock: Callable[[], datetime] = _utc_now,
) -> ExternalPerceptionDeployment:
    """Build no network/model authority unless every required input is explicit."""

    mode = settings.world_v2_external_perception_mode
    registry_path = settings.world_v2_external_perception_source_registry_path
    if mode == "off":
        return ExternalPerceptionDeployment(status="disabled", reason="mode_off")
    if registry_path is None and authorized_search_profile is None:
        return ExternalPerceptionDeployment(status="disabled", reason="registry_not_configured")
    if authorized_search_profile is not None and not isinstance(
        authorized_search_profile.adapter,
        AcceptedWebSearchResultAdapter,
    ):
        raise TypeError("authorized search profile must use the settled-result adapter")
    if (
        authorized_search_profile is not None
        and authorized_search_profile.adapter.world_id != world_id
    ):
        raise ValueError("authorized search profile belongs to another World")

    # Preflight before allocating an async HTTP client. A registry with no
    # enabled sources is a normal fail-closed state unless the independently
    # authorized, already-settled search-result source is present.
    registry = (
        load_external_perception_source_registry(registry_path)
        if registry_path is not None
        else None
    )
    registry_sources_enabled = bool(
        registry is not None and any(item.enabled for item in registry.sources)
    )
    registry_health = (
        external_perception_registry_health(registry).model_dump(mode="json")
        if registry is not None
        else None
    )
    if not registry_sources_enabled and authorized_search_profile is None:
        if registry is None:
            raise AssertionError("missing registry was handled before preflight")
        return ExternalPerceptionDeployment(
            status="disabled",
            reason="no_enabled_sources",
            registry_revision=registry.registry_revision,
            registry_content_hash=registry.content_hash,
            registry_health=registry_health,
        )

    world_ledger: SQLiteWorldLedger | None = None
    if mode == "live" and channel_port is None:
        source_ids = [
            item.source_id
            for item in (registry.sources if registry is not None else ())
            if item.enabled
        ]
        if authorized_search_profile is not None:
            source_ids.append(authorized_search_profile.adapter.source_id)
        accessible_source_ids = tuple(sorted(set(source_ids)))
        registry_authority_hash = (
            registry.content_hash
            if registry is not None
            else "sha256:" + hashlib.sha256("\0".join(accessible_source_ids).encode()).hexdigest()
        )
        world_ledger = SQLiteWorldLedger(path=Path(settings.database_path), world_id=world_id)
        automatic_channel = LedgerPublicInformationChannelPort(
            ledger=world_ledger,
            accessible_source_ids=accessible_source_ids,
            registry_content_hash=registry_authority_hash,
        )
        if not automatic_channel.authority_is_available(actor_ref=actor_ref):
            world_ledger.close()
            return ExternalPerceptionDeployment(
                status="disabled",
                reason="channel_not_configured",
                registry_revision=(registry.registry_revision if registry is not None else None),
                registry_content_hash=(registry.content_hash if registry is not None else None),
                registry_health=registry_health,
            )
        channel_port = automatic_channel

    http_client = (
        httpx.AsyncClient(follow_redirects=False, trust_env=False)
        if registry_sources_enabled
        else None
    )
    external_profiles: tuple[SourceProfile, ...] = ()
    if registry_sources_enabled:
        if registry_path is None or http_client is None:
            raise AssertionError("enabled registry sources lack transport composition")
        source_result = build_production_source_profiles(
            deployment_mode=mode,
            registry_path=registry_path,
            http_client=http_client,
        )
        if source_result.status != "ready":
            raise AssertionError("preflighted source registry unexpectedly became disabled")
        external_profiles = source_result.source_profiles
    source_profiles = (
        *external_profiles,
        *((authorized_search_profile,) if authorized_search_profile is not None else ()),
    )
    for profile in source_profiles:
        if mode == "live" and (
            profile.policy.may_expose_to_character_model
            != profile.policy.may_freeze_durable_snapshot
        ):
            raise ValueError("live source policy exposure and durable snapshot rights must match")

    shadow_runtime: ShadowAttentionRuntime | None = None
    live_runtime: LiveAttentionRuntime | None = None
    registry_hash = registry.content_hash if registry is not None else None
    registry_revision = registry.registry_revision if registry is not None else "internal-only"
    revision_suffix = (registry_hash or "sha256:internal-search").removeprefix("sha256:")[:16]
    if mode == "shadow" and channel_port is not None:
        world_ledger = SQLiteWorldLedger(path=Path(settings.database_path), world_id=world_id)
        compiler = context_capsule_compiler_from_ledger(ledger=world_ledger)
        shadow_runtime = ShadowAttentionRuntime(
            world_id=world_id,
            actor_ref=actor_ref,
            attention_policy_revision=f"external-attention:{revision_suffix}",
            deployment_mode_revision=f"shadow:{registry_revision}",
            worker_id="worker:external-perception:shadow",
            context_port=CapsuleBackedShadowAttentionContextPort(
                ledger=world_ledger,
                capsule_compiler=compiler,
                channel_port=channel_port,
            ),
            model=ChatCompletionShadowAttentionModel(
                model=model,  # type: ignore[arg-type]
                model_id=_model_id(model),
            ),
            merge_wait_seconds=settings.world_v2_external_perception_merge_wait_seconds,
            attempt_retention_seconds=(
                settings.world_v2_external_perception_attempt_retention_seconds
            ),
        )
    elif mode == "live":
        producer = ExternalPerceptionDeliveryProducer()
        acceptance = ExternalPerceptionAcceptanceRuntime.open(
            path=Path(settings.database_path),
            world_id=world_id,
            delivery_producer=producer,
        )
        if world_ledger is None:
            world_ledger = acceptance.ledger
        elif acceptance.ledger is not world_ledger:
            # The acceptance runtime owns a second handle to the same file;
            # use its handle consistently and close the preflight handle.
            world_ledger.close()
            world_ledger = acceptance.ledger
            if isinstance(channel_port, LedgerPublicInformationChannelPort):
                channel_port = LedgerPublicInformationChannelPort(
                    ledger=world_ledger,
                    accessible_source_ids=tuple(item.adapter.source_id for item in source_profiles),
                    registry_content_hash=registry_authority_hash,
                )
        compiler = context_capsule_compiler_from_ledger(ledger=world_ledger)
        acceptance_port = LifeWakingExternalPerceptionAcceptance(
            acceptance=ProducerBackedExternalPerceptionAcceptance(
                producer=producer,
                runtime=acceptance,
            ),
            life=life,
        )
        if channel_port is None:
            raise AssertionError("live channel was checked before resource composition")
        live_runtime = LiveAttentionRuntime(
            world_id=world_id,
            actor_ref=actor_ref,
            attention_policy_revision=f"external-attention:{revision_suffix}",
            deployment_mode_revision=f"live:{registry_revision}",
            worker_id="worker:external-perception:live",
            context_port=CapsuleBackedLiveAttentionContextPort(
                ledger=world_ledger,
                capsule_compiler=compiler,
                channel_port=channel_port,
            ),
            model=ChatCompletionLiveAttentionModel(
                model=model,  # type: ignore[arg-type]
                model_id=_model_id(model),
            ),
            acceptance_port=acceptance_port,
            merge_wait_seconds=settings.world_v2_external_perception_merge_wait_seconds,
            attempt_retention_seconds=(
                settings.world_v2_external_perception_attempt_retention_seconds
            ),
        )

    hub = SQLiteWorldPerceptionHub(
        path=settings.world_v2_external_perception_sidecar_path,
        sources=source_profiles,
        wall_clock=wall_clock,
        shadow_attention=shadow_runtime,
        live_attention=live_runtime,
    )
    return ExternalPerceptionDeployment(
        status="ready",
        reason="ready",
        hub=_OwnedWorldPerceptionHub(
            hub=hub,
            http_client=http_client,
            world_ledger=world_ledger,
        ),
        registry_revision=(registry.registry_revision if registry is not None else None),
        registry_content_hash=(registry.content_hash if registry is not None else None),
        registry_health=registry_health,
    )


__all__ = [
    "ExternalPerceptionDeployment",
    "build_external_world_perception_deployment",
]
