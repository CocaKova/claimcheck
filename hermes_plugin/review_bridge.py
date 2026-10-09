#!/usr/bin/env python3
"""claimcheck review bridge — a no-agent Hermes cron. Review is calibration, so it is sampled, not per item.

Each run looks at receipts written since the last run and opens AT MOST ONE kanban card (a digest of
up to CLAIMCHECK_REVIEW_MAX items) for the agent's own profile, skill `claimcheck-review` force-loaded,
The Office subscribed to its end. A card is opened only when there is something an agent should look at:
any `contradicted` or `pre-existing` receipt, or at least CLAIMCHECK_REVIEW_UNVERIFIED_MIN `unverified`
ones. Only receipts from the platforms in CLAIMCHECK_REVIEW_PLATFORMS (default: hermes — the agent reviews
its own runs; chat sessions of other agents are listed by `claimcheck flagged`, never carded). Silent when
there is nothing. Dedup: a state file plus the kanban idempotency key. Each flagged receipt is re-verified
against its run log first (`store.recheck`): a flag that a rules fix made after the receipt already clears
is never carded.

    hermes cron create "40 6 * * *" --name "Claimcheck review digest" \
        --script claimcheck-review-bridge.py --no-agent --deliver local

A card is sized by work, not by receipt count: at most CLAIMCHECK_REVIEW_MAX_CLAIMS flagged claims (a receipt
is never split), and its runtime grows with them (CLAIMCHECK_REVIEW_MINUTES_PER_CLAIM, base 10 min, cap 60).
Each flagged claim carries leads — where its literals do appear across the session's run log, sub-agent logs,
the context the agent was given and the transcript — so the reviewer reads and judges instead of searching.

Env: CLAIMCHECK_REVIEW_ASSIGNEE (default `default`), CLAIMCHECK_REVIEW_CHAT (Matrix room id; empty = no
subscription), CLAIMCHECK_REVIEW_MAX (receipts per card, default 5), CLAIMCHECK_REVIEW_MAX_CLAIMS (default 8),
CLAIMCHECK_REVIEW_MINUTES_PER_CLAIM (default 3), CLAIMCHECK_REVIEW_PLATFORMS (comma list, default `hermes`;
`*` = all), CLAIMCHECK_REVIEW_UNVERIFIED_MIN (default 5; `off` = only contradicted / pre-existing are ever
carded: for an agent whose unverified flags are almost all false alarms).
Receipts of the review runs themselves are skipped (their request carries the marker below).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import claimcheck  # noqa: F401
except ImportError:  # <repo>/hermes_plugin/ or <site-packages>/claimcheck/hermes/: the package is one or two levels up
    for _p in Path(__file__).resolve().parents[1:3]:
        if (_p / "claimcheck" / "__init__.py").exists():
            sys.path.insert(0, str(_p))
            break
from claimcheck.store import HOME, digest, flagged_claims, is_flagged, iter_receipts, recheck  # noqa: E402

MARKER = "[claimcheck-review]"
STATE = HOME / "review-bridge.json"
HERMES = os.environ.get("HERMES_BIN", str(Path.home() / ".local/bin/hermes"))
ASSIGNEE = os.environ.get("CLAIMCHECK_REVIEW_ASSIGNEE", "default")
CHAT = os.environ.get("CLAIMCHECK_REVIEW_CHAT", "")  # e.g. a Matrix room id; empty = no subscription
MAX = int(os.environ.get("CLAIMCHECK_REVIEW_MAX", "5"))
PLATFORMS = [x.strip() for x in os.environ.get("CLAIMCHECK_REVIEW_PLATFORMS", "hermes").split(",") if x.strip()]
_umin = os.environ.get("CLAIMCHECK_REVIEW_UNVERIFIED_MIN", "5").strip().lower()
WEAK = _umin not in ("off", "0", "none", "")
UNVERIFIED_MIN = int(_umin) if WEAK else 0
MAX_CLAIMS = int(os.environ.get("CLAIMCHECK_REVIEW_MAX_CLAIMS", "8"))
MIN_PER_CLAIM = float(os.environ.get("CLAIMCHECK_REVIEW_MINUTES_PER_CLAIM", "3"))
STRONG = ("contradicted", "pre-existing")

BODY = """{marker} tier: auto-ship

{n} receipt(s) of your own runs have a claim the verifier could not confirm. For each one decide whether
the verifier is right (the report said something the log does not show) or wrong (a false flag), and
leave the proof. Load skill `claimcheck-review` and follow it; reading only, about {per} minutes per claim
({claims} claims, {minutes} minutes in all). Each claim lists `lead:` lines — where its literals DO appear
(run log, sub-agents, the context the agent was given, the transcript) or that they appear nowhere. Start
from the leads; open the raw record only when a lead is ambiguous.

{digest}

Save as you go: after each receipt, post ONE `kanban_comment` with its blocks, so a timeout keeps the work.
On a retry, read the card's comments first and skip receipts already answered. Block shape:
receipt: <id>
claim: <the flagged sentence, shortened>
verdict: verifier-right | false-flag | undecided
why: <one sentence pointing at the log line / file / command, or what you could not settle>
rule: <only for false-flag: what the verifier should have matched>

Then `kanban_complete` with a one-line summary (no need to repeat the blocks).
"""


def review_model() -> str | None:
    """The model the person pinned for reviews (`claimcheck config review.model`), else the profile's own.

    Hermes runs the review card on its profile model unless told otherwise; a pinned model rides on
    `kanban create --model` so a review never has to run on the biggest brain in the house."""
    try:
        from claimcheck import config
        if config.effective_agent() not in (None, "hermes"):
            return None   # pinned to a local endpoint or another CLI: `claimcheck review` handles it, the card stays on the profile model
        return config.get("review.model")
    except Exception:  # pragma: no cover - old claimcheck without config
        return os.environ.get("CLAIMCHECK_REVIEW_MODEL")


def _budget(items: list) -> list:
    """Strong flags first, then weak; whole receipts until MAX receipts or MAX_CLAIMS claims (always at least one)."""
    out, n = [], 0
    for path, doc in items:
        k = len(flagged_claims(doc))
        if out and (len(out) >= MAX or n + k > MAX_CLAIMS):
            continue
        out.append((path, doc))
        n += k
    return out


def runtime_minutes(claims: int) -> int:
    return int(min(60, max(15, 10 + MIN_PER_CLAIM * claims)))


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([HERMES, *args], capture_output=True, text=True, timeout=60)


def _adapter(doc: dict) -> str:
    return (doc.get("run", {}).get("adapter") or doc.get("run", {}).get("agent", {}).get("platform") or "?")


def main() -> int:
    state = json.loads(STATE.read_text()) if STATE.exists() else {"seen": [], "cards": {}}
    seen = set(state["seen"])
    strong, weak, other, cleared = [], [], 0, 0
    for path, doc in iter_receipts():
        rid = doc.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        if not is_flagged(doc) or MARKER in (doc.get("run", {}).get("asked") or ""):
            continue
        if PLATFORMS != ["*"] and _adapter(doc) not in PLATFORMS:
            other += 1
            continue
        doc = recheck(doc)   # stored verdicts predate any later rules fix; review only what today's rules still flag
        if not is_flagged(doc):
            cleared += 1
            continue
        (strong if doc["summary"]["headline"] in STRONG else weak).append((path, doc))
    if not WEAK:
        weak = []   # unverified flags stay on the receipts (`claimcheck flagged`); they are never carded
    picked = _budget(strong + weak) if (strong or (weak and len(weak) >= UNVERIFIED_MIN)) else []
    if picked:
        ids = [d["id"] for _, d in picked]
        key = "receipts-" + hashlib.sha256("".join(ids).encode()).hexdigest()[:16]
        heads = sorted({d["summary"]["headline"] for _, d in picked})
        title = f"Receipt review: {len(picked)} flagged ({', '.join(heads)})"
        claims = sum(len(flagged_claims(d)) for _, d in picked)
        minutes = runtime_minutes(claims)
        body = BODY.format(marker=MARKER, n=len(picked), claims=claims, minutes=minutes, per=f"{MIN_PER_CLAIM:g}",
                           digest="\n\n".join(digest(d, p, with_leads=True) for p, d in picked))
        model = review_model()
        r = subprocess.run([HERMES, "kanban", "create", title, "--assignee", ASSIGNEE, "--body-file", "-",
                            "--idempotency-key", key, "--skill", "claimcheck-review",
                            "--max-runtime", f"{minutes}m", "--created-by", "claimcheck", "--json",
                            *(["--model", model] if model else [])],
                           input=body, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"kanban create failed: {(r.stdout + r.stderr)[-300:]}", file=sys.stderr)
            for rid in ids:
                seen.discard(rid)  # try again next run
        else:
            try:
                tid = json.loads(r.stdout).get("id")
            except ValueError:
                tid = (r.stdout.strip().split() or [""])[-1]
            if tid and CHAT:
                _run("kanban", "notify-subscribe", str(tid), "--platform", "matrix", "--chat-id", CHAT)
            state["cards"][key] = {"task": tid, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "receipts": ids}
            print(f"review card {tid}: {len(picked)} receipt(s) — {', '.join(heads)}"
                  + (f" ({cleared} more cleared by today's rules)" if cleared else ""))
        # items beyond this card's budget stay unseen for the next digest
        chosen = {d["id"] for _, d in picked}
        for _, d in strong + weak:
            if d["id"] not in chosen:
                seen.discard(d["id"])
    state["seen"] = sorted(seen)[-5000:]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
