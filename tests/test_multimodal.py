from companion_daemon.models import MessageAttachment
from companion_daemon.multimodal import attachment_kind, summarize_attachments


def test_attachment_kind_from_content_type_and_filename() -> None:
    assert attachment_kind("image/png") == "image"
    assert attachment_kind(None, "voice.mp3") == "audio"
    assert attachment_kind("application/pdf", "report.pdf") == "file"


def test_summarize_attachments() -> None:
    lines = summarize_attachments(
        [
            MessageAttachment(
                kind="image",
                filename="cat.png",
                content_type="image/png",
                width=640,
                height=480,
            )
        ]
    )

    assert lines == ["1. image: cat.png (image/png, 640x480)"]
