from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from hpe_networking_mcp.pipeline.clients.token_manager import TokenManager


class _TokenResponse:
    def __init__(self, token: str):
        self._token = token

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"access_token": self._token, "expires_in": 7200}


def test_token_manager_deduplicates_concurrent_refreshes(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))
    calls: list[dict[str, object]] = []
    call_lock = threading.Lock()

    def fake_post(url, data=None, headers=None, timeout=None):
        with call_lock:
            calls.append(
                {
                    "url": url,
                    "data": data,
                    "headers": headers,
                    "timeout": timeout,
                }
            )
        time.sleep(0.02)
        return _TokenResponse("fresh-token")

    monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.token_manager.httpx.post", fake_post)

    manager = TokenManager(
        client_id="client-id",
        client_secret="secret",
        token_url="https://sso.example.com/token",
        cache_key="concurrent",
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda _: manager.get_access_token(), range(8)))

    assert tokens == ["fresh-token"] * 8
    assert len(calls) == 1
    assert calls[0]["url"] == "https://sso.example.com/token"


def test_token_manager_force_refresh_still_refreshes(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))
    tokens = iter(["token-1", "token-2"])

    def fake_post(url, data=None, headers=None, timeout=None):
        return _TokenResponse(next(tokens))

    monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.token_manager.httpx.post", fake_post)

    manager = TokenManager(
        client_id="client-id",
        client_secret="secret",
        token_url="https://sso.example.com/token",
        cache_key="force",
    )

    assert manager.get_access_token() == "token-1"
    assert manager.get_access_token(force_refresh=True) == "token-2"


def test_token_manager_generation_increments_on_each_real_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))
    tokens = iter(["token-1", "token-2", "token-3"])

    def fake_post(url, data=None, headers=None, timeout=None):
        return _TokenResponse(next(tokens))

    monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.token_manager.httpx.post", fake_post)

    manager = TokenManager(
        client_id="client-id",
        client_secret="secret",
        token_url="https://sso.example.com/token",
        cache_key="generation",
    )

    assert manager.generation == 0
    manager.get_access_token()
    assert manager.generation == 1
    manager.get_access_token(force_refresh=True)
    assert manager.generation == 2


def test_token_manager_collapses_concurrent_401_refreshes_via_generation(tmp_path, monkeypatch):
    """Simulate N concurrent 401s that all observed the same stale
    generation -- only ONE of them should trigger a real network refresh;
    the rest should see the already-refreshed token via generation
    comparison and skip the redundant call."""
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))
    calls: list[str] = []
    call_lock = threading.Lock()
    tokens = iter([f"token-{i}" for i in range(1, 20)])

    def fake_post(url, data=None, headers=None, timeout=None):
        with call_lock:
            token = next(tokens)
            calls.append(token)
        time.sleep(0.02)
        return _TokenResponse(token)

    monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.token_manager.httpx.post", fake_post)

    manager = TokenManager(
        client_id="client-id",
        client_secret="secret",
        token_url="https://sso.example.com/token",
        cache_key="collapse-401",
    )

    # Establish an initial token (generation 1) as if every caller had
    # already fetched it before their request went out and got a 401.
    manager.get_access_token()
    observed_generation = manager.generation
    assert observed_generation == 1

    def caller(_index: int) -> str:
        return manager.get_access_token(force_refresh=True, observed_generation=observed_generation)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(caller, range(8)))

    # Exactly one additional network refresh should have happened (the
    # initial one + one collapsed refresh == 2 total calls), even though
    # 8 concurrent callers all forced a refresh off the same stale
    # generation.
    assert len(calls) == 2, f"expected 2 total token calls, got {len(calls)}: {calls}"
    # Every caller must observe the *same* refreshed token (the one from
    # the single collapsed refresh), not a mix of results.
    assert len(set(results)) == 1
    assert manager.generation == 2


def test_token_manager_does_not_collapse_when_generation_already_advanced(tmp_path, monkeypatch):
    """If the caller's observed_generation is already stale relative to a
    refresh that happened for an unrelated reason, force_refresh should be
    downgraded to a no-op refresh check rather than firing a redundant
    network call."""
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))
    calls: list[str] = []
    tokens = iter(["token-1", "token-2"])

    def fake_post(url, data=None, headers=None, timeout=None):
        token = next(tokens)
        calls.append(token)
        return _TokenResponse(token)

    monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.token_manager.httpx.post", fake_post)

    manager = TokenManager(
        client_id="client-id",
        client_secret="secret",
        token_url="https://sso.example.com/token",
        cache_key="stale-generation",
    )

    manager.get_access_token()  # generation -> 1, token-1
    manager.get_access_token(force_refresh=True)  # generation -> 2, token-2

    # This caller observed generation 1 (stale) before requesting a forced
    # refresh -- generation 2 already has a fresh token, so no 3rd network
    # call should happen.
    token = manager.get_access_token(force_refresh=True, observed_generation=1)

    assert token == "token-2"
    assert len(calls) == 2
    assert manager.generation == 2


def test_token_cache_write_is_atomic_and_0600(tmp_path, monkeypatch):
    """The token cache is written owner-only (0600) via a temp file that is
    atomically renamed into place, leaving no world-readable window and no
    orphan .tmp file."""
    import json as _json
    import stat

    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))

    def fake_post(url, data=None, headers=None, timeout=None):
        return _TokenResponse("tok-abc")

    monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.token_manager.httpx.post", fake_post)

    manager = TokenManager(
        client_id="client-id",
        client_secret="secret",
        token_url="https://sso.example.com/token",
        cache_key="atomic",
    )
    manager.get_access_token()

    cache_file = manager.cache_file
    assert cache_file.exists()
    # Owner-only permissions.
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
    # No orphan temp file left behind.
    assert not list(tmp_path.glob("*.tmp"))
    # Valid, complete JSON (never a torn partial write).
    data = _json.loads(cache_file.read_text())
    assert data["access_token"] == "tok-abc"


def test_token_cache_write_failure_preserves_prior_cache(tmp_path, monkeypatch):
    """A crash during the atomic swap must leave the previous cached token
    intact rather than corrupting or truncating it."""
    import json as _json

    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "hpe_networking_mcp.pipeline.clients.token_manager.httpx.post",
        lambda url, data=None, headers=None, timeout=None: _TokenResponse("first"),
    )
    manager = TokenManager(
        client_id="client-id",
        client_secret="secret",
        token_url="https://sso.example.com/token",
        cache_key="preserve",
    )
    manager.get_access_token()
    good = manager.cache_file.read_text()

    # A later save crashes exactly at the rename.
    def _boom(src, dst):
        raise RuntimeError("simulated crash during rename")

    monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.token_manager.os.replace", _boom)
    manager.access_token = "second"
    manager._save_token_to_cache()  # swallowed + logged, must not raise

    assert manager.cache_file.read_text() == good
    assert not list(tmp_path.glob("*.tmp"))
    assert _json.loads(good)["access_token"] == "first"


def test_token_metadata_excludes_secret_and_reports_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "hpe_networking_mcp.pipeline.clients.token_manager.httpx.post",
        lambda url, data=None, headers=None, timeout=None: _TokenResponse("secret-token"),
    )
    manager = TokenManager(
        client_id="client-id",
        client_secret="secret",
        token_url="https://sso.example.com/token",
        cache_key="metadata",
    )

    manager.get_access_token()
    metadata = manager.metadata()

    assert metadata["token_present"] is True
    assert metadata["token_valid"] is True
    assert metadata["token_age_seconds"] is not None
    assert metadata["expires_in_seconds"] is not None
    assert "secret-token" not in str(metadata)
    assert "secret" not in str(metadata)


def test_token_manager_fails_closed_when_cache_dir_unwritable(tmp_path, monkeypatch):
    """An unwritable cache dir must raise -- never fall back to CWD.

    A silent fallback writes OAuth tokens into whatever directory the process
    runs from, which is frequently a repo checkout or a container layer.
    """
    import pathlib

    locked = tmp_path / "locked"
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(locked))

    def _deny_mkdir(self, *args, **kwargs):
        raise PermissionError(f"permission denied creating {self}")

    monkeypatch.setattr(pathlib.Path, "mkdir", _deny_mkdir)
    with pytest.raises(RuntimeError, match="TOKEN_CACHE_DIR"):
        TokenManager(client_id="id", client_secret="secret")
