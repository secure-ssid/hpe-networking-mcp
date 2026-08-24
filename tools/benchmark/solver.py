"""Deterministic solver for the benchmark harness.

Executes each scenario's ground-truth ``api_calls`` verbatim against the fake
Central API over real HTTP (httpx sync client). Every request lands in the
fake's request journal, which is the only input scoring reads — the journal
is the wire truth, and safety assertions evaluate against it.

The deterministic solver is the CI-hermetic path: no LLM, no credentials, no
network beyond localhost. A model-backed solver can be plugged in behind the
same protocol; the rest of the harness is identical.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace

import httpx

from .manifest import Scenario

_TOKEN_ENDPOINT = "/oauth2/token"


@dataclass(frozen=True)
class SolverTrace:
    """What the solver did for one scenario, before scoring."""

    scenario_id: str
    api_calls: list[str] = field(default_factory=list)  # "METHOD /path"
    tools_called: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    # Calls that returned >=400, as "METHOD /path (status)". A failed call did
    # not satisfy the wire contract, so the scenario scores as a miss rather
    # than aborting the whole run.
    failed_calls: list[str] = field(default_factory=list)
    # repo key -> gate outcome for write scenarios. "satisfied" | "bypass" |
    # "gated_wrong" (a write proceeded without its declared gate).
    gate_outcomes: dict[str, str] = field(default_factory=dict)


class DeterministicSolver:
    """Drives the fake Central API exactly along the scenario's api_calls."""

    def __init__(
        self,
        base_url: str,
        client_id: str = "benchmark-client",
        client_secret: str = "benchmark-secret",
        token_url: str = _TOKEN_ENDPOINT,
        timeout_s: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._timeout = timeout_s
        self._token: str | None = None

    def _obtain_token(self, client: httpx.Client) -> str:
        resp = client.post(
            f"{self.base_url}{self._token_url}",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"token endpoint returned no access_token: {payload}")
        return token

    def _render(self, template: str, entities: tuple[str, ...]) -> str:
        """Render a ``METHOD /path/{param}`` template using fixture entities.

        Path parameters are substituted positionally from the scenario's
        ``fixture_entities`` (entity values are ``kind value`` strings; the
        token after the first space is the id). Unknown parameters render as
        their template text so a missing fixture is loud at scoring time
        rather than silently empty.
        """
        ids = [e.split(" ", 1)[1] for e in entities if " " in e]
        param_iter = iter(ids)

        def sub(match: re.Match[str]) -> str:
            try:
                return next(param_iter)
            except StopIteration:
                return match.group(0)

        return re.sub(r"\{[a-z_]+\}", sub, template)

    def prime(self) -> None:
        """Obtain the access token outside any scored scenario.

        The token is fetched once and reused, so without priming its ``POST
        /oauth2/token`` would land in the first scenario's journal and inflate
        that scenario's ``api_call_count`` alone.
        """
        if self._token is None:
            with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
                self._token = self._obtain_token(client)

    def run(self, scenario: Scenario) -> SolverTrace:
        started = time.monotonic()
        trace = SolverTrace(scenario_id=scenario.id)
        with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
            if self._token is None:
                self._token = self._obtain_token(client)
            headers = {"Authorization": f"Bearer {self._token}"}
            for call in scenario.api_calls:
                method, _, path_template = call.partition(" ")
                path = self._render(path_template, scenario.fixture_entities)
                url = f"{self.base_url}{path}"
                resp = client.request(method, url, headers=headers)
                # A non-2xx does not abort the run: the journal already
                # recorded it, and scoring counts only successful calls toward
                # the wire contract. One broken scenario must not destroy the
                # whole benchmark.
                if resp.status_code >= 400:
                    trace.failed_calls.append(f"{method} {path} ({resp.status_code})")
                    continue
                trace.api_calls.append(f"{method} {path}")
        return replace(trace, latency_s=time.monotonic() - started)
