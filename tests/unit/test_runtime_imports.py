"""The server must import, start and serve on a base install.

`pyproject.toml` splits the document pipeline (scraping, office/PDF parsing,
chunking, embedding, the LanceDB store), the legacy Redis vector backend and
the Textual TUI into optional extras. That split is only real if the package
keeps working without them, so these tests run the entry points a
``pip install hpe-networking-mcp`` user actually runs -- the console-script
targets and an in-process MCP ``list_tools`` -- with the optional packages
made unimportable.

Absence is simulated with ``sys.modules[name] = None``, which CPython's import
machinery turns into ``ImportError: import of X halted; None in sys.modules``
for both ``import X`` and ``from X import Y``, and into ``ModuleNotFoundError``
(an ``ImportError`` subclass) for ``from X.sub import Y``.
``test_absence_simulation_actually_raises`` pins that behaviour so these tests
cannot silently degrade into no-ops on a future Python.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

try:  # Python >= 3.11
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as _toml  # type: ignore[import-not-found, no-redef]

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Packages that must not be reachable from a base install. `pyarrow` and
#: `onnxruntime` are here as transitive passengers of `lancedb`/`fastembed`
#: rather than as declared dependencies -- they are the two largest things the
#: split removes, so a regression that re-declares their parents is worth
#: catching by name.
HEAVY = ["playwright", "lancedb", "pyarrow", "onnxruntime", "fastembed"]

#: Every module moved out of `[project].dependencies`, by the extra that now
#: carries it. Keyed by import name, not distribution name.
MOVED_TO_EXTRA = {
    "playwright": "ingestion",
    "bs4": "ingestion",
    "langchain_text_splitters": "ingestion",
    "pypdf": "ingestion",
    "pptx": "ingestion",
    "docx": "ingestion",
    "lancedb": "ingestion",
    "fastembed": "ingestion",
    "redis": "redis",
    "numpy": "redis",
    "textual": "tui",
}

#: Distribution names as they appear in `[project].dependencies`.
_MOVED_DISTRIBUTIONS = {
    "playwright",
    "beautifulsoup4",
    "langchain-text-splitters",
    "pypdf",
    "python-pptx",
    "python-docx",
    "lancedb",
    "fastembed",
    "redis",
    "numpy",
    "textual",
}


def _blinded(*names: str) -> str:
    """Python source that makes ``names`` unimportable for the child process."""
    return "import sys;" + "".join(f"sys.modules[{n!r}] = None;" for n in names)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=180,
    )


def _pyproject() -> dict:
    return _toml.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirement_names(requirements: list[str]) -> set[str]:
    names = set()
    for raw in requirements:
        head = raw.split(";")[0].strip()
        for sep in ("[", ">", "<", "=", "!", "~", " "):
            head = head.split(sep)[0]
        names.add(head.strip().lower())
    return names


def test_absence_simulation_actually_raises() -> None:
    """The blinding trick must really induce ImportError, top-level and sub.

    Asserted by catching, not by matching a class name: CPython raises the
    ``ModuleNotFoundError`` subclass for both forms, so a name match would be
    testing the spelling of an exception rather than the property the other
    tests here depend on -- that every guard written as ``except ImportError``
    sees it.
    """
    probe = (
        "\ntry:\n"
        "    {stmt}\n"
        "except ImportError:\n"
        "    print('raised')\n"
        "else:\n"
        "    print('NOT RAISED')\n"
    )
    for stmt in (
        "import numpy",
        "from numpy import array",
        "from websockets.asyncio.client import connect",
    ):
        proc = _run(_blinded("numpy", "websockets") + probe.format(stmt=stmt))
        assert proc.stdout.strip() == "raised", f"{stmt!r}: {proc.stdout}{proc.stderr}"


@pytest.mark.slow
def test_shared_imports_without_optional_packages() -> None:
    """The settings/write-gate core imports with every extra absent."""
    proc = _run(
        _blinded(*MOVED_TO_EXTRA)
        + "import hpe_networking_mcp.mcp_servers.shared as s;"
        "print(s.access_profile())"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()


@pytest.mark.slow
@pytest.mark.parametrize(
    "module",
    [
        "hpe_networking_mcp.mcp_servers.tool_router",
        "hpe_networking_mcp.mcp_servers.rag",
        "hpe_networking_mcp.cli.mcp_cli",
        "hpe_networking_mcp.cli.doctor",
        "hpe_networking_mcp.cli_client.personal_ingest",
    ],
)
def test_entry_point_modules_import_without_optional_packages(module: str) -> None:
    """Every console-script target imports with the extras absent.

    ``mcp_servers.shared`` importing proves very little on its own -- it is a
    settings module. These are the modules `pyproject`'s `[project.scripts]`
    entries resolve through, plus the two that reach the moved packages
    (`rag` for LanceDB/Redis, `personal_ingest` for the parsers).
    """
    proc = _run(_blinded(*MOVED_TO_EXTRA) + f"import {module}; print('ok')")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")


@pytest.mark.slow
def test_router_lists_tools_without_optional_packages() -> None:
    """The router serves its catalog with every optional package absent.

    Importing is not serving. This drives the real MCP entry point -- the
    backend registration walk plus ``list_tools`` -- because a lean install
    that imports but cannot enumerate a tool is not a lean install worth
    having.
    """
    code = _blinded(*MOVED_TO_EXTRA) + (
        "import asyncio;"
        "from hpe_networking_mcp.mcp_servers import tool_router;"
        "tools = asyncio.run(tool_router.mcp.list_tools());"
        "print('TOOLS', len(tools), sorted(t.name for t in tools))"
    )
    proc = _run(code)
    assert proc.returncode == 0, proc.stderr
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("TOOLS ")]
    assert marker, proc.stdout
    count = int(marker[0].split()[1])
    assert count > 0, f"router exposed no tools: {marker[0]}"
    assert "find_tool" in marker[0], marker[0]


@pytest.mark.slow
def test_rag_search_degrades_actionably_without_lancedb() -> None:
    """A missing extra is reported with its install command, never as empty.

    An empty list from ``search_docs`` is a real answer: the corpus was
    consulted and holds nothing. An uninstalled extra consulted nothing, and a
    model handed ``[]`` tells the operator no such documentation exists.
    """
    code = _blinded(*MOVED_TO_EXTRA) + (
        "from hpe_networking_mcp.mcp_servers import rag;"
        "hits = rag.search_docs('aruba central ssid');"
        "print('HITS', hits)"
    )
    proc = _run(code)
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("HITS "))
    assert line != "HITS []", "an absent extra was rendered as an empty result"
    assert "'degraded': True" in line, line
    assert "pip install 'hpe-networking-mcp[ingestion]'" in line, line


def test_declared_runtime_deps_exclude_heavy_modules() -> None:
    names = _requirement_names(_pyproject()["project"]["dependencies"])
    still_there = names & set(HEAVY)
    assert not still_there, f"heavy deps still in the runtime set: {sorted(still_there)}"


def test_moved_distributions_left_the_runtime_set() -> None:
    names = _requirement_names(_pyproject()["project"]["dependencies"])
    still_there = names & _MOVED_DISTRIBUTIONS
    assert not still_there, f"moved deps still declared as runtime: {sorted(still_there)}"


def test_every_moved_distribution_is_reachable_through_an_extra() -> None:
    """Nothing was dropped on the floor: each move landed in a declared extra."""
    extras = _pyproject()["project"]["optional-dependencies"]
    declared: set[str] = set()
    for requirements in extras.values():
        declared |= _requirement_names(requirements)
    missing = _MOVED_DISTRIBUTIONS - declared
    assert not missing, f"moved out of runtime but into no extra: {sorted(missing)}"


def test_websockets_stayed_in_the_runtime_set() -> None:
    """``websockets`` is server runtime, not pipeline weight.

    ``mcp_servers/central_streaming.py`` and ``mcp_servers/mist.py`` both
    import it at module scope and both are registered router backends, so
    moving it would break tool registration, not just a side feature. Pinned
    so a future weight-trimming pass has to argue with this test first.
    """
    names = _requirement_names(_pyproject()["project"]["dependencies"])
    assert "websockets" in names


def test_remedy_names_the_extra_that_is_actually_missing() -> None:
    from hpe_networking_mcp import optional_deps

    for extra in ("ingestion", "redis", "tui"):
        remedy = optional_deps.install_remedy(extra)
        assert f"hpe-networking-mcp[{extra}]" in remedy, remedy


def test_remedy_override_substitutes_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that is not pip-installable can state its own remedy.

    The override is a template, not a fixed sentence. A container that
    hard-coded "ingestion" into its message would misinform the operator whose
    missing extra is `redis` or `tui`, which is the whole failure mode this
    module exists to prevent.
    """
    from hpe_networking_mcp import optional_deps

    monkeypatch.setenv(
        optional_deps.REMEDY_OVERRIDE_ENV,
        "rebuild with `docker build --build-arg INSTALL_EXTRAS={extra}`",
    )
    assert optional_deps.install_remedy("redis").endswith("INSTALL_EXTRAS=redis`")
    assert optional_deps.missing("X", module="redis", extra="redis").remedy.endswith(
        "INSTALL_EXTRAS=redis`"
    )

    # Whitespace-only is not an override; falling back beats emitting a blank
    # hint that names no fix at all.
    monkeypatch.setenv(optional_deps.REMEDY_OVERRIDE_ENV, "   ")
    assert "pip install" in optional_deps.install_remedy("tui")


def test_require_reports_the_capability_and_the_command() -> None:
    from hpe_networking_mcp import optional_deps

    with pytest.raises(optional_deps.MissingOptionalDependency) as caught:
        optional_deps.require(
            "definitely_not_a_real_module_xyz", extra="ingestion", capability="Testing"
        )
    message = str(caught.value)
    assert "Testing" in message
    assert "definitely_not_a_real_module_xyz" in message
    assert "pip install 'hpe-networking-mcp[ingestion]'" in message
    assert isinstance(caught.value, ImportError)


@pytest.mark.slow
def test_find_tool_hint_names_the_install_not_a_rebuild() -> None:
    """A missing package cannot be fixed by rebuilding the index.

    ``find_tool``'s semantic pass needs the embedder and the vector store,
    both in the `ingestion` extra. Its standing hint tells the caller to
    rebuild the tool index -- which needs the same absent packages, so it
    sends them in a circle. The hint must carry the install command instead,
    and must stay present either way.
    """
    code = _blinded(*MOVED_TO_EXTRA) + (
        "from hpe_networking_mcp.mcp_servers import tool_router;"
        # A query with no tool-name token overlap, so the keyword pass yields
        # nothing and the semantic failure is what surfaces.
        "print('OUT', tool_router.find_tool('zzz nonsense qqq'))"
    )
    proc = _run(code)
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("OUT "))
    assert "'hint'" in line, line
    assert "pip install 'hpe-networking-mcp[ingestion]'" in line, line
    assert "scripts/ingest_tools.py" not in line, line


#: Import names carried by an extra *other* than `ingestion`, derived from the
#: same table the rest of this module uses so a package moving between extras
#: moves this gate with it.
_OUTSIDE_INGESTION = tuple(
    sorted(mod for mod, extra in MOVED_TO_EXTRA.items() if extra != "ingestion")
)


def _ingest_docs(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the documented corpus build with every non-`ingestion` extra absent."""
    return _run(
        _blinded(*_OUTSIDE_INGESTION)
        + "import runpy, sys;"
        + f"sys.argv = ['ingest_docs.py', *{argv!r}];"
        + "runpy.run_path('ingestion/ingest_docs.py', run_name='__main__')"
    )


@pytest.mark.slow
def test_documented_corpus_build_runs_on_the_ingestion_extra_alone() -> None:
    """The `ingestion` extra alone must be enough for the documented command.

    README, docs/README, docs/getting-started and docs/production-deployment
    all point an operator at
    `uv run --extra ingestion python ingestion/ingest_docs.py`.
    `ingest_docs` used to import
    `pipeline.clients.redis_client` -- and through it `redis`, which lives in
    a *different* extra -- at module scope, so that documented command died
    with a bare `ModuleNotFoundError` before argparse ran.

    Blinding every non-`ingestion` extra rather than `redis` alone makes this
    a gate on the whole class: any future module-scope import from `redis`,
    `tui` or a later extra fails here.
    """
    proc = _ingest_docs(["--dry-run"])

    assert proc.returncode == 0, proc.stderr
    assert "Dry run" in proc.stdout, proc.stdout


@pytest.mark.slow
def test_ingest_docs_redis_backend_names_the_extra_it_needs() -> None:
    """The opt-in redis path fails with its install command, not a traceback.

    `--backend redis` genuinely needs the `redis` extra. Deferring the import
    is only half the fix: the refusal has to name what to install, the same
    way an unbuilt index names how to build it.
    """
    proc = _ingest_docs(["--backend", "redis"])

    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "pip install 'hpe-networking-mcp[redis]'" in proc.stderr, proc.stderr
