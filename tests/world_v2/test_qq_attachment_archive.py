"""The QQ attachment archive: URL-free, idempotent, hash-bound perception bytes."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import httpx
import pytest

from companion_daemon.world_v2.qq_attachment_archive import (
    QQAttachmentArchive,
    QQOneBotAttachmentArchiver,
    sniff_image_media_type,
)
from companion_daemon.world_v2.qq_ingress_policy import (
    normalize_onebot_qq_ingress,
    onebot_attachment_ref,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 4
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 4

SEGMENT_DATA = {
    "file": "ABCDEF.jpg",
    "url": "https://multimedia.example.invalid/download?fileid=ABCDEF&rkey=SECRET",
    "file_size": "1024",
}


def _image_event(data: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": "10001",
        "message_id": "msg-1",
        "time": 1_700_000_000,
        "message": [{"type": "image", "data": dict(data or SEGMENT_DATA)}],
    }


def test_archive_ref_is_byte_identical_to_ingress_normalization() -> None:
    fragment = normalize_onebot_qq_ingress(_image_event())
    assert fragment is not None and fragment.content_shape == "attachment"
    assert fragment.attachment_refs == (onebot_attachment_ref("image", SEGMENT_DATA),)
    ref = fragment.attachment_refs[0]
    assert ref.startswith("qq-attachment:image:sha256:")
    assert "http" not in ref and "SECRET" not in ref


def test_store_is_idempotent_and_bounded(tmp_path: Path) -> None:
    archive = QQAttachmentArchive(tmp_path / "attachments", max_bytes=64)
    ref = "qq-attachment:image:sha256:" + "a" * 64
    assert archive.store(ref, PNG_BYTES[:32]) is True
    assert archive.store(ref, PNG_BYTES[:32]) is False
    assert archive.read(ref) == PNG_BYTES[:32]
    with pytest.raises(ValueError, match="size bound"):
        archive.store("qq-attachment:image:sha256:" + "b" * 64, b"x" * 65)
    with pytest.raises(ValueError, match="empty"):
        archive.store("qq-attachment:image:sha256:" + "c" * 64, b"")
    with pytest.raises(ValueError, match="opaque token"):
        archive.store("../escape", PNG_BYTES[:16])


def test_archive_directory_never_contains_provider_urls(tmp_path: Path) -> None:
    archive = QQAttachmentArchive(tmp_path / "attachments")
    ref = onebot_attachment_ref("image", SEGMENT_DATA)
    archive.store(ref, JPEG_BYTES)
    files = list((tmp_path / "attachments").iterdir())
    assert len(files) == 1
    assert "http" not in files[0].name and "SECRET" not in files[0].name
    assert b"SECRET" not in files[0].read_bytes()


def test_describe_and_resolve_bind_the_exact_canonical_body(tmp_path: Path) -> None:
    archive = QQAttachmentArchive(tmp_path / "attachments")
    ref = "qq-attachment:image:sha256:" + "d" * 64
    archive.store(ref, PNG_BYTES)
    descriptor = archive.describe(attachment_ref=ref, analysis_kind="vision")
    expected_body = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
    assert descriptor.attachment_ref == ref
    assert descriptor.content_hash == (
        "sha256:" + hashlib.sha256(expected_body.encode()).hexdigest()
    )


@pytest.mark.asyncio
async def test_resolve_reopens_the_bytes_the_descriptor_promised(tmp_path: Path) -> None:
    archive = QQAttachmentArchive(tmp_path / "attachments")
    ref = "qq-attachment:image:sha256:" + "e" * 64
    archive.store(ref, JPEG_BYTES)
    descriptor = archive.describe(attachment_ref=ref, analysis_kind="vision")

    class _Action:
        payload_ref = ref
        payload_hash = descriptor.content_hash

    input_ref, input_hash, body = await archive.resolve(_Action())
    assert (input_ref, input_hash) == (ref, descriptor.content_hash)
    assert "sha256:" + hashlib.sha256(body.encode()).hexdigest() == input_hash
    assert body.startswith("data:image/jpeg;base64,")


def test_describe_fails_closed_for_missing_or_unsupported_content(tmp_path: Path) -> None:
    archive = QQAttachmentArchive(tmp_path / "attachments")
    with pytest.raises(ValueError, match="not archived"):
        archive.describe(
            attachment_ref="qq-attachment:image:sha256:" + "f" * 64,
            analysis_kind="vision",
        )
    junk_ref = "qq-attachment:image:sha256:" + "0" * 64
    archive.store(junk_ref, b"definitely-not-an-image-payload")
    with pytest.raises(ValueError, match="supported image"):
        archive.describe(attachment_ref=junk_ref, analysis_kind="vision")
    png_ref = "qq-attachment:image:sha256:" + "1" * 64
    archive.store(png_ref, PNG_BYTES)
    with pytest.raises(ValueError, match="vision"):
        archive.describe(attachment_ref=png_ref, analysis_kind="transcription")


def test_sniff_recognizes_supported_formats_only() -> None:
    assert sniff_image_media_type(PNG_BYTES) == "image/png"
    assert sniff_image_media_type(JPEG_BYTES) == "image/jpeg"
    assert sniff_image_media_type(b"GIF89a...") == "image/gif"
    assert sniff_image_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_media_type(b"plain text") is None


@pytest.mark.asyncio
async def test_archiver_pulls_segment_url_once_and_is_idempotent(tmp_path: Path) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, content=JPEG_BYTES)

    archiver = QQOneBotAttachmentArchiver(
        archive=QQAttachmentArchive(tmp_path / "attachments"),
        api_url="http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    event = _image_event()
    first = await archiver.archive_from_event(event)
    assert (first.considered, first.archived, first.failed) == (1, 1, 0)
    second = await archiver.archive_from_event(event)
    assert (second.considered, second.already_present) == (1, 1)
    assert calls["count"] == 1
    ref = onebot_attachment_ref("image", SEGMENT_DATA)
    assert archiver.archive.read(ref) == JPEG_BYTES


@pytest.mark.asyncio
async def test_archiver_falls_back_to_get_image_and_degrades_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import companion_daemon.onebot_adapter as onebot_adapter

    async def fake_get_image(api_url: str, *, file: str, access_token: str | None = None):
        assert file == "NOURL.jpg"
        return {"base64": base64.b64encode(PNG_BYTES).decode()}

    monkeypatch.setattr(onebot_adapter, "get_onebot_image", fake_get_image)
    archiver = QQOneBotAttachmentArchiver(
        archive=QQAttachmentArchive(tmp_path / "attachments"),
        api_url="http://127.0.0.1:3000",
    )
    data = {"file": "NOURL.jpg"}
    report = await archiver.archive_from_event(_image_event(data))
    assert (report.archived, report.failed) == (1, 0)
    assert archiver.archive.read(onebot_attachment_ref("image", data)) == PNG_BYTES

    async def broken_get_image(api_url: str, *, file: str, access_token: str | None = None):
        raise RuntimeError("provider down")

    monkeypatch.setattr(onebot_adapter, "get_onebot_image", broken_get_image)
    failing = {"file": "OTHER.jpg"}
    degraded = await archiver.archive_from_event(_image_event(failing))
    assert (degraded.archived, degraded.failed) == (0, 1)
    assert not archiver.archive.has(onebot_attachment_ref("image", failing))


@pytest.mark.asyncio
async def test_archiver_ignores_group_and_non_image_shapes(tmp_path: Path) -> None:
    archiver = QQOneBotAttachmentArchiver(
        archive=QQAttachmentArchive(tmp_path / "attachments"),
        api_url="http://127.0.0.1:3000",
    )
    group = _image_event()
    group["message_type"] = "group"
    assert (await archiver.archive_from_event(group)).considered == 0
    text_only = {
        "post_type": "message",
        "message_type": "private",
        "user_id": "10001",
        "message_id": "msg-2",
        "message": [{"type": "text", "data": {"text": "hi"}}],
    }
    assert (await archiver.archive_from_event(text_only)).considered == 0
