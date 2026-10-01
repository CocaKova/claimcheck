# claimcheck

**Receipts for AI agent runs.** After every turn, a receipt: what the agent touched (commands, files,
machines, tokens), what it said it did, and whether the two match. Every claim in the agent's final
message is marked **verified · unverified · pre-existing · contradicted · unchecked** — by code, against
the tool log, never by another model's opinion. Receipts are signed on your machine and stay on your machine.

Works with **Claude Code, Codex CLI, Gemini CLI, Cursor and Hermes Agent**. One hook command speaks all of
their payloads; one `init` wires whichever of them you have.

## Set it up (one line)

```sh
curl -fsSL https://claimcheck.cc/install.sh | sh
```

Or, if you would rather have your agent do it: install the plugin and say **"set up claimcheck"**.

| Agent | Install |
|---|---|
| Claude Code | `/plugin marketplace add CocaKova/claimcheck` then `/plugin install claimcheck@claimcheck` — hooks are live immediately. The plugin alone writes ledger-only receipts; run the one-liner above as well and the same hooks start giving verdicts |
| Codex CLI · Cursor · Gemini CLI | any Agent Plugins / extension install of this repo, then tell the agent "set up claimcheck" (the bundled skill runs `claimcheck init`) |
| Hermes Agent | the one-liner above: `claimcheck init` links the bundled plugin into `~/.hermes/plugins/` and enables it (or clone this repo and link `hermes_plugin/` yourself) |
| Anything with hooks | `uv tool install claimcheck-receipts` (or `pipx`, or `pip install --user`), then `claimcheck init` |

`claimcheck init` finds every supported agent in your home folder, backs up its settings file once, and adds
two hook lines: after each tool call, and at the end of each turn. `claimcheck init --remove` takes them out
again and leaves everything else as it was.

## Then

```
claimcheck open        # the latest receipt, as a page
claimcheck flagged     # receipts whose headline is not "verified", newest first, with the evidence
claimcheck review      # a second opinion on the flagged ones, on the model you pinned — never your agent's default
claimcheck doctor      # what is wired, how many receipts, any hook errors, which model reviews run on
claimcheck verify <receipt.json>   # prove a receipt file was never edited (schema · content id · signature)
```

Receipts live in `~/.claimcheck/receipts/<session>/<turn>.json` (+ `.html`). The live tool log per session is
`~/.claimcheck/runs/<session>.jsonl`, hash-chained; `claimcheck chain <session>` recomputes it.

## The review model is yours to pick

Verdicts are code and need no model. A *review* — opening the run log to say whether a flag was right or a
false flag — is a reading task, and a small model does it. On a metered plan your agent's default is often
the most expensive one, so claimcheck never lets a review fall through to it: `init` asks you once (your
agent asks you, if it is doing the setup), `claimcheck config review.model <model>` changes it, and
`claimcheck review` refuses to run until one is pinned. Reviews land in `~/.claimcheck/reviews/<receipt-id>.md`
and never touch the signed receipt.

**Running a local model?** Then reviews are free and need no agent CLI at all. `claimcheck local` lists the
servers answering on the usual ports (vLLM :8000, Ollama :11434, LM Studio :1234, llama.cpp :8080, Jan, LiteLLM);
`init` offers the first one it finds before any cloud suggestion. Pin it with
`claimcheck init --review-model <model> --review-endpoint http://127.0.0.1:8000/v1` (or the two `config` keys);
the review is then one `chat/completions` call, and because a bare completion cannot open files, claimcheck
inlines the evidence itself: the turn's run-log records, already redacted, the ones that mention the flagged
literals first, under a size budget. A server that wants a key takes `claimcheck config review.api_key <key>`
(file goes 0600, never echoed back in full). Wrong model name, server down, key missing, a reasoning model
that thought and never answered: each comes back as one plain line saying what to change.

Cloud or agent-routed reviews stay read-only: Claude Code in plan mode, Codex in the read-only sandbox
(with `--oss` and the right local provider when the pinned endpoint is Ollama or LM Studio), Gemini in plan
approval mode, Hermes in quiet one-shot chat.

## What a receipt says

```
receipt rcpt_41569cb5b5eca5688eb6dac7 · claude-code · session 5bbefce1…
asked: run the smoke test and report
verdicts: 3 verified · 0 unverified · 1 pre-existing · 0 contradicted · 2 unchecked → pre-existing
- [pre-existing] Added window_width_override=1280 to settings
    evidence: `window_width_override=1280` appears only in content the agent read; never in anything it wrote or ran
```

- **verified** — the log contains what the sentence says (the command ran and exited 0; the file was written; the value appears in output).
- **unverified** — nothing in the log shows it.
- **pre-existing** — the agent took credit for something it only read.
- **contradicted** — the log shows the opposite (the command it names failed).
- **unchecked** — nothing literal to check; counts neither way.

Not a lie detector: verification demand tracks stakes, not error rate. The receipt is the unit of
accountability for agent work, the same way a till receipt is for a cashier.

## How it stays honest

- **The check runs where the log lives.** The verifier is a small pure-Python library that runs inside the
  hook, in milliseconds, offline. No transcript is ever uploaded anywhere (sharing, below, is opt-in and
  sends only what you share).
- **Captured live, not read back later.** Hooks hand over each tool result before the platform truncates or
  compacts it; each event carries the hash of the previous one.
- **Redacted before hashing.** Passwords, tokens, API keys (OpenAI, Anthropic, Stripe, GitHub, Google and
  more), database URLs and PEM blocks are stripped from every event and from
  the report before anything is hashed or written, so a receipt can be re-verified without the secret ever
  existing again.
- **Deterministic.** Same report + same log = same receipt. The report is split into units by code; a model
  is never asked for a verdict. 12 real sessions are frozen as golden tests with exactly two real catches
  and zero false flags. The extractor and verdict rules ship as the `claimcheck-core` package (binary
  wheels); without it, receipts are ledger-only and say so in `verifier.name`.
- **Signed.** ed25519, one key per machine (`~/.claimcheck/key`), over the canonical JSON; the receipt's id
  is its content hash. `cryptography` or PyNaCl when present, a vendored pure-Python signer otherwise, so a
  hook works with nothing but `python3`.
- **Open format.** `spec/receipt-v0.1.schema.json` + `spec/README.md`. Anyone can write a verifier or a viewer.

## Sharing a receipt (claimcheck.cc, optional)

Everything above works with no account. When someone else has to see a receipt (a client, a boss, a
reviewer), a claimcheck.cc account adds:

- **Share links.** `claimcheck share` turns the newest receipt into `https://claimcheck.cc/r/<link>`, a
  page that opens on a phone with no account on their side. `cloud.share flagged|all` does it after every
  run; `claimcheck history`, `claimcheck dashboard` and `claimcheck unshare <link>` manage them.
- **A live witness.** While the agent runs, each step's fingerprint (its index, hash and the previous hash)
  goes to claimcheck.cc as it happens. A shared receipt that ends on a fingerprint received live says
  *Witnessed live*: the log behind it wasn't rewritten afterwards. A rewritten event shows up as *Record
  rewritten*. What it can't stop: a machine compromised during the run can fingerprint a forged log live.

What leaves your machine, and nothing else: those fingerprints (never content) and the receipts you share,
at `cloud.privacy` (default `summary`: the report and claims, command first words, file names). Off until
`claimcheck login <key>`; `claimcheck logout` turns it off again. Early access to Pro is free while billing
isn't open: get a key at [claimcheck.cc](https://claimcheck.cc/#pricing).

## For the Hermes owner: review cards

`hermes_plugin/review_bridge.py` is a no-agent cron: every flagged receipt becomes one kanban card for the
agent's own profile (skill `claimcheck-review`) and a line in the Office room. The agent reviews its own
receipt against the raw log and answers `verifier-right` or `false-flag` with the proof — false flags become
verifier rules; the rest becomes the precision record. The card runs on the pinned review model
(`kanban create --model`) when one is set, else on the profile's own.

## Repo map

```
claimcheck/          the library + CLI (hook, init, receipt, page, verify, sign, capture, store, engine → claimcheck-core)
hermes_plugin/       Hermes Agent plugin (native hooks) + review bridge
hooks/, bin/, .claude-plugin/   Claude Code plugin (hooks.json → bin/claimcheck-hook shim)
plugin.json, skills/            Agent Plugins 1.0 manifest + Agent Skills (claimcheck-setup, claimcheck-review)
spec/                receipt spec v0.1
tests/               document/signature, plugin, hook (6 agents' payloads), installer, bridge, ledger-only fallback
```

License: Apache-2.0.
