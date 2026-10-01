"""`claimcheck verify` checks a receipt against the run log when the log is on this machine."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CLAIMCHECK_HOME", tempfile.mkdtemp(prefix="claimcheck-test-"))

from claimcheck import capture  # noqa: E402
from claimcheck.capture import GENESIS, RunLog, event_hash  # noqa: E402
from claimcheck.cli import _check_log  # noqa: E402


def _log(sid: str, n: int = 3) -> tuple[RunLog, str]:
    log = RunLog(sid, root=Path(tempfile.mkdtemp()))
    for k in range(n):
        log.append(tool="terminal", args={"command": f"echo {k}"}, result=str(k), ts=1000.0 + k)
    return log, log.records()[-1]["hash"]


def _receipt(sid, head):
    return {"run": {"session_id": sid}, "capture": {"chain_head": head}}


def test_intact_log_passes(monkeypatch):
    log, head = _log("s-ok")
    monkeypatch.setattr(capture, "RUNS", log.path.parent)
    assert _check_log(_receipt("s-ok", head))


def test_edited_event_fails(monkeypatch):
    log, head = _log("s-edit")
    monkeypatch.setattr(capture, "RUNS", log.path.parent)
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[1]); rec["result"] = "something else"
    lines[1] = json.dumps(rec)
    log.path.write_text("\n".join(lines) + "\n")
    assert not _check_log(_receipt("s-edit", head))


def test_consistently_rewritten_log_fails(monkeypatch):
    """Rewriting every hash so the chain is valid again still loses the head the receipt signed."""
    log, head = _log("s-rewrite")
    monkeypatch.setattr(capture, "RUNS", log.path.parent)
    recs = log.records()
    recs[0]["result"] = "forged"
    prev = GENESIS
    for r in recs:
        r["prev"] = prev
        r["hash"] = event_hash(r)
        prev = r["hash"]
    log.path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    assert not _check_log(_receipt("s-rewrite", head))


def test_no_local_log_is_not_a_failure():
    assert _check_log(_receipt("never-seen", "a" * 64))


def _writer(root, sid, k):
    log = RunLog(sid, root=Path(root))
    for n in range(25):
        log.append(tool="terminal", args={"command": f"w{k} {n}"}, result="", ts=1000.0)


def test_parallel_hook_processes_keep_one_chain():
    """Claude Code fires one hook process per parallel tool call (run log f4828bee…, 2026-09-29: two events #351)."""
    import multiprocessing as mp
    from claimcheck.capture import verify_chain
    root = tempfile.mkdtemp()
    procs = [mp.get_context("fork").Process(target=_writer, args=(root, "s-par", k)) for k in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    recs = RunLog("s-par", root=Path(root)).records()
    assert len(recs) == 200
    assert verify_chain(recs)[0], verify_chain(recs)[1]
