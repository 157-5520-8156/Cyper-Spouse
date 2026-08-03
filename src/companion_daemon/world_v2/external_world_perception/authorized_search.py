"""Adapter for already-accepted, read-only web-search result evidence.

This is deliberately not a search executor.  It can only observe immutable
``ToolResultAccepted`` projections that already passed Action authorization,
provider settlement, and result-hash verification.  The resulting signals
still enter the ordinary Hub/attention path; a search result is never a World
fact and never proves that the character noticed it.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from ..schema_core import FrozenModel
from ..schemas import LedgerProjection
from ..ledger import LedgerPort
from .contracts import ExternalSignalSourceItem, ExternalSignalSourcePage, SourceCursor


class ImmutableToolResultContent(FrozenModel):
    result_ref: str = Field(min_length=1, max_length=1_024)
    body: bytes = Field(max_length=2_000_000)
    body_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hash_matches_exact_bytes(self) -> ImmutableToolResultContent:
        actual = "sha256:" + hashlib.sha256(self.body).hexdigest()
        if actual != self.body_hash:
            raise ValueError("immutable tool result hash does not match exact bytes")
        return self


class AcceptedToolResultProjectionReader(Protocol):
    def current_projection(self, *, world_id: str) -> LedgerProjection: ...


class ImmutableToolResultReader(Protocol):
    def read_exact(self, *, result_ref: str) -> ImmutableToolResultContent | None: ...


class LedgerAcceptedToolResultProjectionReader:
    """Production read side for settled tool descriptors at the current head."""

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger

    def current_projection(self, *, world_id: str) -> LedgerProjection:
        if world_id != self._ledger.world_id:
            raise ValueError("accepted search projection requested another World")
        return self._ledger.project()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _item_ref(*, accepted_event_ref: str, ordinal: int, value: object) -> str:
    digest = hashlib.sha256(_canonical(value)).hexdigest()
    return f"{accepted_event_ref}:item:{ordinal}:{digest}"


class AcceptedWebSearchResultAdapter:
    """Poll accepted web-search descriptors without gaining Action authority."""

    def __init__(
        self,
        *,
        source_id: str,
        world_id: str,
        projection_reader: AcceptedToolResultProjectionReader,
        content_reader: ImmutableToolResultReader,
    ) -> None:
        if not source_id or not world_id:
            raise ValueError("accepted web-search source requires source and world ids")
        self.source_id = source_id
        self._world_id = world_id
        self._projection_reader = projection_reader
        self._content_reader = content_reader

    @property
    def world_id(self) -> str:
        return self._world_id

    async def fetch(
        self,
        *,
        after: SourceCursor | None,
        observed_at: datetime,
        deadline_at: datetime,
        limit: int,
    ) -> ExternalSignalSourcePage:
        if deadline_at <= observed_at:
            raise ValueError("accepted web-search read deadline elapsed")
        projection = self._projection_reader.current_projection(world_id=self._world_id)
        cursor_value = str(projection.ledger_sequence)
        if after is not None and after.opaque_value == cursor_value:
            return ExternalSignalSourcePage(
                evidence_media_type="application/json",
                evidence_bytes=b"",
                next_cursor=after,
                not_modified=True,
            )

        requests = {item.request_id: item for item in projection.read_only_tool_requests}
        items: list[ExternalSignalSourceItem] = []
        evidence: list[dict[str, object]] = []
        rejected = 0
        failures: set[str] = set()
        for result in projection.tool_results:
            if len(items) >= limit:
                break
            request = requests.get(result.request_id)
            if request is None or request.target != "tool:web_search":
                continue
            content = self._content_reader.read_exact(result_ref=result.result_ref)
            if (
                content is None
                or content.result_ref != result.result_ref
                or content.body_hash != result.result_hash
            ):
                rejected += 1
                failures.add("accepted_search_result_evidence_unavailable")
                continue
            try:
                decoded = json.loads(content.body)
                rows = decoded["items"]
                if not isinstance(rows, list):
                    raise TypeError("items must be a list")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                rejected += 1
                failures.add("accepted_search_result_shape_invalid")
                continue
            evidence.append(
                {
                    "accepted_event_ref": result.accepted_event_ref,
                    "result_ref": result.result_ref,
                    "result_hash": result.result_hash,
                    "body": content.body.decode("utf-8"),
                }
            )
            for ordinal, row in enumerate(rows):
                if len(items) >= limit:
                    break
                try:
                    items.append(
                        self._normalize_item(
                            row=row,
                            ordinal=ordinal,
                            accepted_event_ref=result.accepted_event_ref,
                            accepted_at=result.accepted_at,
                            result_ref=result.result_ref,
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    rejected += 1
                    failures.add("accepted_search_item_invalid")

        # A cursor checkpoint itself is exact evidence that no qualifying
        # accepted result was present.  This avoids treating an unrelated V2
        # revision as a source transport failure.
        page_evidence = _canonical(
            {
                "world_id": self._world_id,
                "ledger_sequence": projection.ledger_sequence,
                "accepted_results": evidence,
            }
        )
        return ExternalSignalSourcePage(
            evidence_media_type="application/vnd.girl-agent.accepted-web-search+json",
            evidence_bytes=page_evidence,
            next_cursor=SourceCursor(opaque_value=cursor_value),
            items=tuple(items),
            parser_rejected_item_count=rejected,
            parser_failure_codes=tuple(sorted(failures)),
        )

    @staticmethod
    def _normalize_item(
        *,
        row: object,
        ordinal: int,
        accepted_event_ref: str,
        accepted_at: datetime,
        result_ref: str,
    ) -> ExternalSignalSourceItem:
        if not isinstance(row, dict):
            raise TypeError("search item must be an object")
        title = str(row["title"]).strip()
        url = str(row["url"]).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("search item URL is unsafe")
        published_raw = row.get("published_at")
        published = (
            datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
            if published_raw is not None
            else accepted_at
        )
        publisher = str(row.get("publisher") or parsed.hostname).strip()
        summary = str(row.get("snippet") or "").strip()
        identity = {
            "accepted_event_ref": accepted_event_ref,
            "ordinal": ordinal,
            "url": url,
            "title": title,
        }
        return ExternalSignalSourceItem(
            upstream_item_id=_item_ref(
                accepted_event_ref=accepted_event_ref, ordinal=ordinal, value=identity
            ),
            gateway_ref=result_ref,
            upstream_publisher_ref=publisher,
            signal_kind="authorized_web_search_result",
            headline=title,
            licensed_summary=summary,
            canonical_url=url,
            published_at=published,
        )


__all__ = [
    "AcceptedToolResultProjectionReader",
    "AcceptedWebSearchResultAdapter",
    "ImmutableToolResultContent",
    "ImmutableToolResultReader",
    "LedgerAcceptedToolResultProjectionReader",
]
