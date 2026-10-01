"""Build a receipt document (spec v0.1) from a session, its ledger and its verified claims.

Canonical form is JCS-style (sorted keys, no whitespace, UTF-8). Floats are refused so the bytes
are reproducible: money is a decimal string, times are RFC 3339 strings.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import SPEC_VERSION, __version__
from .engine import verifier_block
from .ledger import redact

VERDICTS = ["contradicted", "pre-existing", "unverified", "verified", "unchecked"]
REDACTION_ID = "claimcheck-redact-v2"
# The key lives in ~/.claimcheck/key, readable by the account the agent runs as. Say so in every receipt until
# custody moves somewhere the agent can't reach (a separate user, hardware, a hosted signer).
KEY_CUSTODY = "same-user"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(float(ts) * 1000) % 1000:03d}Z"


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def now_iso() -> str:
    return iso(datetime.now(tz=timezone.utc).timestamp())


def sha256(b: bytes | str) -> str:
    return hashlib.sha256(b.encode() if isinstance(b, str) else b).hexdigest()


def _no_floats(o, path="$"):
    if isinstance(o, float):
        raise TypeError(f"float at {path}: canonical receipts carry no floats")
    if isinstance(o, dict):
        for k, v in o.items():
            _no_floats(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            _no_floats(v, f"{path}[{i}]")


def canon(doc: dict) -> bytes:
    _no_floats(doc)
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _prune(o):
    """Drop None values so optional fields are absent rather than null."""
    if isinstance(o, dict):
        return {k: _prune(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [_prune(v) for v in o]
    return o


def build(session: dict, msgs: list[dict], L: dict, claims: list[dict], *, adapter: str = "hermes",
          platform: str = "hermes", classifier: str = "none", classifier_model: str | None = None,
          privacy: str = "full", capture: dict | None = None, turn_id: str | None = None) -> dict:
    """claims: [{text, kind, targets, verdict, evidence}] in report order."""
    final = redact(next((m["content"] for m in reversed(msgs) if m["role"] == "assistant" and m["content"]), "") or "")
    asked = redact(next((m["content"] for m in msgs if m["role"] == "user" and m["content"]), "") or "")
    start = session.get("started_at") or (msgs[0]["timestamp"] if msgs else 0)
    end = session.get("ended_at") or session.get("last_activity_at") or start
    counts = {v: sum(1 for c in claims if c["verdict"] == v) for v in VERDICTS}
    headline = next((v for v in VERDICTS[:4] if counts[v]), "verified" if counts["verified"] else "unchecked")
    remote_ids = {e["i"] for e in L["remote"]}
    usage = {
        "input_tokens": session.get("input_tokens") or 0,
        "output_tokens": session.get("output_tokens") or 0,
        "reasoning_tokens": session.get("reasoning_tokens") or 0,
        "cost_usd": f"{float(session['estimated_cost_usd']):.4f}" if session.get("estimated_cost_usd") else None,
    }
    doc = {
        "claimcheck": SPEC_VERSION,
        "created_at": now_iso(),
        "run": {
            "adapter": adapter,
            "adapter_version": __version__,
            "session_id": session["id"],
            "turn_id": turn_id,
            "agent": {"platform": platform, "model": session.get("model"), "profile": session.get("profile_name")},
            "started_at": iso(start),
            "ended_at": iso(end),
            "asked": asked.strip()[:2000] or None,
            "title": (session.get("title") or None),
        },
        "capture": {
            "mode": "at-rest", "events": len(L["events"]), "hash_alg": "sha256", "redaction": REDACTION_ID,
            "key_custody": KEY_CUSTODY, **(capture or {}),
        },
        "ledger": {
            "tool_calls": len(L["events"]),
            "by_tool": dict(L["by_tool"]),
            "commands": {
                "total": len(L["commands"]), "failed": len(L["failed"]), "remote": len(L["remote"]),
                "items": [{"i": e["i"], "text": e["command"][:4000], "exit_code": _int(e["exit_code"]),
                           "ts": iso(e["ts"]), "remote": True if e["i"] in remote_ids else None}
                          for e in L["commands"]],
            },
            "files": {"written": sorted(L["written"]), "read": sorted(L["read"])},
            "external": len(L["external"]),
            "usage": usage,
        },
        "report": {"text": final, "sha256": sha256(final)},
        "claims": [{"i": i, "text": c["text"][:1000], "kind": c["kind"], "targets": [str(t)[:300] for t in c["targets"]],
                    "verdict": c["verdict"], "evidence": c["evidence"][:2000]} for i, c in enumerate(claims)],
        "summary": {"verified": counts["verified"], "unverified": counts["unverified"], "pre_existing": counts["pre-existing"],
                    "contradicted": counts["contradicted"], "unchecked": counts["unchecked"], "headline": headline},
        "verifier": verifier_block(classifier, classifier_model),
        "privacy": "full",
    }
    doc = _prune(doc)
    doc = apply_privacy(doc, privacy)
    doc["id"] = content_id(doc)
    return doc


def _int(x):
    try:
        return None if x is None else int(x)
    except (TypeError, ValueError):
        return None


def apply_privacy(doc: dict, level: str) -> dict:
    """full: everything. summary: command first words + hashes, file basenames. hashes: no report text,
    no command items, hashed paths and claim texts."""
    if level not in ("full", "summary", "hashes"):
        raise ValueError(level)
    doc["privacy"] = level
    if level == "full":
        return doc
    cmds = doc["ledger"]["commands"]
    files = doc["ledger"]["files"]
    if level == "summary":
        for it in cmds.get("items", []):
            full = it["text"]
            it["sha256"] = sha256(full)
            it["text"] = (full.strip().split() or [""])[0][:60]
        files["written"] = sorted({Path(p).name or p for p in files["written"]})
        files["read"] = sorted({Path(p).name or p for p in files["read"]})
        return doc
    cmds.pop("items", None)
    files["written"] = sorted(sha256(p) for p in files["written"])
    files["read"] = sorted(sha256(p) for p in files["read"])
    doc["report"].pop("text", None)
    doc["run"].pop("asked", None)
    for c in doc["claims"]:
        c["text"] = sha256(c["text"])
        c["evidence"] = ""
    return doc


def content_id(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k not in ("id", "signature", "created_at")}
    return "rcpt_" + sha256(canon(body))[:24]


def check_id(doc: dict) -> bool:
    return doc.get("id") == content_id(doc)
