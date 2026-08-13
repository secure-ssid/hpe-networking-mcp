"""Tests for `edgeconnect_doctor()` (Swagger discovery probe) and the
`EDGECONNECT_ALLOW_LEGACY_API` compatibility gate.
"""

from __future__ import annotations

import asyncio

import hpe_networking_mcp.mcp_servers.edgeconnect as edgeconnect


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = "{}"

    def json(self):
        return {}


def test_edgeconnect_doctor_reports_unconfigured_without_probing(monkeypatch):
    monkeypatch.delenv("EDGECONNECT_BASE_URL", raising=False)
    monkeypatch.delenv("EDGECONNECT_API_TOKEN", raising=False)

    out = asyncio.run(edgeconnect.edgeconnect_doctor())

    assert out["configured"] is False
    assert out["swagger_discovery_probe"] == {}


def test_edgeconnect_doctor_legacy_mode_disabled_by_default(monkeypatch):
    monkeypatch.delenv("EDGECONNECT_ALLOW_LEGACY_API", raising=False)
    monkeypatch.delenv("EDGECONNECT_BASE_URL", raising=False)
    monkeypatch.delenv("EDGECONNECT_API_TOKEN", raising=False)

    out = asyncio.run(edgeconnect.edgeconnect_doctor())

    assert out["legacy_mode_enabled"] is False
    assert "legacy_mode_note" in out


def test_edgeconnect_doctor_legacy_mode_enabled_via_explicit_flag(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_ALLOW_LEGACY_API", "1")
    monkeypatch.delenv("EDGECONNECT_BASE_URL", raising=False)
    monkeypatch.delenv("EDGECONNECT_API_TOKEN", raising=False)

    out = asyncio.run(edgeconnect.edgeconnect_doctor())

    assert out["legacy_mode_enabled"] is True


def test_edgeconnect_operational_get_is_blocked_without_legacy_opt_in(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.delenv("EDGECONNECT_ALLOW_LEGACY_API", raising=False)

    out = asyncio.run(edgeconnect.edgeconnect_list_appliances())

    assert out["status"] == "blocked"
    assert out["flag"] == "EDGECONNECT_ALLOW_LEGACY_API"


def test_edgeconnect_doctor_probes_swagger_paths_and_reports_status_codes(monkeypatch):
    calls = []

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            calls.append(url)
            if url.endswith("gmsApiInfo.json"):
                return _Resp(200)
            return _Resp(404)

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_doctor())

    assert out["configured"] is True
    assert out["swagger_discovery_probe"]["/gms/apidocs/gmsApiInfo.json"] == 200
    assert out["swagger_discovery_probe"]["/vxoa/apidocs/vxoaApiInfo.json"] == 404
    assert out["swagger_discovery_probe"]["/gms/rest/swagger-resources"] == 404
    assert len(calls) == 3


def test_edgeconnect_doctor_probe_failure_reports_error_string_not_raise(monkeypatch):
    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            raise edgeconnect.httpx.ConnectError("boom")

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_doctor())

    probe = out["swagger_discovery_probe"]["/gms/apidocs/gmsApiInfo.json"]
    assert probe == "error: ConnectError"


def test_edgeconnect_legacy_api_allowed_helper_parses_common_truthy_values():
    for value in ("1", "true", "TRUE", "yes"):
        import os

        os.environ["EDGECONNECT_ALLOW_LEGACY_API"] = value
        assert edgeconnect._edgeconnect_legacy_api_allowed() is True
    for value in ("0", "false", "", "no"):
        import os

        os.environ["EDGECONNECT_ALLOW_LEGACY_API"] = value
        assert edgeconnect._edgeconnect_legacy_api_allowed() is False
    import os

    os.environ.pop("EDGECONNECT_ALLOW_LEGACY_API", None)
