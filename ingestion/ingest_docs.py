"""
Ingest Aruba/HPE docs into the RAG backend.

Default backend is the embedded stack (no servers): chunk prose -> fastembed
(in-process ONNX, nomic prefixes) -> LanceDB at data/, plus parse OpenAPI
specs -> SQLite (data/specs.sqlite). `--backend redis` keeps the optional
Redis Stack + Ollama server deployment path.

Usage:
    uv run python ingestion/ingest_docs.py                     # full LanceDB rebuild
    uv run python ingestion/ingest_docs.py --backend redis     # optional Redis Stack path
    uv run python ingestion/ingest_docs.py --source nac_docs   # one source only
    uv run python ingestion/ingest_docs.py --dry-run           # count chunks, no upload

The default path needs the `ingestion` extra (`uv sync --extra ingestion`).
`--backend redis` additionally needs the `redis` extra, and says so when it is
absent rather than raising `ModuleNotFoundError` from an import.
"""

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bs4 import BeautifulSoup

from hpe_networking_mcp import optional_deps
from hpe_networking_mcp.pipeline.clients.ollama_client import OllamaClient
from ingestion.chunking import chunk_text_with_breadcrumbs


def redis_backend():
    """The optional Redis Stack client module, or an actionable failure.

    `--backend redis` is the legacy server deployment; the default LanceDB
    path never touches it, and the two live in different extras. Importing
    `redis_client` at module scope therefore killed the *documented*
    `uv sync --extra ingestion` install with a raw
    `ModuleNotFoundError: No module named 'redis'` before argparse ran --
    on the one extra named after this very task.

    Resolving it here mirrors `mcp_servers/rag.py`: an uninstalled extra is a
    named, fixable condition that carries its own install command, never a
    traceback through vendor internals.
    """
    try:
        from hpe_networking_mcp.pipeline.clients import redis_client
    except ImportError as exc:
        raise optional_deps.missing(
            "The Redis Stack ingest backend (`--backend redis`)",
            module="redis",
            extra="redis",
        ) from exc
    return redis_client


SOURCES_DIR = Path(__file__).parent / "sources"

# Maps source folder name → doc_type tag.
# `doc_type` is kept for back-compat; new code should filter by `source`.
# Some legacy doc_types are intentionally shared across vendor-specific
# sources, so provenance and precise filtering depend on the separate source
# field emitted for every chunk.
SOURCE_META = {
    "devhub": "devhub",
    "developer_docs": "developer-docs",
    "tech_docs": "tech-docs",
    "nac_docs": "nac",
    "vsg_docs": "vsg",
    "techdocs_html": "techdocs-html",
    "feature_navigator": "feature-navigator",
    "openapi_specs": "openapi",
    "product_specs": "product-openapi",
    "aos_techdocs": "aos-techdocs",
    "aoscx_release_notes": "aoscx-release-notes",
    "aoscx_guides": "aoscx-guides",
    "clearpass_guide": "clearpass-guide",
    "security_advisories": "security-advisory",
    "lifecycle_notices": "lifecycle",
    "juniper_lifecycle": "lifecycle",
    "juniper_security_advisories": "security-advisory",
    "juniper_kb": "juniper-kb",
    "mist_docs": "mist-docs",
    "junos_cli": "junos-cli",
    "mist_api_docs": "mist-api-docs",
    "mist_product_updates": "mist-product-updates",
    "junos_ex_hardware": "junos-ex-hardware",
    "junos_ex_release_notes": "junos-ex-release-notes",
    "junos_mx_hardware": "junos-mx-hardware",
    "junos_mx_release_notes": "junos-mx-release-notes",
    "junos_qfx_hardware": "junos-qfx-hardware",
    "junos_qfx_release_notes": "junos-qfx-release-notes",
    "junos_srx_hardware": "junos-srx-hardware",
    "junos_srx_release_notes": "junos-srx-release-notes",
    "product_datasheets": "product-datasheet",
}

# Source folders holding OpenAPI JSON rather than prose. `openapi_specs` is
# Central-only and feeds the exact SQLite spec index; `product_specs` holds the
# other portal products (AOS-CX, ClearPass, EdgeConnect, ...) and is embedded
# for semantic search instead, so it cannot perturb the generated Central client.
OPENAPI_SOURCES = {"openapi_specs", "product_specs"}

UPLOAD_BATCH = 100

# WebHelp books repeat an identical nav/header/footer shell on every topic, and
# it dwarfs the topic itself (a CLI-Bank page is ~1,000 chars of content inside
# ~4,300 chars of chrome). Taking the whole document would put tens of thousands
# of near-identical boilerplate chunks into the index, so prefer the main
# content region when the page marks one.
_HTML_CONTENT_SELECTORS = ("[role=main]", "main", "#mc-main-content", "div.body", "article")

# A region that matches but holds almost nothing means the page uses the
# selector for something else (a nav landing page, a frameset). Falling back to
# the full document is far better than silently indexing an empty topic.
_MIN_MAIN_CHARS = 200


def html_to_text(html: str) -> str:
    """Extract a page's readable text, preferring its main content region."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for selector in _HTML_CONTENT_SELECTORS:
        try:
            region = soup.select_one(selector)
        except Exception:  # noqa: BLE001 - a bad selector must not lose the page
            continue
        if region is None:
            continue
        text = region.get_text(separator="\n")
        if len(text.strip()) >= _MIN_MAIN_CHARS:
            return text
    return soup.get_text(separator="\n")


def read_file(path: Path) -> str | None:
    suffix = path.suffix.lower()
    try:
        if suffix in (".md", ".txt"):
            return path.read_text(encoding="utf-8", errors="ignore")
        if suffix in (".htm", ".html"):
            return html_to_text(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as e:
        print(f"  SKIP {path.name}: {e}")
    return None


def _md5_uuid(key: str) -> str:
    """Return a stable UUID string derived from an MD5 hash."""
    return str(uuid.UUID(hashlib.md5(key.encode()).hexdigest()))


def stable_id(rel_path: str, chunk_index: int) -> str:
    """Derive a stable chunk id from a path already relative to SOURCES_DIR.

    Callers must pass a path resolved relative to ``SOURCES_DIR`` (see
    ``collect_points``), not a raw ``Path`` as constructed from argv/CLI
    invocation — otherwise the same file chunked via a relative vs. an
    absolute invocation path would hash to different ids.
    """
    return _md5_uuid(f"{rel_path}:{chunk_index}")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_SOURCE_URL_COMMENT_RE = re.compile(r"<!--\s*source:\s*(\S+?)\s*-->")

RAG_METADATA_FIELDS = (
    "vendor",
    "product",
    "platform",
    "model",
    "release",
    "version",
    "document_family",
    "record_type",
    "authority",
    "freshness",
)

_DOCUMENT_FAMILIES = {
    "aoscx_guides": "technical-guide",
    "aoscx_release_notes": "release-notes",
    "clearpass_guide": "technical-guide",
    "feature_navigator": "feature-matrix",
    "junos_ex_release_notes": "release-notes",
    "junos_mx_release_notes": "release-notes",
    "junos_qfx_release_notes": "release-notes",
    "junos_srx_release_notes": "release-notes",
    "lifecycle_notices": "lifecycle",
    "juniper_lifecycle": "lifecycle",
    "juniper_security_advisories": "security-advisory",
    "mist_product_updates": "product-updates",
    "openapi_specs": "api-reference",
    "product_datasheets": "datasheet",
    "product_specs": "api-reference",
    "security_advisories": "security-advisory",
}

_OFFICIAL_VENDOR_HOSTS = {
    "arubanetworking.hpe.com",
    "arubanetworks.com",
    "developer.arubanetworks.com",
    "feature-navigator.arubanetworking.hpe.com",
    "hpe.com",
    "juniper.net",
    "mistsys.com",
    "mist.com",
    "support.hpe.com",
}

_AOSCX_VERSION_RE = re.compile(
    r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](\d{1,4})(?!\d)"
)
_JUNOS_VERSION_RE = re.compile(
    r"(?<!\d)(\d{2})[._-](\d{1,2})(?:[._-]?[rR](\d+))?(?!\d)"
)
_YEAR_MONTH_RE = re.compile(
    r"(?<!\d)(20\d{2})[-_./](0?[1-9]|1[0-2])"
    r"(?:[-_./](0?[1-9]|[12]\d|3[01]))?(?!\d)"
)
_MODEL_RE = re.compile(
    r"(?<![a-z0-9])(ex\d{3,5}(?:-[a-z]{1,3})?|ap\d{2,4}(?:-[a-z]{1,3})?)(?![a-z0-9])",
    re.IGNORECASE,
)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def extract_source_url(text: str) -> str | None:
    """Extract a leading scraper provenance comment from source text."""
    match = _SOURCE_URL_COMMENT_RE.search(text[:500])
    return match.group(1) if match else None


def _file_source_url(path: Path, chunked_text: str) -> str | None:
    """Recover source URL metadata without changing indexed document text."""
    if path.suffix.lower() not in (".htm", ".html"):
        return extract_source_url(chunked_text)
    try:
        raw_head = path.read_text(encoding="utf-8", errors="ignore")[:500]
    except OSError:
        return None
    return extract_source_url(raw_head)


def _normalize_model(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"[-_]+", "-", unquote(str(value)).strip()).upper() or None


def _source_vendor(
    source: str,
    source_url: str | None,
    path: str = "",
) -> str | None:
    host = (urlparse(source_url).hostname or "").lower() if source_url else ""
    haystack = f"{source} {path} {source_url or ''}".lower()
    if host.endswith(("juniper.net", "mistsys.com", "mist.com")):
        return "juniper"
    if host.endswith(("arubanetworks.com", "arubanetworking.hpe.com", "hpe.com")):
        return "aruba"
    if "mist" in haystack or source.startswith(("junos_", "juniper_")):
        return "juniper"
    if source in {
        "aos_techdocs",
        "aoscx_guides",
        "aoscx_release_notes",
        "clearpass_guide",
        "devhub",
        "developer_docs",
        "feature_navigator",
        "nac_docs",
        "product_specs",
        "product_datasheets",
        "security_advisories",
        "tech_docs",
        "techdocs_html",
        "vsg_docs",
    }:
        return "aruba"
    return None


def _source_product(
    source: str,
    path: str,
    source_url: str | None,
    product_hint: str | None,
) -> str | None:
    haystack = f"{source} {path} {source_url or ''} {product_hint or ''}".lower()
    if "mist" in haystack:
        return "mist"
    if "cppm" in haystack or "clearpass" in haystack:
        return "clearpass"
    if "edgeconnect" in haystack:
        return "edgeconnect"
    if "fabric-composer" in haystack or "afc-" in haystack:
        return "fabric-composer"
    if "apstra" in haystack:
        return "apstra"
    if "aos8" in haystack or "aos-8" in haystack:
        return "aos8"
    if "aoscx" in haystack or "aos-cx" in haystack:
        return "aos-cx"
    if "aos-s" in haystack or "aos_s" in haystack:
        return "aos-s"
    if re.search(r"(?<![a-z])uxi(?![a-z])", haystack):
        return "uxi"
    if source == "aos_techdocs" or "aos10" in haystack:
        return "aos10"
    if source.startswith("junos_") or "junos" in haystack:
        return "junos"
    if source == "feature_navigator":
        return "aos-cx" if "wired" in haystack or "switch" in haystack else "aos10"
    if source == "product_datasheets":
        return "ex-series" if "/switches/ex-series/" in haystack else "mist"
    if "central" in haystack or source in {
        "devhub",
        "developer_docs",
        "nac_docs",
        "openapi_specs",
        "techdocs_html",
    }:
        return "central"
    return product_hint.strip().lower() if product_hint and product_hint.strip() else None


def _source_platform(source: str, path: str, product: str | None) -> str | None:
    haystack = f"{source} {path}".lower()
    if product == "junos":
        for platform in ("ex", "mx", "qfx", "srx"):
            if re.search(rf"(?<![a-z]){platform}(?![a-z])", haystack):
                return platform
    if product == "ex-series":
        return "switch"
    if product == "aos-cx":
        return "switch"
    if product == "aos10":
        return "wireless"
    if product == "aos8":
        return "wireless"
    if product == "clearpass":
        return "nac"
    if product in {"mist", "central", "edgeconnect", "fabric-composer", "apstra", "uxi"}:
        return "cloud"
    if source == "product_datasheets":
        return "access-point" if "/ap-" in haystack or "access-point" in haystack else "switch"
    return None


def _release_and_version(
    path: str,
    product: str | None,
    version_hint: str | None,
) -> tuple[str | None, str | None]:
    if version_hint:
        version = str(version_hint).strip()
        if version:
            parts = re.findall(r"\d+", version)
            release = ".".join(parts[:2]) if len(parts) >= 2 else None
            return release, version

    if product == "aos-cx":
        match = _AOSCX_VERSION_RE.search(path)
        if match:
            major, minor, patch = match.groups()
            return f"{int(major)}.{int(minor)}", f"{int(major)}.{int(minor)}.{int(patch):04d}"
    if product == "junos":
        match = _JUNOS_VERSION_RE.search(path)
        if match:
            major, minor, revision = match.groups()
            release = f"{major}.{int(minor)}"
            version = release + (f"R{revision}" if revision else "")
            return release, version
    return None, None


def _source_model(path: str, product: str | None, platform: str | None) -> str | None:
    normalized = path.replace("_", "-")
    if product == "aos-cx":
        match = re.search(
            r"(?:^|/)(?:cli-reference|fundamentals|guide)-([a-z0-9/]+)",
            normalized,
            re.I,
        )
        if match:
            model = match.group(1).split("/", 1)[0]
            if re.fullmatch(r"[0-9]{3,5}(?:l|i)?", model, re.I):
                return _normalize_model(model)
        parts = normalized.split("/")
        if len(parts) > 1 and re.fullmatch(r"[0-9]{3,5}(?:l|i)?", parts[1], re.I):
            return _normalize_model(parts[1])
        for part in parts:
            if re.fullmatch(r"[0-9]{3,5}(?:l|i)?", part, re.I):
                return _normalize_model(part)
    match = _MODEL_RE.search(normalized)
    return _normalize_model(match.group(1)) if match else None


def _authority(
    source: str,
    source_url: str | None,
    document_family: str | None,
    provenance: Mapping[str, object] | None,
    path: str = "",
) -> str | None:
    if provenance and provenance.get("authority"):
        return str(provenance["authority"])
    host = (urlparse(source_url).hostname or "").lower() if source_url else ""
    if host.endswith(tuple(_OFFICIAL_VENDOR_HOSTS)) or _source_vendor(source, source_url, path):
        return (
            "official_vendor_openapi"
            if document_family == "api-reference"
            else "official_vendor"
        )
    return None


def _freshness(
    path: str,
    provenance: Mapping[str, object] | None,
) -> str | None:
    if provenance:
        for key in ("freshness", "reviewed_at", "fetched_at", "retrieved_at"):
            value = provenance.get(key)
            if value:
                return str(value)
    match = _YEAR_MONTH_RE.search(path)
    if match:
        year, month, day = match.groups()
        result = f"{year}-{int(month):02d}"
        return f"{result}-{int(day):02d}" if day else result
    parts = [part.lower() for part in path.replace("\\", "/").split("/")]
    for index, part in enumerate(parts):
        if part not in _MONTHS:
            continue
        if index + 1 >= len(parts):
            continue
        year_match = re.fullmatch(r"(20\d{2})(?:\.[a-z0-9]+)?", parts[index + 1])
        if year_match:
            return f"{year_match.group(1)}-{_MONTHS[part]:02d}"
    return None


def derive_metadata(
    source: str,
    file_path: str,
    source_url: str | None = None,
    *,
    product_hint: str | None = None,
    version_hint: str | None = None,
    record_type: str = "document",
    provenance: Mapping[str, object] | None = None,
) -> dict[str, str | None]:
    """Derive deterministic, conservative metadata from ingestion provenance."""
    source = str(source).strip().lower()
    file_path = str(file_path).replace("\\", "/")
    document_family = _DOCUMENT_FAMILIES.get(source)
    if document_family is None:
        document_family = {
            "security-advisory": "security-advisory",
            "lifecycle": "lifecycle",
        }.get(SOURCE_META.get(source, ""))
    vendor = _source_vendor(source, source_url, file_path)
    product = _source_product(source, file_path, source_url, product_hint)
    platform = _source_platform(source, file_path, product)
    model = _source_model(file_path, product, platform)
    release, version = _release_and_version(file_path, product, version_hint)
    return {
        "vendor": vendor,
        "product": product,
        "platform": platform,
        "model": model,
        "release": release,
        "version": version,
        "document_family": document_family or SOURCE_META.get(source),
        "record_type": record_type,
        "authority": _authority(
            source,
            source_url,
            document_family,
            provenance,
            file_path,
        ),
        "freshness": _freshness(file_path, provenance),
    }


derive_rag_metadata = derive_metadata


def _schema_to_text(spec_name: str, schema_name: str, schema: dict) -> str | None:
    """Convert a single OpenAPI schema object to a human-readable text chunk."""
    lines = [f"API spec: {spec_name}", f"Schema: {schema_name}"]

    if desc := schema.get("description"):
        lines.append(f"Description: {desc}")

    props = schema.get("properties", {})
    if not props:
        return None

    field_lines = []
    for field, fdef in props.items():
        # JSON Schema (fully embedded by OpenAPI 3.1) permits boolean schemas
        # (true/false) anywhere a schema is expected, including in
        # properties — skip those rather than crashing the whole run on
        # ``bool.get``.
        if not isinstance(fdef, dict):
            continue
        parts = [f"  - {field}"]
        if fdesc := fdef.get("description"):
            parts.append(f": {fdesc}")
        if ftype := fdef.get("type"):
            parts.append(f" (type: {ftype})")
        if enum_vals := fdef.get("enum"):
            parts.append(f"\n    Valid values: {', '.join(str(v) for v in enum_vals)}")
            if enum_desc := fdef.get("x-enumDescriptions"):
                for val, vdesc in enum_desc.items():
                    parts.append(f"\n      {val}: {vdesc}")
        field_lines.append("".join(parts))

    if not field_lines:
        return None

    lines.append("Fields:")
    lines.extend(field_lines)
    return "\n".join(lines)


def _endpoint_to_text(spec_name: str, path: str, method: str, op: dict) -> str:
    """Convert an OpenAPI path operation to a human-readable text chunk."""
    lines = [
        f"API spec: {spec_name}",
        f"Endpoint: {method.upper()} {path}",
    ]
    if summary := op.get("summary"):
        lines.append(f"Summary: {summary}")
    if desc := op.get("description"):
        lines.append(f"Description: {desc}")
    return "\n".join(lines)


def collect_openapi_points(source_dir: Path, doc_type: str = "openapi") -> list[dict]:
    """Parse OpenAPI JSON specs and emit one chunk per schema and per endpoint."""
    records = []
    files = sorted(source_dir.glob("*.json"))
    print(f"  {source_dir.name}: {len(files)} JSON files")

    for path in files:
        try:
            spec = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception as e:
            print(f"  SKIP {path.name}: {e}")
            continue

        spec_name = spec.get("info", {}).get("title", path.stem)
        spec_version = spec.get("info", {}).get("version")
        try:
            rel_path = str(path.resolve().relative_to(SOURCES_DIR.resolve()))
        except ValueError:
            # A symlink/mount that resolves outside the sources tree — skip
            # the one file rather than aborting the whole run.
            print(f"  SKIP {path.name}: resolves outside {SOURCES_DIR}")
            continue

        # One chunk per schema
        schemas = spec.get("components", {}).get("schemas", {})
        for schema_name, schema in schemas.items():
            text = _schema_to_text(spec_name, schema_name, schema)
            if not text or not text.strip():
                continue
            chunk_key = f"{rel_path}:schema:{schema_name}"
            metadata = derive_metadata(
                source_dir.name,
                rel_path,
                product_hint=spec_name,
                version_hint=spec_version,
                record_type="schema",
            )
            records.append(
                {
                    "id": _md5_uuid(chunk_key),
                    "text": text,
                    "source": source_dir.name,
                    "doc_type": doc_type,
                    "file_path": rel_path,
                    "chunk_index": len(records),
                    **metadata,
                }
            )

        # One chunk per endpoint operation
        for api_path, path_item in spec.get("paths", {}).items():
            for method, op in path_item.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                if not isinstance(op, dict):
                    continue
                text = _endpoint_to_text(spec_name, api_path, method, op)
                chunk_key = f"{rel_path}:path:{method}:{api_path}"
                metadata = derive_metadata(
                    source_dir.name,
                    rel_path,
                    product_hint=spec_name,
                    version_hint=spec_version,
                    record_type="operation",
                )
                records.append(
                    {
                        "id": _md5_uuid(chunk_key),
                        "text": text,
                        "source": source_dir.name,
                        "doc_type": doc_type,
                        "file_path": rel_path,
                        "chunk_index": len(records),
                        **metadata,
                    }
                )

    return records


def collect_points(source_dir: Path, doc_type: str) -> list[dict]:
    """Walk source_dir, chunk files, return records without vectors (added later)."""
    if source_dir.name in OPENAPI_SOURCES:
        return collect_openapi_points(source_dir, doc_type)

    records = []
    files = [
        p
        for p in source_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in (".md", ".htm", ".html", ".txt")
    ]
    print(f"  {source_dir.name}: {len(files)} files")

    for path in files:
        file_text = read_file(path)
        if not file_text or not file_text.strip():
            continue
        # Resolve both sides before computing the relative path so the id is
        # identical whether ingest_docs.py was invoked with a relative or an
        # absolute path (argv/PYTHONPATH differences otherwise change
        # Path(__file__) and thus SOURCES_DIR's string form).
        try:
            rel_path = str(path.resolve().relative_to(SOURCES_DIR.resolve()))
        except ValueError:
            # A symlink/mount that resolves outside the sources tree — skip
            # the one file rather than aborting the whole run.
            print(f"  SKIP {path.name}: resolves outside {SOURCES_DIR}")
            continue
        source_url = _file_source_url(path, file_text)
        chunks = chunk_text_with_breadcrumbs(file_text)
        metadata = derive_metadata(source_dir.name, rel_path, source_url)
        for i, (chunk, breadcrumb) in enumerate(chunks):
            records.append(
                {
                    "id": stable_id(rel_path, i),
                    "text": chunk,
                    "source": source_dir.name,
                    "doc_type": doc_type,
                    "file_path": rel_path,
                    "chunk_index": i,
                    "content_hash": content_hash(chunk),
                    "source_url": source_url,
                    "heading_breadcrumb": breadcrumb,
                    **metadata,
                }
            )
    return records


def _existing_ids(client, ids: list[str]) -> set[str]:
    """Return subset of ids already in Redis."""
    pipe = client.pipeline(transaction=False)
    for doc_id in ids:
        pipe.exists(f"doc:{doc_id}")
    results = pipe.execute()
    return {doc_id for doc_id, exists in zip(ids, results) if exists}


def upload(records: list[dict], ollama: OllamaClient, client):
    upsert_docs = redis_backend().upsert_docs
    skipped = 0
    uploaded = 0
    for batch_start in range(0, len(records), UPLOAD_BATCH):
        batch = records[batch_start : batch_start + UPLOAD_BATCH]
        existing = _existing_ids(client, [r["id"] for r in batch])
        new = [r for r in batch if r["id"] not in existing]
        skipped += len(batch) - len(new)
        if not new:
            continue
        texts = [r["text"] for r in new]
        vectors = ollama.embed_document(texts)
        docs = [{**r, "embedding": vec} for r, vec in zip(new, vectors)]
        upsert_docs(client, docs)
        uploaded += len(new)
        print(f"    uploaded {uploaded} new / {skipped} skipped / {len(records)} total")


WRITE_BATCH_LANCE = 512


def safe_parallel_workers(requested: int | None) -> int | None:
    """Disable fastembed multiprocessing on macOS where forkserver workers
    can deadlock during long index rebuilds."""
    if requested is not None and requested < 1:
        raise ValueError("--parallel must be at least 1")
    if sys.platform == "darwin" and requested is not None:
        print(
            "  macOS detected: disabling fastembed multiprocessing to avoid "
            "forkserver deadlocks; using the in-process embedder.",
            flush=True,
        )
        return None
    return requested


def source_uses_structured_index(folder: str, backend: str) -> bool:
    """OpenAPI stays exact in SQLite for the embedded backend, not vectors."""
    return backend == "lancedb" and folder == "openapi_specs"


def upload_lancedb(
    records: list[dict], ingested_sources: list[str], parallel: int | None = None
) -> None:
    """Full rebuild of the LanceDB docs table: stream embeddings from fastembed
    (one embed pass so parallel workers spawn once), add rows in batches into a
    staging table, assert every ingested source landed >0 chunks (R2 — a
    silently-empty source poisoned the old index), then atomically swap the
    staging table into place and build its ANN, metadata, and FTS indexes.

    Building into a staging table (rather than overwriting the live "docs"
    table on the first batch) means a crash partway through a large rebuild
    — OOM, disk full, Ctrl-C — leaves the previous good index untouched and
    still serving ``ask_docs``/``search_docs``, instead of a table truncated
    to whatever fraction of the corpus landed before the crash.
    """
    from hpe_networking_mcp.pipeline.clients import lance_client
    from hpe_networking_mcp.pipeline.clients.embed_client import EmbedClient

    db = lance_client.connect()
    embedder = EmbedClient()
    vectors = embedder.iter_embed_documents(
        (r["text"] for r in records), parallel=safe_parallel_workers(parallel)
    )
    staging_name = f"{lance_client.DOCS_TABLE}__staging"
    table = None
    buf: list[dict] = []
    done = 0
    for record, vec in zip(records, vectors):
        buf.append({**record, "vector": vec})
        if len(buf) >= WRITE_BATCH_LANCE:
            if table is None:
                table = lance_client.create_docs_table(db, buf, table_name=staging_name)
            else:
                table.add(buf)
            done += len(buf)
            buf = []
            print(f"    embedded+added {done}/{len(records)}", flush=True)
    if buf:
        if table is None:
            table = lance_client.create_docs_table(db, buf, table_name=staging_name)
        else:
            table.add(buf)
        done += len(buf)
        print(f"    embedded+added {done}/{len(records)}", flush=True)
    if table is None:
        raise SystemExit("No records to ingest — check ingestion/sources/")

    counts = lance_client.source_counts(db, table_name=staging_name)
    print(f"  per-source counts: {counts}")
    empty = [s for s in ingested_sources if counts.get(s, 0) == 0]
    if empty:
        raise SystemExit(f"FAIL: sources with 0 indexed chunks: {empty}")

    print("  swapping staged index into place...", flush=True)
    live_table = lance_client.promote_staging_table(db, staging_name)
    print("  building vector and FTS indexes...", flush=True)
    lance_client.build_search_indexes(live_table)


def upload_lancedb_incremental(
    records: list[dict],
    ingested_sources: list[str],
    parallel: int | None = None,
) -> bool:
    """Upsert changed chunks and delete removed chunks.

    Returns ``False`` when the existing table predates content hashes so the
    caller can perform one full rebuild and establish the incremental schema.

    ``records`` must reflect a *full-corpus* pass over every source whose
    directory is present on disk this run (the lancedb backend never accepts
    ``--source``; see ``main``'s parser.error). Deletion of stale rows is
    deliberately conservative so an incomplete local ``ingestion/sources``
    tree can never silently wipe an index:

    * A chunk removed from a source that *was* walked this run (its file was
      deleted, or the present source produced fewer/zero chunks) is swept —
      its id is gone from the current full-corpus ``records``.
    * A source folder that is missing this run but is still a known
      ``SOURCE_META`` key is treated as *not downloaded here*, not *deleted*:
      its rows are preserved untouched. This is the common case for a partial
      local checkout and must never trigger a mass delete.
    * Only a source that is no longer in ``SOURCE_META`` at all (truly retired
      from the catalog) has its leftover rows swept even though nothing this
      run names it.

    A run that collects zero records fails closed (raises) rather than
    deleting every row — that is overwhelmingly a missing sources tree, not an
    intentional empty corpus.
    """
    from hpe_networking_mcp.pipeline.clients import lance_client
    from hpe_networking_mcp.pipeline.clients.embed_client import EmbedClient

    if not records:
        raise SystemExit(
            "refusing incremental ingest: zero records collected — this is almost "
            "always a missing/undownloaded ingestion/sources tree, not an "
            "intentional empty corpus. The existing docs index was left untouched; "
            "re-run with the source directories present."
        )

    db = lance_client.connect()
    required_columns = {
        "content_hash",
        "source_url",
        "heading_breadcrumb",
        *RAG_METADATA_FIELDS,
    }
    missing_columns = sorted(required_columns - lance_client.docs_columns(db))
    if missing_columns:
        print(
            f"  existing docs table is missing column(s) {missing_columns}; "
            "performing one full rebuild",
            flush=True,
        )
        return False

    existing_hash: dict[str, str] = {}
    existing_source: dict[str, str] = {}
    for row in lance_client.docs_metadata(db):
        rid = str(row["id"])
        existing_hash[rid] = str(row.get("content_hash") or "")
        existing_source[rid] = str(row.get("source") or "")
    current_ids = {str(record["id"]) for record in records}
    changed = [
        record
        for record in records
        if existing_hash.get(str(record["id"])) != record["content_hash"]
    ]

    # Sources actually touched this run: those explicitly ingested plus any
    # that produced a record. A known-but-missing source dir is in neither, so
    # its rows are preserved; a source dropped from SOURCE_META entirely is
    # "retired" and its leftover rows are swept.
    walked_sources = set(ingested_sources) | {str(record.get("source") or "") for record in records}
    known_sources = set(SOURCE_META)
    removed = sorted(
        rid
        for rid in existing_hash
        if rid not in current_ids
        and (
            existing_source.get(rid, "") in walked_sources
            or existing_source.get(rid, "") not in known_sources
        )
    )
    preserved_absent = sorted(
        {
            existing_source.get(rid, "")
            for rid in existing_hash
            if rid not in current_ids
            and existing_source.get(rid, "") in known_sources
            and existing_source.get(rid, "") not in walked_sources
        }
    )
    if preserved_absent:
        print(
            "  preserving rows for known sources absent this run "
            f"(not downloaded, not deleted): {preserved_absent}",
            flush=True,
        )
    print(
        f"  incremental diff: {len(changed)} changed/new, "
        f"{len(removed)} removed, {len(records) - len(changed)} unchanged",
        flush=True,
    )

    if changed:
        embedder = EmbedClient()
        vectors = embedder.iter_embed_documents(
            (record["text"] for record in changed),
            parallel=safe_parallel_workers(parallel),
        )
        batch: list[dict] = []
        completed = 0
        for record, vector in zip(changed, vectors):
            batch.append({**record, "vector": vector})
            if len(batch) >= WRITE_BATCH_LANCE:
                lance_client.merge_docs_rows(db, batch)
                completed += len(batch)
                batch = []
                print(
                    f"    embedded+merged {completed}/{len(changed)}",
                    flush=True,
                )
        if batch:
            lance_client.merge_docs_rows(db, batch)
            completed += len(batch)
            print(f"    embedded+merged {completed}/{len(changed)}", flush=True)
    if removed:
        lance_client.delete_docs_ids(db, removed)

    table = lance_client.docs_table(db)
    if table is None:
        raise SystemExit("LanceDB docs table disappeared during incremental ingest")
    if changed or removed:
        print("  rebuilding vector and FTS indexes...", flush=True)
        lance_client.build_search_indexes(table)

    counts = lance_client.source_counts(db)
    empty = [source for source in ingested_sources if counts.get(source, 0) == 0]
    if empty:
        raise SystemExit(f"FAIL: sources with 0 indexed chunks: {empty}")
    print(f"  per-source counts: {counts}")
    return True


def _required_sources() -> frozenset[str]:
    """Return source folders required for a safe full prose-index rebuild."""
    from hpe_networking_mcp.pipeline.clients import advisory_index

    return frozenset(advisory_index.SOURCE_DIRS)


def missing_required_sources(sources_dir: Path | None = None) -> list[str]:
    """List required source folders that are absent from the local corpus."""
    base = sources_dir if sources_dir is not None else SOURCES_DIR
    return sorted(name for name in _required_sources() if not (base / name).is_dir())


# Authority ranking for dedup: when multiple records share a content_hash the one
# from the highest-authority source is kept as the canonical representative.
# Sources not listed here rank at 0 (lowest).
_DEDUP_SOURCE_PRIORITY: dict[str, int] = {
    # Primary/official prose docs — authoritative sources rank highest
    "developer_docs": 90,
    "techdocs_html": 85,
    "tech_docs": 85,
    "nac_docs": 80,
    "aoscx_guides": 75,
    "aoscx_release_notes": 70,
    "clearpass_guide": 70,
    "mist_docs": 70,
    "aos_techdocs": 65,
    "vsg_docs": 60,
    "feature_navigator": 55,
    "product_datasheets": 50,
    "mist_product_updates": 50,
    "junos_ex_hardware": 45,
    "junos_ex_release_notes": 45,
    "junos_mx_hardware": 45,
    "junos_mx_release_notes": 45,
    "junos_qfx_hardware": 45,
    "junos_qfx_release_notes": 45,
    "junos_srx_hardware": 45,
    "junos_srx_release_notes": 45,
    "security_advisories": 40,
    "juniper_security_advisories": 40,
    "lifecycle_notices": 35,
    "juniper_lifecycle": 35,
    "juniper_kb": 30,
    "devhub": 20,
}


def dedup_records(records: list[dict]) -> tuple[list[dict], int]:
    """Remove exact-duplicate content from the record list before embedding.

    38% of the corpus is boilerplate repeated verbatim across multiple source
    files — license text, standard upgrade steps, overview headers that appear
    in every AOS-CX patch release note. Embedding and storing 100k+ redundant
    chunks wastes space and degrades search quality by flooding results with
    near-identical entries.

    Strategy:
    - Group by ``content_hash`` (exact duplicate detection via SHA-256 prefix).
    - Within each group, keep the record from the highest-priority source
      (``_DEDUP_SOURCE_PRIORITY``). Ties are broken by lexicographic file_path
      order to make the selection stable across runs.
    - Discard the other records — their content_hash is still the canonical
      match key, so search-time ``_dedup_by_content`` will never see the
      duplicates.

    Returns ``(deduped_records, n_dropped)``. Chunks with no ``content_hash``
    (should not happen in current ingestion, but handled defensively) are
    always kept.
    """
    by_hash: dict[str, dict] = {}
    no_hash: list[dict] = []

    for rec in records:
        ch = rec.get("content_hash")
        if not ch:
            no_hash.append(rec)
            continue
        existing = by_hash.get(ch)
        if existing is None:
            by_hash[ch] = rec
        else:
            # Keep the higher-priority source; break ties by file_path
            existing_priority = _DEDUP_SOURCE_PRIORITY.get(existing.get("source", ""), 0)
            new_priority = _DEDUP_SOURCE_PRIORITY.get(rec.get("source", ""), 0)
            if new_priority > existing_priority or (
                new_priority == existing_priority
                and rec.get("file_path", "") < existing.get("file_path", "")
            ):
                by_hash[ch] = rec

    deduped = list(by_hash.values()) + no_hash
    n_dropped = len(records) - len(deduped)
    return deduped, n_dropped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("lancedb", "redis"), default="lancedb")
    parser.add_argument("--source", help="Ingest one source folder only (redis backend)")
    parser.add_argument("--dry-run", action="store_true", help="Count chunks, no upload")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="upsert changed LanceDB chunks instead of rebuilding every embedding",
    )
    # Left unresolved so `--help` needs no `redis` import. The default is
    # `redis_client.DOCS_INDEX`, read in the redis branch below rather than
    # retyped here, so the two cannot drift.
    parser.add_argument(
        "--index",
        default=None,
        dest="index",
        help="Redis index name (--backend redis only; defaults to redis_client.DOCS_INDEX)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help=("fastembed worker processes (Linux; macOS safely falls back in-process)"),
    )
    parser.add_argument(
        "--dedup-on-ingest",
        action="store_true",
        help=(
            "Drop exact-duplicate content (same content_hash) before embedding. "
            "Keeps the most authoritative representative of each unique chunk. "
            "Reduces index size by up to 38%% without re-downloading sources. "
            "Not compatible with --incremental (incremental upsert handles "
            "duplicate avoidance at the row level)."
        ),
    )
    args = parser.parse_args()

    if args.backend == "lancedb" and args.source:
        parser.error(
            "--source only applies to --backend redis; lancedb always rebuilds all sources"
        )
    if args.backend == "redis" and args.incremental:
        parser.error("--incremental applies only to the lancedb backend")
    if args.dedup_on_ingest and args.incremental:
        parser.error("--dedup-on-ingest is incompatible with --incremental")
    if args.dedup_on_ingest and args.backend == "redis":
        parser.error("--dedup-on-ingest applies only to the lancedb backend")
    if args.backend == "lancedb" and not args.incremental and not args.dry_run:
        missing = missing_required_sources(SOURCES_DIR)
        if missing:
            raise SystemExit(
                "FAIL: required source folder(s) missing from ingestion/sources/ "
                f"-- refusing to replace the docs index: {missing}. Refresh "
                "them first, or pass --incremental to preserve existing rows."
            )

    sources = {args.source: SOURCE_META.get(args.source, "unknown")} if args.source else SOURCE_META

    all_records: list[dict] = []
    ingested_sources: list[str] = []
    openapi_specs_present = False
    for folder, doc_type in sources.items():
        source_dir = SOURCES_DIR / folder
        if not source_dir.exists():
            print(f"SKIP: {source_dir} not found")
            continue
        records = collect_points(source_dir, doc_type)
        if source_uses_structured_index(folder, args.backend):
            openapi_specs_present = True
            print(f"  → {len(records)} structured API records (SQLite only)")
            continue
        all_records.extend(records)
        ingested_sources.append(folder)
        print(f"  → {len(records)} chunks")

    print(f"\nTotal chunks: {len(all_records)}")

    if args.dedup_on_ingest:
        all_records, n_dropped = dedup_records(all_records)
        print(
            f"  --dedup-on-ingest: dropped {n_dropped} exact-duplicate chunks "
            f"→ {len(all_records)} unique chunks remain"
        )

    if args.dry_run:
        print("Dry run — no upload.")
        return

    if args.backend == "lancedb":
        print("\nRebuilding embedded indexes (LanceDB + specs SQLite)...")
        incremental_done = args.incremental and upload_lancedb_incremental(
            all_records,
            ingested_sources,
            parallel=args.parallel,
        )
        if not incremental_done:
            missing = missing_required_sources(SOURCES_DIR)
            if missing:
                raise SystemExit(
                    "FAIL: required source folder(s) missing from "
                    "ingestion/sources/ -- refusing the full rebuild fallback "
                    f"after incremental migration: {missing}. Refresh them "
                    "first, or restore a compatible existing index."
                )
            upload_lancedb(all_records, ingested_sources, parallel=args.parallel)
        if openapi_specs_present:
            from hpe_networking_mcp.pipeline.clients import specs_index

            print("  rebuilding shared structured SQLite index...")
            print(f"  {specs_index.rebuild_shared()}")
    else:
        redis_client = redis_backend()
        index = args.index or redis_client.DOCS_INDEX
        print("\nConnecting to Redis Stack + Ollama...")
        client = redis_client.get_client()
        redis_client.ensure_index(client, index)

        with OllamaClient() as ollama:
            print(f"Uploading to index '{index}'...")
            upload(all_records, ollama, client)

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except optional_deps.MissingOptionalDependency as exc:
        # The remedy is the whole point; a traceback through importlib buries
        # it. Same `FAIL: ` shape the missing-sources guards above use.
        raise SystemExit(f"FAIL: {exc}") from exc
