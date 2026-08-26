# Docker secrets for the MCP router

This directory only ever holds `*.example` templates and this README in git.
Everything else here (the *real* secret files an operator creates locally)
is git-ignored — see the repo `.gitignore` — and is mounted into the
`mcp-router` container by `docker-compose.router.yml` using Compose's
file-based `secrets:` mechanism, **not** baked into the image or passed as
plaintext `environment:` values.

Docker mounts each secret at `/run/secrets/<name>` inside the container
(read-only, `0444`, owned by root but world-readable to the container's
user). Two conventions are used here, matching what the code already
supports:

## 1. `CREDS_PATH` — file-path secrets (no extra glue code)

`config/credentials.yaml` (Central/GLP OAuth client id/secret, GLP workspace
id) is loaded from whatever path `CREDS_PATH` points at — see
`hpe_networking_mcp.pipeline.config.load_credentials` and
`hpe_networking_mcp.mcp_servers.shared`. Docker secrets are already files, so
this needs no translation step: `docker-compose.router.yml` sets
`CREDS_PATH=/run/secrets/credentials_yaml` and mounts the secret straight at
that path.

Setup:

```bash
cp config/credentials.yaml.example secrets/credentials.yaml
# edit secrets/credentials.yaml with real Central/GLP client id/secret values
chmod 600 secrets/credentials.yaml
```

`secrets/credentials.yaml` is git-ignored. Never commit it.

## 2. `*_FILE` — literal secret *values* (bearer token, optional-product tokens)

Some environment variables (`MCP_HTTP_BEARER_TOKEN`, and the optional
per-product `*_API_TOKEN` / `*_CLIENT_SECRET` variables) are read as literal
string values, not file paths, so there's no `CREDS_PATH`-style shortcut for
them. `docker/entrypoint.sh` bridges this with the common
`<VAR>_FILE` → `<VAR>` convention used by many official images (e.g.
Postgres's `POSTGRES_PASSWORD_FILE`): if `<VAR>` is unset and `<VAR>_FILE`
points at a readable file, the container's own environment gets `<VAR>` set
to that file's contents before `scripts/run_http_router.sh` starts.
An already-set **non-empty** `<VAR>` wins over `<VAR>_FILE`; a `<VAR>` set to
the EMPTY string beside its own `<VAR>_FILE` is treated as a misconfiguration
and refuses startup rather than silently overriding what the file provides.

The full set of families the bridge recognizes (keep this list in lockstep
with `_BRIDGE_RE` in `docker/entrypoint.sh`): `MCP_HTTP_BEARER_TOKEN`, and
the `<PREFIX>_API_TOKEN`, `<PREFIX>_CLIENT_SECRET`, `<PREFIX>_PASSWORD`,
`<PREFIX>_SESSION_COOKIE`, and `<PREFIX>_CSRF_TOKEN` suffixes. Anything else
ending in `_FILE` is deliberately NOT exported; the entrypoint logs a loud
skip instead.

Setup (the streamable-HTTP bearer token is the one enabled by default in
`docker-compose.router.yml`; skip this if you don't want to require a bearer
token):

```bash
cp secrets/mcp_http_bearer_token.example secrets/mcp_http_bearer_token
# replace the placeholder with a long random value, e.g.:
#   openssl rand -hex 32 > secrets/mcp_http_bearer_token
chmod 600 secrets/mcp_http_bearer_token
```

### One secret value = one file = one Compose secret = one `<VAR>_FILE`

Every credential gets its own file. That is what keeps rotation cheap:
revoking a leaked Mist token rewrites `secrets/mist_api_token` and restarts
the router, and no other credential is read, rewritten, or re-exposed.

`python3 scripts/setup_wizard.py --docker` generates this wiring into
`docker-compose.router.local.yml` for whatever products you select. To do it
by hand, add three things per secret — the file, the service reference, and
the top-level declaration:

```yaml
services:
  mcp-router:
    environment:
      # Central and GreenLake client secrets. load_credentials() ranks
      # process env above the YAML and reads exactly these names, so
      # secrets/credentials.yaml can carry identity only.
      SOURCE_CLIENT_SECRET_FILE: /run/secrets/central_client_secret
      TARGET_CLIENT_SECRET_FILE: /run/secrets/glp_client_secret
      # A product: non-secret identity inline, the credential as a file.
      MIST_HOST: "https://api.mist.com"
      MIST_API_TOKEN_FILE: /run/secrets/mist_api_token
    secrets:
      - central_client_secret
      - glp_client_secret
      - mist_api_token

secrets:
  central_client_secret:
    file: ./secrets/central_client_secret
  glp_client_secret:
    file: ./secrets/glp_client_secret
  mist_api_token:
    file: ./secrets/mist_api_token
```

`SOURCE_*` is the Central account and `TARGET_*` is the GreenLake account, in
`load_credentials()` terms. The wizard writes `secrets/credentials.yaml` with
base URLs, client ids and workspace ids but **no** `client_secret:` keys; a
hand-written file may still carry them inline if you would rather keep the
two together.

Never write a `<VAR>` and its `<VAR>_FILE` twin in the same file: an empty
`<VAR>` beside a `<VAR>_FILE` refuses startup, and a non-empty one silently
wins over the file.

## What NOT to do

* Do not put real secret values in `docker-compose.router.yml`,
  `Dockerfile`, or any `*.example` file in this directory.
* Do not `COPY` this directory into the image — `.dockerignore` already
  excludes everything here except the `.example` files and this README, and
  the Dockerfile never copies `secrets/` at all.
* Do not remove the `.gitignore` entry (`secrets/*`, `!secrets/*.example`,
  `!secrets/README.md`) that keeps real secret files out of version control.

See `docs/production-deployment.md` for the full deployment/security
writeup, and `config/credentials.yaml.example` for the credentials schema.
