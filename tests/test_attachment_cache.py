from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.attachment_cache import AttachmentCache


NOW = datetime(2026, 7, 12, 10, tzinfo=UTC)


def test_cached_attachment_expires_after_thirty_days_by_default(tmp_path) -> None:
    cache = AttachmentCache(tmp_path / "attachments")

    cached = cache.store(
        user_id="user-1",
        attachment_id="message-1/photo",
        content=b"image bytes",
        filename="photo.png",
        content_type="image/png",
        now=NOW,
    )

    assert cached.expires_at == NOW + timedelta(days=30)
    assert cache.read("user-1", "message-1/photo", now=NOW) == b"image bytes"
    assert cache.read("user-1", "message-1/photo", now=NOW + timedelta(days=30)) is None


def test_delete_one_attachment_is_path_safe_and_keeps_other_content(tmp_path) -> None:
    root = tmp_path / "attachments"
    cache = AttachmentCache(root)
    first = cache.store(
        user_id="../../user",
        attachment_id="../../first",
        content=b"first",
        now=NOW,
    )
    cache.store(user_id="../../user", attachment_id="second", content=b"second", now=NOW)

    assert first.path.is_relative_to(root.resolve())
    assert ".." not in first.path.relative_to(root.resolve()).parts
    assert cache.delete_attachment("../../user", "../../first") is True
    assert cache.delete_attachment("../../user", "../../first") is False
    assert cache.read("../../user", "second", now=NOW) == b"second"


def test_delete_user_removes_only_that_users_attachments(tmp_path) -> None:
    cache = AttachmentCache(tmp_path / "attachments")
    cache.store(user_id="alice", attachment_id="one", content=b"1", now=NOW)
    cache.store(user_id="alice", attachment_id="two", content=b"2", now=NOW)
    cache.store(user_id="bob", attachment_id="one", content=b"bob", now=NOW)

    assert cache.delete_user("alice") == 2
    assert cache.read("alice", "one", now=NOW) is None
    assert cache.read("alice", "two", now=NOW) is None
    assert cache.read("bob", "one", now=NOW) == b"bob"


def test_cleanup_expired_removes_due_content_and_keeps_live_content(tmp_path) -> None:
    cache = AttachmentCache(tmp_path / "attachments")
    cache.store(user_id="alice", attachment_id="expired", content=b"old", now=NOW)
    cache.store(
        user_id="alice",
        attachment_id="live",
        content=b"new",
        now=NOW + timedelta(days=1),
    )

    result = cache.cleanup_expired(now=NOW + timedelta(days=30))

    assert result.removed == 1
    assert result.invalid == 0
    assert cache.read("alice", "expired", now=NOW + timedelta(days=30)) is None
    assert cache.read("alice", "live", now=NOW + timedelta(days=30)) == b"new"


def test_cache_refuses_symlink_escape_from_controlled_root(tmp_path) -> None:
    cache = AttachmentCache(tmp_path / "attachments")
    cached = cache.store(user_id="alice", attachment_id="one", content=b"safe", now=NOW)
    cache.delete_attachment("alice", "one")
    outside = tmp_path / "outside"
    outside.mkdir()
    cached.path.parent.parent.mkdir(parents=True, exist_ok=True)
    cached.path.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside controlled root"):
        cache.store(user_id="alice", attachment_id="one", content=b"escape", now=NOW)

    assert not (outside / "content").exists()
