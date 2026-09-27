---
name: claimcheck-setup
description: Set up, check or remove claimcheck (receipts for agent runs) on this machine for the person you are helping. Use when they say "set up claimcheck", "install claimcheck", "turn on receipts", "is claimcheck working", "show me my last receipt", or "remove claimcheck". Everything is one command and explained back in plain words; no editing of settings files by hand.
license: Apache-2.0
compatibility: Needs python3 (3.10+) and a shell. Works with Claude Code, Codex CLI, Gemini CLI, Cursor and Hermes Agent.
metadata:
  author: CocaKova
  homepage: https://claimcheck.cc
---

# claimcheck setup

claimcheck writes a receipt after every turn an agent takes: the tool calls it made (captured live by the
agent's own hooks into a tamper-evident log) and every claim in its final message, each marked verified,
unverified, pre-existing, contradicted or unchecked by code. Receipts are signed with a key made on this
machine and stay on this machine.

You are doing this *for* the person. Run the commands yourself, then tell them what happened in one or two
plain sentences. Never ask them to edit JSON.

## Set up

1. Check whether the command exists: `claimcheck --version`.
2. If it does not, install it with the first of these that works, in order:
   - `uv tool install claimcheck-receipts`
   - `pipx install claimcheck-receipts`
   - `python3 -m pip install --user claimcheck-receipts`
   (If you are running inside a claimcheck plugin folder — there is a `bin/claimcheck-hook` next to this
   skill's parent folder — you may instead use that folder directly: `python3 -m claimcheck.cli` with
   `PYTHONPATH` set to the folder. Say so if you do.)
3. **Ask the person which model should review receipts. This step is required; do not skip it and do not
   pick for them.** A review is a reading task (open the run log, find the line that settles a flagged
   claim), so a small model is enough — and on a metered plan their agent's *default* model is often the
   most expensive one. First run `claimcheck local`: it lists local model servers answering on this
   machine (vLLM, Ollama, LM Studio, llama.cpp, Jan, LiteLLM). Then ask, in one sentence each:
   - if something local answered: "You have <model> running at <url>; reviewing with it costs nothing.
     Pin that, or a cloud model?" Lead with the free option.
   - otherwise: suggest the smallest model on their plan (on Claude Code, `haiku`).
   If they say "whatever you are" or name the big one, that is their call — pin it. If they want to decide
   later, say reviews stay off until they do, and continue.
4. Run `claimcheck init --review-model <their answer>`; for a local model add `--review-endpoint <url>`
   (then reviews are one HTTP call to that server, no agent CLI in the loop); for a cloud model add
   `--review-agent <claude-code|codex|gemini|hermes>` when more than one agent is wired and they said
   which should do the reviewing. Plain `claimcheck init` if they deferred. It finds every supported agent
   in the home folder, wires two hook lines into each one's own settings file (backing the file up first),
   pins the review model, and prints one line per agent plus the review-model line. Later changes:
   `claimcheck config review.model <model>`, `claimcheck config review.endpoint <url>`; a server that
   wants a key: `claimcheck config review.api_key <key>` (stored 0600, never printed back in full).
5. Run `claimcheck doctor` and read it back to the person: which agents are wired, where receipts go,
   which model reviews run on.
6. Tell them: "From your next turn on, every run gets a receipt. `claimcheck open` shows the latest;
   `claimcheck flagged` lists the ones worth a look; `claimcheck review` gets a second opinion on those,
   on <model> only." Do not promise anything about turns that already happened.

## Check

`claimcheck doctor` — wired agents, receipt count, flagged count, the last hook error if any, the review model.
`claimcheck flagged` — receipts whose headline is not "verified", newest first, with the evidence line.
`claimcheck review` — reviews the flagged receipts that have no review yet, on the pinned model only (it
refuses to run without one; `--dry-run` shows the exact command). `claimcheck config` shows the settings.
`claimcheck open` — opens the newest receipt page in the browser (`claimcheck open <receipt-id>` for a specific one).
`claimcheck verify <receipt.json>` — proves a receipt file was not edited (schema, content id, signature).

## Remove

`claimcheck init --remove` takes the hook lines back out of every agent's settings and leaves everything
else exactly as it was. Receipts already written stay in `~/.claimcheck/receipts/` until the person deletes
that folder.

## What to say when something is off

- "claimcheck: command not found" after install → the install folder is not on PATH; `claimcheck init` still
  works via `python3 -m claimcheck.cli init`, and it pins the full interpreter path into the hooks so PATH
  does not matter afterwards.
- No receipt appeared after a turn → the agent's hooks load when it starts; a turn in a session that was
  already open before `init` will not have one. Start a new session and try one tool-using turn.
- A settings file is not valid JSON → say which file `init` named, do not try to repair it silently.
