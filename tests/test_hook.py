"""`claimcheck hook`: the stdin payloads of six agents → one run log shape → a signed receipt."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="claimcheck-hook-"))
ENV = {**os.environ, "CLAIMCHECK_HOME": str(HOME), "PYTHONPATH": str(ROOT)}
sys.path.insert(0, str(ROOT))
os.environ["CLAIMCHECK_HOME"] = str(HOME)
from claimcheck.hook import normalize  # noqa: E402


def hook(payload: dict, **env):
    r = subprocess.run([sys.executable, "-m", "claimcheck.cli", "hook"], input=json.dumps(payload), capture_output=True, text=True,
                       env={**ENV, **env}, cwd=ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    return r


def receipt(sid):
    d = HOME / "receipts" / sid
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime) if d.exists() else []
    return json.loads(files[-1].read_text()) if files else None


def test_claude_code_shape():
    base = {"session_id": "cc1", "transcript_path": "/x/t.jsonl", "cwd": "/home/u/proj", "permission_mode": "default"}
    hook({**base, "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": "toolu_1",
          "tool_input": {"command": "pytest -q", "description": "run tests"}, "tool_response": {"stdout": "9 passed", "stderr": "", "interrupted": False}})
    hook({**base, "hook_event_name": "PostToolUse", "tool_name": "Write", "tool_use_id": "toolu_2",
          "tool_input": {"file_path": "/home/u/proj/app/main.py", "content": "print(1)"}, "tool_response": {"filePath": "/home/u/proj/app/main.py", "success": True}})
    hook({**base, "hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_use_id": "toolu_3",
          "tool_input": {"command": "git push origin main"}, "error": "fatal: no remote", "tool_response": {"stderr": "fatal: no remote"}})
    hook({**base, "hook_event_name": "Stop", "stop_hook_active": False,
          "last_assistant_message": "Done:\n- ran `pytest -q`, 9 passed\n- wrote `app/main.py`\n- pushed with `git push origin main`\n- also updated `docs/never.md`"})
    d = receipt("cc1")
    assert d and d["run"]["adapter"] == "claude-code" and d["run"]["cwd"] == "/home/u/proj"
    assert d["signature"] and d["capture"]["mode"] == "live" and d["ledger"]["tool_calls"] == 3
    v = {c["text"]: c["verdict"] for c in d["claims"]}
    g = lambda frag: next(val for k, val in v.items() if frag in k)  # noqa: E731
    assert g("pytest") == "verified" and g("main.py") == "verified"
    assert g("git push") == "contradicted" and g("never.md") == "unverified"
    assert d["summary"]["headline"] == "contradicted"


def test_codex_shape():
    base = {"session_id": "cx1", "transcript_path": "/x", "cwd": "/w", "turn_id": "turn-7", "permission_mode": "auto", "model": "gpt-5-codex"}
    hook({**base, "hook_event_name": "PostToolUse", "tool_name": "shell", "tool_use_id": "c1",
          "tool_input": {"command": ["bash", "-lc", "npm test"]}, "tool_response": {"output": "12 passing", "exit_code": 0}})
    hook({**base, "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "Ran `npm test`: 12 passing."})
    d = receipt("cx1")
    assert d["run"]["adapter"] == "codex" and d["run"]["turn_id"] == "turn-7" and d["run"]["agent"]["model"] == "gpt-5-codex"
    assert d["ledger"]["commands"]["items"][0]["text"] == "bash -lc npm test"
    assert d["claims"][0]["verdict"] == "verified"


def test_gemini_shape():
    base = {"session_id": "gm1", "transcript_path": "/x", "cwd": "/w", "timestamp": "2026-09-26T00:00:00Z"}
    hook({**base, "hook_event_name": "AfterTool", "tool_name": "write_file", "tool_input": {"file_path": "/w/notes.md", "content": "# hi"},
          "tool_response": {"llmContent": "Successfully wrote /w/notes.md", "returnDisplay": "ok"}})
    hook({**base, "hook_event_name": "AfterAgent", "prompt": "make notes", "prompt_response": "Created `/w/notes.md`.", "stop_hook_active": False})
    d = receipt("gm1")
    assert d["run"]["adapter"] == "gemini" and d["run"]["asked"] == "make notes"
    assert "/w/notes.md" in d["ledger"]["files"]["written"] and d["claims"][0]["verdict"] == "verified"


def test_cursor_shape():
    base = {"conversation_id": "cu1", "generation_id": "g1", "workspace_roots": ["/w"], "transcript_path": "/x", "model": "m"}
    hook({**base, "hook_event_name": "postToolUse", "tool_name": "Shell", "tool_use_id": "t1", "cwd": "/w",
          "tool_input": {"command": "make build"}, "tool_output": "build ok"})
    hook({**base, "hook_event_name": "afterAgentResponse", "text": "Built it with `make build`."})
    d = receipt("cu1")
    assert d["run"]["adapter"] == "cursor" and d["claims"][0]["verdict"] == "verified"


def test_hermes_shell_hook_shape():
    hook({"hook_event_name": "post_tool_call", "tool_name": "terminal", "tool_input": {"command": "ls /tmp"}, "args": {"command": "ls /tmp"},
          "session_id": "hs1", "cwd": "/h", "extra": {"result": json.dumps({"output": "a\nb", "exit_code": 0}), "status": "ok", "turn_id": "hs1:x:ab12"}})
    n = normalize({"hook_event_name": "post_tool_call", "tool_name": "terminal", "args": {}, "session_id": "hs1", "extra": {}})
    assert n["platform"] == "hermes" and n["event"] == "post_tool"
    recs = [json.loads(l) for l in (HOME / "runs" / "hs1.jsonl").read_text().splitlines()]
    assert recs[0]["tool"] == "terminal" and json.loads(recs[0]["result"])["exit_code"] == 0


def test_copilot_camel_shape_and_no_final_message():
    hook({"sessionId": "cp1", "hookName": None, "hook_event_name": "postToolUse", "toolName": "bash", "toolArgs": {"command": "echo hi"},
          "toolResult": {"textResultForLlm": "hi", "resultType": "success"}})
    hook({"sessionId": "cp1", "hook_event_name": "agentStop", "transcript_path": "/x", "stop_reason": "end_turn"})
    d = receipt("cp1")
    assert d and d["run"]["adapter"] == "copilot" and d["claims"] == [] and d["ledger"]["tool_calls"] == 1


def test_garbage_never_fails():
    for payload in ("", "not json", "[1,2]", "{}"):
        r = subprocess.run([sys.executable, "-m", "claimcheck.cli", "hook"], input=payload, capture_output=True, text=True, env=ENV, cwd=ROOT)
        assert r.returncode == 0


def test_pure_backend_signs_and_cryptography_verifies():
    hook({"session_id": "pb1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "true"}, "tool_response": {"stdout": ""}}, CLAIMCHECK_SIGN_BACKEND="pure")
    hook({"session_id": "pb1", "hook_event_name": "Stop", "last_assistant_message": "Ran `true`."}, CLAIMCHECK_SIGN_BACKEND="pure")
    from claimcheck.sign import BACKEND, verify_signature
    assert BACKEND == "cryptography"
    assert verify_signature(receipt("pb1"))[0]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok hook")
