#!/usr/bin/env python
"""Run a non-billing route/configuration preflight for the media providers.

The probes deliberately do not send an Authorization header and never POST an
image-generation payload.  Response bodies are hashed and discarded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from companion_daemon.config import Settings  # noqa: E402


def _safe_endpoint(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value.split("?", 1)[0].split("#", 1)[0]
    return urlunsplit((parsed.scheme, parsed.hostname or parsed.netloc, parsed.path, "", ""))


def _host(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def _timeout_class(exception: BaseException) -> str | None:
    if isinstance(exception, httpx.ConnectTimeout):
        return "connect"
    if isinstance(exception, httpx.ReadTimeout):
        return "read"
    if isinstance(exception, httpx.WriteTimeout):
        return "write"
    if isinstance(exception, httpx.PoolTimeout):
        return "pool"
    if isinstance(exception, httpx.TimeoutException):
        return "timeout"
    return None


def _dns_probe(host: str | None) -> dict[str, object]:
    if not host:
        return {"status": "invalid_endpoint", "address_count": 0}
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return {"status": "error", "exception_class": type(exc).__name__, "address_count": 0}
    return {"status": "ok", "address_count": len(addresses)}


def _route_probe(*, base_url: str, proxy_url: str | None, path: str, method: str) -> dict[str, object]:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    options: dict[str, object] = {"timeout": timeout, "trust_env": False}
    if proxy_url:
        options["proxy"] = proxy_url
    started = time.perf_counter()
    try:
        with httpx.Client(**options) as client:
            response = client.request(method, url, headers={"Accept": "application/json"})
            body = response.content
        return {
            "method": method,
            "path": path,
            "status": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "response_bytes": len(body),
            "response_hash": "sha256:" + hashlib.sha256(body).hexdigest(),
        }
    except Exception as exc:
        return {
            "method": method,
            "path": path,
            "status": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "exception_class": type(exc).__name__,
            "timeout_class": _timeout_class(exc),
        }


def run() -> dict[str, object]:
    settings = Settings(database_path=Path("/tmp/girl-agent-wt-e-preflight.sqlite"))
    base_url = _safe_endpoint(settings.openai_base_url) or ""
    proxy_url = settings.openai_proxy_url
    host = _host(base_url)
    return {
        "scope": "no_auth_route_preflight",
        "credentials": {
            "openai_configured": bool(settings.openai_api_key),
            "deepseek_configured": bool(settings.deepseek_api_key),
        },
        "render": {
            "base_url": base_url,
            "endpoint_hostname": host,
            "model": settings.image_model,
            "dns": _dns_probe(host),
            "proxy_configured": bool(proxy_url),
            "proxy_hostname": _host(proxy_url),
            "configured_timeout_seconds": 180.0,
            "route_probes": [
                _route_probe(
                    base_url=base_url,
                    proxy_url=proxy_url,
                    path="models",
                    method="GET",
                ),
                _route_probe(
                    base_url=base_url,
                    proxy_url=proxy_url,
                    path="images/generations",
                    method="OPTIONS",
                ),
            ],
        },
        "inspection": {
            "base_url": base_url,
            "endpoint_hostname": host,
            "model": settings.world_v2_media_inspection_model,
            "client_timeout_seconds": 45.0,
            "transport_timeout_seconds": 150.0,
            "proxy_configured": bool(proxy_url),
        },
        "qq_delivery": {"attempted": False},
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, sort_keys=True))
