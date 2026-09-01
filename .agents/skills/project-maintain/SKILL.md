---
name: project-maintain
description: Use when a repository's profiles.lock, CI workflow, or agent skills have drifted from what the installed pika expects — reading gate 1's drift messages, running the upgrade, and knowing what --force does and does not touch.
---

# Keeping a repository current with its pika

Two kinds of drift show up as a red gate 1, and they have different remedies:
the repository's pinned pack digests can fall behind the binary (or run
ahead of it), and a generated projection can fall behind the canonical skill
or guidance it was rendered from. Neither is fixed by editing the failing
file by hand.

## `profiles.lock` drift

`.project/profiles.lock` pins the digest of every pack the contract selects.
Gate 1 recomputes those digests against the running binary's embedded
registry and fails on disagreement, printing both numbers:

```
profiles.lock: profiles.lock records registry digest e824...2fdf, and this
pika's embedded pack registry is f34a...9e9e. ... establish which side is
behind before regenerating.
```

**Establish which side is behind before running anything.** The two causes
have opposite remedies, and the wrong one is silent: `pika init --force` run
from an *older* binary rewrites a correct lock to pin older packs, and gate 1
then reports green on a downgraded repository.

```sh
pika version --root .    # does THIS binary's registry match the lock?
which -a pika             # any other pika on this machine?
git log -1 -- .project/profiles.lock   # who wrote the lock, and when
```

If some other installed `pika` matches the lock's digest, that build wrote
it and the one that failed is behind — upgrade or rebuild, then re-run
`pika check --all`. Nothing in the repository needs to change.

If the lock is genuinely the stale side, the remedy is one command, and it
costs nothing you wrote:

```sh
git status --porcelain   # clean tree first, so the diff below is only this command
pika init --force         # profiles, name and module are read back from the repository
git diff                  # the contract, the lock, the PR template, the CI workflow — nothing else
pika check --all          # gate 1 goes green again
```

## What `--force` touches, and what it never does

`--force` regenerates only what the kernel owns: `.project/contract.yaml`,
`.project/profiles.lock`, `.github/pull_request_template.md`, and
`.github/workflows/ci.yml`. Everything else it scaffolds — `README.md`,
`AGENTS.md`, `CONTRIBUTING.md`, the language scaffold, the canonical skills
under `.agents/skills/` — is written only where it is missing, exactly as
`pika apply`'s create-if-missing path already treats them. A project that
has tuned its own skill or edited its own docs does not lose them to an
upgrade. `--reset-docs` (only alongside `--force`) is the one opt-in that
restores the scaffold's own text over yours, docs and skills alike; nothing
else does, and no flag ever touches `.project/exceptions.yaml`.

This is a deliberate change from before M3, when `--force` rewrote every
file `init` manages. The cost of *that* being safe is that a corrected
template now rotates the owning pack's digest — one more lock mismatch, for
everyone — which is why it ships paired with a `--force` nobody has to brace
for.

## Projection drift: stale versus tampered

A harness-native file like `AGENTS.md` carries a generated region between
`<!-- pika:skills:begin -->`/`<!-- pika:skills:end -->` markers. Gate 1
checks it two ways, in this order, because the two failures have opposite
remedies:

**Tampered** — the region's own recorded digest no longer matches its
bytes. Somebody edited kernel-owned text directly.

```
skills projection: tampered AGENTS.md (harness codex) was edited by hand
inside the pika skills markers: ... regenerating would DISCARD whatever is
there rather than keep it — make the change in the canonical skill under
.agents/skills/ (or in the profile pack guidance) and regenerate
```

Regenerating here throws away the edit. Make the change at the source —
the canonical skill file, or the pack's `agent-guidance` — and regenerate
from there instead.

**Stale** — the region is intact but a source it cites (a skill, or a
pack's guidance) has moved on since it was generated.

```
skills projection: stale AGENTS.md (harness codex) cites skill
.agents/skills/project-work/SKILL.md at sha256:4018..., which is now
sha256:9c2f...; regenerate it with `pika skills install`
```

Regenerating here is free: nothing anyone wrote by hand is at risk.

`pika skills` (no subcommand) is the read-only report of every canonical
skill and every declared projection, current or not — reach for it before
`install` the way `pika doctor` is reached for before `pika check`.
`pika skills check` gives the same verdict as `pika skills install` without
writing anything, for a human who wants the drift answer alone.

## `pika doctor` is where this starts

`pika doctor` reports the lock digest comparison, any recorded exception
missing a required field, and — via gate 1's own reporting — projection
drift, all without executing a single gate. It is the first move whenever
`pika check` behaves unexpectedly, not a fallback after guessing.
