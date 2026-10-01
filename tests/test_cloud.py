"""claimcheck.cc client: fingerprints queue in chain order and survive being offline; shared views stay signed."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CLAIMCHECK_HOME", tempfile.mkdtemp(prefix="claimcheck-test-"))

from claimcheck import cloud, config  # noqa: E402
from claimcheck.capture import RunLog  # noqa: E402
from claimcheck.document import check_id  # noqa: E402
from claimcheck.sign import verify_signature  # noqa: E402

DEMO = json.loads((ROOT / "site" / "demo-receipt.json").read_text())


class Fake(BaseHTTPRequestHandler):
    seen: list = []
    receipts: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer cck_test":
            return self._out(401, {"error": "no"})
        if self.path == "/v1/witness":
            Fake.seen.append(body)
            return self._out(200, {"accepted": len(body["events"]), "duplicate": 0, "conflicts": []})
        if self.path == "/v1/receipts":
            Fake.receipts.append(body)
            return self._out(200, {"url": "https://claimcheck.cc/r/abc", "token": "abc", "witness": "witnessed", "expires_at": None})
        self._out(404, {})

    def _out(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def svc(monkeypatch, tmp_path):
    Fake.seen, Fake.receipts = [], []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(cloud, "OUTBOX", tmp_path / "outbox.jsonl")
    monkeypatch.setattr(cloud, "kick", lambda: None)          # flush in-test, not in a child process
    monkeypatch.setenv("CLAIMCHECK_CLOUD_KEY", "cck_test")
    monkeypatch.setenv("CLAIMCHECK_CLOUD_URL", f"http://127.0.0.1:{srv.server_port}")
    yield srv
    srv.shutdown()


def test_events_queue_in_chain_order_and_flush_per_session(svc, tmp_path):
    a, b = RunLog("sa", root=tmp_path), RunLog("sb", root=tmp_path)
    for k in range(3):
        a.append(tool="terminal", args={"command": f"PRIVATE-CONTENT-{k}"}, result="", ts=1.0)
        b.append(tool="terminal", args={"command": f"b{k}"}, result="", ts=1.0)
    assert cloud._queued() == 6
    sent, left = cloud.flush()
    assert (sent, left) == (6, 0)
    by_session = {}
    for call in Fake.seen:
        by_session.setdefault(call["session"], []).extend(call["events"])
    assert [e["i"] for e in by_session["sa"]] == [0, 1, 2]
    assert [e["hash"] for e in by_session["sa"]] == [r["hash"] for r in a.records()]
    assert by_session["sb"][1]["prev"] == b.records()[0]["hash"]
    assert "PRIVATE-CONTENT" not in json.dumps(Fake.seen)     # fingerprints only, never content


def test_offline_keeps_the_queue(svc, tmp_path, monkeypatch):
    log = RunLog("sc", root=tmp_path)
    log.append(tool="terminal", args={"command": "x"}, result="", ts=1.0)
    monkeypatch.setenv("CLAIMCHECK_CLOUD_URL", "http://127.0.0.1:9")   # nothing listens there
    assert cloud.flush(timeout=1) == (0, 1)
    monkeypatch.setenv("CLAIMCHECK_CLOUD_URL", f"http://127.0.0.1:{svc.server_port}")
    assert cloud.flush() == (1, 0)


def test_nothing_is_queued_when_logged_out(svc, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAIMCHECK_CLOUD_KEY")
    RunLog("sd", root=tmp_path).append(tool="terminal", args={"command": "x"}, result="", ts=1.0)
    assert cloud._queued() == 0


def _signed_demo():
    from claimcheck.sign import keygen, load_key, sign
    k = Path(tempfile.mkdtemp()) / "key"
    keygen(k)
    d = json.loads(json.dumps(DEMO))
    d.pop("signature", None)
    return sign(d, load_key(k))


def test_shared_view_is_a_valid_receipt_at_the_chosen_privacy(svc):
    d = _signed_demo()
    code, out = cloud.share(d, "summary")
    assert code == 200 and out["url"].endswith("/r/abc")
    sent = Fake.receipts[-1]
    assert sent["privacy"] == "summary" and check_id(sent) and verify_signature(sent)[0]
    with pytest.raises(ValueError):
        cloud.view(sent, "full")                               # a summary can't be widened back


def test_after_receipt_respects_cloud_share(svc, tmp_path, monkeypatch):
    d = _signed_demo()
    clean = dict(d, summary=dict(d["summary"], unverified=0, pre_existing=0, contradicted=0))
    flagged = dict(d, summary=dict(d["summary"], unverified=1))
    monkeypatch.setenv("CLAIMCHECK_SHARE", "flagged")
    assert cloud.after_receipt(clean, tmp_path / "r1.json") is None
    assert cloud.after_receipt(flagged, tmp_path / "r2.json") == "https://claimcheck.cc/r/abc"
    assert json.loads((tmp_path / "r2.shared").read_text())["token"] == "abc"


def test_config_refuses_bad_choices():
    with pytest.raises(SystemExit):
        config.put("cloud.share", "sometimes")
