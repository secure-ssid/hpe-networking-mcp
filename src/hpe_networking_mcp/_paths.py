"""Repository-root resolution for artifacts that live *outside* the package.

Several modules read or write repo-level, non-package paths -- ``uv.lock``,
``pyproject.toml``, ``dist/``, ``outputs/``, ``state/``. Before the src-layout
move those modules sat one directory below the repository root, so
``Path(__file__).resolve().parents[1]`` was the root. Under ``src/`` layout the
same expression resolves to ``src/hpe_networking_mcp`` -- two levels too
shallow -- which is why, for example, SBOM generation started failing with
``pyproject file not found: .../src/hpe_networking_mcp/pyproject.toml``.

:func:`repo_root` centralizes the fix so the depth constant lives in exactly
one place and cannot drift again. It also degrades sensibly for an *installed*
wheel, where there is no repository around the package at all: the caller's
working directory is then the only meaningful root.
"""

from __future__ import annotations

from pathlib import Path

#: ``src/hpe_networking_mcp``
PACKAGE_ROOT = Path(__file__).resolve().parent
#: Repository root when running from a ``src/`` layout source checkout.
CHECKOUT_ROOT = PACKAGE_ROOT.parents[1]


def repo_root() -> Path:
    """Return the repository root for a source checkout, else the CWD.

    Returns:
        ``CHECKOUT_ROOT`` when it actually looks like this project's checkout
        (it contains ``pyproject.toml``); otherwise ``Path.cwd()``, which is
        where an installed-wheel user's ``config/``, ``data/``, ``outputs/``
        and ``state/`` directories live.
    """
    if (CHECKOUT_ROOT / "pyproject.toml").is_file():
        return CHECKOUT_ROOT
    return Path.cwd()
