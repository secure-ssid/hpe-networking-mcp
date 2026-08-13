"""Unit tests for scripts/check_mist_openapi_drift.py.

No network: every GitHub call is monkeypatched. Covers the reviewed-pin
record (including its cross-check against ingestion/fetch_mist_openapi.py),
the honest reporting of an unverified/frozen pin as ``stale_pin`` rather
than ``fresh``, content-digest handling for both the pinned ref and the
on-disk spec, and the classification of transport vs parse vs missing
failures into distinct exit codes.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy
from scripts import check_mist_openapi_drift as mist

PIN_REF = "f374cffdd5a275c7954645a306fcab7f1227e7a3"


def _pin(**overrides):
    pin = {
        "source": "mist_openapi",
        "repository": mist.REPOSITORY,
        "path": mist.DEFAULT_PATH,
        "reviewed_ref": mist.DEFAULT_REF,
        "reviewed_sha256": mist.DEFAULT_SHA256,
        "reviewed_at": None,
        "review_status": "reviewed",
        "refresh_policy": "frozen",
    }
    pin.update(overrides)
    return pin


def _write_pin(tmp_path, **overrides):
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(_pin(**overrides)), encoding="utf-8")
    return path


class TestCommittedPinRecord:
    def test_repo_pin_record_loads_and_matches_the_generator_module(self):
        pin = mist.load_pin()

        assert pin["repository"] == mist.REPOSITORY
        assert pin["reviewed_ref"] == mist.DEFAULT_REF == PIN_REF
        assert pin["reviewed_sha256"] == mist.DEFAULT_SHA256

    def test_committed_pin_is_frozen_and_flagged_for_review(self):
        """External source refresh is disabled for this change, so the pin was
        deliberately NOT advanced -- it must say so rather than look fresh."""
        pin = mist.load_pin()

        assert pin["review_status"] == "review_needed"
        assert pin["refresh_policy"] == "frozen"

    def test_pin_disagreeing_with_module_is_a_parser_error_not_drift(self, tmp_path):
        path = _write_pin(tmp_path, reviewed_ref="0" * 40)

        findings = mist.evaluate(pin_path=path, spec_path=tmp_path / "absent.json", offline=True)

        assert findings[0].result_class == taxonomy.PARSER_ERROR
        assert "disagrees with" in findings[0].detail

    def test_missing_pin_file_is_a_parser_error(self, tmp_path):
        findings = mist.evaluate(
            pin_path=tmp_path / "nope.json", spec_path=tmp_path / "absent.json", offline=True
        )
        assert findings[0].result_class == taxonomy.PARSER_ERROR

    def test_unknown_review_status_rejected(self, tmp_path):
        path = _write_pin(tmp_path, review_status="probably-fine")
        with pytest.raises(mist.PinError):
            mist.load_pin(path)


class TestReviewedPinEvaluation:
    def test_review_needed_is_stale_pin_even_when_upstream_matches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mist, "fetch_latest_ref", lambda pin, timeout=0: pin["reviewed_ref"])
        finding = mist.evaluate_reviewed_pin(_pin(review_status="review_needed"), offline=False)

        assert finding.result_class == taxonomy.STALE_PIN
        assert "review_needed" in finding.detail

    def test_offline_never_reports_fresh(self):
        finding = mist.evaluate_reviewed_pin(_pin(), offline=True)

        assert finding.result_class == taxonomy.STALE_PIN
        assert finding.result_class != taxonomy.FRESH
        assert "could not be re-verified" in finding.detail

    def test_reviewed_pin_matching_upstream_is_fresh(self, monkeypatch):
        monkeypatch.setattr(mist, "fetch_latest_ref", lambda pin, timeout=0: pin["reviewed_ref"])

        finding = mist.evaluate_reviewed_pin(_pin(), offline=False)

        assert finding.result_class == taxonomy.FRESH

    def test_upstream_advanced_is_stale_pin_not_content_drift(self, monkeypatch):
        monkeypatch.setattr(mist, "fetch_latest_ref", lambda pin, timeout=0: "b" * 40)

        finding = mist.evaluate_reviewed_pin(_pin(), offline=False)

        assert finding.result_class == taxonomy.STALE_PIN
        assert finding.evidence["latest_ref"] == "b" * 40

    @pytest.mark.parametrize(
        "exc,expected",
        [
            (mist.FetchError("rate limited"), taxonomy.UNAVAILABLE),
            (mist.RemoteParseError("not json"), taxonomy.PARSER_ERROR),
            (mist.RemoteMissingError("HTTP 404"), taxonomy.SOURCE_REMOVED),
        ],
    )
    def test_remote_failures_are_classified_apart(self, monkeypatch, exc, expected):
        def _boom(pin, timeout=0):
            raise exc

        monkeypatch.setattr(mist, "fetch_latest_ref", _boom)

        finding = mist.evaluate_reviewed_pin(_pin(), offline=False)

        assert finding.result_class == expected
        assert finding.result_class != taxonomy.CONTENT_DRIFT


class TestDigestHandling:
    def test_local_spec_matching_reviewed_digest_is_fresh(self, tmp_path):
        body = b'{"openapi": "3.0.0"}'
        spec = tmp_path / "mist-openapi.json"
        spec.write_bytes(body)
        pin = _pin(reviewed_sha256=hashlib.sha256(body).hexdigest())

        assert mist.evaluate_local_spec(pin, spec_path=spec).result_class == taxonomy.FRESH

    def test_local_spec_divergence_is_content_drift(self, tmp_path):
        spec = tmp_path / "mist-openapi.json"
        spec.write_bytes(b"tampered")

        finding = mist.evaluate_local_spec(_pin(), spec_path=spec)

        assert finding.result_class == taxonomy.CONTENT_DRIFT
        assert finding.evidence["observed_sha256"] == hashlib.sha256(b"tampered").hexdigest()

    def test_absent_local_spec_is_not_checked(self, tmp_path):
        finding = mist.evaluate_local_spec(_pin(), spec_path=tmp_path / "absent.json")
        assert finding.result_class == taxonomy.NOT_CHECKED

    def test_pinned_ref_content_rewrite_is_content_drift(self, monkeypatch):
        monkeypatch.setattr(mist, "fetch_pinned_digest", lambda pin, timeout=0: "c" * 64)

        finding = mist.evaluate_pinned_ref_content(_pin())

        assert finding.result_class == taxonomy.CONTENT_DRIFT
        assert "no longer reproducible" in finding.detail

    def test_pinned_ref_content_match_is_fresh(self, monkeypatch):
        monkeypatch.setattr(
            mist, "fetch_pinned_digest", lambda pin, timeout=0: pin["reviewed_sha256"]
        )
        assert mist.evaluate_pinned_ref_content(_pin()).result_class == taxonomy.FRESH


class TestMain:
    def test_offline_run_exits_with_the_stale_pin_code(self, tmp_path, capsys):
        exit_code = mist.main(
            ["--offline", "--spec-path", str(tmp_path / "absent.json"), "--no-artifact"]
        )

        assert exit_code == taxonomy.EXIT_STALE_PIN
        out = capsys.readouterr().out
        assert "stale_pin" in out
        # No finding line may claim freshness in an offline run.
        finding_lines = [line for line in out.splitlines() if line.startswith("  ")]
        assert not any(line.strip().startswith("fresh ") for line in finding_lines)

    def test_artifact_records_classes_and_refresh_state(self, tmp_path):
        artifact = tmp_path / "mist.json"

        mist.main(
            [
                "--offline",
                "--spec-path",
                str(tmp_path / "absent.json"),
                "--json-artifact",
                str(artifact),
            ]
        )

        report = json.loads(artifact.read_text())
        assert report["check"] == "mist_openapi_drift"
        assert report["refresh_sources"] is False
        assert report["counts"][taxonomy.STALE_PIN] == 1
        assert report["content_drift_detected"] is False

    def test_legacy_exit_code_mode(self, tmp_path):
        assert (
            mist.main(
                [
                    "--offline",
                    "--spec-path",
                    str(tmp_path / "absent.json"),
                    "--no-artifact",
                    "--exit-code-mode",
                    "legacy",
                ]
            )
            == 1
        )

    def test_main_never_touches_the_network_when_offline(self, tmp_path, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("offline mode must not open a connection")

        monkeypatch.setattr(mist.urllib.request, "urlopen", _boom)

        mist.main(["--offline", "--spec-path", str(tmp_path / "absent.json"), "--no-artifact"])
