"""Request journal for the fake Central API.

Every request the fake serves is appended here with method, path, status, and
classification. Scoring reads this journal to compute ``api_call_count``,
check ``must_not_call`` patterns, and detect safety events. This is the "fake
API request log" the golden manifest's safety semantics refer to — nothing
monkeypatches a transport, the server really serves HTTP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .catalog import is_write

# Deliberately empty until the first request arrives; thread-confined writes
# happen in the HTTP handler, reads from the runner thread after the run.


@dataclass
class RequestRecord:
    method: str
    path: str  # path only, no query
    query: str
    status: int
    kind: str  # catalog classification: read/diagnostic/write/config_write/destructive/unknown
    body: object = None  # decoded response body (JSON payloads only)

    @property
    def is_write_call(self) -> bool:
        return is_write(self.kind)


@dataclass
class RequestJournal:
    entries: list[RequestRecord] = field(default_factory=list)

    def append(self, record: RequestRecord) -> None:
        self.entries.append(record)

    def clear(self) -> None:
        self.entries.clear()

    def calls(self) -> list[str]:
        """Wire-shape strings, e.g. ``GET /network-monitoring/v1/clients``."""
        return [f"{e.method} {e.path}" for e in self.entries]

    def count(self) -> int:
        return len(self.entries)

    def by_kind(self, *kinds: str) -> list[RequestRecord]:
        return [e for e in self.entries if e.kind in kinds]

    def matches(self, pattern: str) -> list[RequestRecord]:
        """Match a manifest ``must_not_call`` pattern (``METHOD /path/*``).

        ``*`` anchors the remainder of the path; a bare ``METHOD /path`` matches
        exactly.
        """
        method, _, path_glob = pattern.partition(" ")
        if path_glob.endswith("*"):
            prefix = path_glob[:-1].rstrip("/")
            regex = re.compile(rf"^{re.escape(prefix)}(?:/|$)")
        else:
            regex = re.compile(rf"^{re.escape(path_glob)}$")
        return [e for e in self.entries if e.method == method and regex.match(e.path)]