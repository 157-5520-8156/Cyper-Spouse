"""Bounded RSS/Atom acquisition adapters.

Feed material is untrusted evidence.  These adapters parse transport payloads;
they do not decide truth, relevance, character attention, or behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import httpx

from .contracts import (
    ExternalSignalSourceFailure,
    ExternalSignalSourceItem,
    ExternalSignalSourcePage,
    MAX_EVIDENCE_BYTES,
    SourceCursor,
)


_UNSAFE_XML_MARKERS = (b"<!doctype", b"<!entity")


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0
        self._elements: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        attributes = {name.casefold(): value for name, value in attrs}
        inline_style = (attributes.get("style") or "").casefold().replace(" ", "")
        hidden = (
            normalized_tag in {"script", "style", "template"}
            or "hidden" in attributes
            or (attributes.get("aria-hidden") or "").casefold() in {"true", "1"}
            or "display:none" in inline_style
            or "visibility:hidden" in inline_style
        )
        self._elements.append((normalized_tag, hidden))
        if hidden:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index][0] != normalized_tag:
                continue
            closed = self._elements[index:]
            del self._elements[index:]
            self._ignored_depth = max(
                0,
                self._ignored_depth - sum(1 for _, hidden in closed if hidden),
            )
            break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    def text(self, *, limit: int) -> str:
        return " ".join(" ".join(self._parts).split())[:limit]


class RssAtomSourceAdapter:
    """Fetch one fixed RSS/Atom URL using conditional HTTP validators."""

    def __init__(
        self,
        *,
        source_id: str,
        feed_url: str,
        signal_kind: str,
        upstream_publisher_ref: str,
        allowed_hosts: frozenset[str],
        http_client: httpx.AsyncClient,
        gateway_ref: str | None = None,
        transport_allowed_hosts: frozenset[str] | None = None,
        allow_undated_items: bool = False,
    ) -> None:
        if not source_id or not signal_kind or not upstream_publisher_ref:
            raise ValueError("RSS source identity is incomplete")
        if not allowed_hosts:
            raise ValueError("RSS source hostname allowlist cannot be empty")
        normalized_allowed_hosts = frozenset(host.casefold() for host in allowed_hosts)
        normalized_transport_hosts = (
            frozenset(host.casefold() for host in transport_allowed_hosts)
            if transport_allowed_hosts is not None
            else normalized_allowed_hosts
        )
        _validate_http_url(feed_url, allowed_hosts=normalized_transport_hosts)
        self.source_id = source_id
        self._feed_url = feed_url
        self._signal_kind = signal_kind
        self._upstream_publisher_ref = upstream_publisher_ref
        self._http_client = http_client
        self._allowed_hosts = normalized_allowed_hosts
        self._gateway_ref = gateway_ref or _default_gateway_ref(feed_url)
        self._allow_undated_items = allow_undated_items

    async def fetch(
        self,
        *,
        after: SourceCursor | None,
        observed_at: datetime,
        deadline_at: datetime,
        limit: int,
    ) -> ExternalSignalSourcePage:
        remaining = (deadline_at - observed_at).total_seconds()
        if remaining <= 0:
            raise ExternalSignalSourceFailure("source_deadline_elapsed")
        headers = _conditional_headers(after)
        try:
            async with self._http_client.stream(
                "GET",
                self._feed_url,
                headers=headers,
                timeout=remaining,
                follow_redirects=False,
            ) as response:
                if response.status_code == 304:
                    return ExternalSignalSourcePage(
                        evidence_media_type="application/octet-stream",
                        evidence_bytes=b"",
                        next_cursor=after,
                        not_modified=True,
                    )
                if response.status_code == 429:
                    raise ExternalSignalSourceFailure(
                        "source_rate_limited",
                        retry_after_seconds=_retry_after_seconds(response),
                    )
                if 300 <= response.status_code < 400:
                    raise ExternalSignalSourceFailure("source_redirect_rejected")
                if response.status_code < 200 or response.status_code >= 300:
                    raise ExternalSignalSourceFailure(f"source_http_{response.status_code}"[:128])
                payload = await _read_bounded(response)
                media_type = response.headers.get("content-type", "application/xml")
                cursor = _response_cursor(response, observed_at=observed_at)
        except ExternalSignalSourceFailure:
            raise
        except httpx.TimeoutException as exc:
            raise ExternalSignalSourceFailure("source_timeout") from exc
        except httpx.HTTPError as exc:
            raise ExternalSignalSourceFailure("source_transport_error") from exc

        items, rejected_count = _parse_feed(
            payload,
            source_id=self.source_id,
            signal_kind=self._signal_kind,
            gateway_ref=self._gateway_ref,
            upstream_publisher_ref=self._upstream_publisher_ref,
            allowed_hosts=self._allowed_hosts,
            limit=limit,
            allow_undated_items=self._allow_undated_items,
        )
        return ExternalSignalSourcePage(
            evidence_media_type=media_type[:256],
            evidence_bytes=payload,
            next_cursor=cursor,
            items=items,
            parser_rejected_item_count=rejected_count,
            parser_failure_codes=("feed_item_malformed",) if rejected_count else (),
        )


class RssHubPullAdapter(RssAtomSourceAdapter):
    """RSSHub transport restricted to a deployment-owned local route allowlist."""

    def __init__(
        self,
        *,
        source_id: str,
        base_url: str,
        route: str,
        allowed_routes: frozenset[str],
        signal_kind: str,
        upstream_publisher_ref: str,
        allowed_item_hosts: frozenset[str],
        http_client: httpx.AsyncClient,
        allow_undated_items: bool = False,
    ) -> None:
        _validate_local_rsshub_base(base_url)
        if route not in allowed_routes:
            raise ValueError("RSSHub route is not in the deployment allowlist")
        route_parts = urlsplit(route)
        if (
            not route.startswith("/")
            or route_parts.scheme
            or route_parts.netloc
            or route_parts.fragment
        ):
            raise ValueError("RSSHub route must be one fixed local path")
        normalized_base = base_url.rstrip("/")
        base_host = urlsplit(normalized_base).hostname
        if base_host is None:
            raise ValueError("RSSHub base URL has no hostname")
        super().__init__(
            source_id=source_id,
            feed_url=f"{normalized_base}{route}",
            signal_kind=signal_kind,
            upstream_publisher_ref=upstream_publisher_ref,
            allowed_hosts=allowed_item_hosts,
            http_client=http_client,
            gateway_ref=f"rsshub:{normalized_base}",
            transport_allowed_hosts=frozenset({base_host}),
            allow_undated_items=allow_undated_items,
        )


async def _read_bounded(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_EVIDENCE_BYTES:
            raise ExternalSignalSourceFailure("source_response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_feed(
    payload: bytes,
    *,
    source_id: str,
    signal_kind: str,
    gateway_ref: str,
    upstream_publisher_ref: str,
    allowed_hosts: frozenset[str],
    limit: int,
    allow_undated_items: bool,
) -> tuple[tuple[ExternalSignalSourceItem, ...], int]:
    lowered = payload.lower()
    if any(marker in lowered for marker in _UNSAFE_XML_MARKERS):
        raise ExternalSignalSourceFailure("source_xml_unsafe_declaration")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ExternalSignalSourceFailure("source_xml_malformed") from exc
    root_name = _local_name(root.tag)
    if root_name == "feed":
        elements = [child for child in root if _local_name(child.tag) == "entry"]
        parser = _parse_atom_entry
    elif root_name in {"rss", "rdf"}:
        elements = [element for element in root.iter() if _local_name(element.tag) == "item"]
        parser = _parse_rss_item
    else:
        raise ExternalSignalSourceFailure("source_feed_format_unsupported")
    parsed: list[ExternalSignalSourceItem] = []
    rejected_count = 0
    for element in elements[:limit]:
        try:
            values = parser(element, allow_undated_items)
            published_at = values["published_at"]
            headline = str(values["headline"])
            canonical_url = _safe_item_url(
                values.get("canonical_url"),
                allowed_hosts=allowed_hosts,
            )
            upstream_item_id = str(values.get("upstream_item_id") or "").strip()
            if not upstream_item_id:
                upstream_item_id = _fallback_item_id(
                    source_id=source_id,
                    headline=headline,
                    canonical_url=canonical_url,
                    published_at=published_at,
                )
            parsed.append(
                ExternalSignalSourceItem(
                    upstream_item_id=upstream_item_id[:1_024],
                    gateway_ref=gateway_ref,
                    upstream_publisher_ref=upstream_publisher_ref,
                    signal_kind=signal_kind,
                    headline=headline[:1_000],
                    licensed_summary=str(values.get("summary") or "")[:8_000],
                    canonical_url=canonical_url,
                    published_at=published_at,
                    updated_at=values.get("updated_at"),
                )
            )
        except (KeyError, TypeError, ValueError):
            # One malformed feed entry cannot poison the whole provider page.
            rejected_count += 1
            continue
    return tuple(parsed), rejected_count


def _parse_rss_item(element: ET.Element, allow_undated_items: bool) -> dict[str, object]:
    headline = _plain(_child_text(element, "title"), limit=1_000)
    if not headline:
        raise ValueError("RSS item has no title")
    published_text = _child_text(element, "pubDate") or _child_text(element, "date")
    if not published_text and not allow_undated_items:
        raise ValueError("feed item has no publication time")
    published = _parse_source_datetime(published_text) if published_text else None
    return {
        "upstream_item_id": _child_text(element, "guid"),
        "headline": headline,
        "summary": _plain(_child_text(element, "description"), limit=8_000),
        "canonical_url": _child_text(element, "link"),
        "published_at": published,
    }


def _parse_atom_entry(element: ET.Element, allow_undated_items: bool) -> dict[str, object]:
    headline = _plain(_child_text(element, "title"), limit=1_000)
    if not headline:
        raise ValueError("Atom entry has no title")
    published_text = _child_text(element, "published") or _child_text(element, "updated")
    if not published_text and not allow_undated_items:
        raise ValueError("feed item has no publication time")
    published = _parse_source_datetime(published_text) if published_text else None
    updated_text = _child_text(element, "updated")
    updated = _parse_source_datetime(updated_text) if updated_text else None
    canonical_url = None
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        relation = child.attrib.get("rel", "alternate")
        if relation == "alternate" and child.attrib.get("href"):
            canonical_url = child.attrib["href"]
            break
    return {
        "upstream_item_id": _child_text(element, "id"),
        "headline": headline,
        "summary": _plain(
            _child_text(element, "summary") or _child_text(element, "content"),
            limit=8_000,
        ),
        "canonical_url": canonical_url,
        "published_at": published,
        "updated_at": updated,
    }


def _child_text(element: ET.Element, name: str) -> str:
    wanted = name.casefold()
    for child in element:
        if _local_name(child.tag) == wanted:
            return "".join(child.itertext()).strip()
    return ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _plain(value: str, *, limit: int) -> str:
    parser = _PlainText()
    parser.feed(value)
    parser.close()
    return parser.text(limit=limit)


def _parse_source_datetime(value: str) -> datetime:
    if not value:
        raise ValueError("feed item has no publication time")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _conditional_headers(after: SourceCursor | None) -> dict[str, str]:
    if after is None:
        return {}
    try:
        cursor = json.loads(after.opaque_value)
    except json.JSONDecodeError as exc:
        raise ExternalSignalSourceFailure("source_cursor_invalid") from exc
    if not isinstance(cursor, dict):
        raise ExternalSignalSourceFailure("source_cursor_invalid")
    headers: dict[str, str] = {}
    if isinstance(cursor.get("etag"), str) and cursor["etag"]:
        headers["If-None-Match"] = cursor["etag"]
    if isinstance(cursor.get("last_modified"), str) and cursor["last_modified"]:
        headers["If-Modified-Since"] = cursor["last_modified"]
    return headers


def _response_cursor(response: httpx.Response, *, observed_at: datetime) -> SourceCursor:
    return SourceCursor(
        opaque_value=json.dumps(
            {
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
                "observed_at": observed_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after", "").strip()
    return int(value) if value.isdecimal() and int(value) > 0 else None


def _safe_item_url(value: object, *, allowed_hosts: frozenset[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        _validate_http_url(candidate, allowed_hosts=allowed_hosts)
    except ValueError:
        return None
    return candidate[:4_096]


def _fallback_item_id(
    *,
    source_id: str,
    headline: str,
    canonical_url: str | None,
    published_at: datetime | None,
) -> str:
    material = "\x1f".join(
        (source_id, canonical_url or "", headline, published_at.isoformat() if published_at else "")
    )
    return "derived:sha256:" + hashlib.sha256(material.encode()).hexdigest()


def _validate_http_url(
    value: str,
    *,
    allowed_hosts: frozenset[str] | None = None,
) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("source URL must be an absolute HTTP(S) URL without userinfo")
    if allowed_hosts is not None and parsed.hostname.casefold() not in allowed_hosts:
        raise ValueError("source URL hostname is not in the deployment allowlist")


def _default_gateway_ref(feed_url: str) -> str:
    parsed = urlsplit(feed_url)
    public_endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return f"direct-rss:{public_endpoint}"


def _validate_local_rsshub_base(value: str) -> None:
    _validate_http_url(value)
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    local = hostname.casefold() == "localhost"
    if not local:
        try:
            local = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            local = False
    if not local or parsed.query or parsed.fragment:
        raise ValueError("RSSHub base URL must identify a local deployment")


__all__ = ["RssAtomSourceAdapter", "RssHubPullAdapter"]
