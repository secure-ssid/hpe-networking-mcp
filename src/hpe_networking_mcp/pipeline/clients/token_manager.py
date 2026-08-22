"""OAuth2 token manager for HPE Aruba Central / GLP APIs.

Ported from aruba-central-portal/utils/token_manager.py with support for
a cache_key parameter so source and target accounts use independent caches.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Default buffer before expiry at which we proactively refresh (seconds).
# Central tokens live ~120 min so 300s buffer is fine; GLP tokens live only
# 15 min so callers should pass a smaller buffer (60-90s) to avoid burning
# a third of every token window. See:
# https://developer.greenlake.hpe.com/docs/greenlake/guides/public/authentication/authentication/
_DEFAULT_EXPIRY_BUFFER = 300


def _default_cache_dir() -> Path:
    """Default token cache directory. Avoids CWD so tokens don't leak
    into whatever directory the MCP server happens to run from."""
    override = os.environ.get("TOKEN_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "hpe-networking-mcp"


class TokenManager:
    """Manages OAuth2 client-credentials tokens with file-based caching."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_url: str = "https://sso.common.cloud.hpe.com/as/token.oauth2",
        cache_key: str = "central",
        cache_context: str = "",
        expiry_buffer: int = _DEFAULT_EXPIRY_BUFFER,
    ):
        """
        Args:
            client_id: OAuth2 client ID.
            client_secret: OAuth2 client secret.
            token_url: Token endpoint URL.
            cache_key: Unique key used to name the cache file, e.g. "source" or "target".
                       Combined with credential context so source/target and tenant
                       changes never collide.
            cache_context: Non-secret account metadata such as API base URL or
                       workspace ID. Changes invalidate cached tokens.
            expiry_buffer: Seconds before the token's stated expiry to refresh
                       proactively. Default 300s is right for Central's 120-min
                       tokens. For GLP (15-min tokens), pass 60-90s.
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.cache_context = cache_context
        self.expiry_buffer = expiry_buffer

        self.cache_fingerprint = self._cache_fingerprint()
        cache_filename = f".token_cache_{cache_key}_{self.cache_fingerprint}.json"
        cache_dir = _default_cache_dir()
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            self.cache_file = cache_dir / cache_filename
        except OSError as exc:
            # Fail closed: never fall back to CWD -- the working directory is
            # frequently a repo checkout or container layer, and an OAuth
            # token written there can leak into archives, images, or git.
            # Operators should point TOKEN_CACHE_DIR at a writable directory.
            raise RuntimeError(
                f"Token cache directory {cache_dir} is not writable ({exc}); "
                "refusing to fall back to the current working directory where "
                "OAuth tokens could leak. Set TOKEN_CACHE_DIR to a writable "
                "location."
            ) from exc

        self.access_token: Optional[str] = None
        self.token_expires_at: Optional[float] = None
        self.token_cached_at: Optional[float] = None
        self._refresh_lock = threading.RLock()
        # Bumped on every successful refresh. Used to collapse concurrent
        # 401-triggered force_refresh calls into a single network round
        # trip -- see get_access_token()'s ``observed_generation`` param.
        self._generation = 0
        self._load_cached_token()

    @property
    def generation(self) -> int:
        """Monotonic counter bumped on every successful token refresh.

        Callers that want concurrent-401 collapse should capture this
        alongside the token they're using, then pass it back as
        ``observed_generation`` to ``get_access_token(force_refresh=True, ...)``
        when that token is rejected.
        """
        return self._generation

    def _needs_refresh(self, force_refresh: bool = False) -> bool:
        return (
            force_refresh
            or not self.access_token
            or not self.token_expires_at
            or time.time() >= (self.token_expires_at - self.expiry_buffer)
        )

    def _cache_fingerprint(self) -> str:
        digest = hashlib.sha256()
        for value in (self.client_id, self.token_url, self.cache_context):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()[:12]

    def _load_cached_token(self) -> None:
        if not self.cache_file.exists():
            return
        try:
            with open(self.cache_file) as f:
                data = json.load(f)
            if data.get("cache_fingerprint") != self.cache_fingerprint:
                logger.debug("Token cache context changed (%s)", self.cache_file)
                return
            self.access_token = data.get("access_token")
            self.token_expires_at = data.get("expires_at")
            self.token_cached_at = data.get("cached_at")
            if self.token_expires_at and time.time() < (self.token_expires_at - self.expiry_buffer):
                logger.debug("Loaded valid token from cache (%s)", self.cache_file)
            else:
                logger.debug("Cached token expired (%s)", self.cache_file)
                self.access_token = None
                self.token_expires_at = None
                self.token_cached_at = None
        except Exception as exc:
            logger.warning("Failed to load token cache %s: %s", self.cache_file, exc)
            self.access_token = None
            self.token_expires_at = None
            self.token_cached_at = None

    def _save_token_to_cache(self) -> None:
        try:
            cached_at = self.token_cached_at or time.time()
            self.token_cached_at = cached_at
            # Write to a per-process temp file (0600 so tokens aren't
            # world-readable), then atomically os.replace() into place: the
            # cache file is shared across processes (MCP servers + pipeline
            # runs use the same cache key), so an in-place truncate-and-write
            # let a concurrent reader see torn JSON — and two concurrent
            # writers could leave corrupt bytes as the final state.
            tmp_file = self.cache_file.with_name(
                f"{self.cache_file.name}.{os.getpid()}.tmp"
            )
            try:
                fd = os.open(
                    tmp_file,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                with os.fdopen(fd, "w") as f:
                    json.dump(
                        {
                            "access_token": self.access_token,
                            "expires_at": self.token_expires_at,
                            "cached_at": cached_at,
                            "cache_fingerprint": self.cache_fingerprint,
                        },
                        f,
                        indent=2,
                    )
                os.replace(tmp_file, self.cache_file)
            finally:
                # A failed write must not leave a token-bearing tmp orphan.
                if tmp_file.exists():
                    tmp_file.unlink()
        except Exception as exc:
            logger.warning("Failed to save token cache: %s", exc)

    def _refresh_token(self) -> None:
        logger.info("Refreshing token (url=%s)", self.token_url)
        try:
            response = httpx.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            response.raise_for_status()
            token_data = response.json()
            self.access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 7200)
            self.token_expires_at = time.time() + expires_in
            self.token_cached_at = time.time()
            self._generation += 1
            self._save_token_to_cache()
            logger.info(
                "Token refreshed (generation=%d). Expires at %s",
                self._generation,
                datetime.fromtimestamp(self.token_expires_at).strftime("%Y-%m-%d %H:%M:%S"),
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Token refresh failed: {exc}") from exc

    def get_access_token(
        self,
        force_refresh: bool = False,
        *,
        observed_generation: Optional[int] = None,
    ) -> str:
        """Return a valid access token, refreshing if needed.

        Args:
            force_refresh: refresh even if the cached token isn't expired.
            observed_generation: the ``generation`` this caller last saw
                *before* deciding it needed a forced refresh (e.g. the
                generation in effect when the request that got a 401 was
                sent). When given, ``force_refresh`` is honored only if no
                other caller has already refreshed since -- i.e.
                ``observed_generation == self.generation`` at the moment
                the refresh lock is acquired. This collapses N concurrent
                401s against the same stale token into a single network
                round trip instead of N serialized ones. When omitted
                (the default), ``force_refresh=True`` always refreshes,
                matching prior behavior.
        """
        effective_force = force_refresh
        if (
            effective_force
            and observed_generation is not None
            and observed_generation != self._generation
        ):
            # Someone else already refreshed since this caller observed its
            # (now stale) generation -- the current token is already newer
            # than what triggered this force_refresh.
            effective_force = False
        if self._needs_refresh(effective_force):
            with self._refresh_lock:
                if (
                    effective_force
                    and observed_generation is not None
                    and observed_generation != self._generation
                ):
                    effective_force = False
                if self._needs_refresh(effective_force):
                    self._refresh_token()
        return self.access_token  # type: ignore[return-value]

    def get_access_token_with_generation(
        self,
        force_refresh: bool = False,
        *,
        observed_generation: Optional[int] = None,
    ) -> tuple[str, int]:
        """Like ``get_access_token`` but also returns the generation in
        effect after the call -- convenient for callers (CentralClient's
        async request path) that need to remember it for a later
        generation-aware 401 retry without a second attribute read that
        could race a concurrent refresh."""
        with self._refresh_lock:
            token = self.get_access_token(
                force_refresh, observed_generation=observed_generation
            )
            return token, self._generation

    def is_token_valid(self) -> bool:
        if not self.access_token or not self.token_expires_at:
            return False
        return time.time() < (self.token_expires_at - self.expiry_buffer)

    def metadata(self, now: float | None = None) -> dict[str, object]:
        """Return non-secret token/cache metadata for diagnostics.

        The access token and client credentials are intentionally omitted.
        This is safe for preflight/status tools that need to distinguish
        missing configuration, an uncached token, and an expiring token
        without forcing a refresh or making a network request.
        """
        current = time.time() if now is None else now
        expires_in = (
            self.token_expires_at - current
            if self.token_expires_at is not None
            else None
        )
        age = (
            current - self.token_cached_at
            if self.token_cached_at is not None
            else None
        )
        return {
            "token_present": bool(self.access_token),
            "token_valid": self.is_token_valid() if now is None else bool(
                self.access_token
                and self.token_expires_at
                and current < (self.token_expires_at - self.expiry_buffer)
            ),
            "generation": self._generation,
            "expires_at": self.token_expires_at,
            "expires_in_seconds": max(0.0, expires_in) if expires_in is not None else None,
            "token_age_seconds": max(0.0, age) if age is not None else None,
            "expiry_buffer_seconds": self.expiry_buffer,
        }
