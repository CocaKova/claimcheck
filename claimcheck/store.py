"""The local receipt store: ~/.claimcheck/receipts/<session>/<turn>.json, newest first."""
from __future__ import annotations

import json
import os
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
