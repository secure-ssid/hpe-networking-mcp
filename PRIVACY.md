# Privacy policy

`hpe-networking-mcp` is a self-hosted MCP server. You run it on your own machine
or your own infrastructure, against your own HPE Aruba Networking tenants. There
is no hosted service, no account, and no operator of this software other than
you.

This document describes what the software does with data. It is not a promise
about what your MCP client, your model provider, or HPE's APIs do with data —
those are governed by their own policies.

## What we collect

Nothing. The project publishes no server, endpoint, or account that could
receive your data.

There is no analytics SDK, crash reporter, usage counter, license check, update
ping, or any other phone-home written into this codebase. Where the word
"telemetry" appears in our source, it is the name of an *HPE product feature*
the tools configure and read — for example `enable_telemetry`
(`src/hpe_networking_mcp/mcp_servers/config.py:2699`), which creates a telemetry
profile on your Central tenant. It is never reporting about you back to us.

Two exceptions worth stating plainly rather than glossing over:

- The upstream MCP Python SDK ships an `OpenTelemetryMiddleware` in its
  wire-tier middleware chain (noted in
  `src/hpe_networking_mcp/mcp_servers/_sdk_compat.py:51`), and `uv.lock`
  therefore resolves one transitive package, `opentelemetry-api` (`uv.lock`
  line 2222). We do not configure it and ship no exporter or collector
  endpoint — the only mention of OpenTelemetry anywhere in `src/` is that
  comment, and nothing imports it; OpenTelemetry
  without an exporter emits nowhere. If you configure your own OTel exporter,
  traces go wherever you point it.
- The server can write a local audit log
  (`src/hpe_networking_mcp/mcp_servers/_middleware/audit_log.py`). It is
  append-only to a path on your own disk and is not transmitted anywhere.

## Where your data goes

The server makes outbound network requests to four categories of destination,
all of which you control. Some vendor endpoints are hardcoded as *defaults* —
for example the GreenLake regional API and data hosts
(`src/hpe_networking_mcp/mcp_servers/glp.py:1776-1790`), the GreenLake global
API host (`glp.py:3097`), the Axis default base URL (`axis.py:51`, applied at
`axis.py:88`), and the Mist default host (`mist.py:87`). Every one of them is a
first-party HPE or partner API for a product you chose to configure, each is
overridable by the matching environment variable, and none is contacted unless
you supply that product's credentials. No host anywhere in this codebase
belongs to the maintainers of this project.

The four categories:

1. **HPE Aruba Networking and partner APIs you point it at.** Each product's
   base URL comes from configuration you supply. `APSTRA_BASE_URL`,
   `AXIS_BASE_URL`, `CLEARPASS_BASE_URL`, `EDGECONNECT_BASE_URL`, and
   `UXI_BASE_URL` are read from the environment by the per-product config
   helpers (for example `apstra.py:248`, `clearpass.py:68`,
   `edgeconnect.py:463`). The Central source/target and GreenLake Platform
   base URLs come from your credentials file or the matching
   `SOURCE_BASE_URL` / `TARGET_BASE_URL` / `GLP_BASE_URL` variables
   (`src/hpe_networking_mcp/pipeline/config.py:88-104`), and are validated at
   load time (`pipeline/config.py:138-149`). Product base URLs are validated
   before use by `validate_product_base_url()`
   (`src/hpe_networking_mcp/mcp_servers/shared.py:935`), and request paths are
   constrained by `safe_api_path()` (`shared.py:2006`). Credentials for a
   product are sent only to that product's own configured endpoint.
2. **A local embedding model, if you enable documentation search.** The
   ingestion pipeline talks to Ollama at `http://localhost:11434` by default
   (`src/hpe_networking_mcp/pipeline/clients/ollama_client.py:6`). Embeddings
   are generated on your machine; the document index is built and stored
   locally. See the "Documentation search is a separate, local build" section of
   the README.
3. **Hugging Face, once, to download embedding model weights.** The first RAG
   query downloads the `nomic-embed-text-v1.5` model into your local Hugging
   Face cache (README, "Documentation search is a separate, local build"). This
   is a model-weights fetch; none of your tenant data is part of it.
4. **Public vendor documentation sources, only when you run the ingestion
   pipeline yourself.** This is an explicit operator-initiated command, not
   something the MCP server does on its own.

Nothing is sent anywhere else, and nothing is sent to the maintainers of this
project.

## Credentials

Credentials are read from a local file you supply — `config/credentials.yaml`
by default, overridable with the `CREDS_PATH` environment variable
(`src/hpe_networking_mcp/mcp_servers/config.py:214`,
`src/hpe_networking_mcp/mcp_servers/shared.py:1131`) — and from environment
variables. That file is git-ignored (`.gitignore:12`) so it is never committed.

The server reads only its own operator-supplied config: that file is opened in
exactly one place (`src/hpe_networking_mcp/pipeline/config.py:50-53`, reached
only via `load_credentials()` → `build_account_contexts()`), the path comes
solely from the `CREDS_PATH` environment variable, no MCP tool parameter
reaches any filesystem open, and no code path under `mcp_servers/` mutates that
variable at runtime. The `path` parameters on the product tools are HTTP URL
paths validated by `safe_api_path()`, not filesystem paths.

Credentials are used to authenticate to the product APIs you configured and are
not written into tool results. Two mechanisms guard this:

- `redact_sensitive()`
  (`src/hpe_networking_mcp/mcp_servers/shared.py:214`) recursively scrubs
  secret-shaped keys and `Bearer`/`Token`/`Basic` strings from tool previews and
  results before they are returned, including secrets nested inside stringified
  JSON blobs.
- Optionally, setting `HPE_MCP_TOKENIZE_SECRETS=1` enables session-scoped
  secret tokenization for model-visible tool traffic
  (`src/hpe_networking_mcp/mcp_servers/_middleware/secret_tokenizer.py`). It is
  off by default.

## Data your model provider sees

Tool results are returned to whatever MCP client you connected, which typically
forwards them to a model provider. Those results contain your network data:
device inventory, client information, configuration, logs, and similar. That
flow is inherent to using an MCP server with a hosted model, and it is your
decision to make.

If you do not want tenant data leaving your environment, run the server against
a local model, or use the credential-free quickstart described in the README,
which exercises the tooling without live tenant access.

## Local state on disk

The server writes only to paths on the machine you run it on: the documentation
index, pipeline run artifacts, and cached tokens. Removing those directories
removes the data.

## Questions

Open an issue at
<https://github.com/secure-ssid/hpe-networking-mcp/issues>. For anything with a
security impact, follow [SECURITY.md](SECURITY.md) instead — do not put
credentials, tenant IDs, or customer data in a public issue.
