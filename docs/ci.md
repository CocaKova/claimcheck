# Receipts on pull requests, and a merge gate

`claimcheck ci check` reads the receipts committed in a pull request, checks each one offline, posts one sticky
comment that sums them up, and exits non-zero when the merge rules say no. All the logic lives in the CLI, so the
same check runs in GitHub Actions, GitLab CI, any other CI, or a pre-push hook on your own machine. The GitHub
Action in this repo is a thin wrapper around it.

## What it checks

For every receipt file added or changed under `.claimcheck/receipts/` in the pull request:

- **the content id**: the receipt wasn't edited after it was made;
- **the ed25519 signature**: unsigned receipts fail;
- **the signer**, if your repo pins keys in `.claimcheck/trusted-keys`. CI reads that file **from the base branch**,
  so a pull request can't add its own key and approve itself;
- **the verdicts** inside it (verified, unverified, pre-existing, contradicted, unchecked). Those were decided on
  the machine that ran the agent, where the run log lives. CI can't re-run them without the log; it proves the
  receipt you're reading is the one that was signed there.

It never runs anything from the pull request. Receipts are read as data (git blobs, or the GitHub API).

The comment shows each receipt's claim counts, its signer, the flagged claims with their evidence, and why the gate
failed if it did. It is **one comment, updated in place**. A re-run with nothing new doesn't touch it.

## Getting receipts into a pull request

On the machine where the agent worked (claimcheck already writes a receipt per run):

```sh
claimcheck ci attach            # receipts made since this branch started that wrote files this branch changes
claimcheck ci attach rcpt_…     # or name them
git add .claimcheck/receipts && git commit -m "receipts for this change"
```

If `.gitignore` has a bare `receipts/` rule it hides `.claimcheck/receipts/` too; `attach` warns about it. Anchor
the rule (`/receipts/`) or add `!.claimcheck/receipts/`.

`attach` writes at `--privacy summary` by default: the agent's report and claims, file names and the first word of
each command, never full commands or file contents. Use `--privacy hashes` for a public repo where even the report
text shouldn't be published. A commit can also name a receipt shared on claimcheck.cc with a trailer
(`Claimcheck-Receipt: https://claimcheck.cc/r/…`); those are fetched, so they need claimcheck.cc to answer.

To pin your keys (recommended for teams): run `claimcheck ci trust` on each machine whose agents' receipts you
accept, and merge `.claimcheck/trusted-keys` to the base branch.

## The merge rules

| Option | Default | Meaning |
|---|---|---|
| `--fail-on` | `contradicted` | Verdicts that fail the gate: `contradicted`, `pre-existing`, `unverified`, a comma list, `flagged` (all three) or `none` |
| `--require-receipts` | `never` | `ai`: fail when a commit says an AI agent co-wrote it (`Co-Authored-By: Claude …`, Codex, Copilot, Cursor, Gemini, Aider…) and the PR has no receipt. `always`: every PR needs one |
| `--require-witness` | off | Team: fail unless claimcheck.cc witnessed each run live |
| `--on-outage` | `open` | What a GitHub or claimcheck.cc outage does (below) |

Always failing, whatever the options: a receipt that was edited, isn't signed, isn't a receipt, or (with pinned
keys) was signed by a key the base branch doesn't list; and a receipt claimcheck.cc saw **rewritten** after the run.

Exit codes: `0` pass, `1` the gate failed, `3` a service didn't answer and `--on-outage closed` is set, `2` usage.

## Outages: fail open by default, and never silently

The verdict never depends on the network. Everything the gate decides comes from the receipts in the repo, checked
offline, so an outage can't let a contradicted or edited receipt through, and can't fail a clean one.

What an outage can cost is the comment, the commit status, or (Team) the page links and witness state. With the
default `--on-outage open` the check keeps its offline verdict and says what it couldn't do: a warning annotation
on the run, a note in the job summary, and a line in the comment if GitHub was up to take it. Failing closed by
default would let any GitHub or claimcheck.cc hiccup block every merge in your repo, with no gain in safety.

Use `--on-outage closed` when the comment or the witness state is itself a record you must keep (for example with
`--require-witness`): an outage then fails the check with exit `3` and a message that says it was an outage, so
nobody mistakes it for a flagged claim.

Every request to GitHub or claimcheck.cc is retried once before it counts as an outage.

If GitHub Actions itself is down, a required check never reports and GitHub holds the merge. The same check runs
before you push (below) and on any other CI, so you're never waiting on one vendor to know the answer.

## GitHub Actions

`.github/workflows/claimcheck.yml`:

```yaml
name: claimcheck
on: pull_request            # never pull_request_target: this job checks out the PR's code

permissions:
  contents: read
  pull-requests: write      # the sticky comment; GitHub makes it read-only on fork PRs (see below)

jobs:
  receipts:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0            # the check needs the PR's commits and its base
          persist-credentials: false
      - uses: CocaKova/claimcheck@v0.3.0   # pin a release, or a full commit SHA
        with:
          require-receipts: ai
          # fail-on: contradicted
          # on-outage: open
          # api-key: ${{ secrets.CLAIMCHECK_API_KEY }}   # Team only: page links + witness
```

Make **claimcheck / receipts** a required status check in the branch protection rules to turn it into a merge
gate. The free path needs no secrets.

The action doesn't `pip install` anything: it runs claimcheck from the action's own files with the runner's
Python, so PyPI being down can't break your merge gate.

### Fork pull requests

On a pull request from a fork, GitHub gives the job a read-only token and no secrets. The check still runs and
still gates the merge; it just can't post the comment, and says so in the log. To get the comment on fork PRs too,
add a second workflow. It runs in your repository's context **without checking out the pull request**: it reads
the receipts through the API as data.

`.github/workflows/claimcheck-comment.yml`:

```yaml
name: claimcheck comment
on:
  workflow_run:
    workflows: [claimcheck]
    types: [completed]

permissions:
  contents: read
  pull-requests: write

jobs:
  comment:
    if: github.event.workflow_run.event == 'pull_request'
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: CocaKova/claimcheck@v0.3.0
        with:
          mode: api                 # no checkout: the PR is read through the API
          require-receipts: ai      # keep the same rules as the gate, so the comment matches it
```

It finds the pull request by the exact commit that was checked. On a same-repo PR both workflows render the same
comment, so the second one changes nothing.

A note for reviewers: on `pull_request`, GitHub runs the workflow file from the pull request itself, so a PR that
edits `.github/workflows/` can change its own checks. That's true of every check, not just this one; review changes
to `.github/` and `.claimcheck/` like code.

## GitLab CI

```yaml
claimcheck:
  image: python:3.12                    # has git
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  variables:
    GIT_DEPTH: 0
  script:
    - pip install claimcheck-receipts
    - claimcheck ci check --base "$CI_MERGE_REQUEST_DIFF_BASE_SHA" --require-receipts ai --comment
```

The job's exit code is the gate ("Pipelines must succeed"). The comment needs a masked CI variable `GITLAB_TOKEN`:
a project access token with `api` scope (GitLab's job token can't write notes). Without it the check still gates
and prints the summary. Merge requests from forks don't get your variables; there the job gates without commenting.

## Any other CI, or no package install

```sh
claimcheck ci check --base origin/main --require-receipts ai --markdown claimcheck.md --json claimcheck.json
```

Without installing: `git clone --depth 1 https://github.com/CocaKova/claimcheck /tmp/cc && PYTHONPATH=/tmp/cc
python3 -m claimcheck.cli ci check --base origin/main`. The check needs only the Python standard library.

## Before you push

`.git/hooks/pre-push` (make it executable):

```sh
#!/bin/sh
exec claimcheck ci check --base origin/main --require-receipts ai
```

The same rules as CI, on your machine, before anything leaves it.

## Free and Team

**Free, in any CI:** everything above that runs offline: the sticky comment with claim counts and flagged claims,
the merge gate on verdicts and on edited, unsigned or unpinned receipts, receipts required for AI commits, pinned
keys, GitHub and GitLab comments, the pre-push hook. It's open source and needs no account.

**Team (claimcheck.cc):** the hosted layer on top. A repo key (`claimcheck ci key create owner/repo`, stored as the
`CLAIMCHECK_API_KEY` secret) shares each receipt in the pull request, so the comment links a page anyone on the team
can open on a phone, and shows whether claimcheck.cc witnessed the run live. `--require-witness` turns that into a
rule, and a log rewritten after the run fails the gate. A repo key can only share receipts: it can't read the
account's history, delete anything or send fingerprints, so a leaked CI secret is a small problem. For the witness
to match, the agents' machines log in to the same claimcheck.cc account (`claimcheck login`).
