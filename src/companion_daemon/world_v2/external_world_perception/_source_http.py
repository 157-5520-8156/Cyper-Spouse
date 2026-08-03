"""Shared conditional-HTTP transport for fixed authoritative source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json

import httpx

from .contracts import ExternalSignalSourceFailure, MAX_EVIDENCE_BYTES, SourceCursor


@dataclass(frozen=True, slots=True)
class SourceHttpResult:
    evidence_bytes: bytes
    media_type: str
    cursor: SourceCursor | None
    not_modified: bool = False


async def fetch_source_bytes(
    *,
    http_client: httpx.AsyncClient,
    url: str,
    after: SourceCursor | None,
    observed_at: datetime,
    deadline_at: datetime,
    accept: str,
    extra_headers: dict[str, str] | None = None,
) -> SourceHttpResult:
    remaining = (deadline_at - observed_at).total_seconds()
    if remaining <= 0:
        raise ExternalSignalSourceFailure("source_deadline_elapsed")
    headers = {"Accept": accept}
    if extra_headers:
        headers.update(extra_headers)
    headers.update(_conditional_headers(after))
    try:
        async with http_client.stream(
            "GET",
            url,
            headers=headers,
            timeout=remaining,
            follow_redirects=False,
        ) as response:
            if response.status_code == 304:
                return SourceHttpResult(
                    evidence_bytes=b"",
                    media_type="application/octet-stream",
                    cursor=after,
                    not_modified=True,
                )
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after", "").strip()
                raise ExternalSignalSourceFailure(
                    "source_rate_limited",
                    retry_after_seconds=(
                        int(retry_after)
                        if retry_after.isdecimal() and int(retry_after) > 0
                        else None
                    ),
                )
            if 300 <= response.status_code < 400:
                raise ExternalSignalSourceFailure("source_redirect_rejected")
            if response.status_code < 200 or response.status_code >= 300:
                raise ExternalSignalSourceFailure(f"source_http_{response.status_code}"[:128])
            declared_length = response.headers.get("content-length", "").strip()
            if declared_length.isdecimal() and int(declared_length) > MAX_EVIDENCE_BYTES:
                raise ExternalSignalSourceFailure("source_response_too_large")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_EVIDENCE_BYTES:
                    raise ExternalSignalSourceFailure("source_response_too_large")
                chunks.append(chunk)
            evidence = b"".join(chunks)
            cursor = SourceCursor(
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
            return SourceHttpResult(
                evidence_bytes=evidence,
                media_type=response.headers.get("content-type", "application/octet-stream")[:256],
                cursor=cursor,
            )
    except ExternalSignalSourceFailure:
        raise
    except httpx.TimeoutException as exc:
        raise ExternalSignalSourceFailure("source_timeout") from exc
    except httpx.HTTPError as exc:
        raise ExternalSignalSourceFailure("source_transport_error") from exc


def _conditional_headers(after: SourceCursor | None) -> dict[str, str]:
    if after is None:
        return {}
    try:
        value = json.loads(after.opaque_value)
    except json.JSONDecodeError as exc:
        raise ExternalSignalSourceFailure("source_cursor_invalid") from exc
    if not isinstance(value, dict):
        raise ExternalSignalSourceFailure("source_cursor_invalid")
    headers: dict[str, str] = {}
    if isinstance(value.get("etag"), str) and value["etag"]:
        headers["If-None-Match"] = value["etag"]
    if isinstance(value.get("last_modified"), str) and value["last_modified"]:
        headers["If-Modified-Since"] = value["last_modified"]
    return headers


__all__ = ["SourceHttpResult", "fetch_source_bytes"]
