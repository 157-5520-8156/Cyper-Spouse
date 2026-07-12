"""Cross-adapter ownership gate for all QQ outbound traffic."""

from __future__ import annotations

from pathlib import Path

from companion_daemon.process_lock import AlreadyRunningError, SingleInstanceLock


SUPPORTED_QQ_ADAPTERS = frozenset({"official", "napcat", "onebot"})


class QQOutboundConfigurationError(ValueError):
    pass


def validate_qq_outbound_configuration(
    *,
    configured_adapter: str,
    launched_adapter: str,
) -> str:
    """Require the process entrypoint and the shared delivery router to agree."""

    configured = configured_adapter.strip().lower()
    launched = launched_adapter.strip().lower()
    if configured not in SUPPORTED_QQ_ADAPTERS:
        raise QQOutboundConfigurationError(
            f"unsupported QQ_ADAPTER {configured_adapter!r}; expected one of "
            f"{sorted(SUPPORTED_QQ_ADAPTERS)}"
        )
    if launched not in SUPPORTED_QQ_ADAPTERS:
        raise QQOutboundConfigurationError(
            f"unsupported launched QQ adapter {launched_adapter!r}; expected one of "
            f"{sorted(SUPPORTED_QQ_ADAPTERS)}"
        )
    if configured != launched:
        raise QQOutboundConfigurationError(
            f"QQ_ADAPTER={configured!r} does not match launched adapter {launched!r}; "
            "only the configured adapter may own QQ outbound delivery"
        )
    return configured


def qq_outbound_owner_lock_path(database_path: Path) -> Path:
    return Path(database_path).parent / "qq-outbound-owner.lock"


class QQOutboundOwnerLease:
    """One process lease shared by official QQ, NapCat, and generic OneBot."""

    def __init__(self, path: Path, *, adapter: str):
        normalized = adapter.strip().lower()
        if normalized not in SUPPORTED_QQ_ADAPTERS:
            raise QQOutboundConfigurationError(f"unsupported QQ adapter owner: {adapter!r}")
        self.path = path
        self.adapter = normalized
        self._lock = SingleInstanceLock(path)

    def __enter__(self) -> QQOutboundOwnerLease:
        try:
            self._lock.__enter__()
        except AlreadyRunningError as exc:
            raise AlreadyRunningError(
                f"QQ outbound owner is already claimed; {self.adapter} cannot start ({exc})"
            ) from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._lock.__exit__(exc_type, exc, tb)
