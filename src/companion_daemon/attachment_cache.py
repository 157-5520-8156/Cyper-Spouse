"""Controlled, expiring local storage for user-provided attachments."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


DEFAULT_RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class CachedAttachment:
    user_id: str
    attachment_id: str
    filename: str | None
    content_type: str | None
    stored_at: datetime
    expires_at: datetime
    path: Path


@dataclass(frozen=True)
class CleanupResult:
    removed: int
    invalid: int


class AttachmentCache:
    """Store attachment bytes below one application-owned root."""

    def __init__(self, root: Path, *, retention: timedelta = DEFAULT_RETENTION) -> None:
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        self.root = root.resolve()
        self.retention = retention

    def store(
        self,
        *,
        user_id: str,
        attachment_id: str,
        content: bytes,
        filename: str | None = None,
        content_type: str | None = None,
        now: datetime,
    ) -> CachedAttachment:
        now = _aware_utc(now)
        directory = self._attachment_dir(user_id, attachment_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "content"
        path.write_bytes(content)
        cached = CachedAttachment(
            user_id=user_id,
            attachment_id=attachment_id,
            filename=filename,
            content_type=content_type,
            stored_at=now,
            expires_at=now + self.retention,
            path=path,
        )
        metadata = asdict(cached)
        metadata["stored_at"] = cached.stored_at.isoformat()
        metadata["expires_at"] = cached.expires_at.isoformat()
        metadata["path"] = str(path)
        (directory / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
        return cached

    def read(
        self, user_id: str, attachment_id: str, *, now: datetime
    ) -> bytes | None:
        cached = self.describe(user_id, attachment_id)
        if cached is None or _aware_utc(now) >= cached.expires_at:
            return None
        try:
            return cached.path.read_bytes()
        except FileNotFoundError:
            return None

    def describe(self, user_id: str, attachment_id: str) -> CachedAttachment | None:
        directory = self._attachment_dir(user_id, attachment_id)
        return self._describe_directory(directory)

    @staticmethod
    def _describe_directory(directory: Path) -> CachedAttachment | None:
        metadata_path = directory / "metadata.json"
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            return CachedAttachment(
                user_id=str(data["user_id"]),
                attachment_id=str(data["attachment_id"]),
                filename=data.get("filename"),
                content_type=data.get("content_type"),
                stored_at=_aware_utc(datetime.fromisoformat(data["stored_at"])),
                expires_at=_aware_utc(datetime.fromisoformat(data["expires_at"])),
                path=directory / "content",
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            return None

    def delete_attachment(self, user_id: str, attachment_id: str) -> bool:
        directory = self._attachment_dir(user_id, attachment_id)
        removed = self._delete_directory(directory)
        try:
            directory.parent.rmdir()
        except (FileNotFoundError, OSError):
            pass
        return removed

    def delete_user(self, user_id: str) -> int:
        user_directory = self.root / _opaque_key(user_id)
        try:
            attachment_directories = list(user_directory.iterdir())
        except FileNotFoundError:
            return 0
        removed = 0
        for directory in attachment_directories:
            if directory.is_dir() and self._delete_directory(directory):
                removed += 1
        try:
            user_directory.rmdir()
        except (FileNotFoundError, OSError):
            pass
        return removed

    def cleanup_expired(self, *, now: datetime) -> CleanupResult:
        now = _aware_utc(now)
        removed = 0
        invalid = 0
        if not self.root.exists():
            return CleanupResult(removed=0, invalid=0)
        for user_directory in self.root.iterdir():
            if not user_directory.is_dir():
                continue
            for directory in user_directory.iterdir():
                if not directory.is_dir():
                    continue
                cached = self._describe_directory(directory)
                if cached is None:
                    invalid += 1
                    self._delete_directory(directory)
                elif now >= cached.expires_at:
                    removed += 1
                    self._delete_directory(directory)
            try:
                user_directory.rmdir()
            except OSError:
                pass
        return CleanupResult(removed=removed, invalid=invalid)

    def _delete_directory(self, directory: Path) -> bool:
        if directory.is_symlink():
            directory.unlink()
            return True
        if not directory.resolve().is_relative_to(self.root):
            raise ValueError("attachment path resolves outside controlled root")
        removed = False
        for name in ("content", "metadata.json"):
            path = directory / name
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass
        return removed

    def _attachment_dir(self, user_id: str, attachment_id: str) -> Path:
        user_key = _opaque_key(user_id)
        attachment_key = _opaque_key(attachment_id)
        directory = self.root / user_key / attachment_key
        if not directory.resolve().is_relative_to(self.root):
            raise ValueError("attachment path resolves outside controlled root")
        return directory


def _opaque_key(value: str) -> str:
    if not value:
        raise ValueError("cache identifiers must not be empty")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
