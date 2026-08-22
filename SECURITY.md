# Security policy

## Reporting a vulnerability

Do not open a public issue with exploitable details, credentials, tokens, tenant
IDs, customer data, or private network information.

Use GitHub's private vulnerability reporting or security advisory flow for this
repository when it is available. If that flow is unavailable, open a public issue
with only a short, non-sensitive summary and ask for a private reporting channel.

Helpful non-secret details:

- Affected commit, release, or branch
- Impact and affected tool/server area
- Minimal reproduction steps using fake hosts, fake IDs, and redacted payloads
- Suggested mitigation, if known

## Credential exposure

If Central, GreenLake Platform, ClearPass, Mist, Apstra, ArubaOS 8, EdgeConnect,
UXI, or other API credentials were exposed, revoke or rotate them before filing a
report. Do not attach real tokens, secrets, tenant IDs, or customer data to
issues, discussions, logs, screenshots, or pull requests.

## Runtime safety controls

- Central and optional platform writes can be disabled independently with
  `HPE_MCP_<PLATFORM>_WRITES`.
- Optional products default to `HPE_MCP_PRODUCT_ACCESS=read-only`; guarded
  writes require dry-run review and explicit confirmation.
- Non-loopback streamable HTTP listeners require explicit host and origin
  allow-lists. `MCP_HTTP_BEARER_TOKEN` protects streamable HTTP routes; bearer
  configuration with SSE fails closed.
- `HPE_MCP_TOKENIZE_SECRETS=1` enables bounded session-scoped secret
  tokenization for model-visible tool traffic.
- EdgeConnect's incompatible pre-9.3 endpoint map is blocked unless a user
  explicitly enables legacy mode for a validated older/lab Orchestrator.

## Supported versions

Security fixes target the `main` branch and the latest published release.
Version 0.10.x is the current development line. Older pre-1.0 releases do not
have guaranteed backports unless a maintainer explicitly notes otherwise.
