"""Reactive enrichment for failed MCP tool calls, grounded in the spec index.

When a tool call fails with a non-2xx status, callers can use
:func:`reactive_hint` to append what the API *documents* that status code
meaning -- so a model learns what 429/403/409 mean instead of retrying
blind -- degrading to a generic HTTP-semantics fallback when the spec index
has no confident answer, and to nothing at all when neither applies. This is
the reactive half of an enrichment design whose proactive half is
``specs_index.get_schema``/``get_enum`` (helping a model that consults the
spec *before* it guesses); this instead catches the model that already
guessed and hit an error.

Adapted from a sibling fork's ``spec_index/error_help.py``, not ported
verbatim: that implementation derives ``platform`` by checking whether
``tool_name`` starts with a known platform prefix (its tools are literally
named ``mist_...``/``central_...``). This repo's MCP tools are named
``verb_noun`` with no platform prefix at all (the owning server provides
that context instead), so ``platform`` here is a required-by-convention
keyword argument the caller resolves itself -- in practice the MCP router,
which already maps a dispatched tool name to its owning backend server and
from there to a platform key for an unrelated feature (execution contracts).
See ``tool_router.py``'s ``_server_platform``/``_router_call_labels``.

Also deliberately narrower than upstream: this module does not attempt the
request-body-field enrichment upstream adds for 400/422 (reversing a tool
name back to its OpenAPI request schema via ``inspect.getsource()`` regexes
or a ``mist_<snake(operationId)>`` naming reversal). Both of those
mechanisms depend on architectural conventions this repo's tools don't
share (a platform-prefixed name, or a single generic resource-passing
wrapper function every config tool routes through) -- a comparable
mechanism here would mean regexing each tool's own source for a literal
API path, which is a materially larger and lower-confidence undertaking.
Left as a documented follow-up rather than a fragile port.
"""

from __future__ import annotations

from hpe_networking_mcp.pipeline.clients import specs_index

# Generic HTTP semantics, independent of any spec -- always available so a
# platform/status-code combination the spec index has no answer for (no
# platform resolved, no dominant spec-grounded description, or an index
# that predates this feature's ``responses`` table) still gets *some*
# guidance instead of silently nothing.
_GENERIC_STATUS_HINTS: dict[int, str] = {
    400: "Bad request -- check required fields, types, and enum values in the request.",
    401: "Unauthorized -- the credential or token is missing, expired, or invalid.",
    403: "Forbidden -- the credential lacks permission for this resource or scope.",
    404: "Not found -- verify the ID, serial, or name and that it exists in this scope.",
    405: "Method not allowed -- this operation isn't supported on this resource.",
    408: "Request timeout -- the upstream device or service took too long to respond.",
    409: "Conflict -- the resource already exists or is in a state that blocks this change.",
    422: "Unprocessable -- the request was well-formed but failed semantic validation.",
    429: "Rate limited -- back off and retry after a delay.",
    500: "Server error -- the upstream service failed unexpectedly; retrying may help.",
    502: "Bad gateway -- an upstream dependency returned an invalid response.",
    503: "Service unavailable -- the upstream service is temporarily down or overloaded.",
    504: "Gateway timeout -- an upstream dependency didn't respond in time.",
}


def _coerce_code(status_code: object) -> int | None:
    if isinstance(status_code, bool):
        return None
    if isinstance(status_code, int):
        return status_code
    try:
        return int(str(status_code))
    except (TypeError, ValueError):
        return None


def reactive_hint(
    tool_name: str | None,
    status_code: object,
    *,
    platform: str | None = None,
    db_path=specs_index.DB_PATH,
) -> str | None:
    """A short enrichment string for a failed tool call, or ``None``.

    Args:
        tool_name: The tool that failed. Currently unused beyond acceptance;
            reserved for a future request-body-field enrichment for 400/422
            (see module docstring), kept in the signature now so wiring
            call sites won't need a signature change when that lands.
        status_code: The upstream/validation status (int or a digit
            string/other status-like value); anything else, a boolean, or
            a 2xx code yields ``None``.
        platform: Resolved by the caller (e.g. the MCP router's own
            tool-name -> backend-server -> platform resolution) -- never
            derived here. ``None`` disables the spec-grounded half; the
            generic fallback still applies.
        db_path: Override for the structured spec index (tests only).

    Never raises: any failure resolving the spec-grounded half (missing,
    corrupt, or pre-rebuild index without the ``responses`` table) silently
    yields no spec-grounded text -- it must never block the generic
    fallback or the caller's own result.
    """
    _ = tool_name  # reserved, see docstring
    code = _coerce_code(status_code)
    if code is None or 200 <= code < 300:
        return None
    spec_desc = None
    if platform:
        try:
            spec_desc = specs_index.get_response_description(platform, code, db_path=db_path)
        except Exception:
            spec_desc = None
    generic = _GENERIC_STATUS_HINTS.get(code)
    if spec_desc:
        spec_desc = spec_desc.strip()
    if spec_desc and generic:
        return f"{generic} This API documents {code} here as: {spec_desc}"
    if spec_desc:
        return f"This API documents {code} here as: {spec_desc}"
    return generic
