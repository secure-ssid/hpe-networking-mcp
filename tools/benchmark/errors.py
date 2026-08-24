"""Benchmark exception taxonomy."""

from __future__ import annotations


class BenchmarkError(Exception):
    """Base error for the benchmark harness."""


class ManifestError(BenchmarkError):
    """A manifest violates the schema."""


class ManifestNotFoundError(ManifestError):
    """A named manifest does not exist."""


class UnknownSuiteError(BenchmarkError):
    """A manifest references a suite id the harness has no handler for."""


class FixtureError(BenchmarkError):
    """A fixture bundle is missing or invalid."""


class SolverError(BenchmarkError):
    """The solver threw before returning a trace."""


class BaselineError(BenchmarkError):
    """A baseline is missing, malformed, or incompatible with the run."""


class BaselineRegression(BaselineError):
    """A run regressed against the recorded baseline.

    Raised by :func:`hpe_networking_mcp.tools.benchmark.baseline.compare_run`
    when the CI gate should fail. Lists the offending metric keys.
    """

    def __init__(self, regressions: list[str]) -> None:
        self.regressions = regressions
        super().__init__("Baseline regression: " + ", ".join(sorted(regressions)))
