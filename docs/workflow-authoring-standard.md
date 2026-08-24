---
title: "Workflow authoring standard"
nav_order: 13
parent: "Reference"
---

# Curated workflow authoring standard

Every new intent-level workflow (client diagnosis, site/device health, WLAN
changes, NAC, firmware, compliance, alerts, incident response) MUST follow
this standard. The docstring is the tool's UI — the LLM selects and calls
tools from it alone.

Reference implementation for every pattern below: `list_clients` and the
alert-action helpers in `src/hpe_networking_mcp/mcp_servers/monitoring.py`.

## 1. Annotations

Declare capability via the shared enums, never ad-hoc:
`READ_ONLY`, `WRITE`, `IDEMPOTENT_WRITE`, `DESTRUCTIVE`
(`mcp_servers/shared.py`). Reads use `READ_ONLY`; anything mutating uses a
write annotation AND the write gate (§5). Annotation and actual behavior must
agree — mislabeled writes are the bug class this exists to prevent.

## 2. Docstring contract

First line: imperative intent plus the selection cue an LLM needs
("List connected clients. ALWAYS filter — unfiltered returns all clients.").
Then, in order:

1. **Parameters** — name server-side filters vs client-side substring filters
   explicitly, and say which to prefer for natural-language queries.
2. **Enums and synonyms** — list valid values verbatim
   ("severity: CRITICAL/MAJOR/MINOR"). If the API's vocabulary differs from
   user vocabulary, document the accepted synonyms and coerce (§3).
3. **Pagination contract** — state cursor vs offset behavior in the docstring,
   verbatim, including which response field carries the cursor
   (`_pagination.next_cursor`).
4. **Limits and defaults** — state the default and the hard cap.

Never document a behavior the code does not implement; the repo runs
docs-consistency tests and this standard extends that expectation to
docstrings.

## 3. Parameter coercion

- Clamp every `limit` through `clamp_limit` — never trust the caller's number.
- Accept natural-language input and coerce: enum-synonym maps (see
  `_REBOOT_REASON_MAP` for the translation-map pattern), case-insensitive
  substring matching, MAC/IP ambiguity resolution (`find_client`).
- Central's v1 and v1alpha1 responses use different field names for the same
  datum. Client-side filters MUST check the tuple of known field names, not a
  single key (see `_match` in `list_clients`).
- Normalize scope identifiers via `pipeline.scope_ids.normalize_scope_id`;
  reject invalid scopes with a `ValueError` that names the field.

## 4. Pagination and bounded output

- Prefer `next_cursor`; accept legacy `offset` and translate it to an
  approximate starting cursor, documenting that translation is approximate.
- Wrap collections in `maybe_bound` / `bound_collection_response`; when the
  wrapper returns the bounded form, populate `_pagination.offset` and
  `_pagination.next_cursor` from the client's returned cursor.
- Item and byte budgets are enforced by the shared bounding path — a curated
  tool must not bypass it by returning raw API payloads.

## 5. Writes (until the transactional model lands)

- Gate registration/execution with `enforce_platform_write(platform, tool)`;
  a blocked write returns the structured blocked payload, never an exception.
- Destructive actions elicit confirmation via `ctx.elicit` with an explicit
  schema. On unsupported elicitation return
  `{"status": "CONFIRMATION_UNAVAILABLE", ...}` and DO NOT perform the
  operation; on decline return `{"status": "CANCELLED", ...}`
  (pattern: `_confirm_alert_action`).
- Every write response includes `endpoint_used`; HTTP errors go through
  `compact_http_error(response, endpoint)` — never return raw response text
  (credential-redaction rules live in the shared path).
- Validate mutation results with `validate_write_result` / `WriteResultError`.

## 6. Errors

Return structured error payloads (`{"error": ..., "endpoint_used": ...}`),
not exceptions, for expected API failures. Exception text leaving the process
is redacted by the shared middleware — do not reconstruct raw vendor errors
in tool responses.

## 7. Required tests per workflow

1. Coercion: synonyms, case variants, ambiguous identifiers, invalid scope.
2. Pagination: cursor forward, offset translation, empty final page.
3. Bounded output: oversized collection returns the bounded envelope with
   `_pagination`, never the full payload.
4. Write gate (writes only): gate unset → structured blocked payload, zero
   HTTP calls (assert on mocked transport).
5. Confirmation (destructive only): elicitation unavailable →
   `CONFIRMATION_UNAVAILABLE` and no mutating call; decline → `CANCELLED`.
6. Dry-run (once the transactional model lands): `dry_run=true` performs no
   mutating HTTP call.

## 8. Module header

Each `mcp_servers/*.py` module keeps its first docstring current: tool count,
coverage summary, and module-wide pagination notes (see `monitoring.py`'s
header). Tool counts in docs are CI-checked via `docs/project-facts.json` —
regenerate it, never hand-edit.
