"""The local receipt store: ~/.claimcheck/receipts/<session>/<turn>.json, newest first."""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

HOME = Path(os.environ.get("CLAIMCHECK_HOME", Path.home() / ".claimcheck"))
RECEIPTS = HOME / "receipts"
FLAGS = ("contradicted", "pre-existing", "unverified")


def iter_receipts(root: Path | None = None, newest_first: bool = True):
    """Yield (path, doc) for every receipt on disk; unreadable files are skipped."""
    root = root or RECEIPTS
    if not root.exists():
        return
    paths = sorted(root.glob("*/*.json"), key=lambda p: p.stat().st_mtime, reverse=newest_first)
    for p in paths:
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and "id" in d and "run" in d:   # receipts only, not sidecars (.shared)
            yield p, d


def is_flagged(doc: dict) -> bool:
    return doc.get("summary", {}).get("headline") in FLAGS


def flagged_claims(doc: dict) -> list[dict]:
    return [c for c in doc.get("claims", []) if c.get("verdict") in FLAGS]


def _request(doc: dict) -> str:
    """The whole request the run was given: the receipt keeps 2,000 chars, a cron prompt runs longer."""
    from .ledger import DB, redact
    sid, asked = doc["run"]["session_id"], doc["run"].get("asked") or ""
    full = ""
    if DB.exists():
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            rows = [r[0] for r in con.execute("select content from messages where session_id=? and role='user' "
                                               "and content is not null order by id", (sid,))]
            con.close()
            full = next((redact(r) for r in rows if redact(r)[:200] == asked[:200]), "")   # this turn's message
        except sqlite3.Error:
            pass
    if not full:
        from .hook import recall_prompt
        full = recall_prompt(sid)
    return full if len(full) > len(asked) else asked


def recheck(doc: dict) -> dict:
    """The receipt as today's rules see it, for triage only: the signed file on disk is never rewritten.

    Re-extracts and re-verifies against the run log, so a flag that a later rules fix already clears does
    not reach a reviewer again. Without the run log or the engine the stored verdicts stand."""
    from . import engine
    from .capture import RunLog
    from .document import VERDICTS
    from .ledger import ledger_from_events
    if not engine.AVAILABLE or not flagged_claims(doc):
        return doc
    events = RunLog(doc["run"]["session_id"]).events()
    if not events:
        return doc
    L = ledger_from_events(events)
    req = _request(doc)
    L["inputs"] = [req] if req else []
    report = (doc.get("report") or {}).get("text") or ""
    if report:   # the whole pipeline again: extraction fixes (proposals, restated tasks) count as much as verdict fixes
        claims = [{**c, "verdict": v, "evidence": ev} for c in engine.extract_claims(report)
                  for v, ev in [engine.verify(c, L)]]
    else:        # summary/hashes privacy keeps no report: re-verify the stored claims
        claims = [{**c, **dict(zip(("verdict", "evidence"), engine.verify(c, L)))} if c.get("verdict") in FLAGS
                  and c.get("targets") else c for c in doc["claims"]]
    counts = Counter(c["verdict"] for c in claims)
    headline = next((v for v in VERDICTS[:4] if counts[v]), "verified" if counts["verified"] else "unchecked")
    summary = {"verified": counts["verified"], "unverified": counts["unverified"], "pre_existing": counts["pre-existing"],
               "contradicted": counts["contradicted"], "unchecked": counts["unchecked"], "headline": headline}
    return {**doc, "claims": claims, "summary": summary}


def digest(doc: dict, path: Path | None = None) -> str:
    """Three lines a phone can read, then the flagged claims with their evidence."""
    r, s = doc["run"], doc["summary"]
    lines = [
        f"receipt {doc['id']} · {r['agent'].get('platform', '?')} · session {r['session_id']}"
        + (f" · turn {r['turn_id'].rsplit(':', 1)[-1]}" if r.get('turn_id') else ""),
        f"asked: {(r.get('asked') or '')[:160].replace(chr(10), ' ')}",
        f"verdicts: {s['verified']} verified · {s['unverified']} unverified · {s['pre_existing']} pre-existing · "
        f"{s['contradicted']} contradicted · {s['unchecked']} unchecked → {s['headline']}",
    ]
    for c in flagged_claims(doc):
        lines.append(f"- [{c['verdict']}] {c['text'][:200]}\n    evidence: {c['evidence'][:300]}")
    if path:
        lines.append(f"file: {path}  (page: {path.with_suffix('.html')})")
    return "\n".join(lines)
