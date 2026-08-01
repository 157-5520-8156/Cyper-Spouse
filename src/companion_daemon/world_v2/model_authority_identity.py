"""Semantic-authority and transport-route identity for model role separation.

One checkpoint reached through two API transports is useful availability
redundancy, but it is still one semantic authority. Source Inventory,
candidate authorship, and Coverage therefore compare a release-pinned
checkpoint registry while health may separately describe transport routes.

Model names are not authority declarations. Provider aliases such as
``qwen/qwen-plus`` and ``qwen-plus`` can name the same checkpoint, while a
mutable gateway may map one friendly name to something else tomorrow. Unknown
routes consequently fail closed instead of being treated as independent merely
because their strings differ.
"""

from __future__ import annotations

from urllib.parse import urlsplit


_SEMANTIC_AUTHORITY_REGISTRY_REVISION = "2026-08-01.1"
_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"
_OPENAI_ENDPOINT = "https://api.openai.com/v1"
_DEEPSEEK_ENDPOINTS = (
    "https://api.deepseek.com",
    "https://api.deepseek.com/v1",
)
_DASHSCOPE_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_LOCAL_OPENAI_COMPATIBLE_ENDPOINTS = (
    "http://127.0.0.1:8188/v1",
    "http://127.0.0.1:8288/v1",
    "http://localhost:8188/v1",
    "http://localhost:8288/v1",
)


def _authority_id(vendor: str, checkpoint: str) -> str:
    return (
        "semantic-authority:"
        f"{_SEMANTIC_AUTHORITY_REGISTRY_REVISION}:{vendor}:{checkpoint}"
    )


def _registry() -> dict[tuple[str, str, str], str]:
    """Build the small exact-route registry shipped with this release.

    Keeping route spellings explicit is intentional. Adding a new proxy or a
    provider's mutable alias requires a reviewed release change instead of a
    broad prefix-stripping heuristic silently expanding a hard-boundary trust
    decision.
    """

    entries: dict[tuple[str, str, str], str] = {}

    def register(
        authority: str,
        *routes: tuple[str, str, str],
    ) -> None:
        for provider, endpoint, model in routes:
            key = (provider.casefold(), endpoint.casefold(), model.casefold())
            existing = entries.setdefault(key, authority)
            if existing != authority:
                raise AssertionError("semantic authority route registered twice")

    for checkpoint in (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-thinking",
    ):
        authority = _authority_id("deepseek", checkpoint)
        register(
            authority,
            *(
                ("deepseek", endpoint, checkpoint)
                for endpoint in _DEEPSEEK_ENDPOINTS
            ),
            (
                "openrouter",
                _OPENROUTER_ENDPOINT,
                f"deepseek/{checkpoint}",
            ),
        )

    register(
        _authority_id("qwen", "qwen-plus"),
        ("openrouter", _OPENROUTER_ENDPOINT, "qwen/qwen-plus"),
        ("dashscope", _DASHSCOPE_ENDPOINT, "qwen-plus"),
    )
    register(
        _authority_id("nousresearch", "hermes-4-70b"),
        (
            "openrouter",
            _OPENROUTER_ENDPOINT,
            "nousresearch/hermes-4-70b",
        ),
    )

    for checkpoint in (
        "gpt-4.1-mini",
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-5-mini",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.6-luna",
    ):
        authority = _authority_id("openai", checkpoint)
        register(
            authority,
            ("openai", _OPENAI_ENDPOINT, checkpoint),
            (
                "openrouter",
                _OPENROUTER_ENDPOINT,
                f"openai/{checkpoint}",
            ),
        )

    for checkpoint in (
        "mlx-community/qwen3-1.7b-4bit",
        "mlx-community/qwen3-4b-instruct-4bit",
        "mlx-community/qwen3-1.7b-instruct-4bit",
    ):
        register(
            _authority_id("local-mlx", checkpoint),
            *(
                ("openai", endpoint, checkpoint)
                for endpoint in _LOCAL_OPENAI_COMPATIBLE_ENDPOINTS
            ),
        )
    return entries


_RELEASE_PINNED_SEMANTIC_AUTHORITIES = _registry()


def _normalized_endpoint(base_url: object) -> str | None:
    raw = str(base_url or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw.rstrip("/"))
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{path.casefold()}"


def _route_provider(*, declared_provider: str, endpoint: str) -> str:
    """Resolve provider identity from an exact endpoint before adapter labels.

    ``OpenAICompatibleChatModel`` describes a wire protocol and therefore uses
    ``provider='openai'`` even when pointed at OpenRouter or DashScope. Exact
    release endpoints are stronger evidence than that generic adapter label.
    Unknown endpoints keep their declaration and still miss the registry.
    """

    if endpoint == _OPENROUTER_ENDPOINT:
        return "openrouter"
    if endpoint in _DEEPSEEK_ENDPOINTS:
        return "deepseek"
    if endpoint == _DASHSCOPE_ENDPOINT:
        return "dashscope"
    if endpoint == _OPENAI_ENDPOINT:
        return "openai"
    return declared_provider


def semantic_authority_id(model: object | None) -> str | None:
    """Return a release-pinned, transport-independent checkpoint identity.

    A caller-owned adapter may carry an explicit audited declaration. Normal
    production leaves otherwise have to match an exact provider, endpoint and
    model route in this release. Missing or unknown identity returns ``None``;
    independence checks interpret that as unproven and fail closed.
    """

    if model is None:
        return None
    declared_authority = getattr(model, "semantic_authority_id", None)
    if declared_authority is not None:
        if not isinstance(declared_authority, str):
            return None
        explicit = declared_authority.strip()
        if explicit:
            return explicit.casefold()[:256]
    provider = str(getattr(model, "provider", "")).strip().casefold()
    endpoint = _normalized_endpoint(getattr(model, "base_url", ""))
    raw_model = str(getattr(model, "model", "")).strip().casefold()
    if not provider or endpoint is None or not raw_model:
        return None
    provider = _route_provider(declared_provider=provider, endpoint=endpoint)
    return _RELEASE_PINNED_SEMANTIC_AUTHORITIES.get(
        (provider, endpoint, raw_model)
    )


def transport_route_id(model: object | None) -> str | None:
    """Return the deployment route independently of semantic checkpoint identity."""

    if model is None:
        return None
    explicit = str(getattr(model, "transport_route_id", "")).strip()
    if explicit:
        return explicit.casefold()[:512]
    provider = str(getattr(model, "provider", "")).strip().casefold() or "unknown"
    base_url = str(getattr(model, "base_url", "")).strip()
    endpoint = ""
    if base_url:
        parsed = urlsplit(base_url)
        endpoint = (parsed.netloc or parsed.path).casefold().rstrip("/")
    raw_model = str(getattr(model, "model", "")).strip().casefold()
    if not raw_model:
        return None
    return f"{provider}:{endpoint}:{raw_model}"[:512]


def possible_provider_lanes(model: object | None) -> tuple[object, ...]:
    """Expand every transport leaf that may produce bytes for one semantic role."""

    if model is None:
        return ()
    origin = getattr(model, "authority_origin", model)
    if origin is not model:
        return possible_provider_lanes(origin)
    primary = getattr(origin, "primary", None)
    secondary = getattr(origin, "secondary", None)
    if primary is not None and secondary is not None:
        return (
            *possible_provider_lanes(primary),
            *possible_provider_lanes(secondary),
        )
    fallback = getattr(origin, "fallback", None)
    if primary is not None and fallback is not None:
        if bool(getattr(origin, "implicit_failover", True)):
            return (
                *possible_provider_lanes(primary),
                *possible_provider_lanes(fallback),
            )
        return possible_provider_lanes(primary)
    return (origin,)


def provider_lane_sets_are_independent(
    left: object | None,
    right: object | None,
) -> bool:
    """Prove that two roles share no possible semantic authority."""

    left_lanes = possible_provider_lanes(left)
    right_lanes = possible_provider_lanes(right)
    if not left_lanes or not right_lanes:
        return False
    for left_lane in left_lanes:
        for right_lane in right_lanes:
            if left_lane is right_lane:
                return False
            left_authority = semantic_authority_id(left_lane)
            right_authority = semantic_authority_id(right_lane)
            if left_authority is None or right_authority is None:
                return False
            if left_authority == right_authority:
                return False
    return True


def transport_route_ids(model: object | None) -> tuple[str, ...]:
    """Return stable, deduplicated route labels for health evidence."""

    return tuple(
        dict.fromkeys(
            route
            for lane in possible_provider_lanes(model)
            if (route := transport_route_id(lane)) is not None
        )
    )


__all__ = [
    "possible_provider_lanes",
    "provider_lane_sets_are_independent",
    "semantic_authority_id",
    "transport_route_id",
    "transport_route_ids",
]
