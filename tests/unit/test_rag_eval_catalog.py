from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_run_eval():
    path = Path(__file__).resolve().parents[1] / "eval" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("run_eval", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_source_rank_matches_citation_url_inside_hit_text():
    run_eval = _load_run_eval()
    hits = [{
        "source": "product_datasheets",
        "file_path": "product_datasheets/switch-ex4100-f-ethernet-switch.md",
        "text": (
            "<!-- source: https://www.juniper.net/us/en/products/switches/"
            "ex-series/ex4100-f-ethernet-switch/specs.html -->"
        ),
    }]

    assert run_eval._source_rank(
        hits,
        ["https://www.juniper.net/us/en/products/switches/ex-series/ex4100-f-ethernet-switch/specs.html"],
        k=5,
    ) == 1


def test_rag_eval_catalog_tracks_expanded_vendor_coverage_and_deferred_blockers():
    run_eval = _load_run_eval()
    active = {question["id"]: question for question in run_eval.load_questions()}
    deferred = {question["id"]: question for question in run_eval.load_deferred_questions()}

    assert len(active) == 42
    assert {
        "glp-company-workspace",
        "passpoint-list-key",
        "aoscx-central-min-version-6300-6400",
        "aoscx-9300s-negative-10-13",
        "clearpass-radius-proxy",
        "howto-passpoint",
        "howto-proxy-profile",
        "junos-ex4100f-configure",
        "mist-edge-part-numbers",
        "hardware-ex4100f-specs",
    } <= active.keys()
    assert {
        "mist-wlan-create-api",
        "howto-mist-templates",
        "ex4400-hardware-specs",
        "qfx5120-hardware-specs",
        "srx4600-hardware-specs",
        "list-advisories-juniper-family",
    } <= active.keys()
    assert active["aoscx-9300s-negative-10-13"]["expect_keywords"] == ["10.14.1061"]
    assert active["hardware-ex4100f-specs"]["max_duplicate_ratio"] == 0.0
    assert active["howto-proxy-profile"]["max_latency_ms"] == 4000
    assert active["hardware-ex4100f-specs"]["graded_sources"][0]["gain"] == 3
    assert active["ex4400-hardware-specs"]["graded_sources"][1]["match"] == "product_datasheets"
    assert {
        "deferred-glp-subscriptions-endpoint",
        "deferred-ap635-hardware-specs",
        "deferred-aoscx-10-13-release-notes-delta",
    } <= deferred.keys()
    assert "GLP OpenAPI document" in deferred["deferred-glp-subscriptions-endpoint"]["blocker"]
    assert "AP-635 datasheet" in deferred["deferred-ap635-hardware-specs"]["blocker"]
    assert "release-note chunks" in deferred["deferred-aoscx-10-13-release-notes-delta"]["blocker"]
    assert "deferred-apstra-prose-howto" in deferred
    assert "docs/rag-coverage-matrix.md" in deferred["deferred-apstra-prose-howto"]["blocker"]
