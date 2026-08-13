"""Reusable async HTTP executors for generated OpenAPI tools.

The shared :mod:`hpe_networking_mcp.mcp_servers.openapi_gen.runtime` registers each manifest
operation as a MCPServer tool that dispatches through a platform-supplied
``read_executor`` / ``write_executor``. Central and GLP need the *same*
execution behavior -- response bounding, content-type handling (JSON,
merge-patch, SCIM, form, multipart, raw), auth injected last, a path allow-list,
and dry_run/confirm write gating -- differing only in *which* account/token they
use. This module factors that behavior out so each platform module only has to
supply:

* an ``async resolve(extra_headers) -> (base_url, headers)`` that reuses that
  platform's existing client/token pattern and injects trusted auth **last**;
* a callable returning the allowed path prefixes (defense-in-depth);
* a write-gate predicate + blocked-response builder.

Nothing here is model-visible; auth headers are never returned to the caller.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json as _json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from hpe_networking_mcp.mcp_servers.shared import (
    bound_collection_response,
    bounded_response_payload,
    clamp_limit,
    redact_sensitive,
)
from hpe_networking_mcp.pipeline.clients.http_retry import parse_retry_after

# resolve(path, extra_headers) -> (base_url, headers-with-auth-last). May raise on
# missing configuration; the executor converts that into an {"error": ...} dict.
AuthResolver = Callable[
    [str, dict[str, str] | None], Awaitable[tuple[str, dict[str, str]]]
]
AuthRefresher = Callable[[], Awaitable[None]]
PrefixGetter = Callable[[], tuple[str, ...]]
WritesAllowed = Callable[[], bool]
BlockedResponse = Callable[[str], dict[str, Any]]

_JSON_LIKE_CONTENT_TYPES = {
    "application/json",
    "application/merge-patch+json",
    "application/scim+json",
    "application/json-patch+json",
}

_TIMEOUT = 30.0
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_RETRYABLE_STATUS = {429, 502, 503, 504}
_MAX_RETRIES = 3
_MAX_RETRY_SLEEP = 5.0


def _retry_delay(resp: Any, attempt: int) -> float | None:
    """Return a bounded delay, or None when the server's hint exceeds it."""
    raw = str((getattr(resp, "headers", {}) or {}).get("Retry-After", "")).strip()
    hint = parse_retry_after(raw)
    if raw and hint is not None:
        return hint if hint <= _MAX_RETRY_SLEEP else None
    return min(_MAX_RETRY_SLEEP, float(2**attempt))


def _clean_params(query: dict[str, Any]) -> dict[str, Any]:
    # None already dropped by the runtime; keep False / 0 / [].
    return {k: v for k, v in query.items() if v is not None}


def _path_ok(path: str, prefixes: tuple[str, ...]) -> bool:
    return bool(prefixes) and path.startswith(prefixes)


def build_multipart_files(
    body: Any, *, max_upload_bytes: int = _MAX_UPLOAD_BYTES
) -> tuple[dict[str, tuple[Any, ...]] | None, dict[str, Any] | None]:
    """Convert an MCP-safe multipart body into httpx file tuples."""
    if not isinstance(body, dict):
        return None, {"error": "multipart/form-data body must be an object of form fields"}
    files: dict[str, tuple[Any, ...]] = {}
    for key, value in body.items():
        field = str(key)
        if isinstance(value, bytes):
            files[field] = (field, value, "application/octet-stream")
        elif isinstance(value, dict) and "content_base64" in value:
            filename = str(value.get("filename") or field)
            if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
                return None, {"error": f"invalid multipart filename for field {field!r}"}
            try:
                content = base64.b64decode(str(value["content_base64"]), validate=True)
            except (binascii.Error, ValueError):
                return None, {"error": f"multipart field {field!r} has invalid base64 content"}
            if len(content) > max_upload_bytes:
                return None, {
                    "error": (
                        f"multipart field {field!r} exceeds the "
                        f"{max_upload_bytes}-byte upload limit"
                    )
                }
            files[field] = (
                filename,
                content,
                str(value.get("content_type") or "application/octet-stream"),
            )
        elif isinstance(value, (dict, list)):
            files[field] = (None, _json.dumps(value), "application/json")
        else:
            files[field] = (None, "" if value is None else str(value))
    return files, None


def apply_request_body(
    kwargs: dict[str, Any],
    req_headers: dict[str, str],
    body: Any,
    content_type: str,
) -> dict[str, Any] | None:
    """Attach ``body`` to httpx request kwargs per ``content_type``.

    Returns an error dict on an invalid body shape, else ``None``.
    """
    if body is None:
        return None
    if content_type in _JSON_LIKE_CONTENT_TYPES:
        kwargs["json"] = body
        # httpx defaults JSON bodies to application/json; honor the declared
        # variant (merge-patch / scim) explicitly.
        if content_type != "application/json":
            req_headers["Content-Type"] = content_type
    elif content_type == "multipart/form-data":
        files, error = build_multipart_files(body)
        if error is not None:
            return error
        kwargs["files"] = files  # httpx sets multipart Content-Type + boundary
    elif content_type == "application/x-www-form-urlencoded":
        if not isinstance(body, dict):
            return {"error": "form-urlencoded body must be an object of form fields"}
        kwargs["data"] = body
        req_headers.setdefault("Content-Type", content_type)
    else:
        kwargs["content"] = body if isinstance(body, (bytes, str)) else str(body)
        req_headers.setdefault("Content-Type", content_type)
    return None


def make_read_executor(
    *,
    resolve: AuthResolver,
    allowed_prefixes: PrefixGetter,
    not_configured: str = "backend not configured",
    refresh_auth: AuthRefresher | None = None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Build the read executor (read-classified methods, bounded, direct)."""

    async def _read(
        method: str,
        path: str,
        query: dict[str, Any],
        headers: dict[str, str],
        body: Any = None,
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        prefixes = allowed_prefixes()
        if not _path_ok(path, prefixes):
            return {"error": f"Generated path must begin with one of {prefixes}."}
        params = _clean_params(query)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                for attempt in range(_MAX_RETRIES + 1):
                    base_url, req_headers = await resolve(path, headers)
                    url = f"{base_url}{path}"
                    request_kwargs: dict[str, Any] = {
                        "headers": req_headers,
                        "params": params,
                    }
                    body_error = apply_request_body(
                        request_kwargs, req_headers, body, content_type
                    )
                    if body_error is not None:
                        return body_error
                    resp = await client.request(method, url, **request_kwargs)
                    if (
                        resp.status_code == 401
                        and refresh_auth is not None
                        and attempt < _MAX_RETRIES
                    ):
                        await refresh_auth()
                        continue
                    if (
                        method in {"GET", "HEAD", "OPTIONS"}
                        and resp.status_code in _RETRYABLE_STATUS
                        and attempt < _MAX_RETRIES
                    ):
                        delay = _retry_delay(resp, attempt)
                        if delay is None:
                            break
                        await asyncio.sleep(delay)
                        continue
                    break
            payload = redact_sensitive(bound_collection_response(
                bounded_response_payload(resp), limit=clamp_limit(None), offset=0
            ))
            return {"status_code": resp.status_code, "data": payload, "url": url}
        except httpx.HTTPError as exc:
            return {"error": str(exc), "url": locals().get("url", path)}
        except (FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
            return {"error": f"{not_configured}: {exc}"}

    return _read


def make_write_executor(
    *,
    resolve: AuthResolver,
    allowed_prefixes: PrefixGetter,
    writes_allowed: WritesAllowed,
    blocked_response: BlockedResponse,
    execute_hint: str,
    not_configured: str = "backend not configured",
    refresh_auth: AuthRefresher | None = None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Build the write executor (gate + dry_run/confirm + content types)."""

    async def _write(
        name: str,
        method: str,
        path: str,
        query: dict[str, Any],
        headers: dict[str, str],
        body: Any,
        content_type: str,
        dry_run: bool,
        confirm: bool,
    ) -> dict[str, Any]:
        if not writes_allowed():
            return blocked_response(name)
        prefixes = allowed_prefixes()
        if not _path_ok(path, prefixes):
            return {"error": f"Generated path must begin with one of {prefixes}."}
        params = _clean_params(query)
        try:
            base_url, req_headers = await resolve(path, headers)
        except (FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
            return {"error": f"{not_configured}: {exc}"}
        url = f"{base_url}{path}"
        preview: dict[str, Any] = {
            "method": method,
            "path": path,
            "url": url,
            "params": redact_sensitive(params),
            "json": redact_sensitive(body),
            "content_type": content_type,
        }
        if dry_run:
            return {"dry_run": True, **preview, "execute_hint": execute_hint}
        if not confirm:
            return {
                "error": "confirm=True is required when dry_run=False.",
                "dry_run": True,
                **preview,
            }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                for attempt in range(_MAX_RETRIES + 1):
                    base_url, req_headers = await resolve(path, headers)
                    url = f"{base_url}{path}"
                    kwargs: dict[str, Any] = {"headers": req_headers, "params": params}
                    body_error = apply_request_body(
                        kwargs, req_headers, body, content_type
                    )
                    if body_error is not None:
                        return body_error
                    resp = await client.request(method, url, **kwargs)
                    if (
                        method == "PUT"
                        and resp.status_code == 401
                        and refresh_auth is not None
                        and attempt < _MAX_RETRIES
                    ):
                        await refresh_auth()
                        continue
                    if (
                        method == "PUT"
                        and resp.status_code in _RETRYABLE_STATUS
                        and attempt < _MAX_RETRIES
                    ):
                        delay = _retry_delay(resp, attempt)
                        if delay is None:
                            break
                        await asyncio.sleep(delay)
                        continue
                    break
            return {
                "status_code": resp.status_code,
                "data": redact_sensitive(bounded_response_payload(resp)),
                "url": url,
            }
        except httpx.HTTPError as exc:
            return {"error": str(exc), "url": locals().get("url", path)}
        except (FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
            return {"error": f"{not_configured}: {exc}"}

    return _write
