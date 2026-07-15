from __future__ import annotations

import pytest

from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    SQLiteImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)


def _record(*, text: str = "茶泡好了。") -> StoredLifeContent:
    return StoredLifeContent(
        content_ref="life-content:tea:1",
        content_kind="occurrence_result",
        content_payload_hash=life_content_payload_hash(text),
        text=text,
    )


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_immutable_life_content_store_round_trips_and_rejects_rebinding(
    adapter: str, tmp_path
) -> None:
    store = (
        InMemoryImmutableLifeContentStore()
        if adapter == "memory"
        else SQLiteImmutableLifeContentStore(path=str(tmp_path / "life.sqlite"), world_id="world:1")
    )
    try:
        record = _record()
        store.put_if_absent(record)
        store.put_if_absent(record)
        assert store.read_exact(content_ref=record.content_ref) == record
        with pytest.raises(ValueError, match="already bound"):
            store.put_if_absent(_record(text="茶洒了。"))
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


def test_sqlite_life_content_store_survives_restart(tmp_path) -> None:
    path = str(tmp_path / "life.sqlite")
    record = _record(text="晚风把窗帘吹起来了。")
    writer = SQLiteImmutableLifeContentStore(path=path, world_id="world:1")
    writer.put_if_absent(record)
    writer.close()

    reader = SQLiteImmutableLifeContentStore(path=path, world_id="world:1")
    try:
        assert reader.read_exact(content_ref=record.content_ref) == record
        assert reader.read_exact(content_ref="life-content:unknown") is None
    finally:
        reader.close()


def test_life_content_rejects_a_hash_that_does_not_bind_the_text() -> None:
    with pytest.raises(ValueError, match="hash does not match"):
        StoredLifeContent(
            content_ref="life-content:bad",
            content_kind="experience_summary",
            content_payload_hash="0" * 64,
            text="这不是对应的内容。",
        )
