# Adoption review

Status: **APPLIED** — the adoption drafts were promoted into a live contract.

## Applied (9)

- [x] create `.project/contract.yaml`
- [x] create `.project/profiles.lock`
- [x] create `.project/exceptions.yaml`
- [x] create `AGENTS.md`
- [x] create `.agents/skills/project-maintain/SKILL.md`
- [x] create `.agents/skills/project-research/SKILL.md`
- [x] create `.agents/skills/project-review/SKILL.md`
- [x] create `.agents/skills/project-work/SKILL.md`
- [x] write `AGENTS.md`

## Skipped (4 — left on disk as apply found them)

- `README.md` — already exists; kept the existing file
- `CONTRIBUTING.md` — already exists; kept the existing file
- `.github/workflows/ci.yml` — already exists; kept the existing file
- `.github/pull_request_template.md` — already exists; kept the existing file

## Exceptions (28 recorded naming deviations)

`pika adopt` wrote these records into `.project/exceptions.yaml`; each waives one naming rule for one path, and each is keyed to that exact path — a path added later is not covered and will still fail gate 1. Approving `pika apply` accepts every record below, so read the reasons first: keep the record, or rename the path to satisfy the rule and delete the record.

By rule:

- `naming-kebab-case`: 28

By directory:

- (repository root): 7
- `docs/`: 1
- `docs/_includes/`: 1
- `docs/architecture/`: 1
- `scripts/`: 1
- `src/hpe_networking_mcp/`: 1
- `src/hpe_networking_mcp/mcp_servers/`: 2
- `src/hpe_networking_mcp/mcp_servers/_middleware/`: 12
- `src/hpe_networking_mcp/mcp_servers/skills/`: 2

- `CHANGELOG.md` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `CODE_OF_CONDUCT.md` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `MIGRATION.md` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `PRIVACY.md` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `SECURITY.md` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `SUPPORT.md` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `THIRD_PARTY_NOTICES.md` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `docs/_config.yml` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `docs/_includes/head_custom.html` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `docs/architecture/RAG-ARCHITECTURE.md` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `scripts/_apstra_operations.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/_paths.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_cache_hygiene.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/__init__.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/_outcome.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/audit_log.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/install.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/mac_normalizer.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/metrics.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/null_strip.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/pii_tokenizer.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/rate_limit.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/response_envelope.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/secret_tokenizer.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_middleware/unknown_tool_suggest.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/_sdk_compat.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/skills/TEMPLATE.md` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit
- `src/hpe_networking_mcp/mcp_servers/skills/_engine.py` — rule `naming-kebab-case`
  - reason: pre-existing repository layout; adopt records the convention instead of renaming files for style conformity
  - owner: pika adopt
  - review condition: re-review when the path is next modified or at the next convention audit

## Gate 1 on the applied contract

Pass — no findings.

## Next step

Run `pika check --all` to verify the applied contract, then commit every path listed under **Applied** above together with `review/`.
