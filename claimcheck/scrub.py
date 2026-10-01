"""`claimcheck scrub`: re-apply the current redaction rules to everything already on disk.

Redaction rules improve; run logs and receipts written under older rules may still hold a secret the new rules
catch. Scrub re-redacts every run-log record, rebuilds the hash chain (which also mends a chain broken by an
old concurrent-append bug), then brings each receipt along: redacted again, `capture.chain_head` moved to the
same event's new hash, `capture.redaction` set to the current rule-set, `capture.rescrubbed` stamped, new
content id, signed again with this machine's key, page re-rendered. A receipt that says `rescrubbed` was
changed after it was first signed, by its owner, for this reason only; `scrub.log` records each run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import capture
from .capture import GENESIS, RunLog, event_hash, verify_chain
from .document import REDACTION_ID, content_id, now_iso, sha256
from .ledger import redact


def _redact_tree(x, skip=("id", "signature", "hash", "prev", "sha256", "chain_head")):
    if isinstance(x, str):
        return redact(x)
    if isinstance(x, list):
        return [_redact_tree(v, skip) for v in x]
    if isinstance(x, dict):
        return {k: (v if k in skip else _redact_tree(v, skip)) for k, v in x.items()}
    return x


def _write(path: Path, text: str):
    tmp = path.with_name(path.name + ".scrub-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def scrub_log(log: RunLog, dry_run: bool = False) -> tuple[bool, dict[str, str], str]:
    """(rewritten?, old hash -> new hash, why). Rewrites when a record re-redacts differently or the chain is broken."""
    with capture._lock(log.session_id, log.path):
        recs = log.records()
        if not recs:
            return False, {}, ""
        chain_ok, _ = verify_chain(recs)
        new = []
        changed = False
        for r in recs:
            n = dict(r)
            n["args"] = json.loads(redact(json.dumps(r.get("args", {}), ensure_ascii=False)))
            n["result"] = redact(r.get("result", ""))
            changed |= n["args"] != r.get("args", {}) or n["result"] != r.get("result", "")
            new.append(n)
        if not changed and chain_ok:
            return False, {}, ""
        mapping, prev = {}, GENESIS
        for i, n in enumerate(new):
            old_hash = n.get("hash")
            n["i"], n["prev"] = i, prev
            n["hash"] = event_hash(n)
            if old_hash:
                mapping[old_hash] = n["hash"]
            prev = n["hash"]
        why = " + ".join(x for x in ("secrets" if changed else "", "" if chain_ok else "broken chain") if x)
        if not dry_run:
            _write(log.path, "".join(json.dumps(n, ensure_ascii=False, sort_keys=True) + "\n" for n in new))
        return True, mapping, why


def scrub_receipt(path: Path, mapping: dict[str, str], key=None, dry_run: bool = False) -> bool:
    d = json.loads(path.read_text())
    if "claimcheck" not in d or "capture" not in d:
        return False
    n = _redact_tree(d)
    head = n["capture"].get("chain_head")
    if head in mapping:
        n["capture"]["chain_head"] = mapping[head]
    if n == d:
        return False
    if isinstance(n.get("report"), dict) and "text" in n["report"]:
        n["report"]["sha256"] = sha256(n["report"]["text"])
    n["capture"]["redaction"] = REDACTION_ID
    n["capture"]["rescrubbed"] = now_iso()
    n.pop("signature", None)
    n["id"] = content_id(n)
    if d.get("signature"):
        from .sign import sign
        sign(n, key)
    if not dry_run:
        _write(path, json.dumps(n, indent=1, ensure_ascii=False))
        html = path.with_suffix(".html")
        try:
            from .page import render
            _write(html, render(n, None))
        except Exception:  # the JSON is the receipt; the page is a convenience
            pass
    return True


def scrub(home: Path, dry_run: bool = False) -> dict:
    runs, receipts = home / "runs", home / "receipts"
    key = None
    if (home / "key").exists():
        from .sign import load_key
        key = load_key(home / "key")
    out = {"logs": 0, "logs_rewritten": 0, "chains_mended": 0, "receipts": 0, "receipts_rewritten": 0, "asked_rewritten": 0}
    mappings: dict[str, dict[str, str]] = {}
    for p in sorted(runs.glob("*.jsonl")) if runs.exists() else []:
        out["logs"] += 1
        log = RunLog(p.stem, root=runs)
        rewrote, mapping, why = scrub_log(log, dry_run)
        if rewrote:
            out["logs_rewritten"] += 1
            out["chains_mended"] += "broken chain" in why
            mappings[p.stem] = mapping
    for p in sorted(runs.glob("*.asked")) if runs.exists() else []:
        txt = p.read_text()
        if redact(txt) != txt:
            out["asked_rewritten"] += 1
            if not dry_run:
                _write(p, redact(txt))
    for p in sorted(receipts.glob("*/*.json")) if receipts.exists() else []:
        out["receipts"] += 1
        if scrub_receipt(p, mappings.get(capture._safe_id(p.parent.name), {}), key, dry_run):
            out["receipts_rewritten"] += 1
    if not dry_run and (out["logs_rewritten"] or out["receipts_rewritten"] or out["asked_rewritten"]):
        with open(home / "scrub.log", "a") as f:
            f.write(json.dumps({"at": now_iso(), "redaction": REDACTION_ID, **out}) + "\n")
    return out
