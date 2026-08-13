"""Load credentials and build AccountContext objects for source and target accounts."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from hpe_networking_mcp.pipeline.models import AccountContext
from hpe_networking_mcp.pipeline.url_validation import validate_infra_url

# GLP workspace IDs are UUID-ish opaque identifiers. They are interpolated
# into the derived OAuth token URL path, so anything outside this character
# set (a slash, "..", a "?" or "#", whitespace) could silently retarget the
# credential exchange at a different path or host. Reject those loudly
# instead of building a wrong-but-plausible URL.
_SAFE_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_workspace_id(workspace_id: str, label: str) -> str:
    """Return ``workspace_id`` if it is safe to embed in a URL path."""
    if not _SAFE_WORKSPACE_ID.match(workspace_id):
        raise ValueError(
            f"{label} {workspace_id!r} contains characters that are not allowed in a "
            "GLP workspace ID (expected letters, digits, '.', '-' or '_'). "
            "Refusing to build a token URL from it."
        )
    return workspace_id


def load_credentials(creds_path: str = "config/credentials.yaml") -> dict[str, Any]:
    """Load credentials from YAML + environment variable overrides.

    Precedence (highest first): process environment > ``.env`` > YAML >
    built-in defaults. ``load_dotenv(override=False)`` only *fills in*
    variables the process environment does not already have -- a value the
    caller's shell (or an orchestrator/container) already set always wins
    over whatever ``.env`` says, matching
    ``hpe_networking_mcp.mcp_servers.shared``'s own ``load_dotenv`` call.
    ``override=True`` here previously let a stale ``.env`` value silently
    clobber an intentionally-set process env var on every call.
    """
    load_dotenv(override=False)

    config: dict[str, Any] = {}
    creds_file = Path(creds_path)
    if creds_file.exists():
        with open(creds_file) as f:
            config = yaml.safe_load(f) or {}

    # Back-compat for clearer YAML key names. The codebase's original
    # "source_account" / "target_account" names are a holdover from the
    # cross-account migration hpe_networking_mcp.pipeline. For MCP-only use the clearer
    # "central_account" (for the Central/source creds) and "glp_account"
    # (for the GLP/target creds) keys are accepted as aliases. If both
    # are present in a file, the legacy key wins so existing installs
    # aren't surprised by a rename.
    _SECTION_ALIASES = {
        "source_account": ("central_account",),
        "target_account": ("glp_account",),
    }

    def _section(name: str) -> dict[str, Any]:
        sect = config.get(name)
        if sect:
            return sect
        for alias in _SECTION_ALIASES.get(name, ()):
            alias_sect = config.get(alias)
            if alias_sect:
                return alias_sect
        return {}

    def _get(section: str, key: str, env_var: str, default: str = "") -> str:
        # env var wins; then the canonical section name; then any alias.
        env_val = os.getenv(env_var)
        if env_val:
            return env_val
        return _section(section).get(key, default)

    glp_section = config.get("glp", {})

    return {
        "source": {
            "base_url": _get("source_account", "base_url", "SOURCE_BASE_URL"),
            "client_id": _get("source_account", "client_id", "SOURCE_CLIENT_ID"),
            "client_secret": _get("source_account", "client_secret", "SOURCE_CLIENT_SECRET"),
            "glp_workspace_id": _get("source_account", "glp_workspace_id", "SOURCE_GLP_WORKSPACE"),
        },
        "target": {
            "base_url": _get("target_account", "base_url", "TARGET_BASE_URL"),
            "client_id": _get("target_account", "client_id", "TARGET_CLIENT_ID"),
            "client_secret": _get("target_account", "client_secret", "TARGET_CLIENT_SECRET"),
            "glp_workspace_id": _get("target_account", "glp_workspace_id", "TARGET_GLP_WORKSPACE"),
        },
        "glp": {
            "token_url": os.getenv("GLP_TOKEN_URL", glp_section.get("token_url", "")),
            "base_url": os.getenv(
                "GLP_BASE_URL",
                glp_section.get("base_url", "https://global.api.greenlake.hpe.com"),
            ),
        },
    }


def build_account_contexts(
    creds_path: str = "config/credentials.yaml",
) -> tuple[AccountContext, AccountContext]:
    """Build source and target AccountContext from credentials.

    Returns:
        (source_context, target_context)
    """
    creds = load_credentials(creds_path)
    glp_base_url = creds["glp"]["base_url"].strip().rstrip("/")
    configured_token_url = creds["glp"]["token_url"].strip()
    source_base_url = creds["source"]["base_url"].strip()
    target_base_url = creds["target"]["base_url"].strip()

    # Validate every base/token URL once, up front, so a bad value fails with
    # a clear message naming the offending setting rather than surfacing
    # later as an opaque auth failure (or, worse, a request to the wrong
    # host). Central and GLP go through the exact same
    # ``validate_infra_url`` check the optional-product backends use
    # (``hpe_networking_mcp.mcp_servers.shared.validate_product_base_url``) --
    # HTTPS + a public host required by default, with local/lab URLs allowed
    # only through the same HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS opt-in.
    # Empty values are left alone here (unset is a separate, valid state
    # handled by the callers below / TokenManager); only a *non-empty*
    # malformed or non-local-opted-in value is rejected.
    if configured_token_url:
        validate_infra_url(
            configured_token_url, label="GLP token URL (glp.token_url / GLP_TOKEN_URL)"
        )
    if glp_base_url:
        validate_infra_url(glp_base_url, label="GLP base URL (glp.base_url / GLP_BASE_URL)")
    if source_base_url:
        validate_infra_url(
            source_base_url,
            label="Central source base URL (source_account.base_url / SOURCE_BASE_URL)",
        )
    if target_base_url:
        validate_infra_url(
            target_base_url,
            label="Central target base URL (target_account.base_url / TARGET_BASE_URL)",
        )

    def _glp_token_url(workspace_id: str, label: str) -> str:
        """Resolve the GLP OAuth token URL for one account context.

        Precedence is unchanged: an explicitly configured token URL wins;
        otherwise it is derived from the GLP base URL and workspace ID.
        Returning "" when there is no workspace ID is also unchanged and
        deliberate — an empty value is safe (the GLP client simply has no
        token endpoint) whereas a guessed one is not.
        """
        if configured_token_url:
            return configured_token_url
        if not workspace_id:
            return ""
        if not glp_base_url:
            raise ValueError(
                f"Cannot derive a GLP token URL for the {label} account: "
                "glp.base_url / GLP_BASE_URL is empty and glp.token_url is unset."
            )
        _validate_workspace_id(workspace_id, f"{label} glp_workspace_id")
        return f"{glp_base_url}/authorization/v2/oauth2/{workspace_id}/token"

    source = AccountContext(
        label="source",
        base_url=creds["source"]["base_url"],
        client_id=creds["source"]["client_id"],
        client_secret=creds["source"]["client_secret"],
        glp_workspace_id=creds["source"]["glp_workspace_id"],
        glp_token_url=_glp_token_url(creds["source"]["glp_workspace_id"], "source"),
        glp_base_url=glp_base_url,
    )

    target = AccountContext(
        label="target",
        base_url=creds["target"]["base_url"],
        client_id=creds["target"]["client_id"],
        client_secret=creds["target"]["client_secret"],
        glp_workspace_id=creds["target"]["glp_workspace_id"],
        glp_token_url=_glp_token_url(creds["target"]["glp_workspace_id"], "target"),
        glp_base_url=glp_base_url,
    )

    return source, target
