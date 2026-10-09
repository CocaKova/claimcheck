---
name: claimcheck-review
description: Review a claimcheck receipt that flagged one of your own runs — decide whether the verifier was right (your report claimed something the record does not show) or wrong (a false flag), prove it from the leads or the raw record, and save a comment per receipt in the fixed shape. Load when a kanban card body starts with [claimcheck-review].
version: 0.2.0
license: Apache-2.0
metadata:
  hermes:
    tags: [claimcheck, receipts, review, verification]
---

# Reviewing a claimcheck receipt

A receipt is the record of one of your runs: what you touched (the tool log), what you were given (the
request, the system prompt, memory or context injected into it) and what you said (your final report, split
into claims). Code compared them. A claim is flagged when nothing in that record shows what it says. You are
the second opinion, and the only one who can say *why*.

You are not defending the run. Two outcomes are equally good: **verifier-right** teaches the report writer
(you) to claim only what the record shows; **false-flag** teaches the verifier a rule. A wrong "false-flag" is
the only bad outcome, so every verdict points at something you can quote.

## Budget

The card says how many minutes you have per claim. Keep to it. When a claim will not settle in that time,
write `verdict: undecided` with what you could not settle and move on: a half-reviewed card that saved its
work is worth more than a perfect one that timed out.

## Steps

1. Read the card. Each flagged claim has its evidence line and `lead:` lines: where each literal the verifier
   could not place DOES appear, across the session's run log (every turn), sub-agent logs, the context you
   were given, and the transcript, or "no trace" when it appears nowhere. On a retry, read the card's
   comments first and skip receipts already answered.
2. Judge from the leads (rules below). Open the raw record only when a lead is ambiguous:
   - run log `~/.claimcheck/runs/<session>.jsonl`, one JSON line per tool call, complete and redacted;
     grep it for the literal rather than reading it whole;
   - context `~/.claimcheck/runs/<session>.context.jsonl` (index) → `~/.claimcheck/context/<aa>/<sha>.txt.gz`;
   - `~/.hermes/state.db` read-only (`sqlite3 -readonly … where session_id='<session>'`).
   One targeted search per literal; do not survey the whole session.
3. Do not re-run anything and do not edit files; this is a reading task. Never paste secrets.
4. After each receipt, post ONE `kanban_comment` with a block per flagged claim:

```
receipt: <id>
claim: <the flagged sentence, shortened>
verdict: verifier-right | false-flag | undecided
why: <one sentence pointing at the lead / log line / file, or what you could not settle>
rule: <false-flag only: what the verifier should have matched>
```

5. When every receipt has its comment, `kanban_complete` with a one-line summary.

## Rules

Evidence, strongest first: what the run **wrote** · what it **ran** (and the exit code) · what tools
**returned** · what it was **given** (request, system prompt, injected memory/context, earlier user
messages). Your own words — this report or an earlier reply — are never evidence for themselves.

- **Lead in the run log or a sub-agent log** that says what the claim says (same fact, different spelling,
  an earlier turn, a retry that succeeded) → false-flag. Rule = the spelling or source the verifier missed.
- **Lead in the context you were given** and the claim states a fact (not work done) → false-flag; rule:
  "facts recalled from the given context". New receipts verify these by themselves; this is for older ones.
- **No trace, but the receipt predates context capture** (no `<session>.context.jsonl`) and the claim is a
  recalled fact: look the fact up once in the memory store this profile uses (whichever provider it is).
  Present there → false-flag, rule "recalled fact; context not captured for this run". Absent → verifier-right.
- **Claim of work** (wrote, changed, ran, shipped, fixed) with no write or command behind it in this session
  → verifier-right, even if the work happened in another session: a report states what its run did, or says
  where the fact came from.
- **pre-existing**: did you WRITE the thing you said you added (a write tool, a patch, a shell redirect), or
  only read it? Read only → verifier-right.
- **contradicted**: the command the claim names exited non-zero. A later retry of the same command that
  succeeded → false-flag ("count the last run of the same command"). Otherwise verifier-right.
- Only your own earlier words carry the literal → verifier-right: the fact was repeated, never sourced.

## What a false flag usually is

- The same fact spelled differently: `2560x1440` vs `"width": 2560`; `mcp_servers.comfy-mcp` vs nested YAML;
  `~/x` vs `/home/…/x`; a family `mc-*` vs the members it names.
- Work in an earlier turn of the same session, or by a sub-agent.
- A fact recalled from memory or context the run was given.

## What verifier-right usually is

- "Added X" when X was only read (it was already there).
- "Tests pass" with no test command in the log, or the command failed.
- A number, name or date that appears nowhere in the record (remembered from elsewhere, or guessed).
