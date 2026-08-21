from scripts import report_capability_gaps


class _FakePath:
    def __init__(self, *, exists: bool, content: str = "") -> None:
        self._exists = exists
        self._content = content

    def exists(self) -> bool:
        return self._exists

    def read_text(self) -> str:
        return self._content


# The totals below are typed by hand on purpose. Their only other possible
# source is docs/capability-gap-matrix.md, which is rendered from these same
# counts -- deriving them from it would compare the code against itself and
# assert nothing. A changed annotation must cost a human one conscious
# re-type here, and that edit is the moment to regenerate the report.
def test_capability_report_counts_and_classification_are_deterministic():
    first = report_capability_gaps.render_report()
    second = report_capability_gaps.render_report()
    rows = report_capability_gaps.collect_rows()

    assert first == second
    assert sum(row["generated"] for row in rows) == 6144
    assert sum(row["registered_generated"] for row in rows) == 6127
    assert sum(row["curated"] for row in rows) == 584
    assert sum(row["registered"] for row in rows) == 6711

    capabilities = sum((row["capabilities"] for row in rows), report_capability_gaps.Counter())
    assert capabilities == {
        "read": 3159,
        "diagnostic": 165,
        "write": 2545,
        "destructive": 842,
    }
    assert "| Executable backend tools | **4,109**" in first
    assert "| Indexed endpoints | **5,960**" in first
    assert "| Dynamic-mode surface | 36 " in first
    assert "24-tool dynamic-mode claim is also stale and contradictory" in first


def test_capability_report_detects_stale_or_missing_output():
    rendered = report_capability_gaps.render_report()

    assert report_capability_gaps.check_report(
        _FakePath(exists=True, content=rendered), rendered
    )
    assert not report_capability_gaps.check_report(
        _FakePath(exists=True, content=rendered + "\nstale"), rendered
    )
    assert not report_capability_gaps.check_report(_FakePath(exists=False), rendered)


def test_the_committed_matrix_file_is_not_stale():
    """Counts nobody wrote to disk are counts nobody reads.

    The test above pins code to counts and the test above that pins the
    checker's behaviour against a fake path. Neither looks at the report
    actually committed, so the cheapest way to green a changed annotation --
    re-type the literal, skip `--write` -- left the published matrix stating
    a number the code contradicts, and every doc pinned to that matrix by
    tests/unit/test_docs_capability_totals_consistency.py agreeing with the
    stale value. `scripts/validate_release.py` runs `--check`, but it runs
    the unit suite first, so the suite has to be able to say this itself.
    """
    rendered = report_capability_gaps.render_report()

    assert report_capability_gaps.check_report(
        report_capability_gaps.REPORT_PATH, rendered
    ), (
        f"{report_capability_gaps.REPORT_PATH.name} no longer matches what the "
        "committed code renders; regenerate it with "
        "`python3 scripts/report_capability_gaps.py --write`"
    )
