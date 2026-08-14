"""Personal document collections (local metadata store).

Phase 1 keeps files + a JSON sidecar index under the user data dir.
Embedding/search against LanceDB can plug in later without changing the
metadata contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hpe_networking_mcp.cli_client.config import default_user_data_dir

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


@dataclass
class DocumentRecord:
    id: str
    collection: str
    source_uri: str
    stored_path: str
    title: str = ""
    product: str = ""
    vendor: str = ""
    media_type: str = ""
    checksum_sha256: str = ""
    bytes: int = 0
    added_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentRecord:
        return cls(
            id=str(data["id"]),
            collection=str(data["collection"]),
            source_uri=str(data["source_uri"]),
            stored_path=str(data["stored_path"]),
            title=str(data.get("title") or ""),
            product=str(data.get("product") or ""),
            vendor=str(data.get("vendor") or ""),
            media_type=str(data.get("media_type") or ""),
            checksum_sha256=str(data.get("checksum_sha256") or ""),
            bytes=int(data.get("bytes") or 0),
            added_at=float(data.get("added_at") or time.time()),
            tags=list(data.get("tags") or []),
        )


def _safe_collection(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", name.strip()).strip("-._")
    if not cleaned:
        raise ValueError("collection name is empty after sanitization")
    return cleaned[:64]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _guess_media(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
        ".html": "text/html",
        ".htm": "text/html",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    }.get(ext, "application/octet-stream")


class DocumentStore:
    """Filesystem-backed personal document store."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_user_data_dir() / "collections"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self._docs: dict[str, DocumentRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.index_path.is_file():
            self._docs = {}
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._docs = {}
            return
        docs = raw.get("documents") if isinstance(raw, dict) else None
        if not isinstance(docs, list):
            self._docs = {}
            return
        out: dict[str, DocumentRecord] = {}
        for item in docs:
            if not isinstance(item, dict):
                continue
            try:
                rec = DocumentRecord.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue
            out[rec.id] = rec
        self._docs = out

    def _save(self) -> None:
        payload = {
            "version": 1,
            "documents": [
                d.to_dict()
                for d in sorted(self._docs.values(), key=lambda r: r.added_at)
            ],
        }
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.index_path)

    def list(self, collection: str | None = None) -> list[DocumentRecord]:
        docs = list(self._docs.values())
        if collection:
            c = _safe_collection(collection)
            docs = [d for d in docs if d.collection == c]
        return sorted(docs, key=lambda d: (d.collection, d.title or d.source_uri))

    def add_file(
        self,
        source: str | Path,
        *,
        collection: str = "personal",
        title: str | None = None,
        product: str = "",
        vendor: str = "",
        tags: list[str] | None = None,
    ) -> DocumentRecord:
        coll = _safe_collection(collection)
        src = Path(source).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(f"not a file: {src}")

        digest = _sha256_file(src)
        doc_id = digest[:16]
        dest_dir = self.root / coll / "files"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{doc_id}{src.suffix.lower()}"
        if not dest.exists():
            shutil.copy2(src, dest)

        rec = DocumentRecord(
            id=doc_id,
            collection=coll,
            source_uri=src.as_uri(),
            stored_path=str(dest),
            title=title or src.name,
            product=product,
            vendor=vendor,
            media_type=_guess_media(src),
            checksum_sha256=digest,
            bytes=src.stat().st_size,
            tags=list(tags or []),
        )
        self._docs[doc_id] = rec
        self._save()
        return rec

    def add_uri_record(
        self,
        uri: str,
        *,
        collection: str = "personal",
        title: str | None = None,
        product: str = "",
        vendor: str = "",
        tags: list[str] | None = None,
        stored_path: str = "",
        media_type: str = "",
        checksum: str = "",
        size: int = 0,
    ) -> DocumentRecord:
        """Register a remote URI without downloading (Phase 1 bookmark)."""
        coll = _safe_collection(collection)
        parsed = urlparse(uri)
        if parsed.scheme not in {"http", "https", "file"}:
            raise ValueError(f"unsupported URI scheme: {parsed.scheme!r}")
        digest = checksum or hashlib.sha256(uri.encode("utf-8")).hexdigest()
        doc_id = digest[:16]
        rec = DocumentRecord(
            id=doc_id,
            collection=coll,
            source_uri=uri,
            stored_path=stored_path,
            title=title or Path(parsed.path).name or uri,
            product=product,
            vendor=vendor,
            media_type=media_type,
            checksum_sha256=digest,
            bytes=size,
            tags=list(tags or []),
        )
        self._docs[doc_id] = rec
        self._save()
        return rec

    def remove(self, doc_id: str, *, delete_file: bool = True) -> bool:
        rec = self._docs.pop(doc_id, None)
        if rec is None:
            return False
        if delete_file and rec.stored_path:
            p = Path(rec.stored_path)
            if p.is_file() and self.root in p.resolve().parents:
                try:
                    p.unlink()
                except OSError:
                    pass
        self._save()
        return True

    def search(
        self,
        query: str,
        *,
        collection: str | None = None,
        limit: int = 20,
    ) -> list[DocumentRecord]:
        """Simple substring search over title/uri/tags/product (no embeddings yet)."""
        q = query.lower().strip()
        if not q:
            return []
        hits: list[tuple[int, DocumentRecord]] = []
        for rec in self.list(collection):
            hay = " ".join(
                [
                    rec.title,
                    rec.source_uri,
                    rec.product,
                    rec.vendor,
                    " ".join(rec.tags),
                    rec.media_type,
                ]
            ).lower()
            if q not in hay:
                continue
            # crude rank: title match wins
            score = 0
            if q in rec.title.lower():
                score += 10
            if q in " ".join(rec.tags).lower():
                score += 5
            if q in rec.product.lower():
                score += 3
            hits.append((score, rec))
        hits.sort(key=lambda t: (-t[0], t[1].title))
        return [r for _, r in hits[:limit]]
