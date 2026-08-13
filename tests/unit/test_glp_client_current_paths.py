from __future__ import annotations

from hpe_networking_mcp.pipeline.clients.glp_client import GLPClient


class _FakeCentral:
    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, params: dict | None = None):
        self.calls.append((path, params))
        return {"items": [{"value": "AP-505", "count": 3}]}


def _client() -> tuple[GLPClient, _FakeCentral]:
    fake = _FakeCentral()
    client = GLPClient.__new__(GLPClient)
    client._client = fake
    client.workspace_id = "workspace"
    client._device_id_cache = {}
    return client, fake


def test_group_devices_uses_documented_v2beta1_path():
    client, fake = _client()

    result = client.group_devices_v2beta1(
        group_by="model", limit=25, offset=5, filter="deviceType eq 'AP'"
    )

    assert result == [{"value": "AP-505", "count": 3}]
    assert fake.calls == [
        (
            "/devices/v2beta1/devices/group",
            {
                "group-by": "model",
                "limit": 25,
                "offset": 5,
                "filter": "deviceType eq 'AP'",
            },
        )
    ]


def test_audit_detail_uses_plural_details_path():
    client, fake = _client()

    client.get_audit_log_v2beta1_detail("audit-1")

    assert fake.calls[0][0] == "/audit-log/v2beta1/logs/audit-1/details"
