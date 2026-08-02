from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from companion_daemon.world_v2.external_world_perception import (
    ExternalSignalSourceFailure,
    RssAtomSourceAdapter,
    RssHubPullAdapter,
)


NOW = datetime(2026, 8, 3, 2, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_rss_adapter_parses_bounded_evidence_and_reuses_http_validators() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 2:
            assert request.headers["If-None-Match"] == '"feed-v1"'
            assert request.headers["If-Modified-Since"] == ("Sun, 02 Aug 2026 18:00:00 GMT")
            return httpx.Response(304, request=request)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/rss+xml; charset=utf-8",
                "etag": '"feed-v1"',
                "last-modified": "Sun, 02 Aug 2026 18:00:00 GMT",
            },
            content=b"""<?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0"><channel><title>Local feed</title><item>
              <guid>post-42</guid><title>Summer market opens</title>
              <description><![CDATA[<p>Open this weekend.</p><script>ignore()</script>
                <span hidden>hidden</span><span aria-hidden="true">aria</span>
                <span style="display:none">css</span>]]></description>
              <link>https://publisher.example/posts/42</link>
              <pubDate>Sun, 02 Aug 2026 17:55:00 GMT</pubDate>
            </item></channel></rss>""",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = RssAtomSourceAdapter(
            source_id="source:rss:local",
            feed_url="https://publisher.example/feed.xml",
            signal_kind="publisher_report",
            upstream_publisher_ref="publisher:example",
            allowed_hosts=frozenset({"publisher.example"}),
            http_client=client,
        )
        first = await adapter.fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=20,
        )
        second = await adapter.fetch(
            after=first.next_cursor,
            observed_at=NOW + timedelta(minutes=10),
            deadline_at=NOW + timedelta(minutes=10, seconds=5),
            limit=20,
        )

    assert first.evidence_bytes.startswith(b"<?xml")
    assert len(first.items) == 1
    assert first.items[0].upstream_item_id == "post-42"
    assert first.items[0].headline == "Summer market opens"
    assert first.items[0].licensed_summary == "Open this weekend."
    assert first.items[0].gateway_ref == "direct-rss:https://publisher.example/feed.xml"
    assert first.items[0].upstream_publisher_ref == "publisher:example"
    assert first.items[0].published_at == datetime(2026, 8, 2, 17, 55, tzinfo=UTC)
    assert second.not_modified is True
    assert second.next_cursor == first.next_cursor


@pytest.mark.asyncio
async def test_rss_adapter_redacts_feed_query_from_default_gateway_identity() -> None:
    feed = b"""<rss><channel><item><guid>one</guid><title>One</title>
      <link>https://publisher.example/posts/one</link>
      <pubDate>Sun, 02 Aug 2026 17:55:00 GMT</pubDate>
    </item></channel></rss>"""

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=feed, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = RssAtomSourceAdapter(
            source_id="source:rss:authenticated",
            feed_url="https://publisher.example/feed.xml?token=do-not-store",
            signal_kind="publisher_report",
            upstream_publisher_ref="publisher:example",
            allowed_hosts=frozenset({"publisher.example"}),
            http_client=client,
        )
        page = await adapter.fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=20,
        )

    assert page.items[0].gateway_ref == "direct-rss:https://publisher.example/feed.xml"
    assert "do-not-store" not in page.items[0].gateway_ref


@pytest.mark.asyncio
async def test_rss_adapter_keeps_nested_hidden_html_out_of_summary() -> None:
    feed = b"""<rss><channel><item><guid>one</guid><title>One</title>
      <description><![CDATA[<div hidden><div>nested</div>leak</div><p>Shown</p>]]></description>
      <link>https://publisher.example/posts/one</link>
      <pubDate>Sun, 02 Aug 2026 17:55:00 GMT</pubDate>
    </item></channel></rss>"""

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=feed, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = RssAtomSourceAdapter(
            source_id="source:rss:hidden-nesting",
            feed_url="https://publisher.example/feed.xml",
            signal_kind="publisher_report",
            upstream_publisher_ref="publisher:example",
            allowed_hosts=frozenset({"publisher.example"}),
            http_client=client,
        )
        page = await adapter.fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=20,
        )

    assert page.items[0].licensed_summary == "Shown"


@pytest.mark.asyncio
async def test_rsshub_adapter_only_accepts_an_exact_allowlisted_local_route() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    ) as client:
        with pytest.raises(ValueError, match="allowlist"):
            RssHubPullAdapter(
                source_id="source:rsshub:trend",
                base_url="http://127.0.0.1:1200",
                route="/unapproved/route",
                allowed_routes=frozenset({"/approved/route"}),
                signal_kind="social_trend_report",
                upstream_publisher_ref="publisher:upstream-platform",
                allowed_item_hosts=frozenset({"upstream.example"}),
                http_client=client,
            )


@pytest.mark.asyncio
async def test_rsshub_transport_host_is_not_an_allowed_evidence_link_host() -> None:
    feed = b"""<rss><channel><item><guid>one</guid><title>One</title>
      <link>http://127.0.0.1:1200/private/admin</link>
      <pubDate>Sun, 02 Aug 2026 17:55:00 GMT</pubDate>
    </item></channel></rss>"""

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=feed, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = RssHubPullAdapter(
            source_id="source:rsshub:safe-links",
            base_url="http://127.0.0.1:1200",
            route="/approved/route",
            allowed_routes=frozenset({"/approved/route"}),
            signal_kind="social_report",
            upstream_publisher_ref="publisher:upstream-platform",
            allowed_item_hosts=frozenset({"upstream.example"}),
            http_client=client,
        )
        page = await adapter.fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=20,
        )

    assert page.items[0].canonical_url is None


@pytest.mark.asyncio
async def test_atom_adapter_preserves_publisher_identity_and_rejects_unsafe_xml() -> None:
    atom = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>tag:publisher.example,2026:7</id><title>One update</title>
      <summary>Plain evidence</summary>
      <link rel="alternate" href="https://publisher.example/updates/7" />
      <published>2026-08-03T01:00:00Z</published>
      <updated>2026-08-03T01:05:00Z</updated>
    </entry></feed>"""

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=atom, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = RssAtomSourceAdapter(
            source_id="source:atom:publisher",
            feed_url="https://publisher.example/atom.xml",
            signal_kind="publisher_update",
            upstream_publisher_ref="publisher:example",
            allowed_hosts=frozenset({"publisher.example"}),
            http_client=client,
        )
        page = await adapter.fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=20,
        )

    assert page.items[0].upstream_item_id == "tag:publisher.example,2026:7"
    assert page.items[0].updated_at == datetime(2026, 8, 3, 1, 5, tzinfo=UTC)

    unsafe = atom.replace(b'<?xml version="1.0"?>', b"<!DOCTYPE feed [<!ENTITY x 'y'>]>")

    async def unsafe_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=unsafe, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unsafe_response)) as unsafe_client:
        unsafe_adapter = RssAtomSourceAdapter(
            source_id="source:atom:unsafe",
            feed_url="https://publisher.example/unsafe.xml",
            signal_kind="publisher_update",
            upstream_publisher_ref="publisher:example",
            allowed_hosts=frozenset({"publisher.example"}),
            http_client=unsafe_client,
        )
        with pytest.raises(ExternalSignalSourceFailure) as captured:
            await unsafe_adapter.fetch(
                after=None,
                observed_at=NOW,
                deadline_at=NOW + timedelta(seconds=5),
                limit=20,
            )
    assert captured.value.failure_code == "source_xml_unsafe_declaration"


@pytest.mark.asyncio
async def test_feed_reports_a_malformed_entry_without_dropping_valid_siblings() -> None:
    feed = b"""<rss><channel>
      <item><guid>bad</guid><description>missing title and time</description></item>
      <item><guid>good</guid><title>Valid sibling</title>
        <pubDate>Sun, 02 Aug 2026 17:55:00 GMT</pubDate></item>
    </channel></rss>"""

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=feed, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = RssAtomSourceAdapter(
            source_id="source:rss:partial",
            feed_url="https://publisher.example/partial.xml",
            signal_kind="publisher_report",
            upstream_publisher_ref="publisher:example",
            allowed_hosts=frozenset({"publisher.example"}),
            http_client=client,
        )
        page = await adapter.fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=20,
        )

    assert tuple(item.upstream_item_id for item in page.items) == ("good",)
    assert page.parser_rejected_item_count == 1
    assert page.parser_failure_codes == ("feed_item_malformed",)


@pytest.mark.asyncio
async def test_rss_adapter_bounds_payload_and_propagates_retry_after() -> None:
    responses = [
        httpx.Response(
            429,
            headers={"retry-after": "77"},
        ),
        httpx.Response(200, content=b"x" * 2_000_001),
    ]

    async def respond(request: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        response.request = request
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = RssAtomSourceAdapter(
            source_id="source:rss:bounded",
            feed_url="https://publisher.example/feed.xml",
            signal_kind="publisher_report",
            upstream_publisher_ref="publisher:example",
            allowed_hosts=frozenset({"publisher.example"}),
            http_client=client,
        )
        with pytest.raises(ExternalSignalSourceFailure) as rate_limited:
            await adapter.fetch(
                after=None,
                observed_at=NOW,
                deadline_at=NOW + timedelta(seconds=5),
                limit=20,
            )
        assert rate_limited.value.failure_code == "source_rate_limited"
        assert rate_limited.value.retry_after_seconds == 77

        with pytest.raises(ExternalSignalSourceFailure) as oversized:
            await adapter.fetch(
                after=None,
                observed_at=NOW,
                deadline_at=NOW + timedelta(seconds=5),
                limit=20,
            )
        assert oversized.value.failure_code == "source_response_too_large"


@pytest.mark.asyncio
async def test_direct_rss_transport_requires_an_exact_hostname_allowlist() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    ) as client:
        with pytest.raises(ValueError, match="allowlist"):
            RssAtomSourceAdapter(
                source_id="source:rss:blocked-host",
                feed_url="https://unapproved.example/feed.xml",
                signal_kind="publisher_report",
                upstream_publisher_ref="publisher:example",
                allowed_hosts=frozenset({"publisher.example"}),
                http_client=client,
            )
