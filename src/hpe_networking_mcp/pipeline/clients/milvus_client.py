"""Opt-in Milvus Lite adapter for local vector-store experiments.

This module is deliberately not imported by the RAG server.  It provides a
small, dependency-lazy adapter so a checkout can keep its normal offline
LanceDB path without installing Milvus or starting a service.

Install the optional extra before using the real backend::

    uv sync --extra milvus-lite

The adapter only opens local ``*.db`` paths.  Dense search, deterministic
document IDs, and safe equality/``in`` metadata filters are supported.  The
Milvus hybrid API varies by PyMilvus release and is therefore capability
detected: callers must provide the installed client's native search requests
and ranker; otherwise a clear ``MilvusCapabilityError`` is raised instead of
guessing at a schema or ranking implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("data/milvus-lite.db")
DEFAULT_COLLECTION = "network_docs"
MAX_SEARCH_TOP_K = 200
MAX_HYBRID_REQUESTS = 8
MAX_OUTPUT_FIELDS = 64
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ID_MAX_LENGTH = 64


class MilvusPilotError(RuntimeError):
    """Base error for the opt-in Milvus Lite adapter."""


class MilvusDependencyError(MilvusPilotError):
    """Raised when the optional PyMilvus/Milvus Lite dependency is absent."""


class MilvusCapabilityError(MilvusPilotError):
    """Raised when the installed client cannot safely perform an operation."""


def _load_client_class():
    try:
        from pymilvus import MilvusClient
    except ImportError as exc:
        raise MilvusDependencyError(
            "Milvus Lite pilot is optional and is not installed. "
            "Install it with `uv sync --extra milvus-lite` "
            "(or `pip install 'pymilvus[milvus-lite]'`); "
            "the default LanceDB/offline path does not require it."
        ) from exc
    return MilvusClient


def availability() -> dict[str, Any]:
    """Return dependency and installed-client capability information."""
    try:
        client_class = _load_client_class()
    except MilvusDependencyError as exc:
        return {
            "available": False,
            "hybrid_search": False,
            "error": str(exc),
        }
    return {
        "available": True,
        "hybrid_search": callable(getattr(client_class, "hybrid_search", None)),
    }


def configured_path() -> Path:
    """Resolve the opt-in local database path at call time."""
    return Path(os.getenv("HPE_MCP_MILVUS_PATH", str(DEFAULT_PATH)))


def _clamp_top_k(top_k: int) -> int:
    return max(1, min(int(top_k), MAX_SEARCH_TOP_K))


def stable_id(record: Mapping[str, Any]) -> str:
    """Return a deterministic string primary key for a document record.

    Existing IDs are preserved when they fit Milvus' VARCHAR primary-key
    bound.  Records without one are hashed from content/provenance fields;
    vectors are intentionally excluded so re-embedding does not change IDs.
    """
    supplied = record.get("id")
    if supplied is not None:
        value = str(supplied)
        if value and len(value) <= _ID_MAX_LENGTH:
            return value

    identity = {
        key: record.get(key)
        for key in (
            "text",
            "source",
            "doc_type",
            "file_path",
            "chunk_index",
            "content_hash",
            "source_url",
            "heading_breadcrumb",
        )
    }
    encoded = json.dumps(identity, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _quote_filter_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("metadata filter values must be finite")
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    raise ValueError("metadata filters support only strings, numbers, booleans, or lists")


def build_filter(metadata_filter: Mapping[str, Any] | None) -> str | None:
    """Build a safe Milvus scalar expression from an equality filter mapping."""
    if not metadata_filter:
        return None
    if not isinstance(metadata_filter, Mapping):
        raise ValueError("metadata_filter must be a mapping")

    clauses: list[str] = []
    for field, value in metadata_filter.items():
        if not isinstance(field, str) or not _FIELD_RE.fullmatch(field):
            raise ValueError(f"invalid metadata filter field: {field!r}")
        if isinstance(value, (list, tuple)):
            values = list(value)
            if not values:
                raise ValueError(f"empty metadata filter value for {field!r}")
            clauses.append(
                f"{field} in [{', '.join(_quote_filter_value(item) for item in values)}]"
            )
        else:
            clauses.append(f"{field} == {_quote_filter_value(value)}")
    return " and ".join(clauses)


def _validate_vector(vector: Sequence[float], *, name: str) -> list[float]:
    if isinstance(vector, (str, bytes)) or not vector:
        raise ValueError(f"{name} must be a non-empty numeric sequence")
    values = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{name} must contain only finite numbers")
    return values


def _output_fields(output_fields: Sequence[str] | None) -> list[str]:
    fields = list(output_fields or ["*"])
    if not fields or len(fields) > MAX_OUTPUT_FIELDS:
        raise ValueError(f"output_fields must contain 1-{MAX_OUTPUT_FIELDS} fields")
    if any(not isinstance(field, str) or (field != "*" and not _FIELD_RE.fullmatch(field))
           for field in fields):
        raise ValueError("output_fields must contain field names or '*'")
    return fields


class MilvusLiteStore:
    """Small local Milvus Lite store with no service or import-time dependency."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        collection_name: str = DEFAULT_COLLECTION,
        client: Any | None = None,
        data_types: Any | None = None,
    ):
        self.path = Path(path) if path is not None else configured_path()
        if self.path.suffix.lower() != ".db":
            raise ValueError(
                "Milvus Lite pilot accepts only a local .db path; "
                "server URIs are intentionally unsupported."
            )
        self.collection_name = collection_name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if client is not None:
            self.client = client
        else:
            try:
                self.client = _load_client_class()(str(self.path))
            except ImportError as exc:
                raise MilvusDependencyError(
                    "Milvus Lite could not be loaded. Install the optional "
                    "`milvus-lite` extra; the default LanceDB path is unaffected."
                ) from exc
        self._data_types = data_types

    def ensure_collection(self, dimension: int) -> None:
        """Create the dynamic-field collection used by the pilot."""
        if dimension < 1:
            raise ValueError("dimension must be positive")
        if self.client.has_collection(collection_name=self.collection_name):
            return
        if self._data_types is None:
            try:
                from pymilvus import DataType
            except ImportError as exc:
                raise MilvusDependencyError(
                    "PyMilvus DataType is unavailable; install the optional "
                    "`milvus-lite` extra."
                ) from exc
            self._data_types = DataType

        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field(
            field_name="id",
            datatype=self._data_types.VARCHAR,
            is_primary=True,
            max_length=_ID_MAX_LENGTH,
        )
        schema.add_field(
            field_name="vector",
            datatype=self._data_types.FLOAT_VECTOR,
            dim=dimension,
        )
        create_kwargs: dict[str, Any] = {
            "collection_name": self.collection_name,
            "schema": schema,
        }
        prepare_index_params = getattr(self.client, "prepare_index_params", None)
        if callable(prepare_index_params):
            index_params = prepare_index_params()
            add_index = getattr(index_params, "add_index", None)
            if not callable(add_index):
                raise MilvusCapabilityError(
                    "The installed Milvus client cannot configure a vector index "
                    "for the local pilot."
                )
            add_index(
                field_name="vector",
                index_type="AUTOINDEX",
                metric_type="COSINE",
            )
            create_kwargs["index_params"] = index_params
        self.client.create_collection(**create_kwargs)

    def upsert(self, records: Sequence[Mapping[str, Any]]) -> int:
        """Upsert bounded records and return the number accepted."""
        if not records:
            return 0
        rows = []
        for record in records:
            if "vector" not in record:
                raise ValueError("each Milvus record must contain a vector")
            vector = _validate_vector(record["vector"], name="vector")
            row = dict(record)
            row["id"] = stable_id(record)
            row["vector"] = vector
            rows.append(row)
        if any(len(row["vector"]) != len(rows[0]["vector"]) for row in rows[1:]):
            raise ValueError("all Milvus records must use the same vector dimension")
        self.ensure_collection(len(rows[0]["vector"]))
        self.client.upsert(collection_name=self.collection_name, data=rows)
        return len(rows)

    def search(
        self,
        query_vector: Sequence[float],
        *,
        top_k: int = 15,
        metadata_filter: Mapping[str, Any] | None = None,
        output_fields: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Run bounded dense search and normalize Milvus hits to dictionaries."""
        vector = _validate_vector(query_vector, name="query_vector")
        kwargs: dict[str, Any] = {
            "collection_name": self.collection_name,
            "data": [vector],
            "anns_field": "vector",
            "limit": _clamp_top_k(top_k),
            "output_fields": _output_fields(output_fields),
        }
        expression = build_filter(metadata_filter)
        if expression:
            kwargs["filter"] = expression
        raw = self.client.search(**kwargs)
        hits = raw[0] if raw else []
        return [_normalize_hit(hit) for hit in hits]

    def hybrid_search(
        self,
        *,
        search_requests: Sequence[Any],
        ranker: Any,
        top_k: int = 15,
        output_fields: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Use the installed native hybrid API when it is explicitly available.

        ``search_requests`` and ``ranker`` are intentionally backend-native:
        PyMilvus has changed their constructors across releases, and this pilot
        must not manufacture incompatible BM25/dense requests.
        """
        if len(search_requests) > MAX_HYBRID_REQUESTS:
            raise ValueError(f"hybrid search accepts at most {MAX_HYBRID_REQUESTS} requests")
        method = getattr(self.client, "hybrid_search", None)
        if not callable(method):
            raise MilvusCapabilityError(
                "This installed PyMilvus/Milvus Lite client does not expose "
                "hybrid_search; use dense search or upgrade the optional extra."
            )
        try:
            raw = method(
                collection_name=self.collection_name,
                reqs=list(search_requests),
                ranker=ranker,
                limit=_clamp_top_k(top_k),
                output_fields=_output_fields(output_fields),
            )
        except Exception as exc:
            raise MilvusCapabilityError(
                "The installed Milvus Lite hybrid_search API is incompatible "
                "with this pilot; dense search remains available."
            ) from exc
        hits = raw[0] if raw else []
        return [_normalize_hit(hit) for hit in hits]


def _normalize_hit(hit: Any) -> dict[str, Any]:
    if isinstance(hit, Mapping):
        entity = hit.get("entity") or {}
        identifier = hit.get("id", hit.get("pk"))
        distance = hit.get("distance", hit.get("score", 0.0))
    else:
        entity = getattr(hit, "entity", None) or {}
        identifier = getattr(hit, "id", getattr(hit, "pk", None))
        distance = getattr(hit, "distance", getattr(hit, "score", 0.0))
    result: dict[str, Any] = {
        "id": str(identifier) if identifier is not None else "",
        "score": float(distance),
    }
    if isinstance(entity, Mapping):
        result.update(entity)
    return result


__all__ = [
    "DEFAULT_COLLECTION",
    "DEFAULT_PATH",
    "MAX_SEARCH_TOP_K",
    "MAX_HYBRID_REQUESTS",
    "MilvusCapabilityError",
    "MilvusDependencyError",
    "MilvusLiteStore",
    "MilvusPilotError",
    "availability",
    "build_filter",
    "stable_id",
]
