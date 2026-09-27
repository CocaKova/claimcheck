#!/usr/bin/env python3
"""claimcheck review bridge — a no-agent Hermes cron. Review is calibration, so it is sampled, not per item.

Each run looks at receipts written since the last run and opens AT MOST ONE kanban card (a digest of
up to CLAIMCHECK_REVIEW_MAX items) for the agent's own profile, skill `claimcheck-review` force-loaded,
The Office subscribed to its end. A card is opened only when there is something an agent should look at:
any `contradicted` or `pre-existing` receipt, or at least CLAIMCHECK_REVIEW_UNVERIFIED_MIN `unverified`
ones. Only receipts from the platforms in CLAIMCHECK_REVIEW_PLATFORMS (default: hermes — the agent reviews
its own runs; chat sessions of other agents are listed by `claimcheck flagged`, never carded). Silent when
there is nothing. Dedup: a state file plus the kanban idempotency key.

    hermes cron create "40 6 * * *" --name "Claimcheck review digest" \
        --script claimcheck-review-bridge.py --no-agent --deliver local

Env: CLAIMCHECK_REVIEW_ASSIGNEE (default `default`), CLAIMCHECK_REVIEW_CHAT (Matrix room id; empty = no
subscription), CLAIMCHECK_REVIEW_MAX (items per card, default 5), CLAIMCHECK_REVIEW_PLATFORMS
(comma list, default `hermes`; `*` = all), CLAIMCHECK_REVIEW_UNVERIFIED_MIN (default 5).
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
from claimcheck.store import HOME, digest, is_flagged, iter_receipts  # noqa: E402

MARKER = "[claimcheck-review]"
STATE = HOME / "review-bridge.json"
HERMES = os.environ.get("HERMES_BIN", str(Path.home() / ".local/bin/hermes"))
ASSIGNEE = os.environ.get("CLAIMCHECK_REVIEW_ASSIGNEE", "default")
CHAT = os.environ.get("CLAIMCHECK_REVIEW_CHAT", "")  # e.g. a Matrix room id; empty = no subscription
MAX = int(os.environ.get("CLAIMCHECK_REVIEW_MAX", "5"))
PLATFORMS = [x.strip() for x in os.environ.get("CLAIMCHECK_REVIEW_PLATFORMS", "hermes").split(",") if x.strip()]
UNVERIFIED_MIN = int(os.environ.get("CLAIMCHECK_REVIEW_UNVERIFIED_MIN", "5"))
STRONG = ("contradicted", "pre-existing")

BODY = """{marker} tier: auto-ship

{n} receipt(s) of your own runs have a claim the verifier could not confirm. For each one decide whether
the verifier is right (the report said something the log does not show) or wrong (a false flag), and
leave the proof. Load skill `claimcheck-review` and follow it; a few minutes per item, reading only.

{digest}

Finish with `kanban_complete` and ONE comment with a block per item in this exact shape:
receipt: <id>
verdict: verifier-right | false-flag
why: <one sentence pointing at the log line / file / command>
rule: <only for false-flag: what the verifier should have matched>
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


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([HERMES, *args], capture_output=True, text=True, timeout=60)


def _adapter(doc: dict) -> str:
    return (doc.get("run", {}).get("adapter") or doc.get("run", {}).get("agent", {}).get("platform") or "?")


def main() -> int:
    state = json.loads(STATE.read_text()) if STATE.exists() else {"seen": [], "cards": {}}
    seen = set(state["seen"])
    strong, weak, other = [], [], 0
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
        (strong if doc["summary"]["headline"] in STRONG else weak).append((path, doc))
    picked = (strong + weak)[:MAX] if (strong or len(weak) >= UNVERIFIED_MIN) else []
    if picked:
        ids = [d["id"] for _, d in picked]
        key = "receipts-" + hashlib.sha256("".join(ids).encode()).hexdigest()[:16]
        heads = sorted({d["summary"]["headline"] for _, d in picked})
        title = f"Receipt review: {len(picked)} flagged ({', '.join(heads)})"
        body = BODY.format(marker=MARKER, n=len(picked), digest="\n\n".join(digest(d, p) for p, d in picked))
        model = review_model()
        r = subprocess.run([HERMES, "kanban", "create", title, "--assignee", ASSIGNEE, "--body-file", "-",
                            "--idempotency-key", key, "--skill", "claimcheck-review",
                            "--max-runtime", "25m", "--created-by", "claimcheck", "--json",
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
            print(f"review card {tid}: {len(picked)} receipt(s) — {', '.join(heads)}")
        # items beyond MAX stay unseen for the next digest
        for _, d in (strong + weak)[MAX:]:
            seen.discard(d["id"])
    state["seen"] = sorted(seen)[-5000:]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
