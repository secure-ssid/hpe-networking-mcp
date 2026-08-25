# Security policy

## Reporting a vulnerability

Do not open a public issue with exploitable details, credentials, tokens, tenant
IDs, customer data, or private network information.

Report privately by email to **<security@securessid.com>**. This is the
channel to prefer.

If GitHub's private vulnerability reporting is enabled for this repository, you
may use that flow instead:
<https://github.com/secure-ssid/hpe-networking-mcp/security/advisories/new>.
If that flow is not available to you, use the email address above rather than a
public issue.

If the email bounces or you cannot reach us privately by either route, open an
issue at <https://github.com/secure-ssid/hpe-networking-mcp/issues> with only a
short, non-sensitive summary and ask for a private reporting channel.

### Response window

This is a small, volunteer-maintained project, so these are targets rather than
a contractual SLA:

- **Acknowledgement:** within 5 business days of the report.
- **Initial assessment** (confirmed / not reproducible / needs more detail):
  within 10 business days.
- **Fix or documented mitigation** for a confirmed issue: tracked publicly once
  a fix or mitigation is available, coordinated with the reporter on timing.

If you have not heard back within the acknowledgement window, please email
again or, if you filed an advisory, ping that thread — it means the report was
missed, not declined.

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
