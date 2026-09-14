#!/usr/bin/env python3
"""Stage manually downloaded Aruba/HPE QuickSpecs PDFs for RAG ingestion.

QuickSpecs are intentionally not fetched by the source refresh scheduler:
HPE and Aruba product-document endpoints block automated retrieval from this
environment. The operator downloads PDFs into ``ingestion/quickspecs/`` and
places a same-stem ``.url`` sidecar beside each PDF. This script extracts the
PDF text, adds the source URL as provenance, and writes deterministic Markdown
under ``ingestion/sources/hpe_quickspecs/``. The existing ``ingest_docs.py``
pipeline then applies ``chunk_text_with_breadcrumbs`` and indexes the named
source.

Usage:
    uv run python ingestion/ingest_hpe_quickspecs.py
    uv run python ingestion/ingest_hpe_quickspecs.py --ingest
    uv run python ingestion/ingest_hpe_quickspecs.py --ingest --dry-run
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT / "ingestion" / "quickspecs"
DEFAULT_OUTPUT_DIR = ROOT / "ingestion" / "sources" / "hpe_quickspecs"
SOURCE = "hpe_quickspecs"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from ingestion.chunking import chunk_text_with_breadcrumbs  # noqa: E402


def extract_pdf_text(path: Path) -> str:
    """Extract page text using the ingestion extra's pypdf dependency."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "Reading QuickSpecs PDFs requires pypdf; run `uv sync --extra ingestion`."
        ) from exc

    reader = PdfReader(str(path))
    return "\n\n".join(
        page_text.strip()
        for page_text in (page.extract_text() or "" for page in reader.pages)
        if page_text.strip()
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "quickspec"


def _output_path(pdf_path: Path, input_dir: Path, output_dir: Path) -> Path:
    relative = pdf_path.relative_to(input_dir)
    stem_parts = [_slug(part) for part in relative.with_suffix("").parts]
    return output_dir.joinpath(*stem_parts).with_suffix(".md")


def _source_url(pdf_path: Path) -> str:
    sidecar = pdf_path.with_suffix(".url")
    if not sidecar.is_file():
        raise ValueError(f"{pdf_path}: missing provenance sidecar {sidecar.name}")
    value = sidecar.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{sidecar}: URL is empty")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{sidecar}: expected exactly one URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{sidecar}: expected an https URL")
    return value


def _title(pdf_path: Path) -> str:
    return re.sub(r"[_-]+", " ", pdf_path.stem).strip() or "QuickSpecs"


def stage_pdf(pdf_path: Path, input_dir: Path, output_dir: Path) -> tuple[Path, int]:
    """Extract one PDF and return its staged path and chunk count."""
    source_url = _source_url(pdf_path)
    extracted = extract_pdf_text(pdf_path).strip()
    if not extracted:
        raise ValueError(f"{pdf_path}: PDF contains no extractable text")

    document = (
        f"<!-- source: {source_url} -->\n"
        "<!-- source mode: manually refreshed operator input -->\n\n"
        f"# {_title(pdf_path)} QuickSpecs\n\n"
        f"{extracted}\n"
    )
    chunks = chunk_text_with_breadcrumbs(document)
    if not chunks:
        raise ValueError(f"{pdf_path}: extracted text produced no chunks")

    output_path = _output_path(pdf_path, input_dir, output_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not output_path.exists() or output_path.read_text(encoding="utf-8") != document:
        output_path.write_text(document, encoding="utf-8")
    return output_path, len(chunks)


def stage_directory(input_dir: Path, output_dir: Path) -> list[tuple[Path, int]]:
    """Stage every PDF in deterministic order."""
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"QuickSpecs input directory not found: {input_dir}")

    pdfs = sorted(path for path in input_dir.rglob("*.pdf") if path.is_file())
    if not pdfs:
        raise SystemExit(f"No QuickSpecs PDFs found under {input_dir}")

    staged = []
    for pdf_path in pdfs:
        staged.append(stage_pdf(pdf_path, input_dir, output_dir))
    return staged


def run_ingest(*, backend: str, dry_run: bool) -> None:
    command = [
        sys.executable,
        str(ROOT / "ingestion" / "ingest_docs.py"),
        "--backend",
        backend,
    ]
    if backend == "lancedb":
        # LanceDB preserves known sources missing from a partial local corpus
        # during incremental ingest; selecting the manual source explicitly is
        # supported by the wrapper, not by ingest_docs.py's full-corpus CLI.
        command.append("--incremental")
    else:
        command.extend(["--source", SOURCE])
    if dry_run:
        command.append("--dry-run")
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--ingest", action="store_true", help="run ingest_docs.py after staging")
    parser.add_argument("--dry-run", action="store_true", help="count chunks without indexing")
    parser.add_argument("--backend", choices=("lancedb", "redis"), default="lancedb")
    args = parser.parse_args()

    staged = stage_directory(args.input_dir, args.output_dir)
    for path, chunk_count in staged:
        try:
            display_path = path.relative_to(ROOT)
        except ValueError:
            display_path = path
        print(f"  staged {display_path} ({chunk_count} chunks)")
    print(f"Staged {len(staged)} QuickSpecs PDF(s) as source={SOURCE}")

    if args.dry_run and not args.ingest:
        raise SystemExit("--dry-run requires --ingest")
    if args.ingest:
        run_ingest(backend=args.backend, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
