"""Placeholder-host guard: no real credential is ever aimed at a host the
operator does not control.

The dangerous pattern this covers is a *real* API token paired with an
unedited documentation hostname (``https://apstra.example.com``). RFC 2606
guarantees ``example.com`` itself resolves nowhere useful, but the same
config mistake with a typo'd or attacker-registered host would ship a bearer
token off-box on the first tool call. So the guard has to fire before any
DNS/TCP happens and before an ``Authorization`` header is built.

Covers, end to end:
  1. placeholder detection itself (and its false-positive floor),
  2. zero network I/O + zero auth headers when a base URL is rejected,
  3. no token text in any error envelope, dry-run preview, or redaction gap,
  4. every optional backend routes its base URL through the same guard,
  5. ``hpe-mcp-doctor`` reports the condition instead of "configured, fine".

Every credential value here is the literal string ``test-token`` -- no real
secret appears in this file.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from pathlib import Path

import httpx
import pytest

from hpe_networking_mcp.mcp_servers.shared import (
    redact_sensitive,
    validate_product_base_url,
)
from hpe_networking_mcp.pipeline.url_validation import (
    placeholder_host_reason,
    placeholder_urls_allowed,
    validate_infra_url,
)

# Obviously-fake stand-in for a credential. Never a real token.
FAKE_TOKEN = "test-token"
PLACEHOLDER_HOST = "apstra.example.com"
REAL_HOST = "apstra.corp-internal-example-net.io"


# ---------------------------------------------------------------------------
# 1. Placeholder detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "apstra.example.com",
        "orch.example.com",
        "mm.example.com",
        "EXAMPLE.COM",
        "deep.sub.example.org",
        "thing.example.net",
        "host.example.edu",
        "orchestrator.example",
        "conductor.invalid",
        "sensor.test",
        "changeme.corp.com",
        "change-me.corp.com",
        "your-host.corp.com",
        "yourdomain.net",
        "replace-me.net",
        "placeholder.net",
        "<your-host>",
    ],
)
def test_placeholder_hosts_are_detected(host):
    assert placeholder_host_reason(host) is not None


@pytest.mark.parametrize(
    "host",
    [
        # Real customer-shaped hosts that merely *look* similar. A guard that
        # flags any of these would be worse than no guard at all.
        "example-corp.net",
        "myexample.com",
        "exampleton.co.uk",
        "apstra.acme.com",
        "api.mist.com",
        "internal.testing.acme.com",
        "latest.acme.io",
        "cp.university.edu",
        "conductor.acme.example-labs.com",
        "apigw-uswest4.central.arubanetworks.com",
    ],
)
def test_real_hosts_are_not_flagged(host):
    assert placeholder_host_reason(host) is None


def test_placeholder_url_is_rejected_before_scheme_and_local_rules(monkeypatch):
    """The placeholder message wins over the https/local messages.

    ``http://apstra.example.com`` violates two rules at once. The actionable
    one is "you never replaced the example hostname", so that is what the
    operator must be told.
    """
    monkeypatch.delenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", raising=False)
    monkeypatch.delenv("HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS", raising=False)

    with pytest.raises(ValueError, match="example.com"):
        validate_infra_url(f"http://{PLACEHOLDER_HOST}", label="Apstra base URL")


def test_placeholder_rejection_names_the_setting_and_stays_actionable(monkeypatch):
    monkeypatch.delenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", raising=False)

    with pytest.raises(ValueError) as excinfo:
        validate_product_base_url(f"https://{PLACEHOLDER_HOST}", product="Apstra")

    message = str(excinfo.value)
    assert "Apstra base URL" in message
    assert "Replace it with the real hostname" in message
    assert "no API token is sent to an unverified host" in message


def test_local_lab_opt_in_does_not_also_unlock_placeholder_hosts(monkeypatch):
    """The two opt-ins are independent: a lab operator who allowed local URLs
    has not thereby allowed a documentation host."""
    monkeypatch.delenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", raising=False)
    monkeypatch.setenv("HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS", "1")

    with pytest.raises(ValueError, match="example.com"):
        validate_infra_url(f"https://{PLACEHOLDER_HOST}", label="Apstra base URL")


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_placeholder_opt_in_restores_fixture_hosts(monkeypatch, value):
    monkeypatch.setenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", value)

    assert placeholder_urls_allowed() is True
    assert (
        validate_infra_url(f"https://{PLACEHOLDER_HOST}/", label="Apstra base URL")
        == f"https://{PLACEHOLDER_HOST}"
    )


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_falsy_placeholder_opt_in_still_rejects(monkeypatch, value):
    monkeypatch.setenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", value)

    assert placeholder_urls_allowed() is False
    with pytest.raises(ValueError, match="example.com"):
        validate_infra_url(f"https://{PLACEHOLDER_HOST}", label="Apstra base URL")


def test_real_public_host_still_validates(monkeypatch):
    monkeypatch.delenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", raising=False)

    assert (
        validate_infra_url(f"https://{REAL_HOST}/", label="Apstra base URL")
        == f"https://{REAL_HOST}"
    )


# ---------------------------------------------------------------------------
# Network spy -- refuses real I/O, records everything it is asked to do
# ---------------------------------------------------------------------------


class _StubResponse:
    status_code = 200
    text = "{}"
    content = b"{}"
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}

    def json(self):
        # Shaped to satisfy the token/response parsers on the positive-control
        # path so a backend reaches its *request*, not an early parse error.
        return {"access_token": "stub", "expires_in": 3600, "data": []}


class _NetworkSpy:
    """Stands in for ``httpx.AsyncClient`` and records construction + calls.

    Never performs real I/O. Construction alone is recorded, so a backend that
    opens a client and only *then* discovers a bad URL is still caught.
    """

    def __init__(self) -> None:
        self.constructions: list[dict] = []
        self.requests: list[tuple[str, str, dict]] = []

    @property
    def auth_headers(self) -> list[str]:
        return [
            value
            for _, _, headers in self.requests
            for name, value in headers.items()
            if name.lower() in {"authorization", "x-auth-token"}
        ]

    def install(self, monkeypatch) -> "_NetworkSpy":
        spy = self

        class _SpyClient:
            def __init__(self, *args, **kwargs):
                spy.constructions.append(dict(kwargs))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def get(self, url, headers=None, params=None, **kwargs):
                return spy._record("GET", url, headers)

            async def post(self, url, headers=None, **kwargs):
                return spy._record("POST", url, headers)

            async def request(self, method, url, headers=None, **kwargs):
                return spy._record(method, url, headers)

        monkeypatch.setattr(httpx, "AsyncClient", _SpyClient)
        return self

    def _record(self, method, url, headers):
        self.requests.append((method, str(url), dict(headers or {})))
        return _StubResponse()


# ---------------------------------------------------------------------------
# Backend matrix -- every optional product, one entry each
# ---------------------------------------------------------------------------


def _env(host: str) -> dict:
    """Per-backend env, parameterized on the product hostname under test."""
    return {
        "apstra": {
            "APSTRA_BASE_URL": f"https://{host}",
            "APSTRA_API_TOKEN": FAKE_TOKEN,
            "APSTRA_USERNAME": None,
            "APSTRA_PASSWORD": None,
        },
        "aos8": {
            "AOS8_BASE_URL": f"https://{host}",
            "AOS8_API_TOKEN": FAKE_TOKEN,
            "AOS8_USERNAME": None,
            "AOS8_PASSWORD": None,
        },
        "edgeconnect": {
            "EDGECONNECT_BASE_URL": f"https://{host}",
            "EDGECONNECT_API_TOKEN": FAKE_TOKEN,
        },
        "clearpass": {
            "CLEARPASS_BASE_URL": f"https://{host}",
            "CLEARPASS_API_TOKEN": FAKE_TOKEN,
        },
        "mist": {
            "MIST_HOST": f"https://{host}",
            "MIST_API_TOKEN": FAKE_TOKEN,
        },
        "uxi": {
            "UXI_BASE_URL": f"https://{host}",
            "UXI_TOKEN_URL": f"https://{host}/oauth2/token",
            "UXI_CLIENT_ID": "test-client-id",
            "UXI_CLIENT_SECRET": FAKE_TOKEN,
        },
        "axis": {
            "AXIS_BASE_URL": f"https://{host}",
            "AXIS_API_TOKEN": FAKE_TOKEN,
        },
    }


# module attribute -> awaitable factory for one representative read path that
# must reach the network when configured with a real host.
_INVOCATIONS = {
    "apstra": lambda mod: mod.apstra_get("/api/blueprints"),
    "aos8": lambda mod: mod.aos8_get("/v1/configuration/object/ap_group"),
    "edgeconnect": lambda mod: mod.edgeconnect_doctor(),
    "clearpass": lambda mod: mod.clearpass_get("/api/endpoint"),
    "mist": lambda mod: mod.mist_get("/api/v1/self"),
    "uxi": lambda mod: mod.uxi_get("/sensors"),
    "axis": lambda mod: mod._axis_request("GET", "/api/devices"),
}
OPTIONAL_BACKENDS = sorted(_INVOCATIONS)


def _apply_env(monkeypatch, product: str, host: str) -> None:
    monkeypatch.delenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", raising=False)
    monkeypatch.delenv("HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS", raising=False)
    # Legacy/compat gates that would otherwise short-circuit before the URL
    # guard and make this test vacuously pass.
    monkeypatch.setenv("EDGECONNECT_ALLOW_LEGACY_API", "1")
    for name, value in _env(host)[product].items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _load(product: str):
    return importlib.import_module(f"hpe_networking_mcp.mcp_servers.{product}")


def _call(product: str):
    result = _INVOCATIONS[product](_load(product))
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


# ---------------------------------------------------------------------------
# 2 + 4. Parity: every backend refuses a placeholder host with zero I/O
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("product", OPTIONAL_BACKENDS)
def test_placeholder_base_url_makes_no_request_and_builds_no_auth_header(
    monkeypatch, product
):
    """A placeholder host must short-circuit before DNS/TCP and before any
    Authorization header exists. Proven by an httpx spy that records even
    client *construction*."""
    _apply_env(monkeypatch, product, PLACEHOLDER_HOST)
    spy = _NetworkSpy().install(monkeypatch)

    result = _call(product)

    assert spy.constructions == [], f"{product} opened an HTTP client anyway"
    assert spy.requests == [], f"{product} sent a request to a placeholder host"
    assert spy.auth_headers == [], f"{product} built an auth header anyway"

    assert isinstance(result, (dict, tuple)), f"{product} returned {type(result)!r}"
    error = result[-1] if isinstance(result, tuple) else result.get("error")
    assert error, f"{product} did not report an error"
    assert "example.com" in error
    assert "Replace it with the real hostname" in error


@pytest.mark.parametrize("product", OPTIONAL_BACKENDS)
def test_positive_control_real_host_does_reach_the_network(monkeypatch, product):
    """Guards the test above against passing vacuously.

    With an identical setup but a *real* hostname, the same call must open a
    client -- otherwise the "zero requests" assertion above would pass for a
    backend that never makes requests at all, and would keep passing if a
    future refactor dropped the guard.
    """
    _apply_env(monkeypatch, product, REAL_HOST)
    spy = _NetworkSpy().install(monkeypatch)

    try:
        _call(product)
    except Exception:  # noqa: BLE001 - only "did it try" matters here
        pass

    assert spy.constructions, f"{product} never attempted a request for a real host"


@pytest.mark.parametrize("product", OPTIONAL_BACKENDS)
def test_every_optional_backend_routes_base_url_through_the_shared_guard(product):
    """Structural parity check.

    A future backend that builds its own URL handling, or validates only after
    constructing headers, fails here even if it happens to have no test of its
    own.
    """
    source = Path(
        _load(product).__file__  # type: ignore[arg-type]
    ).read_text()
    assert "validate_product_base_url" in source, (
        f"{product} does not route its base URL through validate_product_base_url"
    )


def test_backend_matrix_covers_every_registered_optional_product():
    """Fails when a new optional backend is added but not covered above."""
    from hpe_networking_mcp.cli import doctor

    credentialed = {
        name for name, envs in doctor.OPTIONAL_PRODUCT_ENVS.items() if envs
    }
    assert credentialed - set(OPTIONAL_BACKENDS) == set(), (
        "new optional product(s) are not covered by the placeholder-URL matrix"
    )


# ---------------------------------------------------------------------------
# 3. No secret leaks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("product", OPTIONAL_BACKENDS)
def test_rejection_envelope_never_echoes_the_credential(monkeypatch, product):
    _apply_env(monkeypatch, product, PLACEHOLDER_HOST)
    _NetworkSpy().install(monkeypatch)

    assert FAKE_TOKEN not in repr(_call(product))


def test_uxi_token_url_placeholder_blocks_the_client_secret_post(monkeypatch):
    """UXI POSTs ``client_secret`` to its token URL, so that URL is at least
    as sensitive as the base URL. A placeholder there must abort first."""
    _apply_env(monkeypatch, "uxi", REAL_HOST)
    monkeypatch.setenv("UXI_TOKEN_URL", "https://sso.example.com/oauth2/token")
    spy = _NetworkSpy().install(monkeypatch)

    result = asyncio.run(_load("uxi").uxi_get("/sensors"))

    assert spy.requests == []
    assert "UXI token URL" in result["error"]
    assert FAKE_TOKEN not in repr(result)


def test_dry_run_preview_carries_no_credential(monkeypatch):
    """Write previews echo url/params/body back to the model; none of that may
    contain the configured token."""
    _apply_env(monkeypatch, "aos8", REAL_HOST)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setenv("HPE_MCP_AOS8_WRITES", "1")
    _NetworkSpy().install(monkeypatch)

    preview = asyncio.run(
        _load("aos8").aos8_write(
            "POST",
            "/v1/configuration/object/ap_group",
            body={"api_token": FAKE_TOKEN},
            dry_run=True,
        )
    )

    assert FAKE_TOKEN not in repr(preview)
    assert preview["json"]["api_token"] == "******"


def test_redact_sensitive_covers_a_renamed_edgeconnect_auth_header(monkeypatch):
    """EDGECONNECT_AUTH_HEADER is operator-configurable, so a hardcoded
    redaction allow-list is bypassable by simply renaming the header.

    The default ``X-Auth-Token`` is caught by the static "token" suffix rule;
    a custom name like ``X-Ec-Session`` is not, and must be picked up from the
    env var instead.
    """
    monkeypatch.setenv("EDGECONNECT_AUTH_HEADER", "X-Ec-Session")

    scrubbed = redact_sensitive(
        {"X-Ec-Session": FAKE_TOKEN, "X-Auth-Token": FAKE_TOKEN, "keep": "visible"}
    )

    assert scrubbed["X-Ec-Session"] == "******"
    assert scrubbed["X-Auth-Token"] == "******"
    assert scrubbed["keep"] == "visible"
    assert FAKE_TOKEN not in repr(scrubbed)


def test_renamed_auth_header_redaction_survives_nesting_and_case(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_AUTH_HEADER", "Ec-Session-Blob")

    scrubbed = redact_sensitive(
        {"echo": {"request": [{"headers": {"ec_session_blob": FAKE_TOKEN}}]}}
    )

    assert FAKE_TOKEN not in repr(scrubbed)


def test_unset_configurable_header_does_not_redact_everything(monkeypatch):
    """The env-driven rule must not degrade into redacting unrelated keys when
    the variable is unset or blank."""
    monkeypatch.delenv("EDGECONNECT_AUTH_HEADER", raising=False)

    assert redact_sensitive({"hostname": "sw-01", "": "x"}) == {
        "hostname": "sw-01",
        "": "x",
    }


# ---------------------------------------------------------------------------
# 5. Doctor surfaces the condition
# ---------------------------------------------------------------------------


def _doctor_env(monkeypatch, **overrides) -> None:
    """Simulate a clean operator environment for the doctor checks.

    Drops the suite-wide ``HPE_MCP_ALLOW_PLACEHOLDER_URLS`` opt-in from
    ``tests/conftest.py`` too: the doctor reports that variable as its own
    finding, so leaving it set would mask every other assertion here.
    """
    from hpe_networking_mcp.cli import doctor

    monkeypatch.delenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", raising=False)
    for envs in doctor.OPTIONAL_PRODUCT_URL_ENVS.values():
        for name in envs:
            monkeypatch.delenv(name, raising=False)
    for envs in doctor.OPTIONAL_PRODUCT_TOKEN_ENVS.values():
        for name in envs:
            monkeypatch.delenv(name, raising=False)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)


def test_doctor_fails_when_an_enabled_product_has_a_placeholder_url(monkeypatch):
    from hpe_networking_mcp.cli import doctor

    _doctor_env(
        monkeypatch,
        APSTRA_BASE_URL=f"https://{PLACEHOLDER_HOST}",
        APSTRA_API_TOKEN=FAKE_TOKEN,
    )

    checks = doctor._placeholder_product_checks({"apstra"})

    assert [c.status for c in checks] == ["FAIL"]
    assert "APSTRA_BASE_URL" in checks[0].detail
    assert FAKE_TOKEN not in checks[0].detail


def test_doctor_warns_when_a_credential_sits_beside_a_placeholder_url(monkeypatch):
    """The audited case: product not enabled, but a real token is already
    configured against a host the operator does not control."""
    from hpe_networking_mcp.cli import doctor

    _doctor_env(
        monkeypatch,
        EDGECONNECT_BASE_URL="https://orch.example.com",
        EDGECONNECT_API_TOKEN=FAKE_TOKEN,
    )

    checks = doctor._placeholder_product_checks(set())

    assert [c.status for c in checks] == ["WARN"]
    assert "EDGECONNECT_BASE_URL" in checks[0].detail
    assert FAKE_TOKEN not in checks[0].detail


def test_doctor_is_silent_for_placeholder_url_without_any_credential(monkeypatch):
    from hpe_networking_mcp.cli import doctor

    _doctor_env(monkeypatch, AOS8_BASE_URL="https://mm.example.com")

    assert doctor._placeholder_product_checks(set()) == []


def test_doctor_is_silent_for_real_hosts(monkeypatch):
    from hpe_networking_mcp.cli import doctor

    _doctor_env(
        monkeypatch,
        CLEARPASS_BASE_URL=f"https://{REAL_HOST}",
        CLEARPASS_API_TOKEN=FAKE_TOKEN,
    )

    assert doctor._placeholder_product_checks({"clearpass"}) == []


def test_doctor_required_env_stops_calling_a_placeholder_url_configured(monkeypatch):
    """Regression for the audit finding: an enabled product wired to
    ``*.example.com`` used to report "required env vars are set"."""
    from hpe_networking_mcp.cli import doctor

    _doctor_env(
        monkeypatch,
        APSTRA_BASE_URL=f"https://{PLACEHOLDER_HOST}",
        APSTRA_API_TOKEN=FAKE_TOKEN,
    )
    monkeypatch.setenv("HPE_MCP_PRODUCTS", "apstra")
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)

    checks = {c.name: c for c in doctor._runtime_checks()}

    assert checks["apstra required env"].status == "WARN"
    assert "APSTRA_BASE_URL" in checks["apstra required env"].detail
    assert checks["apstra base URL"].status == "FAIL"


def test_doctor_placeholder_helper_reports_names_never_values(monkeypatch):
    from hpe_networking_mcp.cli import doctor

    _doctor_env(monkeypatch, AOS8_BASE_URL=f"https://{PLACEHOLDER_HOST}")

    assert doctor._placeholder_url_env_vars("aos8") == ["AOS8_BASE_URL"]
    assert doctor._placeholder_url_env_vars("mist") == []


def test_doctor_reports_the_placeholder_bypass_env_var(monkeypatch):
    """The escape hatch must not be silent -- an operator who sets it has
    turned the guard off for every product at once."""
    from hpe_networking_mcp.cli import doctor

    _doctor_env(monkeypatch)
    monkeypatch.setenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", "1")

    checks = doctor._placeholder_product_checks(set())

    assert [c.name for c in checks] == ["Placeholder URL guard"]
    assert checks[0].status == "WARN"


@pytest.mark.parametrize("value", ["", "0", "false", "off"])
def test_doctor_is_quiet_when_the_bypass_is_unset_or_falsy(monkeypatch, value):
    from hpe_networking_mcp.cli import doctor

    _doctor_env(monkeypatch)
    monkeypatch.setenv("HPE_MCP_ALLOW_PLACEHOLDER_URLS", value)

    assert doctor._placeholder_product_checks(set()) == []
