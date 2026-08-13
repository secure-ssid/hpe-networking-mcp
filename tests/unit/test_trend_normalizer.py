"""Unit tests for hpe_networking_mcp.mcp_servers.trend_normalizer.

Reference-capability audit note: see the module docstring for provenance --
this is an original implementation closing a capability gap (no reusable
trend/time-series normalization helper previously existed in this repo;
Central's *-trends and Mist's insights tools both pass raw vendor JSON
straight through today).
"""

from __future__ import annotations

import pytest

from hpe_networking_mcp.mcp_servers.trend_normalizer import (
    DEFAULT_MAX_SAMPLES,
    normalize_trend_series,
)


class TestListOfSampleDicts:
    def test_basic_timestamp_value_pairs(self):
        raw = [
            {"timestamp": "2026-08-12T00:00:00Z", "value": 10},
            {"timestamp": "2026-08-12T00:05:00Z", "value": 20},
        ]

        result = normalize_trend_series(raw, metric="cpu")

        assert result["normalized"] is True
        assert result["metric"] == "cpu"
        assert result["sample_count"] == 2
        assert result["truncated"] is False
        assert result["samples"] == [
            {"timestamp": "2026-08-12T00:00:00Z", "value": 10},
            {"timestamp": "2026-08-12T00:05:00Z", "value": 20},
        ]

    def test_alternate_key_names_recognized(self):
        raw = [{"ts": 1000, "utilization": 55}, {"ts": 1005, "utilization": 60}]

        result = normalize_trend_series(raw)

        assert result["normalized"] is True
        assert result["samples"] == [
            {"timestamp": 1000, "value": 55},
            {"timestamp": 1005, "value": 60},
        ]

    def test_bare_numeric_list(self):
        result = normalize_trend_series([1, 2, 3])

        assert result["normalized"] is True
        assert result["samples"] == [
            {"timestamp": None, "value": 1},
            {"timestamp": None, "value": 2},
            {"timestamp": None, "value": 3},
        ]

    def test_empty_list_is_normalized_empty_series(self):
        result = normalize_trend_series([])

        assert result["normalized"] is True
        assert result["samples"] == []
        assert result["sample_count"] == 0


class TestContainerDict:
    @pytest.mark.parametrize("key", ["data", "series", "points", "samples", "values", "datapoints"])
    def test_common_container_keys_recognized(self, key):
        raw = {key: [{"timestamp": 1, "value": 5}, {"timestamp": 2, "value": 6}]}

        result = normalize_trend_series(raw)

        assert result["normalized"] is True
        assert result["sample_count"] == 2


class TestParallelArrays:
    def test_timestamp_and_value_arrays_zip_together(self):
        raw = {
            "timestamp": ["t0", "t1", "t2"],
            "cpu_utilization": [1, 2, 3],
        }

        result = normalize_trend_series(raw, metric="cpu")

        assert result["normalized"] is True
        assert result["samples"] == [
            {"timestamp": "t0", "value": 1},
            {"timestamp": "t1", "value": 2},
            {"timestamp": "t2", "value": 3},
        ]

    def test_mismatched_length_arrays_not_zipped(self):
        raw = {"timestamp": ["t0", "t1"], "cpu_utilization": [1, 2, 3]}

        result = normalize_trend_series(raw)

        assert result["normalized"] is False
        assert result["raw"] == raw


class TestUnrecognizedShapes:
    def test_plain_scalar_dict_is_unnormalized(self):
        raw = {"status": "ok", "count": 3}

        result = normalize_trend_series(raw)

        assert result["normalized"] is False
        assert result["samples"] == []
        assert result["raw"] == raw

    def test_string_is_unnormalized(self):
        result = normalize_trend_series("not a trend payload")

        assert result["normalized"] is False
        assert result["raw"] == "not a trend payload"

    def test_none_is_unnormalized(self):
        result = normalize_trend_series(None)

        assert result["normalized"] is False
        assert result["raw"] is None

    def test_list_of_non_sample_items_is_unnormalized(self):
        result = normalize_trend_series(["not", "a", "sample"])

        assert result["normalized"] is False


class TestBounding:
    def test_default_max_samples_bounds_large_series(self):
        raw = [{"timestamp": i, "value": i} for i in range(DEFAULT_MAX_SAMPLES + 50)]

        result = normalize_trend_series(raw)

        assert result["normalized"] is True
        assert result["sample_count"] == DEFAULT_MAX_SAMPLES + 50
        assert len(result["samples"]) == DEFAULT_MAX_SAMPLES
        assert result["truncated"] is True
        # Most-recent-last order is preserved -- oldest entries are dropped.
        assert result["samples"][0]["timestamp"] == 50
        assert result["samples"][-1]["timestamp"] == DEFAULT_MAX_SAMPLES + 49

    def test_custom_max_samples(self):
        raw = [{"timestamp": i, "value": i} for i in range(10)]

        result = normalize_trend_series(raw, max_samples=3)

        assert result["truncated"] is True
        assert result["samples"] == [
            {"timestamp": 7, "value": 7},
            {"timestamp": 8, "value": 8},
            {"timestamp": 9, "value": 9},
        ]

    def test_invalid_max_samples_raises(self):
        with pytest.raises(ValueError):
            normalize_trend_series([], max_samples=0)


class TestDoesNotMutateInput:
    def test_input_list_unchanged(self):
        raw = [{"timestamp": 1, "value": 2}]
        raw_copy = [dict(item) for item in raw]

        normalize_trend_series(raw)

        assert raw == raw_copy
