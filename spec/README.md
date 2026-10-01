# Receipt spec v0.1

Schema: `receipt-v0.1.schema.json` (JSON Schema 2020-12). One document per agent run (or per turn of a
session, with `run.turn_id`). The document is content-addressed and signed.

## Shape

| Member | What it holds |
|---|---|
| `claimcheck` | spec version, `"0.1"` |
| `id` | `rcpt_` + first 24 hex of sha256 over the canonical document without `id`, `signature` and `created_at` |
| `run` | adapter, session/turn ids, agent platform + model, start/end, the request (`asked`), title |
| `capture` | `live` (events hash-chained by the adapter as they happened) or `at-rest` (rebuilt from the platform's stored log); event count; `chain_head` |
| `ledger` | tool-call counts by tool; commands (total, failed, remote, items with exit code); files written and read; external actions; token usage and cost |
| `report` | the agent's final message (redacted) and its sha256 |
| `claims` | the deterministic sentence split of the report: `i`, text, kind, atomic targets, verdict, evidence |
| `summary` | verdict counts and the `headline` (worst verdict present; `unchecked` never sets it) |
| `verifier` | name, version, `rules_sha256` (hash of the extraction + verification source), whether a model classified kinds |
| `privacy` | `full` · `summary` · `hashes` — how much of the ledger and report this copy carries |
| `signature` | ed25519 over the JCS canonical bytes of the document without `signature`; `kid` = sha256(pubkey)[:16] |

## Canonical form

RFC 8785 style: keys sorted, no whitespace, UTF-8, no floats anywhere (money is a decimal string,
times are RFC 3339 `…Z` strings with milliseconds). `id` is computed over the document without `id`,
`signature` and `created_at`; the signature then covers everything including `id` and `created_at`.

## Privacy levels

- **full** — command lines (redacted), full paths, report text, evidence.
- **summary** — command first word + sha256 of the full line; file basenames; report text and claims kept.
- **hashes** — no command items, no report text, no `asked`; paths and claim texts replaced by sha256;
  evidence blank. Verdict counts, kinds and the headline remain. Someone holding the local run log can
  expand everything client-side by recomputing the hashes.

## Redaction

Applied to every event and to the report before hashing. Rule-set id in `capture.redaction`
(`claimcheck-redact-v2`): password/secret/token/api-key assignments in shell, YAML and JSON (value
replaced, name kept), env-style `*_KEY`/`*_PASS`/`*_SECRET`/`*_TOKEN`/`*_DSN`/`*_SESSION` names,
`--password x` flags, `scheme://user:PASSWORD@host`, `Bearer`/`Basic`/`Token` credentials, Slack and
Discord webhook URLs, and secrets recognised by shape: OpenAI/Anthropic `sk-…`, Stripe `sk_/rk_live|test_…`
and `whsec_…`, GitHub, GitLab, npm, Hugging Face, Google `AIza…`, Slack, Tailscale, ntfy, Telegram bot,
AWS, age, JWTs, PEM private keys. Placeholders (`$VAR`, `<…>`, `{{…}}`) and paths to secret files are
kept. v1 receipts predate the JSON, env-suffix, URL and vendor-shape rules.
Hashes cover the redacted text, so a
receipt can be re-verified without the secret ever existing again.

## Verification of a receipt (no run log needed)

1. Schema valid.
2. `id` equals the recomputed content id.
3. Signature valid for `kid` (embedded `pub`, or the key on record for that runtime).

Re-verification of *verdicts* needs the run log: recompute claims from `report.text` with the verifier at
`verifier.version` / `rules_sha256` and compare.

## Re-redaction (`claimcheck scrub`)

When the redaction rules improve, `claimcheck scrub` re-applies them to run logs and receipts already on
disk: records are redacted again and the chain rebuilt (a chain broken by a concurrent append is mended the
same way); each receipt is redacted again, its `chain_head` moved to the same event's new hash, `redaction`
set to the current rule-set and `capture.rescrubbed` stamped, then it is signed again by the same key.
`~/.claimcheck/scrub.log` records each run. Nothing but redaction changes.

## What a signature proves, and what it doesn't

A valid signature proves the receipt has not changed since it was signed, by the holder of key `kid`.
`capture.key_custody` says who that holder is. Today every adapter keeps the key in `~/.claimcheck/key`,
readable by the account the agent runs as (`same-user`): an agent with a shell could rewrite its run log
before the receipt is made, or sign a receipt of its own. So a `same-user` receipt is evidence of what the
log said at signing time, not proof the log is complete or untouched. Custody that closes that gap —
`separate-user`, `hardware`, `hosted` (event hashes sent off the machine as they happen) — is planned, and
receipts will say which one they had.

`claimcheck verify <receipt>` also checks the local run log when it is on the machine: the chain must be
intact and must still contain the receipt's `chain_head`, so a log edited after the receipt fails.

## Live capture (adapters)

An adapter that runs beside the agent appends every tool call to `~/.claimcheck/runs/<session_id>.jsonl`
as it happens: `{i, ts, turn_id, tool_call_id, tool, args, result, status, duration_ms, prev, hash}`,
redacted before hashing, `hash = sha256(canon(record minus hash))`, `prev` = previous hash (genesis =
64 zeros). The receipt then carries `capture.mode = "live"`, `capture.chain_head` and `capture.events`
(this turn's events) and `capture.log_ref`. `claimcheck chain <session>` recomputes the chain.

One receipt per **turn** (Hermes `on_session_end` fires per run; Claude Code `Stop` per turn), with
`run.turn_id`. The `ledger` section is that turn's events (what this run did); claims are verified
against the whole session's events so far (a report may refer to work from an earlier turn). Runs with no
tool calls get no receipt unless the adapter is told to receipt chat.
