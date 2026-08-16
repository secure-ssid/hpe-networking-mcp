from scripts import report_capability_gaps


class _FakePath:
    def __init__(self, *, exists: bool, content: str = "") -> None:
        self._exists = exists
        self._content = content

    def exists(self) -> bool:
        return self._exists

    def read_text(self) -> str:
        return self._content


def test_capability_report_counts_and_classification_are_deterministic():
    first = report_capability_gaps.render_report()
    second = report_capability_gaps.render_report()
    rows = report_capability_gaps.collect_rows()

    assert first == second
    assert sum(row["generated"] for row in rows) == 6144
    assert sum(row["registered_generated"] for row in rows) == 6127
    assert sum(row["curated"] for row in rows) == 578
    assert sum(row["registered"] for row in rows) == 6705

    capabilities = sum((row["capabilities"] for row in rows), report_capability_gaps.Counter())
    assert capabilities == {
        "read": 3154,
        "diagnostic": 165,
        "write": 2544,
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
