---
name: claimcheck-review
description: Review a claimcheck receipt that flagged one of your own runs — decide whether the verifier was right (your report claimed something the tool log does not show) or wrong (a false flag), prove it from the run log or state.db, and leave a comment in the fixed shape. Load when a kanban card body starts with [claimcheck-review].
version: 0.1.0
license: Apache-2.0
metadata:
  hermes:
    tags: [claimcheck, receipts, review, verification]
---

# Reviewing a claimcheck receipt

A receipt is the record of one of your runs: what you touched (the tool log) and what you said you did
(your final report, split into claims). Code compared the two. A claim is flagged when the log does not
show what the sentence says. You are the second opinion, and the only one who can say *why*.

You are not defending the run. Two outcomes are equally good: **verifier-right** teaches the report
writer (you) to claim only what the log shows; **false-flag** teaches the verifier a rule. A wrong
"false-flag" is the only bad outcome, so prove every verdict from the raw record.

## Steps (a few minutes)

1. Read the card. It carries the receipt id, the flagged claims with their evidence lines, and the
   file path of the receipt (`~/.claimcheck/receipts/<session>/<turn>.json`).
2. Open the raw record for that session, in this order of trust:
   - the live run log `~/.claimcheck/runs/<session>.jsonl` (one JSON line per tool call: `tool`, `args`,
     `result`, `status`, `turn_id`; results are complete, secrets redacted);
   - if there is no run log, `~/.hermes/state.db` read-only:
     `sqlite3 -readonly ~/.hermes/state.db "select role, substr(content,1,400), tool_calls from messages where session_id='<session>' order by id"`.
3. For each flagged claim, find the exact tool call (or its absence) that settles it:
   - **unverified** — did any command, write or output contain the literal? If yes, quote the line: false-flag.
     If the literal appears nowhere, verifier-right (the report described something that did not happen,
     or described it in words the log never shows — say which).
   - **pre-existing** — did you WRITE the thing you said you added, or only read it? A write in a
     `patch`/`write_file`/`skill_manage`/`brain_edit` call or a shell redirection counts; a `read_file`
     or `cat` does not.
   - **contradicted** — the command the claim names exited non-zero. Did a later retry succeed? Quote it:
     false-flag (and tell the rule: "count the last run of the same command"). Otherwise verifier-right.
4. Do not re-run anything and do not edit files; this is a reading task. Never paste secrets into the comment.
5. `kanban_complete` the card with ONE comment in exactly this shape (one block per flagged claim):

```
claim: <the flagged sentence, shortened>
verdict: verifier-right | false-flag
why: <one sentence pointing at the log line: tool, command or path, and what it shows>
rule: <false-flag only: what the verifier should have matched, e.g. "YAML `key: value` equals `key=value`">
```

## What a false flag usually is

- The same fact spelled differently: `2560x1440` vs `"width": 2560`; `mcp_servers.comfy-mcp` vs nested YAML;
  a path quoted with `~` vs `/home/...`.
- Work done in an earlier turn of the same session, or by a sub-agent (its calls sit in its own run log,
  `~/.claimcheck/runs/<child-session>.jsonl`).
- A retry that succeeded after the failure the verifier saw.

## What verifier-right usually is

- "Added X" when X was only read (it was already there).
- "Tests pass" with no test command in the log, or the command failed.
- A number or name that appears in no output (remembered from an earlier session, or guessed).
