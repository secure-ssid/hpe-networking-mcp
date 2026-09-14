from __future__ import annotations

from pathlib import Path

from ingestion import ingest_docs
from ingestion import ingest_hpe_quickspecs as quickspecs


def test_stage_directory_preserves_provenance_and_uses_shared_chunking(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "quickspecs"
    output_dir = tmp_path / "sources" / "hpe_quickspecs"
    input_dir.mkdir()
    pdf_path = input_dir / "cx6300.pdf"
    pdf_path.write_bytes(b"test PDF placeholder")
    pdf_path.with_suffix(".url").write_text(
        "https://www.hpe.com/psnow/doc/a00073540enw.pdf\n",
        encoding="utf-8",
    )
    extracted = (
        "HPE Aruba Networking CX 6300 Switch Series\n\n"
        + ("Stacking and PoE specifications are documented here. " * 40)
    )
    monkeypatch.setattr(quickspecs, "extract_pdf_text", lambda _: extracted)

    staged = quickspecs.stage_directory(input_dir, output_dir)

    assert len(staged) == 1
    staged_path, chunk_count = staged[0]
    assert staged_path == output_dir / "cx6300.md"
    assert chunk_count > 1
    document = staged_path.read_text(encoding="utf-8")
    assert "<!-- source: https://www.hpe.com/psnow/doc/a00073540enw.pdf -->" in document
    assert "manually refreshed operator input" in document

    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", tmp_path / "sources")
    records = ingest_docs.collect_points(output_dir, "hpe-quickspecs")

    assert len(records) == chunk_count
    assert records[0]["source_url"] == "https://www.hpe.com/psnow/doc/a00073540enw.pdf"
    assert records[0]["vendor"] == "aruba"
    assert records[0]["product"] == "aos-cx"
    assert records[0]["platform"] == "switch"
    assert records[0]["model"] == "CX6300"


def test_stage_directory_requires_a_url_sidecar(tmp_path: Path):
    input_dir = tmp_path / "quickspecs"
    input_dir.mkdir()
    (input_dir / "cx6300.pdf").write_bytes(b"test PDF placeholder")

    try:
        quickspecs.stage_directory(input_dir, tmp_path / "output")
    except ValueError as exc:
        assert "missing provenance sidecar" in str(exc)
    else:
        raise AssertionError("missing QuickSpecs URL sidecar did not fail")
