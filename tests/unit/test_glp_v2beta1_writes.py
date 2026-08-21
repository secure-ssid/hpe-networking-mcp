"""Unit tests for the v2beta1 GLP device PATCH write path.

These mutations (``archive_device``, ``unarchive_device``,
``assign_subscription``, ``unassign_subscription``) all go through
``GLPClient._patch_devices_v2beta1`` against
``PATCH /devices/v2beta1/devices?id=<uuid>`` with
``Content-Type: application/merge-patch+json``.

Test bar:
- Feature flag off (default) → ``NotImplementedError`` with payload preview
- Feature flag on + serial resolves → 202 polled to completion
- Feature flag on + serial unresolvable → ``RuntimeError`` (not silent success)
- Correct body shape for each of the 4 operations per HPE docs
- ``resolve_device_id`` caches results to avoid a round trip per call
- 4xx from GLP raises with status + body preview

No live calls. No writes anywhere.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hpe_networking_mcp.pipeline.clients.glp_client import _V2BETA1_WRITES_FLAG, GLPClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    """Make sure the write flag isn't leaked across tests."""
    monkeypatch.delenv(_V2BETA1_WRITES_FLAG, raising=False)
    yield
    monkeypatch.delenv(_V2BETA1_WRITES_FLAG, raising=False)


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setenv(_V2BETA1_WRITES_FLAG, "1")


def _make_glp_client(list_devices_response=None, patch_response=None, poll_response=None):
    """Build a GLPClient whose inner ``_client`` is fully mocked."""
    glp = GLPClient.__new__(GLPClient)  # bypass __init__ to skip real client
    glp.workspace_id = "test-workspace"
    glp._device_id_cache = {}

    inner = MagicMock()
    glp._client = inner

    # GET for serial→id resolution
    if list_devices_response is None:
        list_devices_response = {
            "items": [{"id": "uuid-abc-123", "serialNumber": "SERIAL1"}]
        }
    inner.get.return_value = list_devices_response

    # PATCH response (202 async with Location)
    if patch_response is None:
        patch_response = MagicMock()
        patch_response.status_code = 202
        patch_response.headers = {"Location": "/devices/v1/async-operations/task-xyz"}
        patch_response.text = ""
        patch_response.json.return_value = {}
    inner._request.return_value = patch_response

    # poll_task: short-circuit the sleep loop
    if poll_response is None:
        poll_response = {"status": "completed", "transactionId": "task-xyz"}

    def fake_poll(task_id, timeout=None, interval=None):
        return poll_response

    glp.poll_task = fake_poll
    return glp, inner


# ---------------------------------------------------------------------------
# Feature flag gate
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    def test_writes_off_by_default_raises(self, clean_env):
        glp, _ = _make_glp_client()
        with pytest.raises(NotImplementedError) as exc:
            glp.archive_device("SERIAL1")
        assert _V2BETA1_WRITES_FLAG in str(exc.value)
        assert "archived" in str(exc.value)  # payload preview in message

    def test_writes_on_proceeds(self, clean_env, writes_on):
        glp, inner = _make_glp_client()
        result = glp.archive_device("SERIAL1")
        assert result["status"] == "completed"
        # The PATCH actually fired
        inner._request.assert_called_once()

    def test_flag_accepts_various_truthy_values(self, clean_env, monkeypatch):
        for val in ("1", "true", "TRUE", "yes", "True"):
            monkeypatch.setenv(_V2BETA1_WRITES_FLAG, val)
            glp, _ = _make_glp_client()
            glp.archive_device("SERIAL1")  # shouldn't raise

    def test_flag_rejects_falsy(self, clean_env, monkeypatch):
        for val in ("0", "false", "no", "", "off"):
            monkeypatch.setenv(_V2BETA1_WRITES_FLAG, val)
            glp, _ = _make_glp_client()
            with pytest.raises(NotImplementedError):
                glp.archive_device("SERIAL1")


# ---------------------------------------------------------------------------
# Serial → device ID resolution
# ---------------------------------------------------------------------------


class TestResolveDeviceId:
    def test_returns_device_id_from_filter_query(self, clean_env):
        glp, inner = _make_glp_client(
            list_devices_response={"items": [{"id": "uuid-123", "serialNumber": "X"}]}
        )
        assert glp.resolve_device_id("X") == "uuid-123"
        inner.get.assert_called_once()
        call_params = inner.get.call_args.kwargs["params"]
        assert "filter" in call_params
        assert "serialNumber eq 'X'" in call_params["filter"]

    def test_returns_none_when_device_not_found(self, clean_env):
        glp, _ = _make_glp_client(list_devices_response={"items": []})
        assert glp.resolve_device_id("NOPE") is None

    def test_cache_avoids_second_get(self, clean_env):
        glp, inner = _make_glp_client()
        glp.resolve_device_id("SERIAL1")
        glp.resolve_device_id("SERIAL1")
        assert inner.get.call_count == 1

    def test_cache_miss_swallowed_not_raised(self, clean_env):
        """Network error during lookup returns None, not raise."""
        glp, inner = _make_glp_client()
        inner.get.side_effect = RuntimeError("network exploded")
        assert glp.resolve_device_id("SERIAL1") is None

    def test_rejects_unsafe_serial_without_calling_api(self, clean_env):
        """OData injection defence — bogus chars in serial => None, no GET."""
        glp, inner = _make_glp_client()
        for bad in ["SERIAL'", "SER IAL", "SERIAL;DROP", "S'; DROP--", ""]:
            assert glp.resolve_device_id(bad) is None
        inner.get.assert_not_called()

    def test_accepts_real_world_serial_formats(self, clean_env):
        """Concrete serials from the lab should all pass validation."""
        glp, _ = _make_glp_client()
        for good in ["SG30LMR164", "CNP6L2H02W", "VNVQMPJ028", "PHSXM52029",
                     "test_device-01", "ABCDEFG-1234_5"]:
            assert glp._is_safe_serial(good), f"{good!r} rejected"

    def test_cache_is_per_instance_not_shared(self, clean_env):
        """Regression: fixes a prior class-level mutable default that would
        have let one GLPClient's cache leak into another's."""
        glp_a, _ = _make_glp_client()
        glp_b, _ = _make_glp_client()
        glp_a._device_id_cache["X"] = "uuid-A"
        assert glp_b._device_id_cache.get("X") is None, (
            "cache leaked between instances — check __init__ vs class scope"
        )


# ---------------------------------------------------------------------------
# Payload shape per HPE docs
# ---------------------------------------------------------------------------


class TestPayloadShapes:
    def test_archive_body_is_only_archived_true(self, clean_env, writes_on):
        glp, inner = _make_glp_client()
        glp.archive_device("SERIAL1")
        call = inner._request.call_args
        body = call.kwargs["json"]
        assert body == {"archived": True}, f"got {body}"

    def test_unarchive_body_is_only_archived_false(self, clean_env, writes_on):
        glp, inner = _make_glp_client()
        glp.unarchive_device("SERIAL1")
        body = inner._request.call_args.kwargs["json"]
        assert body == {"archived": False}

    def test_assign_subscription_body_uuid_passthrough(self, clean_env, writes_on):
        """A real UUID is used as-is — no key-resolution GET for it."""
        glp, inner = _make_glp_client()
        sub_uuid = "123e4567-e89b-12d3-a456-426614174000"
        glp.assign_subscription("SERIAL1", subscription_id=sub_uuid)
        body = inner._request.call_args.kwargs["json"]
        assert body == {"subscription": [{"id": sub_uuid}]}
        # only the serial→device-id GET happened, not a subscription lookup
        get_urls = [c.args[0] for c in inner.get.call_args_list]
        assert all("/subscriptions/" not in u for u in get_urls)

    def test_assign_subscription_resolves_key_to_uuid(self, clean_env, writes_on):
        """A non-UUID subscription key is resolved via the subscriptions API."""
        glp, inner = _make_glp_client()
        glp.assign_subscription("SERIAL1", subscription_id="EVAL-KEY-789")
        body = inner._request.call_args.kwargs["json"]
        assert body == {"subscription": [{"id": "uuid-abc-123"}]}
        get_urls = [c.args[0] for c in inner.get.call_args_list]
        assert any("/subscriptions/v1/subscriptions" in u for u in get_urls)

    def test_assign_subscription_unresolvable_key_raises(self, clean_env, writes_on):
        """Key that resolves to nothing → ValueError, no PATCH sent."""
        glp, inner = _make_glp_client(list_devices_response={"items": []})
        with pytest.raises(ValueError, match="Could not resolve subscription key"):
            glp.assign_subscription("SERIAL1", subscription_id="BOGUS-KEY")
        inner._request.assert_not_called()

    def test_unassign_subscription_body_is_empty_list(self, clean_env, writes_on):
        glp, inner = _make_glp_client()
        glp.unassign_subscription("SERIAL1")
        body = inner._request.call_args.kwargs["json"]
        assert body == {"subscription": []}

    def test_patch_uses_merge_patch_content_type(self, clean_env, writes_on):
        glp, inner = _make_glp_client()
        glp.archive_device("SERIAL1")
        headers = inner._request.call_args.kwargs["headers"]
        assert headers["Content-Type"] == "application/merge-patch+json"

    def test_patch_uses_id_query_param_not_body(self, clean_env, writes_on):
        glp, inner = _make_glp_client()
        glp.archive_device("SERIAL1")
        params = inner._request.call_args.kwargs["params"]
        assert params == {"id": "uuid-abc-123"}

    def test_patch_targets_correct_endpoint(self, clean_env, writes_on):
        glp, inner = _make_glp_client()
        glp.archive_device("SERIAL1")
        method, path = inner._request.call_args.args[:2]
        assert method == "PATCH"
        assert path == "/devices/v2beta1/devices"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_unresolvable_serial_raises(self, clean_env, writes_on):
        glp, _ = _make_glp_client(list_devices_response={"items": []})
        with pytest.raises(RuntimeError, match="Could not resolve serial"):
            glp.archive_device("GHOST")

    def test_4xx_from_glp_raises(self, clean_env, writes_on):
        bad_resp = MagicMock()
        bad_resp.status_code = 400
        bad_resp.text = '{"errorCode":"BAD_REQUEST","message":"archived must be sole field"}'
        bad_resp.headers = {}
        glp, _ = _make_glp_client(patch_response=bad_resp)
        with pytest.raises(RuntimeError, match="HTTP 400"):
            glp.archive_device("SERIAL1")

    def test_202_without_location_raises(self, clean_env, writes_on):
        # Async 202 but server forgot Location header
        resp = MagicMock()
        resp.status_code = 202
        resp.headers = {}
        resp.text = ""
        resp.json.return_value = {}
        glp, _ = _make_glp_client(patch_response=resp)
        with pytest.raises(RuntimeError, match="no Location header"):
            glp.archive_device("SERIAL1")

    def test_200_synchronous_completion_returns_body(self, clean_env, writes_on):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.text = '{"status":"completed","syncResult":"ok"}'
        resp.json.return_value = {"status": "completed", "syncResult": "ok"}
        glp, _ = _make_glp_client(patch_response=resp)
        result = glp.archive_device("SERIAL1")
        assert result["syncResult"] == "ok"
