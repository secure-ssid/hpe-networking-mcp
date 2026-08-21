from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "ingestion" / "scrape_feature_navigator.py"
SPEC = importlib.util.spec_from_file_location("scrape_feature_navigator", SCRIPT)
scraper = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(scraper)


def test_format_switch_history_preserves_release_matrix():
    product = {
        "productID": 52,
        "productName": "CX 6100",
        "productType": "Fixed",
        "minSupportedRelease": "10.06.0001",
    }
    rows = [
        {
            "FeatureType": "VSF",
            "FeatureName": "VSF stacking",
            "FeaturePubRel": "10.16",
            "10.13.1000": "",
            "10.16.1006": "Yes",
        }
    ]

    payload = scraper.format_switch_history(
        product, ["10.13.1000", "10.16.1006"], rows
    )

    assert payload["product_id"] == 52
    assert payload["releases"] == ["10.13.1000", "10.16.1006"]
    assert payload["features"][0]["support"] == {
        "10.13.1000": "Not documented",
        "10.16.1006": "Yes",
    }
