"""A local model is found, offered first, and reviews with it are one HTTP call with the evidence inlined."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UH = Path(tempfile.mkdtemp(prefix="claimcheck-userhome-"))
CH = UH / ".claimcheck"
BASE_ENV = {k: v for k, v in os.environ.items() if not k.startswith(("CLAIMCHECK_", "RECEIPT_LLM", "OPENAI_BASE_URL", "OLLAMA_HOST"))}
BASE_ENV.update({"CLAIMCHECK_USER_HOME": str(UH), "CLAIMCHECK_HOME": str(CH), "PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})

SEEN: list[dict] = []          # every chat/completions body the fake server received
MODE = {"kind": "ok"}          # ok | wrong-model | needs-key | thinking-only


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if MODE["kind"] == "needs-key" and not self.headers.get("Authorization"):
            return self._send(401, {"error": "key"})
        if self.path.endswith("/v1/models"):
            return self._send(200, {"object": "list", "data": [{"id": "tiny-local-7b"}, {"id": "other-3b"}]})
        self._send(404, {"error": "no"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(n) or b"{}")
        SEEN.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": req})
        if MODE["kind"] == "needs-key" and not self.headers.get("Authorization"):
            return self._send(401, {"error": {"message": "missing api key"}})
        if req.get("model") != "tiny-local-7b":
            return self._send(404, {"error": {"message": f"The model `{req.get('model')}` does not exist."}})
        if MODE["kind"] == "thinking-only":
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "hmm"}}]})
        text = "<think>look at the log</think>claim: ran foo\nverdict: false-flag\nwhy: the terminal record has `foo --bar` exit 0\nrule: none\n"
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})


SRV = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=SRV.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{SRV.server_address[1]}"
ENV = {**BASE_ENV, "CLAIMCHECK_LOCAL_URLS": URL}   # `claimcheck local` probes only the fake


def cc(*args, env=None, ok=True):
    r = subprocess.run([sys.executable, "-m", "claimcheck.cli", *args], capture_output=True, text=True, env=env or ENV, cwd=ROOT, timeout=60)
    if ok:
        assert r.returncode == 0, r.stderr
    return r


def _receipt_and_log():
    d = CH / "receipts" / "sess1"; d.mkdir(parents=True, exist_ok=True)
    doc = {"claimcheck": "0.1", "id": "rcpt_local0001", "created_at": "2026-09-27T00:00:00Z",
           "run": {"adapter": "claude-code", "session_id": "sess1", "turn_id": "t2", "agent": {"platform": "claude-code"}, "asked": "do x"},
           "summary": {"verified": 0, "unverified": 1, "pre_existing": 0, "contradicted": 0, "unchecked": 0, "headline": "unverified"},
           "claims": [{"text": "ran `foo --bar`", "kind": "command_ran", "targets": ["`foo --bar`"], "verdict": "unverified",
                       "evidence": "`foo --bar` not found in any write, command or output"}]}
    (d / "t2.json").write_text(json.dumps(doc))
    runs = CH / "runs"; runs.mkdir(exist_ok=True)
    recs = [  # an earlier-turn record that settles it, a big irrelevant record in this turn, a small one in this turn
        {"i": 1, "tool": "Bash", "args": {"command": "foo --bar"}, "result": "ok\nexit 0", "ts": "2026-09-27T00:00:01Z", "turn_id": "t1", "status": "ok", "prev": "0", "hash": "1"},
        {"i": 2, "tool": "Read", "args": {"file_path": "/x/y.txt"}, "result": "z" * 5000, "ts": "2026-09-27T00:00:02Z", "turn_id": "t2", "status": "ok", "prev": "1", "hash": "2"},
        {"i": 3, "tool": "Bash", "args": {"command": "ls"}, "result": "a b c", "ts": "2026-09-27T00:00:03Z", "turn_id": "t2", "status": "ok", "prev": "2", "hash": "3"},
    ]
    (runs / "sess1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    return doc


def _reset():
    for k in ("review.model", "review.agent", "review.endpoint", "review.api_key"):
        cc("config", k, "--unset")
    rv = CH / "reviews"
    if rv.exists():
        for f in rv.iterdir():
            f.unlink()
    SEEN.clear(); MODE["kind"] = "ok"


def test_local_lists_what_answers_and_says_none_otherwise():
    out = cc("local").stdout
    assert "cost nothing" in out and URL + "/v1" in out and "tiny-local-7b" in out
    out2 = cc("local", env={**BASE_ENV, "CLAIMCHECK_LOCAL_URLS": "http://127.0.0.1:9"}).stdout
    assert "none answering" in out2


def test_init_pins_a_local_endpoint_and_normalizes_the_url():
    _reset(); (UH / ".claude").mkdir(exist_ok=True)
    out = cc("init", "claude-code", "--review-model", "tiny-local-7b", "--review-endpoint", URL + "/", "--no-input").stdout
    assert f"Review model: tiny-local-7b at {URL}/v1 (local, costs nothing)" in out
    cfg = json.loads((CH / "config.json").read_text())["review"]
    assert cfg == {"model": "tiny-local-7b", "endpoint": URL + "/v1", "agent": "endpoint"}


def test_init_finds_the_home_of_a_local_model_named_without_a_url():
    _reset(); (UH / ".claude").mkdir(exist_ok=True)
    out = cc("init", "claude-code", "--review-model", "other-3b", "--no-input").stdout
    assert f"other-3b at {URL}/v1" in out
    assert json.loads((CH / "config.json").read_text())["review"]["agent"] == "endpoint"


def test_init_without_a_model_relays_the_free_option_for_the_agent_to_read_back():
    _reset(); (UH / ".claude").mkdir(exist_ok=True)
    out = cc("init", "claude-code", "--no-input").stdout
    assert "NOT SET" in out and "cost nothing" in out and "tiny-local-7b" in out


def test_endpoint_review_inlines_the_evidence_literal_hits_first_and_writes_the_file():
    _reset(); _receipt_and_log()
    cc("config", "review.model", "tiny-local-7b"); cc("config", "review.endpoint", URL)
    dry = cc("review", "--dry-run").stdout
    assert f"would POST to {URL}/v1/chat/completions model=tiny-local-7b" in dry
    out = cc("review").stdout
    assert "1 false-flag" in out
    f = CH / "reviews" / "rcpt_local0001.md"
    assert f.exists()
    txt = f.read_text()
    assert f"endpoint: {URL}/v1" in txt and "verdict: false-flag" in txt and "<think>" not in txt
    body = SEEN[-1]["body"]
    assert body["model"] == "tiny-local-7b" and body["temperature"] == 0
    user = body["messages"][1]["content"]
    assert "1 record(s) mention a flagged literal" in user
    assert user.index('"command": "foo --bar"') < user.index('"command": "ls"'), "the record that settles it comes first"
    assert "(+" in user and "zzzzzzzzzz" in user, "long results are cut, not dropped"


def test_endpoint_errors_come_back_as_one_plain_line_each():
    _reset(); _receipt_and_log()
    cc("config", "review.endpoint", URL)
    cc("config", "review.model", "nope-9b")
    out = cc("review").stdout
    assert "does not serve a model called `nope-9b`" in out and "tiny-local-7b" in out
    cc("config", "review.model", "tiny-local-7b")
    MODE["kind"] = "thinking-only"
    assert "spent its whole answer thinking" in cc("review").stdout
    MODE["kind"] = "needs-key"
    assert "wants an API key" in cc("review").stdout
    cc("config", "review.api_key", "sk-test-1234567")
    assert "1 false-flag" in cc("review").stdout and SEEN[-1]["auth"] == "Bearer sk-test-1234567"
    shown = cc("config").stdout
    assert "sk-test-1234567" not in shown and "review.api_key = sk-…67" in shown
    assert oct(os.stat(CH / "config.json").st_mode & 0o777) == "0o600"
    _reset(); _receipt_and_log()
    cc("config", "review.model", "tiny-local-7b"); cc("config", "review.endpoint", "http://127.0.0.1:9/v1")
    assert "nothing answering at http://127.0.0.1:9/v1" in cc("review").stdout


def test_codex_gets_oss_and_the_provider_for_a_local_endpoint():
    _reset(); _receipt_and_log()
    cc("config", "review.model", "llama3.2"); cc("config", "review.agent", "codex"); cc("config", "review.endpoint", "http://localhost:11434")
    fake_bin = UH / "bin"; fake_bin.mkdir(exist_ok=True)
    (fake_bin / "codex").write_text("#!/bin/sh\necho stub\n"); (fake_bin / "codex").chmod(0o755)
    out = cc("review", "--dry-run", env={**ENV, "PATH": f"{fake_bin}:{ENV['PATH']}"}).stdout
    assert "-m llama3.2 -s read-only --oss --local-provider ollama" in out


def test_bridge_leaves_the_card_on_the_profile_model_when_reviews_go_elsewhere():
    _reset()
    sys.path.insert(0, str(ROOT))
    os.environ.update({"CLAIMCHECK_HOME": str(CH)})
    import importlib
    from claimcheck import config, store
    importlib.reload(store); importlib.reload(config)
    import hermes_plugin.review_bridge as rb
    config.put("review.model", "tiny-local-7b"); config.put("review.endpoint", URL)
    assert rb.review_model() is None
    config.put("review.endpoint", None); config.put("review.agent", "hermes")
    assert rb.review_model() == "tiny-local-7b"
