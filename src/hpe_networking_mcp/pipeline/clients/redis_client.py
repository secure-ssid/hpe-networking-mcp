"""Legacy Redis Stack vector search client for document RAG.

This is the optional server backend. The default download-and-run path uses
LanceDB + SQLite + fastembed.
"""

import os

import numpy as np
import redis
from redis.commands.search.field import NumericField, TagField, TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

DEFAULT_REDIS_URL = "redis://localhost:6379"
REDIS_URL = os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
DOCS_INDEX = "network_docs"
EMBEDDING_DIMS = 768  # nomic-embed-text
MAX_SEARCH_TOP_K = 200


def _clamp_top_k(top_k: int) -> int:
    return max(1, min(top_k, MAX_SEARCH_TOP_K))


def get_redis_url() -> str:
    return os.getenv("REDIS_URL", DEFAULT_REDIS_URL)


def get_client(url: str | None = None) -> redis.Redis:
    """Return a Redis client connected to Redis Stack."""
    return redis.from_url(url or get_redis_url(), decode_responses=False)


def ensure_index(
    client: redis.Redis,
    index_name: str = DOCS_INDEX,
    dims: int = EMBEDDING_DIMS,
) -> None:
    """Create the vector search index if it doesn't exist."""
    try:
        client.ft(index_name).info()
        print(f"Index '{index_name}' already exists")
        return
    except Exception:
        pass

    schema = (
        TextField("$.text", as_name="text", no_stem=True),
        TagField("$.source", as_name="source"),
        TagField("$.doc_type", as_name="doc_type"),
        TextField("$.file_path", as_name="file_path"),
        NumericField("$.chunk_index", as_name="chunk_index"),
        VectorField(
            "$.embedding",
            "HNSW",
            {
                "TYPE": "FLOAT32",
                "DIM": dims,
                "DISTANCE_METRIC": "COSINE",
                "INITIAL_CAP": 50000,
            },
            as_name="embedding",
        ),
    )

    client.ft(index_name).create_index(
        schema,
        definition=IndexDefinition(prefix=["doc:"], index_type=IndexType.JSON),
    )
    print(f"Created index '{index_name}'")


def upsert_docs(
    client: redis.Redis,
    docs: list[dict],
    index_name: str = DOCS_INDEX,
) -> int:
    """Upsert documents with embeddings into Redis.

    Each doc must have id, text, source, doc_type, file_path, chunk_index, and embedding.
    Returns count of documents upserted.
    """
    pipe = client.pipeline(transaction=False)
    for doc in docs:
        key = f"doc:{doc['id']}"
        payload = {
            "text": doc["text"],
            "source": doc.get("source", ""),
            "doc_type": doc.get("doc_type", ""),
            "file_path": doc.get("file_path", ""),
            "chunk_index": doc.get("chunk_index", 0),
            "embedding": doc["embedding"],
        }
        pipe.json().set(key, "$", payload)
    pipe.execute()
    return len(docs)


def vector_search(
    client: redis.Redis,
    query_vector: list[float],
    top_k: int = 15,
    source_filter: str | None = None,
    index_name: str = DOCS_INDEX,
) -> list[dict]:
    """Search for similar documents using vector similarity.

    Returns list of dicts with text, source, doc_type, file_path, chunk_index, score.
    """
    top_k = _clamp_top_k(top_k)
    vec_bytes = np.array(query_vector, dtype=np.float32).tobytes()

    filter_str = "*"
    if source_filter:
        filter_str = f"@source:{{{source_filter}}}"

    q = (
        Query(f"({filter_str})=>[KNN {top_k} @embedding $vec AS score]")
        .sort_by("score")
        .return_fields("text", "source", "doc_type", "file_path", "chunk_index", "score")
        .paging(0, top_k)
        .dialect(2)
    )

    results = client.ft(index_name).search(q, query_params={"vec": vec_bytes})

    hits = []
    for doc in results.docs:
        # Redis cosine distance: 0 = identical, 1 = orthogonal. Convert to similarity.
        raw_score = float(getattr(doc, "score", 1.0))
        similarity = max(0.0, min(1.0, 1.0 - raw_score))
        hits.append({
            "text": getattr(doc, "text", ""),
            "source": getattr(doc, "source", ""),
            "doc_type": getattr(doc, "doc_type", ""),
            "file_path": getattr(doc, "file_path", ""),
            "chunk_index": int(getattr(doc, "chunk_index", 0) or 0),
            "score": round(similarity, 4),
        })
    return hits


def delete_doc(client: redis.Redis, doc_id: str) -> None:
    """Delete a single document by its ID."""
    client.delete(f"doc:{doc_id}")


def doc_count(client: redis.Redis, index_name: str = DOCS_INDEX) -> int:
    """Return number of indexed documents."""
    try:
        info = client.ft(index_name).info()
        return int(info.get("num_docs", 0))
    except Exception:
        return 0


TOOLS_INDEX = "aruba_tools"


def ensure_tools_index(
    client: redis.Redis,
    index_name: str = TOOLS_INDEX,
    dims: int = EMBEDDING_DIMS,
) -> None:
    """Create the tools vector search index if it doesn't exist."""
    try:
        client.ft(index_name).info()
        print(f"Index '{index_name}' already exists")
        return
    except Exception:
        pass

    schema = (
        TextField("$.name", as_name="name"),
        TextField("$.description", as_name="description"),
        TagField("$.server", as_name="server"),
        TextField("$.schema_json", as_name="schema_json"),
        VectorField(
            "$.embedding",
            "HNSW",
            {
                "TYPE": "FLOAT32",
                "DIM": dims,
                "DISTANCE_METRIC": "COSINE",
                "INITIAL_CAP": 10000,
            },
            as_name="embedding",
        ),
    )

    client.ft(index_name).create_index(
        schema,
        definition=IndexDefinition(prefix=["tool:"], index_type=IndexType.JSON),
    )
    print(f"Created index '{index_name}'")


def upsert_tools(
    client: redis.Redis,
    tools: list[dict],
    index_name: str = TOOLS_INDEX,
) -> int:
    """Upsert tool definitions with embeddings into Redis.

    Each tool must have: id (str), server, name, description, schema_json (str),
    params (list[str]), embedding (list[float]).
    Returns count of tools upserted.
    """
    pipe = client.pipeline(transaction=False)
    for tool in tools:
        key = f"tool:{tool['id']}"
        payload = {
            "server": tool.get("server", ""),
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "schema_json": tool.get("schema_json", ""),
            "params": tool.get("params", []),
            "embedding": tool["embedding"],
        }
        pipe.json().set(key, "$", payload)
    pipe.execute()
    return len(tools)


def search_tools(
    client: redis.Redis,
    query_vector: list[float],
    top_k: int = 10,
    index_name: str = TOOLS_INDEX,
) -> list[dict]:
    """Search tool definitions using vector similarity.

    Returns list of dicts with name, description, server, schema_json, score.
    """
    top_k = _clamp_top_k(top_k)
    vec_bytes = np.array(query_vector, dtype=np.float32).tobytes()
    q = (
        Query(f"*=>[KNN {top_k} @embedding $vec AS score]")
        .sort_by("score")
        .return_fields("name", "description", "server", "schema_json", "score")
        .paging(0, top_k)
        .dialect(2)
    )
    results = client.ft(index_name).search(q, query_params={"vec": vec_bytes})
    hits = []
    for doc in results.docs:
        # Redis cosine distance: 0 = identical, 1 = orthogonal. Convert to similarity.
        raw_score = float(getattr(doc, "score", 1.0))
        similarity = max(0.0, min(1.0, 1.0 - raw_score))
        hits.append({
            "name": getattr(doc, "name", ""),
            "description": getattr(doc, "description", ""),
            "server": getattr(doc, "server", ""),
            "schema_json": getattr(doc, "schema_json", "{}"),
            "score": round(similarity, 4),
        })
    return hits
