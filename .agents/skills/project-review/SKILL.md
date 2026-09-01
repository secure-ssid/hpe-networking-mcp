---
name: project-review
description: Use when reviewing work in a pika-governed repository — what counts as evidence, what the receipt means, and what a reviewer must never do.
---

# Reviewing in a pika repository

## What counts as evidence

In order of authority:

1. **A green `pika check`.** The ladder ran the repository's own declared
   commands. This is the only completion signal.
2. **The evidence receipt** at `.project/evidence/<work-id>.json` — issued by
   the kernel from what it observed: the gates it ran, their exits and
   durations, the commit and tree it produced, baseline failures and
   regressions.
3. **The run record** at `.project/state/work/<work-id>/record.json` — local,
   gitignored, showing every phase the run passed through.

A narrative claim is not on this list. Neither is a diff that looks correct.

## What a reviewer must not do

**Do not write or overwrite a receipt.** A receipt supplied by the subject of
the work is a claim; one issued by the component that ran the gates is evidence.
`publish_evidence` refuses to overwrite a kernel-issued receipt, and that
refusal is the point.

**Do not accept a green ladder that was made green by weakening it.** Check
whether a gate command, an assertion, or a test's scope changed in the same
change that made it pass. `pika explain <gate-id>` tells you what a gate is for.

**Do not treat a warning as a finding to fix.** Warnings do not fail gates. Ask
whether the warning is telling you something true before asking anyone to act
on it.

**Do not read raw transcripts.** They are local by design. Review the receipt,
the record, and the diff.

## Reading a receipt

The fields that matter most:

- `completion.complete` — with `reason` required when false, and `blocker`
  forbidden when true.
- `commands[]` — each gate's argv, exit and duration, in ladder order:
  baseline first, then recheck.
- `baseline_failures[]` versus `regressions[]` — a pre-existing failure is not
  a regression, and conflating them is how a red baseline gets blamed on a
  change that did not cause it.
- `changed_files[]` with ownership.

Every string in a receipt has passed redaction. If you find a credential in one,
that is a defect in the redactor, not something to fix by hand.

## Verdicts

Distinguish clearly:

- **the change is wrong** — cite the file, line, and the behavior it breaks;
- **the change is unproven** — the ladder did not cover it, so say what would;
- **the change is fine and something adjacent is wrong** — say so separately,
  and do not block on it.

An unproven change is not the same as a broken one. Say which you mean.
