"""Actionable failures for packages that live in an optional extra.

The base install carries what serving MCP tools needs and nothing else. The
document pipeline (scraping, office/PDF parsing, chunking, embedding, the
LanceDB vector store) and the legacy Redis vector backend are extras, so on a
perfectly healthy install their packages are simply absent.

That absence has to surface the same way ``specs_index.MISSING_INDEX_REMEDY``
surfaces a missing spec index: as a named, fixable condition. Two failure
modes are ruled out on purpose.

*A bare ``ModuleNotFoundError``* tells an operator a package name and nothing
about which install they are one command away from. Every raise here names
the capability that was attempted, the module that was missing, and the exact
``pip install`` that restores it.

*An empty result* is strictly worse, and is the reason this module exists
rather than a scattering of ``except ImportError: return []``. ``search_docs``
returning ``[]`` is a real answer -- the corpus was consulted and holds
nothing. An uninstalled extra consulted nothing. A model handed ``[]`` reports
"there is no documentation for that" to an operator, and in a
network-automation tool that fabrication is worse than an error. So callers at
the MCP tool boundary catch :class:`MissingOptionalDependency` and render it
through the degraded shape (``error`` + ``degraded`` + ``hint``), never as a
successful empty one.

The install command is written once, here, so it cannot drift from the extras
actually declared in ``pyproject.toml``.
"""

from __future__ import annotations

import importlib
import os
from types import ModuleType

#: Distribution name on PyPI; the thing an operator types after ``pip install``.
DISTRIBUTION = "hpe-networking-mcp"

#: Lets a deployment that is not pip-installable state its own remedy. The
#: container image is the case in point: ``/app/.venv`` is uv-managed under
#: ``UV_NO_SYNC=1``, so telling that operator to run ``pip install`` sends
#: them to a command that appears to work and vanishes with the container.
#: The value is a template, not a fixed sentence -- ``{extra}`` is substituted
#: with the extra actually missing, so one setting stays correct for
#: ``ingestion``, ``redis`` and ``tui`` alike.
REMEDY_OVERRIDE_ENV = "HPE_MCP_INSTALL_REMEDY"

#: The placeholder callers may put in ``REMEDY_OVERRIDE_ENV``.
REMEDY_EXTRA_PLACEHOLDER = "{extra}"


def install_remedy(extra: str) -> str:
    """The remedy alone, carrying no diagnostic context.

    Callers that surface a fix on its own -- a degraded ``hint`` field -- read
    this instead of slicing a longer message apart.

    Substitution is a literal ``str.replace``, not ``str.format``: the value
    is operator-supplied prose that will contain backticks, quotes and often
    braces, and ``format`` would raise on any of them rather than degrade.
    """
    override = os.environ.get(REMEDY_OVERRIDE_ENV, "").strip()
    if override:
        return override.replace(REMEDY_EXTRA_PLACEHOLDER, extra)
    return f"install it with `pip install '{DISTRIBUTION}[{extra}]'`"


class MissingOptionalDependency(ImportError):
    """A capability was invoked whose extra is not installed.

    Subclasses :class:`ImportError` so existing ``except ImportError`` guards
    keep working, while ``extra``/``remedy`` let a caller render the fix
    without re-deriving it.
    """

    def __init__(self, message: str, *, extra: str) -> None:
        super().__init__(message)
        self.extra = extra
        self.remedy = install_remedy(extra)


def missing(capability: str, *, module: str, extra: str) -> MissingOptionalDependency:
    """Build the error for a capability whose extra is absent."""
    return MissingOptionalDependency(
        f"{capability} needs the optional `{module}` package, which is not installed "
        f"— {install_remedy(extra)}",
        extra=extra,
    )


def require(module: str, *, extra: str, capability: str) -> ModuleType:
    """Import ``module``, or raise an actionable :class:`MissingOptionalDependency`.

    ``ImportError`` is caught rather than ``ModuleNotFoundError`` so a package
    that is present but broken (a half-installed wheel, a missing native
    library under ``onnxruntime``) also lands on the actionable message
    instead of a traceback through vendor internals.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise missing(capability, module=module, extra=extra) from exc
