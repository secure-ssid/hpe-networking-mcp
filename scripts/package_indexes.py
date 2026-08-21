#!/usr/bin/env python3
"""Package local RAG/OpenAPI indexes for a GitHub Release asset.

Also owns the *local* index manifest pair that ships inside that archive and
is left behind on disk after a download/restore:

    data/SOURCE-MANIFEST.json   verbatim copy of ingestion/source_manifest.json
    data/INDEX-MANIFEST.json    derived description of the local artifacts

Those two files used to drift apart silently -- a downloaded 9-source
``data/SOURCE-MANIFEST.json`` snapshot sat next to a freshly generated
16-source ``data/INDEX-MANIFEST.json`` summary, so the pair claimed two
different RAG source sets at once. ``--check-local-manifests`` (also wired
into ``scripts/validate_release.py``'s strict mode) now fails on that drift,
and ``--write-local-manifests`` reconciles both files against the *declared*
sources and the *actual* local artifacts.

Reconciliation deliberately implies nothing about upstream freshness: it
never fetches a source, and the generated manifest records
``provenance.source_refresh_performed = false`` plus each artifact's own
modification timestamp so a reconciled manifest can never be mistaken for a
re-scraped index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hpe_networking_mcp.pipeline import project_facts  # noqa: E402

DATA_DIR = ROOT / "data"
DIST_DIR = ROOT / "dist"
REQUIRED_ARTIFACTS = ("docs.lance", "tools.lance", "specs.sqlite")
SOURCE_MANIFEST = ROOT / "ingestion" / "source_manifest.json"
SOURCES_ROOT = ROOT / "ingestion" / "sources"
LATEST_ARCHIVE = "hpe-networking-mcp-rag-index-latest.tar.gz"

#: Bumped when the generated INDEX-MANIFEST.json shape changes so a stale
#: manifest is rejected instead of silently half-read.
#: v3 adds the per-source "sources" block (digest/count/refresh-timestamp/
#: required flag) described in the module docstring's fail-closed contract.
INDEX_MANIFEST_SCHEMA_VERSION = 3
LOCAL_SOURCE_MANIFEST = DATA_DIR / "SOURCE-MANIFEST.json"
LOCAL_INDEX_MANIFEST = DATA_DIR / "INDEX-MANIFEST.json"
_RECONCILE_COMMAND = "uv run python scripts/package_indexes.py --write-local-manifests"


def _project_version() -> str:
    pyproject = ROOT / "pyproject.toml"
    for line in pyproject.read_text().splitlines():
        if line.strip().startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_counts(path: Path) -> dict[str, int]:
    """Row counts for the structured tables, via the one definition of them.

    ``project_facts.SPECS_TABLES`` is where the table list and the
    omit-what-is-absent rule live; a third private copy here drifted the
    moment the offline build started shipping only the OpenAPI tables.
    """
    return project_facts.specs_counts(path)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _source_manifest_summary(path: Path | None = None) -> dict[str, object]:
    path = path or SOURCE_MANIFEST
    if not path.exists():
        raise SystemExit(f"Missing source manifest: {_display_path(path)}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid source manifest JSON: {exc}") from exc
    if not isinstance(data, list):
        raise SystemExit("Invalid source manifest: top-level JSON value must be a list")
    sources = sorted(
        str(item.get("source", "")).strip()
        for item in data
        if isinstance(item, dict) and str(item.get("source", "")).strip()
    )
    return {
        "path": _display_path(path),
        "sha256": _sha256(path),
        "source_count": len(sources),
        "sources": sources,
    }


def _modified_at(path: Path) -> str:
    """UTC modification timestamp of ``path``.

    Recorded per artifact so a manifest regenerated today over month-old
    indexes still reports *when the index itself was built*, rather than
    letting ``built_at`` imply a fresh rebuild. For a directory artifact this
    is the newest contained file's timestamp: a LanceDB table directory's own
    mtime does not change when a new table version is written underneath it,
    which would otherwise report a just-rebuilt index as months old.
    """
    if path.is_dir():
        mtimes = [item.stat().st_mtime for item in path.rglob("*") if item.is_file()]
        newest = max(mtimes) if mtimes else path.stat().st_mtime
    else:
        newest = path.stat().st_mtime
    return datetime.fromtimestamp(newest, tz=timezone.utc).isoformat(timespec="seconds")


def _tree_sha256(root: Path) -> str:
    """Content hash of a directory artifact.

    Hashes each file's repo-relative path, size, and bytes in sorted order,
    so the digest is stable across machines and changes whenever any indexed
    file changes -- the property the reconciliation gate needs to detect an
    index that was rebuilt without regenerating its manifest.
    """
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(str(path.stat().st_size).encode())
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _required_source_names() -> frozenset[str]:
    """Source folders whose complete absence must fail a rebuild/package closed.

    Reuses ``advisory_index.SOURCE_DIRS`` -- the same four security-advisory/
    lifecycle source families ``ingestion/ingest_docs.py``'s
    ``required_sources()`` and ``specs_index.rebuild_shared`` already treat
    as load-bearing, so this manifest's notion of "required" can never drift
    from the code path that actually enforces it. Imported lazily to keep
    this script's baseline import light.
    """
    from hpe_networking_mcp.pipeline.clients import advisory_index

    return frozenset(advisory_index.SOURCE_DIRS)


def _source_artifact_summary(source_dir: Path) -> dict[str, object]:
    """On-disk digest/count/refresh-timestamp summary for one declared source.

    Mirrors ``_artifact_manifest``'s per-index-artifact digest (``_tree_sha256``/
    ``_modified_at``) at the per-source-folder granularity, so a single
    ``ingestion/sources/<name>`` directory going missing, appearing empty, or
    silently changing content is visible in the manifest the same way a
    rebuilt docs.lance already is. A present-but-empty directory reports
    ``sha256: None`` -- there is no meaningful content digest for zero files.
    """
    if not source_dir.is_dir():
        return {
            "present": False,
            "file_count": 0,
            "bytes": 0,
            "sha256": None,
            "last_refreshed_at": None,
        }
    files = [item for item in source_dir.rglob("*") if item.is_file()]
    return {
        "present": True,
        "file_count": len(files),
        "bytes": sum(item.stat().st_size for item in files),
        "sha256": _tree_sha256(source_dir) if files else None,
        "last_refreshed_at": _modified_at(source_dir) if files else None,
    }


def _per_source_manifest(
    declared_sources: list[str],
    sources_dir: Path,
    docs_sources_counts: dict[str, int],
) -> dict[str, dict[str, object]]:
    """Per-declared-source digest/count/refresh-timestamp/required summary.

    Combines the declared source list (``ingestion/source_manifest.json``),
    each source's on-disk artifact under ``ingestion/sources/<name>``, its
    required/optional status (see ``_required_source_names``), and how many
    chunks of it actually landed in the live docs.lance table (from
    ``_index_contents``'s ``docs_sources``) -- so a required source that is
    present on disk but was never actually indexed (every file failed to
    parse, say) is visible here too, not just a wholly-missing directory.
    """
    required = _required_source_names()
    return {
        name: {
            **_source_artifact_summary(sources_dir / name),
            "required": name in required,
            "indexed_chunk_count": docs_sources_counts.get(name, 0),
        }
        for name in declared_sources
    }


def _index_contents(data_dir: Path | None = None) -> dict[str, object]:
    """Row/identity counts actually stored in the local LanceDB tables.

    Sizes and file counts alone cannot tell a complete index from a
    truncated one, and they cannot tell a pre-rename tool table from the
    current catalog. Returns ``{"available": False, ...}`` instead of raising
    when the tables are missing/unreadable so packaging a partial data
    directory still produces a manifest.
    """
    data_dir = data_dir or DATA_DIR
    contents: dict[str, object] = {"available": False}
    try:
        from hpe_networking_mcp.pipeline.clients import lance_client

        db = lance_client.connect(data_dir)
        docs = lance_client.docs_table(db)
        tools = lance_client.tools_table(db)
        if docs is None and tools is None:
            contents["reason"] = "no LanceDB docs/tools table under data/"
            return contents
        contents["available"] = True
        if docs is not None:
            rows = docs.count_rows()
            sources = (
                docs.search().select(["source"]).limit(rows).to_arrow().column("source").to_pylist()
            )
            counts: dict[str, int] = {}
            for source in sources:
                counts[source] = counts.get(source, 0) + 1
            contents["docs_rows"] = rows
            contents["docs_sources"] = dict(sorted(counts.items()))
        if tools is not None:
            rows = tools.count_rows()
            servers = (
                tools.search().select(["server"]).limit(rows).to_arrow().column("server").to_pylist()
            )
            per_server: dict[str, int] = {}
            for server in servers:
                per_server[server] = per_server.get(server, 0) + 1
            contents["tool_rows"] = rows
            contents["tool_servers"] = dict(sorted(per_server.items()))
    except Exception as exc:  # pragma: no cover - defensive, reported not raised
        contents["available"] = False
        contents["reason"] = f"{type(exc).__name__}: {exc}"
    return contents


def _artifact_manifest(version: str) -> dict[str, object]:
    artifacts: dict[str, object] = {}
    for name in REQUIRED_ARTIFACTS:
        path = DATA_DIR / name
        if path.is_dir():
            files = [item for item in path.rglob("*") if item.is_file()]
            artifacts[name] = {
                "kind": "directory",
                "files": len(files),
                "bytes": sum(item.stat().st_size for item in files),
                "sha256": _tree_sha256(path),
                "modified_at": _modified_at(path),
            }
        elif path.is_file():
            detail: dict[str, object] = {
                "kind": "file",
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "modified_at": _modified_at(path),
            }
            if name == "specs.sqlite":
                detail["counts"] = _sqlite_counts(path)
            artifacts[name] = detail
    return {
        "schema_version": INDEX_MANIFEST_SCHEMA_VERSION,
        "package_version": version,
        "project_version": _project_version(),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": {
            "source_refresh_performed": False,
            "note": (
                "Describes the local index artifacts as they exist on disk. "
                "Generating this manifest never fetches or re-scrapes an "
                "upstream source; per-artifact modified_at is the real index "
                "build time, while built_at is only when this description was "
                "written."
            ),
        },
        "artifacts": artifacts,
        "index_contents": (index_contents := _index_contents()),
        "source_manifest": (source_summary := _source_manifest_summary()),
        "sources": _per_source_manifest(
            source_summary["sources"],
            SOURCES_ROOT,
            index_contents.get("docs_sources") or {},
        ),
        "restore": "tar -xzf hpe-networking-mcp-rag-index-<version>.tar.gz",
        "rebuild": (
            "uv run python ingestion/ingest_docs.py && "
            "uv run python scripts/ingest_tools.py --complete-catalog"
        ),
    }


def write_local_manifests(version: str | None = None) -> tuple[Path, Path]:
    """Reconcile ``data/SOURCE-MANIFEST.json`` and ``data/INDEX-MANIFEST.json``.

    ``data/SOURCE-MANIFEST.json`` is written as a byte-identical copy of the
    tracked ``ingestion/source_manifest.json`` (the single declaration of RAG
    sources), and ``data/INDEX-MANIFEST.json`` is derived from the artifacts
    actually present under ``data/``. Neither step contacts a source.

    Args:
        version: Release label recorded as ``package_version``; defaults to
            ``v<project version>``.

    Returns:
        The ``(source_manifest_path, index_manifest_path)`` that were written.
    """
    if not DATA_DIR.is_dir():
        raise SystemExit(f"Missing data directory: {_display_path(DATA_DIR)}")
    version = version or f"v{_project_version()}"
    LOCAL_SOURCE_MANIFEST.write_bytes(SOURCE_MANIFEST.read_bytes())
    LOCAL_INDEX_MANIFEST.write_text(json.dumps(_artifact_manifest(version), indent=2) + "\n")
    return LOCAL_SOURCE_MANIFEST, LOCAL_INDEX_MANIFEST


def check_local_manifests(require_artifacts: bool = True) -> list[str]:
    """Return the reconciliation problems in the local manifest pair.

    Args:
        require_artifacts: Report missing ``data/`` artifacts as problems.
            Set False for a no-data checkout that will restore a pinned
            bundle later.

    Returns:
        A list of human-readable problems; empty means the on-disk manifests
        exactly describe the declared sources and the local artifacts.
    """
    problems: list[str] = []
    missing_artifacts = [name for name in REQUIRED_ARTIFACTS if not (DATA_DIR / name).exists()]
    if missing_artifacts and require_artifacts:
        problems.append(f"missing index artifacts under data/: {', '.join(missing_artifacts)}")
    if missing_artifacts and not require_artifacts:
        return problems

    if not LOCAL_SOURCE_MANIFEST.is_file():
        problems.append(f"missing {_display_path(LOCAL_SOURCE_MANIFEST)}")
    elif LOCAL_SOURCE_MANIFEST.read_bytes() != SOURCE_MANIFEST.read_bytes():
        declared = _source_manifest_summary()
        try:
            local_sources = sorted(
                str(item.get("source", ""))
                for item in json.loads(LOCAL_SOURCE_MANIFEST.read_text())
                if isinstance(item, dict) and item.get("source")
            )
        except (json.JSONDecodeError, TypeError):
            local_sources = []
        problems.append(
            f"{_display_path(LOCAL_SOURCE_MANIFEST)} does not match "
            f"{_display_path(SOURCE_MANIFEST)} "
            f"({len(local_sources)} local source(s) vs {declared['source_count']} declared; "
            f"missing locally: {sorted(set(declared['sources']) - set(local_sources)) or 'none'})"
        )

    if not LOCAL_INDEX_MANIFEST.is_file():
        problems.append(f"missing {_display_path(LOCAL_INDEX_MANIFEST)}")
        return problems

    try:
        manifest = json.loads(LOCAL_INDEX_MANIFEST.read_text())
    except json.JSONDecodeError as exc:
        problems.append(f"{_display_path(LOCAL_INDEX_MANIFEST)} is not valid JSON: {exc}")
        return problems

    if manifest.get("schema_version") != INDEX_MANIFEST_SCHEMA_VERSION:
        problems.append(
            f"{_display_path(LOCAL_INDEX_MANIFEST)} schema_version "
            f"{manifest.get('schema_version')!r} != {INDEX_MANIFEST_SCHEMA_VERSION}"
        )
        return problems

    if manifest.get("project_version") != _project_version():
        problems.append(
            f"INDEX-MANIFEST.json project_version {manifest.get('project_version')!r} != "
            f"pyproject version {_project_version()!r}"
        )

    declared = _source_manifest_summary()
    recorded_sources = manifest.get("source_manifest") or {}
    if recorded_sources.get("sha256") != declared["sha256"]:
        problems.append(
            "INDEX-MANIFEST.json source_manifest.sha256 does not match "
            f"{_display_path(SOURCE_MANIFEST)}"
        )
    if recorded_sources.get("sources") != declared["sources"]:
        recorded_names = set(recorded_sources.get("sources") or [])
        problems.append(
            "INDEX-MANIFEST.json records "
            f"{len(recorded_names)} source(s) but {declared['source_count']} are declared "
            f"(missing: {sorted(set(declared['sources']) - recorded_names) or 'none'}; "
            f"unknown: {sorted(recorded_names - set(declared['sources'])) or 'none'})"
        )

    recorded_artifacts = manifest.get("artifacts") or {}
    for name in REQUIRED_ARTIFACTS:
        path = DATA_DIR / name
        if not path.exists():
            continue
        recorded = recorded_artifacts.get(name)
        if not isinstance(recorded, dict):
            problems.append(f"INDEX-MANIFEST.json is missing an entry for {name}")
            continue
        if path.is_dir():
            files = [item for item in path.rglob("*") if item.is_file()]
            actual: dict[str, object] = {
                "files": len(files),
                "bytes": sum(item.stat().st_size for item in files),
                "sha256": _tree_sha256(path),
            }
        else:
            actual = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
        for key, value in actual.items():
            if recorded.get(key) != value:
                problems.append(
                    f"INDEX-MANIFEST.json {name}.{key} is {recorded.get(key)!r}, "
                    f"local artifact has {value!r}"
                )
        if name == "specs.sqlite":
            counts = _sqlite_counts(path)
            if recorded.get("counts") != counts:
                problems.append(
                    f"INDEX-MANIFEST.json specs.sqlite counts {recorded.get('counts')!r} != "
                    f"local {counts!r}"
                )

    recorded_per_source = manifest.get("sources") or {}
    required_gaps = sorted(
        name
        for name, detail in recorded_per_source.items()
        if isinstance(detail, dict)
        and detail.get("required")
        and not detail.get("indexed_chunk_count")
    )
    if required_gaps:
        problems.append(
            f"required source(s) have zero indexed chunks in INDEX-MANIFEST.json: {required_gaps}"
        )
    return problems


def package_indexes(version: str, output_dir: Path) -> tuple[Path, Path]:
    missing = [name for name in REQUIRED_ARTIFACTS if not (DATA_DIR / name).exists()]
    if missing:
        raise SystemExit(f"Missing index artifacts under data/: {', '.join(missing)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"hpe-networking-mcp-rag-index-{version}.tar.gz"
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    manifest = _artifact_manifest(version)
    required_gaps = sorted(
        name
        for name, detail in manifest["sources"].items()
        if detail["required"] and not detail["indexed_chunk_count"]
    )
    if required_gaps:
        raise SystemExit(
            "Refusing to package: required source(s) have zero indexed chunks in "
            f"data/docs.lance: {required_gaps}. Rebuild with a complete "
            "ingestion/sources/ checkout before packaging a release."
        )

    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "INDEX-MANIFEST.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        with tarfile.open(archive, "w:gz") as tar:
            for name in REQUIRED_ARTIFACTS:
                tar.add(DATA_DIR / name, arcname=f"data/{name}")
            tar.add(SOURCE_MANIFEST, arcname="data/SOURCE-MANIFEST.json")
            tar.add(manifest_path, arcname="data/INDEX-MANIFEST.json")

    checksum.write_text(f"{_sha256(archive)}  {archive.name}\n")
    return archive, checksum


def write_latest_alias(archive: Path, output_dir: Path) -> tuple[Path, Path]:
    latest_archive = output_dir / LATEST_ARCHIVE
    latest_checksum = latest_archive.with_suffix(latest_archive.suffix + ".sha256")

    if archive.resolve(strict=False) != latest_archive.resolve(strict=False):
        shutil.copyfile(archive, latest_archive)
    latest_checksum.write_text(f"{_sha256(latest_archive)}  {latest_archive.name}\n")
    return latest_archive, latest_checksum


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default=f"v{_project_version()}",
        help="Release/index version label used in the archive filename",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DIST_DIR,
        help="Directory for generated archive and checksum",
    )
    parser.add_argument(
        "--skip-latest-copy",
        action="store_true",
        help="Only write the versioned archive/checksum, not the downloader-friendly latest copy.",
    )
    parser.add_argument(
        "--write-local-manifests",
        action="store_true",
        help=(
            "Reconcile data/SOURCE-MANIFEST.json and data/INDEX-MANIFEST.json against the "
            "declared sources and local artifacts, then exit (no archive is built, no source "
            "is fetched)."
        ),
    )
    parser.add_argument(
        "--check-local-manifests",
        action="store_true",
        help=(
            "Exit non-zero when the local manifest pair does not describe the declared "
            "sources and local artifacts, then exit."
        ),
    )
    parser.add_argument(
        "--allow-missing-artifacts",
        action="store_true",
        help=(
            "With --check-local-manifests, treat an artifact-free data/ directory as a "
            "no-data checkout instead of a failure."
        ),
    )
    args = parser.parse_args()

    if args.check_local_manifests:
        problems = check_local_manifests(require_artifacts=not args.allow_missing_artifacts)
        if problems:
            print("Local index manifests are out of sync:")
            for problem in problems:
                print(f"  - {problem}")
            print(f"Reconcile with `{_RECONCILE_COMMAND}`.")
            return 1
        print("Local index manifests match the declared sources and local artifacts")
        return 0

    if args.write_local_manifests:
        source_path, index_path = write_local_manifests(args.version)
        print(f"Wrote {_display_path(source_path)}")
        print(f"Wrote {_display_path(index_path)}")
        return 0

    archive, checksum = package_indexes(args.version, args.output_dir)
    print(f"Wrote {archive}")
    print(f"Wrote {checksum}")
    if not args.skip_latest_copy:
        latest_archive, latest_checksum = write_latest_alias(archive, args.output_dir)
        print(f"Wrote {latest_archive}")
        print(f"Wrote {latest_checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
