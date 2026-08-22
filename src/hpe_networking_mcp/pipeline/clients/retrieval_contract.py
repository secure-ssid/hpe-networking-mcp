"""Backend-neutral contracts for document retrieval.

The concrete LanceDB, Redis, and future hosted backends can expose different
storage details while returning the same small, bounded shape to callers.
This module deliberately has no storage or embedding dependencies.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeAlias

MAX_TOP_K = 200
DEFAULT_TOP_K = 10
MAX_HIT_TEXT_CHARS = 600
MAX_PROVENANCE_CHARS = 2_048
MAX_SOURCE_FILTERS = 20

_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]+$")
SourceFilter: TypeAlias = str | Sequence[str] | None


def _normalize_source_filter(value: SourceFilter) -> tuple[str, ...] | None:
    if value is None or value == "" or value == ():
        return None
    values = (value,) if isinstance(value, str) else tuple(value)
    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError(f"invalid source filter: {value!r}")
        source = item.strip().lower()
        if not _IDENTIFIER_RE.fullmatch(source):
            raise ValueError(f"invalid source filter: {value!r}")
        if source not in normalized:
            normalized.append(source)
    if not normalized:
        return None
    if len(normalized) > MAX_SOURCE_FILTERS:
        raise ValueError(f"source filter has more than {MAX_SOURCE_FILTERS} values")
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    """Normalized controls shared by retrieval backends.

    ``top_k`` is clamped to the project-wide result bound.  Source names are
    canonicalized to a de-duplicated, lower-case tuple so adapters do not need
    to repeat validation or handle the string-versus-sequence distinction.
    """

    top_k: int = DEFAULT_TOP_K
    source_filter: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int):
            raise TypeError("top_k must be an integer")
        object.__setattr__(self, "top_k", max(1, min(self.top_k, MAX_TOP_K)))
        object.__setattr__(
            self,
            "source_filter",
            _normalize_source_filter(self.source_filter),
        )

    @classmethod
    def normalize(
        cls,
        *,
        top_k: int = DEFAULT_TOP_K,
        source_filter: SourceFilter = None,
    ) -> RetrievalOptions:
        """Build options from the loose values accepted by public callers."""
        return cls(top_k=top_k, source_filter=_normalize_source_filter(source_filter))


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """A bounded, backend-independent document hit."""

    text: str
    source: str
    doc_type: str
    file_path: str
    chunk_index: int
    score: float
    source_url: str | None = None
    heading_breadcrumb: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        if isinstance(self.chunk_index, bool) or not isinstance(self.chunk_index, int):
            raise TypeError("chunk_index must be an integer")
        if self.chunk_index < 0:
            raise ValueError("chunk_index must be non-negative")
        if not isinstance(self.score, (int, float)) or not math.isfinite(self.score):
            raise ValueError("score must be finite")
        object.__setattr__(self, "text", self.text[:MAX_HIT_TEXT_CHARS])
        for field_name in ("source_url", "heading_breadcrumb"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string or None")
            if value is not None:
                object.__setattr__(self, field_name, value[:MAX_PROVENANCE_CHARS])

    def as_dict(self) -> dict[str, object]:
        """Return the bounded wire shape used by existing RAG callers."""
        result: dict[str, object] = {
            "text": self.text,
            "source": self.source,
            "doc_type": self.doc_type,
            "file_path": self.file_path,
            "chunk_index": self.chunk_index,
            "score": round(float(self.score), 4),
        }
        if self.source_url is not None:
            result["source_url"] = self.source_url
        if self.heading_breadcrumb is not None:
            result["heading_breadcrumb"] = self.heading_breadcrumb
        return result


@dataclass(frozen=True, slots=True)
class BackendIndexIdentity:
    """Identity and compatibility metadata for a retrieval index."""

    backend: str
    index: str
    index_version: str | None = None
    schema_version: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None

    @property
    def backend_name(self) -> str:
        return self.backend

    @property
    def index_name(self) -> str:
        return self.index


# These names make the contract easy to discover without duplicating types.
BackendIdentity = BackendIndexIdentity
IndexIdentity = BackendIndexIdentity
RetrievalIdentity = BackendIndexIdentity
RetrievalBackendIdentity = BackendIndexIdentity


class RetrievalBackend(Protocol):
    """Protocol implemented by any backend that can retrieve document hits."""

    @property
    def identity(self) -> BackendIndexIdentity:
        """Return the backend/index identity used for this query."""
        ...

    def retrieve(
        self,
        query: str,
        options: RetrievalOptions | None = None,
    ) -> Sequence[RetrievalHit]:
        """Retrieve bounded hits for ``query`` using normalized options."""
        ...
