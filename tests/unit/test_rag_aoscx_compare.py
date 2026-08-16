from __future__ import annotations

from hpe_networking_mcp.mcp_servers import rag


def test_compare_aoscx_releases_forwards_bounded_request(monkeypatch):
    captured = {}

    def fake_compare(**kwargs):
        captured.update(kwargs)
        return {"count": 1, "results": [{"feature_name": "VSF stacking"}]}

    monkeypatch.setattr(rag.aoscx_release_index, "compare", fake_compare)

    result = rag.compare_aoscx_releases(
        "6100",
        "10.13",
        "10.16",
        sections=["features"],
        limit=25,
    )

    assert result["count"] == 1
    assert captured == {
        "platform": "6100",
        "from_version": "10.13",
        "to_version": "10.16",
        "sections": ["features"],
        "limit": 25,
    }


def test_compare_aoscx_releases_returns_validation_error(monkeypatch):
    def invalid(**kwargs):
        raise ValueError("bad range")

    monkeypatch.setattr(rag.aoscx_release_index, "compare", invalid)

    result = rag.compare_aoscx_releases("6100", "10.16", "10.13")
    assert result["errors"] == ["bad range"]
    assert result["results"] == []
