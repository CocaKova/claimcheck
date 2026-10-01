"""claimcheck.cc from this machine: witness a run as it happens, share receipts, list what you shared.

Off until `claimcheck login <key>`. What leaves the machine, and nothing else:
  witness  for each tool event: the session id, the event's index, its hash and the previous hash. Never content.
           claimcheck.cc keeps the first fingerprint it gets for each event, so a log rewritten later no longer
           matches, and a shared receipt says whether its log was witnessed live.
  share    a receipt you share (`claimcheck share`, or `cloud.share` = flagged | all), at the privacy level you
           choose (`cloud.privacy`, default summary: report and claims, command first words, file names).

Fingerprints queue in ~/.claimcheck/witness-outbox.jsonl in the same locked step that appends the event to the run
log, so the queue is always in chain order; a detached `python -m claimcheck.cloud flush` sends it, so a tool call
never waits on the network. Offline, the queue simply waits; those receipts then show as witnessed late.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .store import HOME

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

DEFAULT_URL = "https://api.claimcheck.cc"  # the API host; share links are claimcheck.cc/r/... (the site forwards those)
OUTBOX = HOME / "witness-outbox.jsonl"
PRIVACY_ORDER = ("full", "summary", "hashes")
_cache: dict = {"mtime": None, "cfg": {}}


def _cfg() -> dict:
    """Config, re-read only when the file changes (the Hermes gateway is a long-lived process)."""
    from . import config
    try:
        m = config.FILE.stat().st_mtime
    except OSError:
        m = None
    if m != _cache["mtime"]:
        _cache["mtime"], _cache["cfg"] = m, config.load() if m else {}
    return _cache["cfg"]


def setting(key: str):
    from . import config
    return config.get(key, _cfg())


def key() -> str | None:
    return setting("cloud.key")


def base_url() -> str:
    return (setting("cloud.url") or DEFAULT_URL).rstrip("/")


def witness_on() -> bool:
    return bool(key()) and setting("cloud.witness") != "off"


@contextlib.contextmanager
def _flock(path: Path, blocking: bool = True):
    """Yields True when the lock is held (always, when blocking)."""
    if fcntl is None:  # pragma: no cover
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------- witness

def queue(session_id: str, rec: dict) -> None:
    """Called by RunLog.append while it holds the log's lock: one line, in chain order."""
    line = json.dumps({"s": session_id, "i": rec["i"], "hash": rec["hash"], "prev": rec["prev"]}) + "\n"
    with _flock(OUTBOX.with_suffix(".lock")):
        with open(OUTBOX, "a") as f:
            f.write(line)


def kick() -> None:
    """Start a background flush unless one is already running."""
    with _flock(OUTBOX.with_suffix(".flushing"), blocking=False) as free:
        if not free:
            return
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        subprocess.Popen([sys.executable, "-m", "claimcheck.cloud", "flush"], env=env, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        pass


def flush(timeout: float = 10) -> tuple[int, int]:
    """Send queued fingerprints; (sent, still queued). One flusher at a time; a busy one means 'try later'."""
    with _flock(OUTBOX.with_suffix(".flushing"), blocking=False) as free:
        if not free or not key():
            return 0, _queued()
        sent = 0
        while True:
            with _flock(OUTBOX.with_suffix(".lock")):
                lines = OUTBOX.read_text().splitlines() if OUTBOX.exists() else []
            if not lines:
                return sent, 0
            batch, session = [], None
            for ln in lines[:500]:
                try:
                    ev = json.loads(ln)
                except ValueError:
                    batch.append(None)
                    continue
                if session is None:
                    session = ev["s"]
                if ev["s"] != session:
                    break
                batch.append(ev)
            events = [{"i": e["i"], "hash": e["hash"], "prev": e["prev"]} for e in batch if e]
            code, _ = api("POST", "/v1/witness", {"session": session, "events": events}, timeout=timeout) if events else (200, {})
            if code == 0 or code == 429 or code >= 500 or code == 401:
                return sent, len(lines)          # offline, busy or logged out: keep the queue for next time
            with _flock(OUTBOX.with_suffix(".lock")):  # appends only grow the tail: drop the prefix we sent
                now = OUTBOX.read_text().splitlines()
                OUTBOX.write_text("".join(x + "\n" for x in now[len(batch):]))
            sent += len(events)


def _queued() -> int:
    try:
        with open(OUTBOX) as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# ---------------------------------------------------------------- API

def api(method: str, path: str, body: dict | None = None, timeout: float = 10, api_key: str | None = None,
        url: str | None = None) -> tuple[int, dict]:
    """(status, json). Status 0 = could not reach the service."""
    k = api_key or key()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request((url or base_url()) + path, data=data, method=method)
    req.add_header("User-Agent", "claimcheck-cli")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if k:
        req.add_header("Authorization", f"Bearer {k}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": e.reason}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, {"error": f"can't reach {url or base_url()}: {getattr(e, 'reason', e)}"}


# ---------------------------------------------------------------- share

def view(doc: dict, privacy: str) -> dict:
    """The receipt at `privacy`. Stricter than stored = a derived receipt, re-signed here; looser can't be made."""
    from .document import apply_privacy, content_id
    have = doc.get("privacy", "full")
    if privacy == have:
        return doc
    if PRIVACY_ORDER.index(privacy) < PRIVACY_ORDER.index(have):
        raise ValueError(f"this receipt was made at '{have}'; it can't be shared at the more open '{privacy}'")
    v = apply_privacy(json.loads(json.dumps(doc)), privacy)
    v.pop("signature", None)
    v["id"] = content_id(v)
    if doc.get("signature"):
        from .sign import sign
        sign(v)
    return v


def share(doc: dict, privacy: str | None = None) -> tuple[int, dict]:
    privacy = privacy or setting("cloud.privacy") or "summary"
    try:
        v = view(doc, privacy)
    except ValueError as e:
        return 400, {"error": str(e)}
    flush(timeout=5)  # the head's fingerprint should be there before the receipt is judged against it
    return api("POST", "/v1/receipts", v, timeout=20)


def after_receipt(doc: dict, path: Path) -> str | None:
    """Hook/plugin tail: send the run's fingerprints, then share per `cloud.share`. Returns the link, if any."""
    if not key():
        return None
    flush(timeout=3)
    mode = setting("cloud.share") or "off"
    s = doc.get("summary", {})
    flagged = any(s.get(k) for k in ("unverified", "pre_existing", "contradicted"))
    if mode == "all" or (mode == "flagged" and flagged):
        code, out = share(doc)
        if code == 200:
            with contextlib.suppress(OSError):
                path.with_suffix(".shared").write_text(json.dumps(out))
            return out.get("url")
    return None


if __name__ == "__main__":  # python -m claimcheck.cloud flush
    if sys.argv[1:] == ["flush"]:
        flush()
