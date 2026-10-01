"""`claimcheck scrub`: old secrets go, chains stay valid, receipts still verify and still point into their log."""
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
from claimcheck.capture import RunLog, verify_chain  # noqa: E402
from claimcheck.document import check_id, content_id  # noqa: E402
from claimcheck.scrub import scrub  # noqa: E402
from claimcheck.sign import keygen, load_key, sign, verify_signature  # noqa: E402

LEAK = "MY_PASS=correcthorsebattery"   # a shape v1 redaction let through


def _home(monkeypatch, *, broken=False):
    home = Path(tempfile.mkdtemp())
    (home / "runs").mkdir(); (home / "receipts" / "s1").mkdir(parents=True)
    keygen(home / "key")
    log = RunLog("s1", root=home / "runs")
    monkeypatch.setattr(capture, "redact", lambda s: s)   # write the log the way v1 would have
    for k, cmd in enumerate(["ls", f"export {LEAK}", "make"]):
        log.append(tool="terminal", args={"command": cmd}, result=f"out {k}", ts=1000.0 + k)
    monkeypatch.undo()
    recs = log.records()
    if broken:   # two events sharing an index, as the pre-flock race left them
        recs[2]["i"] = 1
        log.path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in recs))
    doc = json.loads((ROOT / "site" / "demo-receipt.json").read_text())
    doc["run"]["session_id"] = "s1"
    doc["capture"]["chain_head"] = recs[1]["hash"]
    doc["ledger"]["commands"]["items"] = [{"i": 1, "text": f"export {LEAK}"}]
    doc.pop("signature", None)
    doc["id"] = content_id(doc)
    sign(doc, load_key(home / "key"))
    (home / "receipts" / "s1" / "t1.json").write_text(json.dumps(doc))
    return home


def test_scrub_removes_secret_and_keeps_everything_verifiable(monkeypatch):
    home = _home(monkeypatch)
    assert "correcthorsebattery" in (home / "runs" / "s1.jsonl").read_text()
    r = scrub(home)
    assert r["logs_rewritten"] == 1 and r["receipts_rewritten"] == 1
    log_text = (home / "runs" / "s1.jsonl").read_text()
    rec_text = (home / "receipts" / "s1" / "t1.json").read_text()
    assert "correcthorsebattery" not in log_text + rec_text
    recs = RunLog("s1", root=home / "runs").records()
    assert verify_chain(recs)[0]
    d = json.loads(rec_text)
    assert check_id(d) and verify_signature(d)[0]
    assert d["capture"]["chain_head"] == recs[1]["hash"]
    assert d["capture"]["rescrubbed"]
    assert (home / "scrub.log").exists()


def test_scrub_mends_a_broken_chain(monkeypatch):
    home = _home(monkeypatch, broken=True)
    assert not verify_chain(RunLog("s1", root=home / "runs").records())[0]
    r = scrub(home)
    assert r["chains_mended"] == 1
    assert verify_chain(RunLog("s1", root=home / "runs").records())[0]


def test_scrub_is_idempotent_and_dry_run_writes_nothing(monkeypatch):
    home = _home(monkeypatch)
    before = (home / "runs" / "s1.jsonl").read_text()
    assert scrub(home, dry_run=True)["logs_rewritten"] == 1
    assert (home / "runs" / "s1.jsonl").read_text() == before
    scrub(home)
    again = scrub(home)
    assert again["logs_rewritten"] == 0 and again["receipts_rewritten"] == 0
