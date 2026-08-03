from __future__ import annotations

from datetime import UTC, datetime
import hashlib

import pytest

from companion_daemon.world_v2.external_world_perception.authorized_search import (
    AcceptedWebSearchResultAdapter,
    ImmutableToolResultContent,
)
from companion_daemon.world_v2.external_world_perception.contracts import SourceCursor
from companion_daemon.world_v2.schemas import (
    LedgerProjection,
    ReadOnlyToolRequestProjection,
    ToolResultProjection,
)


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _projection(*, target: str = "tool:web_search") -> LedgerProjection:
    request = ReadOnlyToolRequestProjection(
        request_id="request:search:1",
        action_id="action:search:1",
        source_event_ref="event:observation:1",
        source_world_revision=4,
        source_payload_hash="a" * 64,
        tool_name="web_search",
        query_ref="query:1",
        query_hash="sha256:" + "b" * 64,
        target=target,
    )
    result = ToolResultProjection(
        result_id="result:search:1",
        request_id=request.request_id,
        action_id=request.action_id,
        result_ref="tool-result:search:1",
        result_hash=_sha(_BODY),
        receipt_event_ref="event:receipt:1",
        receipt_event_payload_hash="c" * 64,
        external_result_id="external-result:1",
        accepted_event_ref="event:tool-result:1",
        accepted_at=NOW,
    )
    return LedgerProjection(
        world_id="world:test",
        world_revision=12,
        deliberation_revision=2,
        ledger_sequence=18,
        semantic_hash="d" * 64,
        read_only_tool_requests=(request,),
        tool_results=(result,),
    )


_BODY = b'{"items":[{"title":"A new telescope opened","url":"https://example.test/a","snippet":"The public observatory opened today.","published_at":"2026-08-03T10:00:00Z","publisher":"Example News"}]}'


class ProjectionReader:
    def __init__(self, projection: LedgerProjection) -> None:
        self.projection = projection

    def current_projection(self, *, world_id: str) -> LedgerProjection:
        assert world_id == "world:test"
        return self.projection


class ContentReader:
    def __init__(self, content: ImmutableToolResultContent | None) -> None:
        self.content = content
        self.reads: list[str] = []

    def read_exact(self, *, result_ref: str) -> ImmutableToolResultContent | None:
        self.reads.append(result_ref)
        return self.content


@pytest.mark.asyncio
async def test_only_accepted_web_search_results_become_external_signals() -> None:
    reader = ContentReader(
        ImmutableToolResultContent(
            result_ref="tool-result:search:1", body=_BODY, body_hash=_sha(_BODY)
        )
    )
    adapter = AcceptedWebSearchResultAdapter(
        source_id="source:accepted-web-search",
        world_id="world:test",
        projection_reader=ProjectionReader(_projection()),
        content_reader=reader,
    )

    page = await adapter.fetch(
        after=None,
        observed_at=NOW,
        deadline_at=NOW.replace(minute=1),
        limit=20,
    )

    assert page.next_cursor == SourceCursor(opaque_value="18")
    assert len(page.items) == 1
    assert page.items[0].upstream_item_id.startswith("event:tool-result:1:item:")
    assert page.items[0].upstream_publisher_ref == "Example News"
    assert page.items[0].licensed_summary == "The public observatory opened today."
    assert reader.reads == ["tool-result:search:1"]


@pytest.mark.asyncio
async def test_non_web_search_and_unreadable_or_hash_mismatched_results_fail_closed() -> None:
    wrong_target = AcceptedWebSearchResultAdapter(
        source_id="source:accepted-web-search",
        world_id="world:test",
        projection_reader=ProjectionReader(_projection(target="tool:weather")),
        content_reader=ContentReader(None),
    )
    page = await wrong_target.fetch(
        after=None, observed_at=NOW, deadline_at=NOW.replace(minute=1), limit=20
    )
    assert page.items == ()

    bad = AcceptedWebSearchResultAdapter(
        source_id="source:accepted-web-search",
        world_id="world:test",
        projection_reader=ProjectionReader(_projection()),
        content_reader=ContentReader(
            ImmutableToolResultContent(
                result_ref="tool-result:search:1",
                body=b'{"items":[]}',
                body_hash=_sha(b'{"items":[]}'),
            )
        ),
    )
    bad_page = await bad.fetch(
        after=None, observed_at=NOW, deadline_at=NOW.replace(minute=1), limit=20
    )
    assert bad_page.items == ()
    assert bad_page.parser_rejected_item_count == 1


@pytest.mark.asyncio
async def test_cursor_makes_settled_search_ingestion_effect_once() -> None:
    adapter = AcceptedWebSearchResultAdapter(
        source_id="source:accepted-web-search",
        world_id="world:test",
        projection_reader=ProjectionReader(_projection()),
        content_reader=ContentReader(
            ImmutableToolResultContent(
                result_ref="tool-result:search:1", body=_BODY, body_hash=_sha(_BODY)
            )
        ),
    )

    page = await adapter.fetch(
        after=SourceCursor(opaque_value="18"),
        observed_at=NOW,
        deadline_at=NOW.replace(minute=1),
        limit=20,
    )

    assert page.not_modified is True
    assert page.items == ()
