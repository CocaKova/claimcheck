"""Context: what the agent was GIVEN, as opposed to what it did.

A report often states facts the run never looked up: a memory system recalled them into the prompt, the
system prompt carried them, or an earlier message said them. Without that text the verifier flags every
recalled fact as invented, whatever the memory setup is. Adapters hand over the blocks they can see
(Hermes: every API request's system prompt and user-side messages; hook platforms: the prompt, plus the
transcript's injected attachments) and the verifier treats them as evidence for facts, never for work.

Storage scales with what is new, not with session length or the number of turns:
  ~/.claimcheck/context/<aa>/<sha256>.txt.gz        one blob per distinct block, shared across sessions
  ~/.claimcheck/runs/<session>.context.jsonl        one line per block the session saw first: turn, source, sha
A system prompt sent on every request of every session is stored once; the conversation history that is
re-sent with each request adds only its newest message. Blocks are redacted before they are hashed.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from .capture import HOME, RunLog, _lock
from .ledger import redact

BLOBS = HOME / "context"
MAX_BLOCK = 2 * 1024 * 1024     # chars kept per block
MIN_BLOCK = 24                  # "ok", "continue": nothing a claim could rest on
MAX_LOAD = 8 * 1024 * 1024      # chars of context handed to the verifier per receipt, newest first

_seen: dict[str, set] = {}      # session → shas already indexed (skips disk work on every repeated request)
_seen_guard = threading.Lock()


def _index(session_id: str) -> Path:
    log = RunLog(session_id)
    return log.path.with_name(log.path.stem + ".context.jsonl")


def _blob(sha: str) -> Path:
    return BLOBS / sha[:2] / f"{sha}.txt.gz"


def _known(session_id: str) -> set:
    with _seen_guard:
        s = _seen.get(session_id)
        if s is None:
            s = {r["sha"] for r in _records(session_id)}
            if len(_seen) > 256:          # a long-lived gateway sees many sessions; keep the cache bounded
                _seen.pop(next(iter(_seen)))
            _seen[session_id] = s
        return s


def _records(session_id: str) -> list[dict]:
    p = _index(session_id)
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def add(session_id: str, turn_id: str | None, source: str, text: str) -> str | None:
    """Record one block the agent was given. Idempotent per session; returns the block's sha or None."""
    if not session_id or not isinstance(text, str):
        return None
    text = text.strip()
    if len(text) < MIN_BLOCK:
        return None
    text = redact(text[:MAX_BLOCK])
    sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    known = _known(session_id)
    if sha in known:
        return sha
    b = _blob(sha)
    if not b.exists():
        b.parent.mkdir(parents=True, exist_ok=True)
        tmp = b.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, b)
    idx = _index(session_id)
    with _lock(session_id + "#context", idx):
        if sha in known:
            return sha
        idx.parent.mkdir(parents=True, exist_ok=True)
        with open(idx, "a", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": turn_id, "source": str(source)[:64], "sha": sha, "chars": len(text),
                                "ts": round(time.time(), 3)}, sort_keys=True) + "\n")
        known.add(sha)
    return sha


def add_many(session_id: str, turn_id: str | None, blocks) -> int:
    """(source, text) pairs; returns how many were new to the session."""
    if not session_id:
        return 0
    known = _known(session_id)
    before = len(known)
    for source, text in blocks:
        add(session_id, turn_id, source, text)
    return len(known) - before


def load(session_id: str, turn_id: str | None = None) -> list[str]:
    """The blocks a session had been given up to and including `turn_id` (all of them when the turn is unknown),
    newest first, capped at MAX_LOAD characters so a months-long session never makes a receipt slow."""
    recs = _records(session_id)
    if turn_id is not None:
        last = max((i for i, r in enumerate(recs) if r.get("turn_id") == turn_id), default=None)
        if last is not None:
            recs = recs[:last + 1]
    out, total = [], 0
    for r in reversed(recs):
        try:
            with gzip.open(_blob(r["sha"]), "rt", encoding="utf-8") as f:
                t = f.read()
        except (OSError, KeyError, EOFError):
            continue
        if total + len(t) > MAX_LOAD and out:
            break
        out.append(t)
        total += len(t)
    return out


# ---------- adapters ----------

def _text_of(content) -> str:
    """Message content in any of the shapes providers use: a string, or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") in (None, "text", "input_text") and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "\n".join(parts)
    return ""


GIVEN_ROLES = ("system", "developer", "user")   # the agent's own words are never evidence for themselves


def blocks_from_messages(messages, system_prompt: str | None = None):
    """(source, text) for every block an LLM request carried that the agent did not write itself. Tool results
    are in the run log already; assistant text is the agent talking."""
    if system_prompt:
        yield "system", system_prompt if isinstance(system_prompt, str) else _text_of(system_prompt)
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in GIVEN_ROLES:
            continue
        yield role, _text_of(m.get("content"))


def blocks_from_transcript(path: str | os.PathLike, session_id: str):
    """(source, text) for the new part of a Claude Code style JSONL transcript: user prompts and the injected
    attachments (memory files, instructions, session context) in the exact text the model saw. Reads from
    where the last call stopped, so a long session costs only its newest lines."""
    p = Path(path)
    if not p.exists():
        return
    mark = _index(session_id).with_suffix(".offset")
    try:
        off = int(mark.read_text())
    except (OSError, ValueError):
        off = 0
    size = p.stat().st_size
    if off > size:          # transcript replaced (resume, compaction): read it again; blobs dedupe
        off = 0
    with open(p, "rb") as f:
        f.seek(off)
        data = f.read()
    end = data.rfind(b"\n") + 1      # never consume a half-written last line
    for raw in data[:end].splitlines():
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("type") == "attachment":
            for part in r.get("rendered") or []:
                t = part.get("content") if isinstance(part, dict) else part
                if isinstance(t, str):
                    yield "attachment:" + str((r.get("attachment") or {}).get("type", "?")), t
        elif r.get("type") == "user" and not r.get("isMeta"):
            c = (r.get("message") or {}).get("content")
            if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                continue    # tool output: the run log has it
            yield "user", _text_of(c)
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(str(off + end))
    except OSError:
        pass
