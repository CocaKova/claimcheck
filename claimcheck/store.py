"""The local receipt store: ~/.claimcheck/receipts/<session>/<turn>.json, newest first."""
from __future__ import annotations

import json
import os
import re
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
    try:
        from . import context
        L["context"] = context.load(doc["run"]["session_id"], doc["run"].get("turn_id"))
    except Exception:  # a store without context still rechecks against the log
        L["context"] = []
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


# ---------- leads: where a flagged literal does appear, so a reviewer reads instead of hunting ----------

LEAD_SPAN = 90
LEADS_PER_LITERAL = 3


def _flag_literals(c: dict) -> list[str]:
    """The literals the verifier could not place: backticked in its evidence line, else the claim's targets."""
    ev = c.get("evidence") or ""
    lits = [x for x in re.findall(r"`([^`]{2,200})`", ev)]
    if not lits:
        lits = [str(t) for t in c.get("targets") or []]
    out = []
    for x in lits:
        x = re.sub(r"(…|\.{2,3})$", "", x.strip().strip("'\""))
        if len(x) >= 2 and x.lower() not in (o.lower() for o in out):
            out.append(x)
    return out[:6]


def _lit_rx(lit: str):
    body = r"[^\s'\"`]*".join(re.escape(x) for x in lit.lower().split("*"))
    if lit.startswith("~/"):
        body = "(?:~|" + re.escape(str(Path.home()).lower()) + ")" + body[1:]
    return re.compile(body)


def _snip(text: str, m: re.Match) -> str:
    a, b = max(0, m.start() - LEAD_SPAN), min(len(text), m.end() + LEAD_SPAN)
    return ("…" if a else "") + re.sub(r"\s+", " ", text[a:b]) + ("…" if b < len(text) else "")


def _sources(doc: dict):
    """(label, text) in order of evidential weight. Every source is local and already redacted."""
    from .capture import RunLog
    sid, tid = doc["run"]["session_id"], doc["run"].get("turn_id")
    sids = [sid]
    from .ledger import DB, redact
    rows = []
    if DB.exists():
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)
            try:
                sids += [r[0] for r in con.execute("select id from sessions where parent_session_id=?", (sid,))]
            except sqlite3.Error:
                pass
            rows = con.execute("select role, content from messages where session_id=? and role in ('user','assistant') "
                               "and content is not null order by id", (sid,)).fetchall()
            con.close()
        except sqlite3.Error:
            rows = []
    for n, s in enumerate(sids):
        for r in RunLog(s).records():
            where = "this turn" if r.get("turn_id") == tid else "earlier turn"
            label = f"run log ({'sub-agent ' + s[-12:] + ', ' if n else ''}{where}) {r.get('tool')}"
            yield label, json.dumps(r.get("args"), ensure_ascii=False) + " → " + str(r.get("result"))
    try:
        from . import context
        for t in context.load(sid):
            yield "context it was given (recalled, not observed)", t
    except Exception:
        pass
    report = re.sub(r"\s+", " ", ((doc.get("report") or {}).get("text") or ""))[:200]
    for role, content in rows:
        if role == "assistant" and report and report in re.sub(r"\s+", " ", redact(content)):
            continue    # the report under review is not a lead for itself
        yield ("request/user message (given)" if role == "user" else "the agent's OWN earlier words (not evidence)"), redact(content)


def leads(doc: dict) -> dict[int, list[str]]:
    """For each flagged claim (by index in doc['claims']): where each unplaced literal appears, with a snippet,
    across the session's run log (every turn), sub-agent logs, the context it was given and the transcript.
    One pass over the sources per receipt, however many claims and literals."""
    want = {i: _flag_literals(c) for i, c in enumerate(doc.get("claims", [])) if c.get("verdict") in FLAGS}
    rx = {lit: _lit_rx(lit) for lits in want.values() for lit in lits}
    hits: dict[str, list[str]] = {lit: [] for lit in rx}
    try:
        for label, text in _sources(doc):
            low = (text or "").lower()
            for lit, r in rx.items():
                if len(hits[lit]) >= LEADS_PER_LITERAL:
                    continue
                m = r.search(low)
                if m:
                    hits[lit].append(f"{label}: {_snip(text, m)}")
            if all(len(v) >= LEADS_PER_LITERAL for v in hits.values()):
                break
    except Exception as e:  # leads are a convenience; the digest stands without them
        return {i: [f"(leads unavailable: {e.__class__.__name__})"] for i in want}
    out = {}
    for i, lits in want.items():
        lines = []
        for lit in lits:
            lines += [f"`{lit}` → {h}" for h in hits[lit]] or [f"`{lit}` → no trace in the run log, sub-agents, context or transcript"]
        out[i] = lines
    return out


def digest(doc: dict, path: Path | None = None, with_leads: bool = False) -> str:
    """Three lines a phone can read, then the flagged claims with their evidence (and, for a reviewer, leads)."""
    r, s = doc["run"], doc["summary"]
    lines = [
        f"receipt {doc['id']} · {r['agent'].get('platform', '?')} · session {r['session_id']}"
        + (f" · turn {r['turn_id'].rsplit(':', 1)[-1]}" if r.get('turn_id') else ""),
        f"asked: {(r.get('asked') or '')[:160].replace(chr(10), ' ')}",
        f"verdicts: {s['verified']} verified · {s['unverified']} unverified · {s['pre_existing']} pre-existing · "
        f"{s['contradicted']} contradicted · {s['unchecked']} unchecked → {s['headline']}",
    ]
    L = leads(doc) if with_leads else {}
    for i, c in enumerate(doc.get("claims", [])):
        if c.get("verdict") not in FLAGS:
            continue
        lines.append(f"- [{c['verdict']}] {c['text'][:200]}\n    evidence: {c['evidence'][:300]}")
        for lead in L.get(i, []):
            lines.append(f"    lead: {lead[:400]}")
    if path:
        lines.append(f"file: {path}  (page: {path.with_suffix('.html')})")
    return "\n".join(lines)
