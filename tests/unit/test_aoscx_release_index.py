from __future__ import annotations

import json

from hpe_networking_mcp.pipeline.clients import aoscx_release_index


def _write_history(tmp_path):
    source = tmp_path / "feature_navigator"
    source.mkdir()
    payload = {
        "schema_version": 1,
        "product_id": 52,
        "product_name": "CX 6100",
        "product_type": "Fixed",
        "minimum_supported_release": "10.06.0001",
        "releases": ["10.13.1000", "10.16.1006"],
        "source_url": "https://feature-navigator.example/wired?productId=52",
        "features": [
            {
                "feature_type": "VSF",
                "feature_name": "VSF stacking",
                "feature_publication_release": "10.16",
                "support": {
                    "10.13.1000": "Not documented",
                    "10.16.1006": "Yes",
                },
            },
            {
                "feature_type": "Layer 2",
                "feature_name": "VLANs",
                "feature_publication_release": None,
                "support": {"10.13.1000": "Yes", "10.16.1006": "Yes"},
            },
        ],
    }
    (source / "cx-cx-6100-history.json").write_text(json.dumps(payload))


def test_build_and_compare_feature_history(tmp_path, monkeypatch):
    _write_history(tmp_path)
    db_path = tmp_path / "specs.sqlite"
    counts = aoscx_release_index.build_feature_index(tmp_path, db_path)
    assert counts == {"platforms": 1, "releases": 2, "feature_support": 4}

    monkeypatch.setattr(aoscx_release_index, "_release_note_rows", lambda platform: [])
    result = aoscx_release_index.compare(
        "CX 6100",
        "10.13",
        "10.16",
        sections=["features"],
        db_path=db_path,
    )

    assert result["feature_versions"] == {
        "from": "10.13.1000",
        "to": "10.16.1006",
    }
    assert result["feature_change_count"] == 1
    assert result["results"][0]["feature_name"] == "VSF stacking"
    assert result["results"][0]["change"] == "added"
    assert result["results"][0]["file_path"].endswith("-history.json")


def test_compare_release_notes_uses_exact_exclusive_range(tmp_path, monkeypatch):
    _write_history(tmp_path)
    db_path = tmp_path / "specs.sqlite"
    aoscx_release_index.build_feature_index(tmp_path, db_path)
    rows = [
        {
            "file_path": "aoscx_release_notes/6100/10-13-1180/enhancements.html",
            "chunk_index": 0,
            "text": "Baseline enhancement",
        },
        {
            "file_path": "aoscx_release_notes/6100/10-15-0005/enhancements.html",
            "chunk_index": 0,
            "text": "RIP and RIPng support",
        },
        {
            "file_path": "aoscx_release_notes/6100/10-16-1051/enhancements.html",
            "chunk_index": 0,
            "text": "There are no enhancements in this release.",
        },
    ]
    monkeypatch.setattr(aoscx_release_index, "_release_note_rows", lambda platform: rows)

    result = aoscx_release_index.compare(
        "6100",
        "10.13",
        "10.16",
        sections=["enhancements"],
        db_path=db_path,
    )

    assert result["release_note_versions"] == {
        "from": "10.13.1180",
        "to": "10.16.1051",
    }
    assert result["range_semantics"] == "from exclusive, to inclusive"
    assert [item["release"] for item in result["results"]] == ["10.15.0005"]


def test_compare_rejects_unknown_section(tmp_path):
    _write_history(tmp_path)
    db_path = tmp_path / "specs.sqlite"
    aoscx_release_index.build_feature_index(tmp_path, db_path)

    try:
        aoscx_release_index.compare(
            "6100", "10.13", "10.16", sections=["security"], db_path=db_path
        )
    except ValueError as exc:
        assert "sections must contain only" in str(exc)
    else:
        raise AssertionError("expected ValueError")
