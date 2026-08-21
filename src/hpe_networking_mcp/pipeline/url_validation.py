"""Shared HTTP(S) base/token URL validation for Central, GLP, and the
optional-product backends.

Deliberately dependency-free with respect to the rest of the package (no
imports from ``hpe_networking_mcp.mcp_servers`` or
``hpe_networking_mcp.pipeline.config``) so both of those modules -- which
already import *each other* indirectly (``mcp_servers.shared`` imports
``pipeline.config.build_account_contexts``) -- can import this module
without creating an import cycle.

Beyond scheme/host safety this also refuses *placeholder* hosts -- the
``*.example.com`` / ``changeme`` / ``your-host`` values this repo's own
docs, ``.example`` config files and module docstrings ship -- so a real API
token is never attached to a host the operator does not actually control.
That check runs inside :func:`validate_infra_url`, i.e. before any backend
builds an ``Authorization`` header or opens a socket.

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
_ALLOW_PLACEHOLDER_URLS_ENV = "HPE_MCP_ALLOW_PLACEHOLDER_URLS"
_FALSY_ENV_VALUES = {"0", "false", "no", "off"}
_LOCAL_HOST_NAMES = {"localhost", "localhost.localdomain"}

# RFC 2606 / RFC 6761 reserved documentation + testing names. None of these
# can belong to a real deployment, and every one of them appears verbatim in
# this repo's docstrings, `.example` config files and test fixtures -- so a
# base URL still pointing at one means the operator never edited it.
_RESERVED_PLACEHOLDER_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
)
# Deliberately excludes `.local`, `.internal` and `.home.arpa`: those are
# reserved too, but real private/lab deployments legitimately use them, and
# the local/private-host rules below already cover the loopback cases.
_RESERVED_PLACEHOLDER_TLDS = frozenset({"example", "invalid", "test"})
# Substrings that effectively never occur in a real hostname but do occur in
# unedited copy/paste config. Kept deliberately narrow so a legitimate
# customer host is never rejected -- note a bare "example" is NOT a marker,
# because "example-corp.net" is a perfectly valid real domain.
_PLACEHOLDER_HOST_MARKERS = (
    "changeme",
    "change-me",
    "change_me",
    "replaceme",
    "replace-me",
    "replace_me",
    "placeholder",
    "yourhost",
    "your-host",
    "your_host",
    "yourdomain",
    "your-domain",
    "your_domain",
    "your-org",
    "your-company",
    "your-tenant",
    "fill-me-in",
    "fqdn-here",
    "hostname-here",
    "<",
    ">",
)


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


def placeholder_urls_allowed() -> bool:
    """True if ``HPE_MCP_ALLOW_PLACEHOLDER_URLS`` opts into placeholder hosts.

    Same "set at all, and not one of the falsy strings" semantics as
    :func:`local_urls_allowed`. This exists for this repo's own automated
    tests and doc fixtures -- which use RFC 2606 ``*.example.com`` hosts as
    stand-ins for real products -- and must never be set in a deployment
    that holds real API tokens.
    """
    raw = os.environ.get(_ALLOW_PLACEHOLDER_URLS_ENV)
    if raw is None:
        return False
    return raw.strip().lower() not in _FALSY_ENV_VALUES


def placeholder_host_reason(host: str) -> str | None:
    """Explain why ``host`` looks like an unedited documentation placeholder.

    Pure and environment-independent, so the caller that merely *reports*
    (``hpe-mcp-doctor``) and the caller that *enforces*
    (:func:`validate_infra_url`) share one definition instead of drifting.

    Args:
        host: a bare hostname with no scheme, port or brackets, e.g.
            ``"apstra.example.com"``. Case-insensitive.

    Returns:
        A short human-readable reason phrase, or ``None`` when ``host`` does
        not look like a placeholder.
    """
    candidate = host.strip().lower().strip("[]").rstrip(".")
    if not candidate:
        return None
    for marker in _PLACEHOLDER_HOST_MARKERS:
        if marker in candidate:
            return f"contains the placeholder marker {marker!r}"
    tld = candidate.rsplit(".", 1)[-1]
    if tld in _RESERVED_PLACEHOLDER_TLDS:
        return f"uses the reserved documentation/testing TLD '.{tld}'"
    for domain in _RESERVED_PLACEHOLDER_DOMAINS:
        if candidate == domain or candidate.endswith(f".{domain}"):
            return f"is under the reserved documentation domain {domain!r}"
    return None


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
            credentials, is still an unedited documentation placeholder
            (see :func:`placeholder_host_reason`), or resolves to a
            local/private host without the opt-in.
    """
    base_url = value.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not include credentials")
    if parsed.scheme not in {"https", "http"}:
        raise ValueError(f"{label} must be an absolute HTTP(S) URL")
    if not parsed.netloc:
        raise ValueError(f"{label} must include a host; relative URLs cannot be used")

    host = (parsed.hostname or "").strip().lower()

    # Placeholder check first, and before *any* caller builds an
    # Authorization header or opens a socket: a real token paired with a
    # host the operator does not control is the dangerous case, and
    # "you never replaced the example hostname" is a far more actionable
    # message than a downstream DNS/TLS failure.
    if not placeholder_urls_allowed():
        reason = placeholder_host_reason(host)
        if reason is not None:
            raise ValueError(
                f"{label} host {host!r} {reason}; it is still an example value "
                "from the setup docs, not a real deployment. Replace it with "
                "the real hostname -- no API token is sent to an unverified "
                f"host. ({_ALLOW_PLACEHOLDER_URLS_ENV}=1 exists only for "
                "automated tests and doc fixtures.)"
            )

    allow_local = local_urls_allowed()
    if parsed.scheme != "https" and not allow_local:
        raise ValueError(
            f"{label} must use https; set {_ALLOW_LOCAL_URLS_ENV}=1 only for "
            "local lab testing"
        )

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
