"""Hermes plugin, driven by hand: the hook payloads Hermes sends → a signed live-capture receipt on disk."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CLAIMCHECK_HOME"] = tempfile.mkdtemp(prefix="claimcheck-plugin-")
os.environ["RECEIPT_DB"] = "/nonexistent/state.db"          # no live DB in tests

import jsonschema  # noqa: E402

from claimcheck.capture import RunLog, verify_chain  # noqa: E402
from claimcheck.document import check_id  # noqa: E402
from claimcheck.sign import verify_signature  # noqa: E402

spec = importlib.util.spec_from_file_location("hermes_plugins.claimcheck", ROOT / "hermes_plugin" / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
CH = Path(plugin.RECEIPTS).parent  # where the plugin really writes: capture/store bind CLAIMCHECK_HOME on first import, which another test module may have done first
SCHEMA = json.loads((ROOT / "spec" / "receipt-v0.1.schema.json").read_text())
V = jsonschema.Draft202012Validator(SCHEMA)


class Ctx:
    def __init__(self, **cfg):
        self.cfg, self.hooks = cfg, {}

    def get_config(self, k, d=None):
        return self.cfg.get(k, d)

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def _run(sid, report, turn="t1"):
    ctx = Ctx(privacy="full")
    plugin.register(ctx)
    h = ctx.hooks
    h["post_tool_call"](tool_name="terminal", args={"command": "pytest -q tests/"}, session_id=sid, turn_id=turn,
                        tool_call_id="c1", status="ok", duration_ms=1200,
                        result=json.dumps({"output": "12 passed in 0.4s", "exit_code": 0}))
    h["post_tool_call"](tool_name="write_file", args={"path": "/home/x/app/config.py", "content": "PORT = 8080\nPASSWORD = 'hunter2secret'\n"},
                        session_id=sid, turn_id=turn, tool_call_id="c2", status="ok", duration_ms=5, result="ok")
    h["post_tool_call"](tool_name="read_file", args={"path": "/home/x/app/settings.yaml"}, session_id=sid, turn_id=turn,
                        tool_call_id="c3", status="ok", duration_ms=2, result="window_width_override: 1280\n")
    h["post_tool_call"](tool_name="terminal", args={"command": "git push origin main"}, session_id=sid, turn_id=turn,
                        tool_call_id="c4", status="error", duration_ms=900,
                        result=json.dumps({"output": "fatal: could not read from remote", "exit_code": 128}))
    h["post_llm_call"](session_id=sid, turn_id=turn, user_message="ship it", assistant_response=report, model="m", platform="cli")
    h["on_session_end"](session_id=sid, turn_id=turn, completed=True, failed=False, interrupted=False, model="m", platform="cli")
    d = CH / "receipts" / sid / f"{turn}.json"
    return json.loads(d.read_text()) if d.exists() else None, d


REPORT = """Done.
- Ran `pytest -q tests/` — 12 passed.
- Wrote `/home/x/app/config.py` with PORT = 8080.
- Added window_width_override=1280 to settings.yaml.
- Pushed to origin with `git push origin main`.
- Rewrote `/home/x/app/never_touched.py`.
"""


def test_live_receipt_end_to_end():
    doc, path = _run("sess_live_1", REPORT)
    assert doc, "no receipt written"
    assert not list(V.iter_errors(doc)), [e.message for e in V.iter_errors(doc)][:3]
    assert check_id(doc) and verify_signature(doc)[0]
    assert doc["capture"]["mode"] == "live" and doc["capture"]["events"] == 4
    assert doc["run"]["turn_id"] == "t1" and doc["ledger"]["commands"]["failed"] == 1
    assert "/home/x/app/config.py" in doc["ledger"]["files"]["written"]
    by = {c["text"]: c["verdict"] for c in doc["claims"]}
    v = lambda frag: next(val for k, val in by.items() if frag in k)  # noqa: E731
    assert v("pytest") == "verified"
    assert v("config.py") == "verified"
    assert v("window_width_override") == "pre-existing"      # only ever read
    assert v("git push") == "contradicted"                    # exit 128
    assert v("never_touched") == "unverified"
    assert doc["summary"]["headline"] == "contradicted"
    assert path.with_suffix(".html").exists()
    # chain: verifiable, redacted before hashing, tamper-evident
    log = RunLog("sess_live_1")
    recs = log.records()
    ok, head = verify_chain(recs)
    assert ok and head == doc["capture"]["chain_head"]
    assert "hunter2secret" not in log.path.read_text() and "[REDACTED]" in log.path.read_text()
    recs[1]["args"]["path"] = "/home/x/app/other.py"
    assert verify_chain(recs)[0] is False


def test_no_tools_no_receipt_unless_chatty():
    ctx = Ctx(); plugin.register(ctx); h = ctx.hooks
    h["post_llm_call"](session_id="sess_chat", turn_id="t1", user_message="hi", assistant_response="Hello! I ran `make all`.", model="m", platform="cli")
    h["on_session_end"](session_id="sess_chat", turn_id="t1", model="m", platform="cli")
    assert not (CH / "receipts" / "sess_chat").exists()


def test_second_turn_gets_its_own_receipt_and_ledger():
    sid = "sess_turns"
    _run(sid, "Ran `pytest -q tests/`.", turn="t1")
    ctx = Ctx(); plugin.register(ctx); h = ctx.hooks
    h["post_tool_call"](tool_name="terminal", args={"command": "ls"}, session_id=sid, turn_id="t2", tool_call_id="c9",
                        status="ok", result=json.dumps({"output": "a b", "exit_code": 0}))
    h["post_llm_call"](session_id=sid, turn_id="t2", user_message="list", assistant_response="Ran `ls`; earlier I ran `pytest -q tests/`.", model="m", platform="cli")
    h["on_session_end"](session_id=sid, turn_id="t2", model="m", platform="cli")
    d2 = json.loads((CH / "receipts" / sid / "t2.json").read_text())
    assert d2["ledger"]["tool_calls"] == 1                      # this turn's ledger
    assert all(c["verdict"] == "verified" for c in d2["claims"])  # verified against the whole session
    assert d2["capture"]["chain_head"] == RunLog(sid).head()


if __name__ == "__main__":
    test_live_receipt_end_to_end(); test_no_tools_no_receipt_unless_chatty(); test_second_turn_gets_its_own_receipt_and_ledger()
    print("ok plugin")
