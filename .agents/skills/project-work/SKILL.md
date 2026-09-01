---
name: project-work
description: Use when running, repairing, or resuming work in a pika-governed repository — covers pika work, improve, resume, recover, and what each refusal means.
---

# Driving pika

pika is a kernel that decides whether your changes are safe to keep. You propose;
it verifies and commits. Nothing you assert counts as evidence — only a green
verification ladder does.

## Pick the command

| Situation | Command |
|---|---|
| Checks are failing and you want them fixed | `pika improve` |
| You have a goal to implement | `pika work "<goal>"` |
| A previous run was interrupted | `pika resume <work-id>` |
| A run or transaction was killed and left a lock | `pika recover --apply` |
| You want to know what is wrong before acting | `pika doctor` |
| You do not understand a rule, gate, or error code | `pika explain <id>` |

`pika status` lists runs. `pika status <work-id>` shows one in full.

## The loop

`improve` and `work` do the same thing, differing only in what starts them:

1. run the ladder (`pika check`)
2. hand **only the failing gates** to the configured agent — warnings never go
3. re-run the ladder against what the agent produced
4. commit on a branch **only if the recheck is green**

You are step 3's subject, not its judge. The kernel re-verifies independently.

## Rules that are not obvious

**The ladder is the evidence.** A green `pika check` is the only completion
signal. Never report done from a narrative, a diff that looks right, or a test
you ran by hand.

**`blocked` means diagnose, not retry.** A blocked run recorded a reason. Read
it with `pika status <work-id>`. Re-running without changing anything reproduces
the same block.

**A refusal naming another holder means someone else is writing.** A run-lease
refusal or `scope_conflict` means a second process holds this repository — one
repository runs one run at a time, because both would commit through the same
working tree. Do not loop, do not wait, and **never delete a lock file**.
`pika recover` clears only a holder it can prove is dead; a holder on another
machine is reported unverifiable and is never swept.

**Never commit `.project/state/`.** It holds prompts, check reports, and briefly
the raw agent transcript. The kernel filters it out of commits — do not defeat
that by moving, renaming, or copying content out of it.

**The receipt is issued by the kernel.** `.project/evidence/<work-id>.json` is
written by pika from what it observed. Do not write one, and do not overwrite
one; `publish_evidence` refuses.

**A warning is not a failure.** `file-size-review` warnings are visible on
purpose and do not fail a gate. Do not "fix" them by splitting files unless
asked, and never by recording an exception — an exception silences the signal
without doing the work.

**Reads need an envelope over MCP.** `envelope_denied` on `inspect_repo` or
`read_contract` is not a bug in your request; the operator has not authorized
one yet. The remedy is `pika authorize --scope read`.

## Failure handling

Fix the source, not the symptom. Do not suppress a warning, special-case an
input, weaken an assertion, or narrow a test to make a gate pass. If a gate is
wrong, say so and stop — a green ladder obtained by weakening it is worse than a
red one.

If you cannot make the ladder green, leave it red and report why. A blocked run
with an honest reason is a successful outcome; a green run that lied is not.
