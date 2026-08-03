"""Fail-closed deployment registry for authoritative external-signal sources.

Loading this registry creates adapters; it never performs network I/O.  The
registry is deployment authority, not evidence that the character noticed a
signal and not permission to use private user location.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from ..schema_core import FrozenModel
from .contracts import SourcePolicyRevision, SourceProfile
from .nws import NwsAlertsAdapter
from .rss import RssAtomSourceAdapter, RssHubPullAdapter
from .usgs import UsgsEarthquakeGeoJsonAdapter


DeploymentMode = Literal["off", "shadow", "live"]
AdapterKind = Literal["usgs_geojson", "nws_alerts", "rss_atom", "rsshub"]
FactoryStatus = Literal["disabled", "ready"]
FactoryReason = Literal["mode_off", "registry_not_configured", "no_enabled_sources", "ready"]

_MAX_REGISTRY_BYTES = 1_000_000
_EXPECTED_SOURCE_IDS: dict[str, str] = {
    "usgs_geojson": "usgs.earthquake.global.v1",
    "nws_alerts": "noaa.nws.alerts.us.cap.v1",
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_source_registry_content_hash(value: Mapping[str, object]) -> str:
    """Hash the semantic JSON registry, excluding its self-describing hash."""

    material = {key: item for key, item in value.items() if key != "content_hash"}
    return "sha256:" + hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


class ExternalPerceptionSourceRegistration(FrozenModel):
    """One explicit adapter, endpoint, audited rights basis, and resource budget."""

    enabled: bool
    adapter_kind: AdapterKind
    source_id: str = Field(min_length=1, max_length=512)
    endpoint: str = Field(min_length=1, max_length=4_096)
    route: str | None = Field(default=None, min_length=1, max_length=2_048)
    signal_kind: str | None = Field(default=None, min_length=1, max_length=128)
    allow_undated_items: bool = False
    upstream_publisher_ref: str | None = Field(default=None, min_length=1, max_length=1_024)
    allowed_item_hosts: tuple[str, ...] = Field(default=(), min_length=0, max_length=32)
    policy_owner_ref: str = Field(min_length=1, max_length=512)
    license_evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=16)
    policy: SourcePolicyRevision
    poll_interval_seconds: int = Field(gt=0, le=86_400)
    signal_ttl_seconds: int = Field(gt=0, le=31_536_000)
    raw_retention_seconds: int = Field(gt=0, le=31_536_000)
    normalized_retention_seconds: int = Field(gt=0, le=31_536_000)
    fetch_deadline_seconds: int = Field(gt=0, le=120)
    page_limit: int = Field(gt=0, le=500)

    @model_validator(mode="after")
    def authority_is_exact_and_auditable(self) -> ExternalPerceptionSourceRegistration:
        if self.adapter_kind in _EXPECTED_SOURCE_IDS:
            if self.source_id != _EXPECTED_SOURCE_IDS[self.adapter_kind]:
                raise ValueError("registry source id does not match adapter authority")
            if (
                self.route
                or self.signal_kind
                or self.upstream_publisher_ref
                or self.allowed_item_hosts
            ):
                raise ValueError("native adapters cannot declare RSSHub authority")
        elif not self.source_id.startswith("cn.") or not self.source_id.endswith(".v1"):
            raise ValueError("RSS source id must use the closed cn.*.v1 namespace")
        endpoint = urlsplit(self.endpoint)
        if self.adapter_kind == "rsshub":
            if endpoint.scheme not in {"http", "https"} or not _is_loopback_host(endpoint.hostname):
                raise ValueError("RSSHub endpoint must be a credential-free loopback URL")
            if endpoint.username is not None or endpoint.password is not None:
                raise ValueError("RSSHub endpoint must be a credential-free loopback URL")
            route = urlsplit(self.route or "")
            if (
                not self.route
                or not self.route.startswith("/")
                or route.scheme
                or route.netloc
                or route.fragment
            ):
                raise ValueError("RSSHub route must be one fixed local path")
            if not self.signal_kind or not self.upstream_publisher_ref:
                raise ValueError("RSSHub source identity is incomplete")
            if not self.allowed_item_hosts:
                raise ValueError("RSSHub allowed_item_hosts must contain at least 1 item")
            if any(not _is_plain_hostname(host) for host in self.allowed_item_hosts):
                raise ValueError("RSSHub item hosts must be exact hostnames")
            if self.allow_undated_items and self.signal_kind != "platform_trend_observation":
                raise ValueError("undated RSSHub fallback is restricted to trend observations")
            if self.allow_undated_items and (
                self.policy.may_expose_to_character_model or self.policy.may_freeze_durable_snapshot
            ):
                raise ValueError(
                    "undated RSSHub observations cannot enter model or durable World state"
                )
        elif self.adapter_kind == "rss_atom":
            if self.allow_undated_items:
                raise ValueError("publisher RSS must retain source publication time")
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.fragment
            ):
                raise ValueError("publisher RSS endpoint must be credential-free HTTPS")
            if self.route is not None:
                raise ValueError("publisher RSS cannot declare an RSSHub route")
            if not self.signal_kind or not self.upstream_publisher_ref:
                raise ValueError("publisher RSS source identity is incomplete")
            if not self.allowed_item_hosts:
                raise ValueError("publisher RSS allowed_item_hosts must contain at least 1 item")
            if any(not _is_plain_hostname(host) for host in self.allowed_item_hosts):
                raise ValueError("publisher RSS item hosts must be exact hostnames")
        elif (
            endpoint.scheme != "https"
            or endpoint.username is not None
            or endpoint.password is not None
        ):
            raise ValueError("registry endpoint must be credential-free HTTPS")
        for evidence_ref in self.license_evidence_refs:
            parsed = urlsplit(evidence_ref)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError("registry license evidence must be an exact HTTPS reference")
        return self


def _is_loopback_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    normalized = hostname.casefold().rstrip(".")
    return normalized == "localhost" or normalized == "127.0.0.1" or normalized == "::1"


def _is_plain_hostname(value: str) -> bool:
    parsed = urlsplit(f"//{value}")
    return (
        bool(parsed.hostname)
        and parsed.hostname == value.casefold().rstrip(".")
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and "/" not in value
    )


class ExternalPerceptionSourceRegistry(FrozenModel):
    """Immutable, revisioned deployment authority loaded from one JSON file."""

    schema_version: Literal[1]
    registry_revision: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    uses_user_location: Literal[False]
    sources: tuple[ExternalPerceptionSourceRegistration, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def identity_and_sources_are_closed(self) -> ExternalPerceptionSourceRegistry:
        # Optional adapter-specific fields must not retroactively change the
        # semantic hash of registries created before that adapter existed.
        material = self.model_dump(
            mode="json",
            exclude={"content_hash"},
            exclude_defaults=True,
        )
        if self.content_hash != canonical_source_registry_content_hash(material):
            raise ValueError("registry content hash mismatch")
        source_ids = tuple(item.source_id for item in self.sources)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("registry source ids must be unique")
        policy_revisions = tuple(item.policy.policy_revision for item in self.sources)
        if len(policy_revisions) != len(set(policy_revisions)):
            raise ValueError("registry policy revisions must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ProductionSourceFactoryResult:
    """Explicit ready/disabled result; absence can never become implicit acquisition."""

    deployment_mode: DeploymentMode
    status: FactoryStatus
    reason: FactoryReason
    source_profiles: tuple[SourceProfile, ...] = ()
    registry_revision: str | None = None
    registry_content_hash: str | None = None


def load_external_perception_source_registry(
    path: str | Path,
) -> ExternalPerceptionSourceRegistry:
    """Read and validate one bounded JSON registry without acquiring any source."""

    registry_path = Path(path)
    try:
        with registry_path.open("rb") as stream:
            raw = stream.read(_MAX_REGISTRY_BYTES + 1)
    except OSError as exc:
        raise ValueError("external perception source registry is unreadable") from exc
    if not raw or len(raw) > _MAX_REGISTRY_BYTES:
        raise ValueError("external perception source registry size is invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("external perception source registry is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("external perception source registry must be an object")
    return ExternalPerceptionSourceRegistry.model_validate_json(raw)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("external perception source registry contains duplicate JSON keys")
        value[key] = item
    return value


def build_production_source_profiles(
    *,
    deployment_mode: DeploymentMode,
    registry_path: str | Path | None,
    http_client: httpx.AsyncClient,
) -> ProductionSourceFactoryResult:
    """Build configured source adapters without polling them or reading location.

    ``live`` permits only sources carrying both exposure rights to cross into
    character attention and immutable World evidence. Fetch-only sources may
    share the Hub but remain structurally absent from the attention coordinator.
    ``shadow`` can use a narrower policy but still requires explicit fetch/cache
    authority and review evidence.
    """

    if deployment_mode == "off":
        return ProductionSourceFactoryResult(
            deployment_mode=deployment_mode,
            status="disabled",
            reason="mode_off",
        )
    if deployment_mode not in {"shadow", "live"}:
        raise ValueError("external perception deployment mode is invalid")
    if registry_path is None:
        return ProductionSourceFactoryResult(
            deployment_mode=deployment_mode,
            status="disabled",
            reason="registry_not_configured",
        )

    registry = load_external_perception_source_registry(registry_path)
    enabled = tuple(item for item in registry.sources if item.enabled)
    if not enabled:
        return ProductionSourceFactoryResult(
            deployment_mode=deployment_mode,
            status="disabled",
            reason="no_enabled_sources",
            registry_revision=registry.registry_revision,
            registry_content_hash=registry.content_hash,
        )

    profiles: list[SourceProfile] = []
    for item in enabled:
        if deployment_mode == "live" and (
            item.policy.may_expose_to_character_model != item.policy.may_freeze_durable_snapshot
        ):
            raise ValueError("live source policy exposure and durable snapshot rights must match")
        adapter = _build_adapter(item, http_client=http_client)
        profiles.append(
            SourceProfile(
                adapter=adapter,
                policy=item.policy,
                poll_interval_seconds=item.poll_interval_seconds,
                signal_ttl_seconds=item.signal_ttl_seconds,
                raw_retention_seconds=item.raw_retention_seconds,
                normalized_retention_seconds=item.normalized_retention_seconds,
                fetch_deadline_seconds=item.fetch_deadline_seconds,
                page_limit=item.page_limit,
            )
        )
    return ProductionSourceFactoryResult(
        deployment_mode=deployment_mode,
        status="ready",
        reason="ready",
        source_profiles=tuple(profiles),
        registry_revision=registry.registry_revision,
        registry_content_hash=registry.content_hash,
    )


def _build_adapter(
    registration: ExternalPerceptionSourceRegistration,
    *,
    http_client: httpx.AsyncClient,
) -> UsgsEarthquakeGeoJsonAdapter | NwsAlertsAdapter | RssAtomSourceAdapter | RssHubPullAdapter:
    if registration.adapter_kind == "usgs_geojson":
        return UsgsEarthquakeGeoJsonAdapter(
            http_client=http_client,
            feed_url=registration.endpoint,
        )
    if registration.adapter_kind == "nws_alerts":
        return NwsAlertsAdapter(
            http_client=http_client,
            feed_url=registration.endpoint,
        )
    if registration.adapter_kind == "rsshub":
        assert registration.route is not None
        assert registration.signal_kind is not None
        assert registration.upstream_publisher_ref is not None
        return RssHubPullAdapter(
            source_id=registration.source_id,
            base_url=registration.endpoint,
            route=registration.route,
            allowed_routes=frozenset({registration.route}),
            signal_kind=registration.signal_kind,
            upstream_publisher_ref=registration.upstream_publisher_ref,
            allowed_item_hosts=frozenset(registration.allowed_item_hosts),
            allow_undated_items=registration.allow_undated_items,
            http_client=http_client,
        )
    if registration.adapter_kind == "rss_atom":
        assert registration.signal_kind is not None
        assert registration.upstream_publisher_ref is not None
        endpoint_host = urlsplit(registration.endpoint).hostname
        assert endpoint_host is not None
        return RssAtomSourceAdapter(
            source_id=registration.source_id,
            feed_url=registration.endpoint,
            signal_kind=registration.signal_kind,
            upstream_publisher_ref=registration.upstream_publisher_ref,
            allowed_hosts=frozenset(registration.allowed_item_hosts),
            transport_allowed_hosts=frozenset({endpoint_host}),
            http_client=http_client,
        )
    raise AssertionError("closed adapter kind was not handled")


__all__ = [
    "ExternalPerceptionSourceRegistration",
    "ExternalPerceptionSourceRegistry",
    "ProductionSourceFactoryResult",
    "build_production_source_profiles",
    "canonical_source_registry_content_hash",
    "load_external_perception_source_registry",
]
