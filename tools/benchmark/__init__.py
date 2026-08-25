"""Credential-free, self-contained benchmark harness for the curated Central workflow layer.

The harness measures a target MCP surface against a gold scenario manifest the
same way every run: task success, tool-selection accuracy, token usage,
latency, API-call count, and safety failures. It consumes the golden-scenario
manifest schema (see :mod:`hpe_networking_mcp.tools` documenting
``tools/benchmark/manifest.py``) verbatim, with zero interpretation.

Nothing here touches a live Aruba Central tenant. The fake Central API is an
in-process, on-off HTTP server with a deterministic dataset; every benchmark
run is reproducible and credential-free.
"""

__version__ = "0.1.0"
