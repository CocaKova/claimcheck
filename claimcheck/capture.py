"""Live capture: an append-only, hash-chained run log written by an adapter while the agent works.

One file per session, `~/.claimcheck/runs/<session_id>.jsonl`, one event per line. Each event carries
the hash of the previous one; the receipt records the chain head and the event count, so anyone with
the log can recompute the head and anyone with only the receipt can tell which run it refers to.
Events are redacted before they are hashed, so re-verification never needs the raw secret.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from .document import canon, iso, parse_iso, sha256
from .ledger import event_from_hook, redact

HOME = Path(os.environ.get("CLAIMCHECK_HOME", Path.home() / ".claimcheck"))
RUNS = HOME / "runs"
GENESIS = "0" * 64
MAX_RESULT = 256 * 1024  # bytes of tool result kept per event; the platform's own store truncates far earlier

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@contextlib.contextmanager
def _lock(session_id: str, path: Path):
    """One writer per session log. Threads (Hermes runs parallel tool calls in-process) share the threading lock;
    processes (Claude Code fires one hook process per parallel tool call) share an flock on a sidecar file."""
    with _locks_guard:
        tl = _locks.setdefault(session_id, threading.Lock())
    with tl:
        if fcntl is None:  # pragma: no cover - Windows: threads only
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def _safe_id(session_id: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(session_id))[:200] or "unknown"


def event_hash(ev: dict) -> str:
    return sha256(canon({k: v for k, v in ev.items() if k != "hash"}))


class RunLog:
    """The hash-chained event log of one session."""

    def __init__(self, session_id: str, root: Path | None = None):
        self.session_id = str(session_id)
        self.path = (root or RUNS) / f"{_safe_id(self.session_id)}.jsonl"

    # ---- writing ----

    def append(self, *, tool: str, args, result, ts: float, turn_id: str | None = None,
               tool_call_id: str | None = None, status: str | None = None, duration_ms: int | None = None,
               agent_id: str | None = None, exit_code=None) -> dict:
        """Append one event; returns the stored record (with `i`, `prev`, `hash`)."""
        if not isinstance(args, dict):
            args = {"_raw": str(args)}
        content = result if isinstance(result, str) else (json.dumps(result, ensure_ascii=False) if result is not None else "")
        truncated = False
        if len(content.encode("utf-8", "replace")) > MAX_RESULT:
            content, truncated = content[:MAX_RESULT], True
        rec = {
            "tool": str(tool)[:256],
            "args": json.loads(redact(json.dumps(args, ensure_ascii=False, default=str))),
            "result": redact(content),
            "ts": iso(ts),
        }
        for k, v in (("turn_id", turn_id), ("tool_call_id", tool_call_id), ("status", status),
                     ("duration_ms", None if duration_ms is None else int(duration_ms)),
                     ("agent_id", agent_id), ("exit_code", exit_code), ("truncated", truncated or None)):
            if v is not None:
                rec[k] = v
        with _lock(self.session_id, self.path):
            i, prev = self._tail()
            rec["i"], rec["prev"] = i, prev
            rec["hash"] = event_hash(rec)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        return rec

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS
        with open(self.path, "rb") as f:
            f.seek(max(0, self.path.stat().st_size - 1_048_576))
            last = f.read().splitlines()[-1]
        rec = json.loads(last)
        return rec["i"] + 1, rec["hash"]

    # ---- reading ----

    def exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def head(self) -> str:
        i, prev = self._tail()
        return prev

    def events(self, turn_id: str | None = None) -> list[dict]:
        """Ledger events (see ledger.py) rebuilt from the records, optionally one turn only."""
        evs = []
        for r in self.records():
            if turn_id is not None and r.get("turn_id") != turn_id:
                continue
            e = event_from_hook(r["tool"], r["args"], r["result"], parse_iso(r["ts"]).timestamp(),
                                turn_id=r.get("turn_id"), tool_call_id=r.get("tool_call_id"),
                                status=r.get("status"), agent_id=r.get("agent_id"), chain_i=r["i"], hash=r["hash"])
            if e["exit_code"] is None and r.get("exit_code") is not None:
                e["exit_code"] = r["exit_code"]
            evs.append(e)
        return evs


def verify_chain(records: list[dict]) -> tuple[bool, str]:
    """Recompute every hash and link; (ok, head or reason)."""
    prev = GENESIS
    for n, r in enumerate(records):
        if r.get("i") != n:
            return False, f"event {n}: index {r.get('i')}"
        if r.get("prev") != prev:
            return False, f"event {n}: prev link broken"
        if event_hash(r) != r.get("hash"):
            return False, f"event {n}: hash mismatch (edited)"
        prev = r["hash"]
    return True, prev


def turn_stem(turn_id) -> str:
    """File stem for one turn's receipt: Hermes `sid:uuid:short` → short; a bare UUID → its first 8 hex; else a timestamp."""
    import time
    if not turn_id:
        return time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    last = str(turn_id).rsplit(":", 1)[-1]
    return _safe_id(last[:8] if len(last) > 16 and last.count("-") >= 4 else last)[:32]
