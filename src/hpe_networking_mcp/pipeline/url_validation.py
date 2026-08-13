"""Shared HTTP(S) base/token URL validation for Central, GLP, and the
optional-product backends.

Deliberately dependency-free with respect to the rest of the package (no
imports from ``hpe_networking_mcp.mcp_servers`` or
``hpe_networking_mcp.pipeline.config``) so both of those modules -- which
already import *each other* indirectly (``mcp_servers.shared`` imports
``pipeline.config.build_account_contexts``) -- can import this module
without creating an import cycle.

Every managed/optional product base URL and every GLP token URL is expected
to go through :func:`validate_infra_url` (directly, or via the
``validate_product_base_url`` alias kept in ``mcp_servers.shared`` for
existing callers) so "must be HTTPS, must not be a local/private host" is
enforced the same way everywhere, with the same
``HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS`` opt-in for local lab testing.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

_ALLOW_LOCAL_URLS_ENV = "HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS"
_FALSY_ENV_VALUES = {"0", "false", "no", "off"}
_LOCAL_HOST_NAMES = {"localhost", "localhost.localdomain"}


def local_urls_allowed() -> bool:
    """True if ``HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS`` opts into local/lab URLs.

    Mirrors ``hpe_networking_mcp.mcp_servers.shared._env_bool``'s "set at all,
    and not one of the falsy strings" semantics so the same env var behaves
    identically everywhere it gates a URL check.
    """
    raw = os.environ.get(_ALLOW_LOCAL_URLS_ENV)
    if raw is None:
        return False
    return raw.strip().lower() not in _FALSY_ENV_VALUES


def validate_infra_url(value: str, *, label: str) -> str:
    """Validate a base/token URL used to reach a managed product or GLP.

    Requires an absolute ``http(s)://host...`` URL with no embedded
    credentials. Non-HTTPS schemes and local/private/loopback hosts are
    rejected unless ``HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS`` is set -- that
    opt-in exists for local lab testing only and must never be set in a
    production deployment.

    Args:
        value: the raw URL string (YAML value or env var value).
        label: a human-readable name for the setting being validated, used
            verbatim in any raised error so the operator knows exactly which
            setting to fix (e.g. ``"Central base URL (central_account.base_url
            / SOURCE_BASE_URL)"``).

    Returns:
        The URL with any trailing slash stripped.

    Raises:
        ValueError: ``value`` is not an absolute http(s) URL, embeds
            credentials, or resolves to a local/private host without the
            opt-in.
    """
    base_url = value.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not include credentials")
    if parsed.scheme not in {"https", "http"}:
        raise ValueError(f"{label} must be an absolute HTTP(S) URL")
    if not parsed.netloc:
        raise ValueError(f"{label} must include a host; relative URLs cannot be used")

    allow_local = local_urls_allowed()
    if parsed.scheme != "https" and not allow_local:
        raise ValueError(
            f"{label} must use https; set {_ALLOW_LOCAL_URLS_ENV}=1 only for "
            "local lab testing"
        )

    host = (parsed.hostname or "").strip().lower()
    if host in _LOCAL_HOST_NAMES and not allow_local:
        raise ValueError(
            f"{label} host {host!r} is local; set {_ALLOW_LOCAL_URLS_ENV}=1 only "
            "for local lab testing"
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return base_url
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    ) and not allow_local:
        raise ValueError(
            f"{label} host {host!r} is not public; set {_ALLOW_LOCAL_URLS_ENV}=1 "
            "only for local lab testing"
        )
    return base_url
