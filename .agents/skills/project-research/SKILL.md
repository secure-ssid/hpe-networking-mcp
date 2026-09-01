---
name: project-research
description: Use when learning what a pika-governed repository actually declares — reading the contract, the lock, the exceptions record, and a gate or error code, without running or changing anything.
---

# Reading a pika repository

Three things answer almost every question about what a repository requires,
none of them run a gate or write a byte: `inspect_repo`, `read_contract`, and
`pika doctor`/`pika explain` from a shell.

## Where the durable state lives

| Path | What it is |
|---|---|
| `.project/contract.yaml` | The committed contract: profiles, per-slot verification commands, agent mappings, declared skill projections. Operator-owned; a schema error refuses to load rather than guessing. |
| `.project/profiles.lock` | The pinned digest of every pack the contract selects, against this binary's embedded registry. A mismatch means the lock and the binary disagree about what the packs are — not that the repository is broken. |
| `.project/exceptions.yaml` | Recorded naming deviations, one entry per path (or a list, for a path that violates more than one rule at once). Every record carries `rule-id`, `reason`, `owner`, `review-condition` — all four, or gate 1 fails on the record itself. |
| `.project/state/` | Local, gitignored, transient: envelopes, run records, recovery journals. Never committed, never durable evidence. |
| `.project/evidence/<work-id>.json` | The kernel-issued receipt for one run: gates, exits, durations, the commit produced. Read this for what happened; do not write one. |

## Reading the contract without a shell

`read_contract` (MCP) or `pika doctor`/`cat .project/contract.yaml` (shell)
loads the contract and resolves its profiles against the embedded pack
registry — the same resolution `pika check` uses, so what you read is what
will actually run. It reports:

- the profiles selected, in composition order (core first, one language pack
  at most);
- the effective command for every verification slot — the contract's own
  value where the contract sets one, else the pack's discovery hint;
- schema and profile-reference errors, rather than a partial guess.

A contract that does not parse is a fact to report, not a default to fall
back on.

## Reading the repository inventory

`inspect_repo` (MCP) walks the tree and reports packages, detected languages
and kinds, existing check commands, and git/workflow state — the same
discovery `pika adopt` runs, read-only. Use it to answer "what does this
repository actually contain" before proposing a change, especially in an
unfamiliar or partially-adopted repository.

Both `inspect_repo` and `read_contract` require a capability envelope as of
M3 — enumerating a repository is a capability an agent is granted, not a
neutral act. `envelope_denied` on either is not a bug in the request; the
remedy is `pika authorize --scope read`, which grants no writes and no exec
at all.

## Understanding one rule, gate, or error code

```sh
pika explain naming-kebab-case
pika explain file-size-review
pika explain test
pika explain envelope_denied
```

`explain` resolves ids from the contract's own profiles, so it explains
*this* repository's rules, not a generic list. For a naming rule it prints
the owner, severity, what it matches, the rationale, the remediation, and a
copy-pasteable exception record. For a gate it names the command that will
run. Run it with an unknown id to print the ids this repository actually
knows, rather than guessing at one.

## Diagnosing without running anything

`pika doctor` answers "why is this repository not working" without executing
a single gate: the resolved root, the contract and lock state, whether the
exceptions record loads, the envelope, any interrupted transaction or held
lease, and the command (or discovery hint) each gate will run. Reach for it
before `pika check` when something about the repository's state is unclear,
not after a gate has already failed for a reason worth spawning a process to
find.

## What none of this does

Reading the contract, the lock, or the inventory answers what the repository
*declares*. It says nothing about whether the ladder currently passes — only
`pika check` runs the commands and produces that evidence, and only that
evidence is the completion signal.
