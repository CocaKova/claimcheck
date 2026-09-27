"""`claimcheck init`: wires every agent found in a home folder, idempotently, reversibly, keeping their other settings."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UH = Path(tempfile.mkdtemp(prefix="claimcheck-userhome-"))
ENV = {**os.environ, "CLAIMCHECK_USER_HOME": str(UH), "CLAIMCHECK_HOME": str(UH / ".claimcheck"), "PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}


def cc(*args):
    r = subprocess.run([sys.executable, "-m", "claimcheck.cli", *args], capture_output=True, text=True, env=ENV, cwd=ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_init_wires_idempotently_and_removes_cleanly():
    (UH / ".claude").mkdir(); (UH / ".codex").mkdir(); (UH / ".gemini").mkdir(); (UH / ".cursor").mkdir()
    (UH / ".claude" / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}]}}))
    (UH / ".gemini" / "settings.json").write_text(json.dumps({"theme": "dark"}))
    out = cc("init")
    assert "Claude Code: wired" in out and "Codex CLI: wired" in out and "Gemini CLI: wired" in out and "Cursor: wired" in out and "Signing key" in out
    cl = json.loads((UH / ".claude" / "settings.json").read_text())
    assert cl["model"] == "opus" and cl["hooks"]["Stop"][0]["hooks"][0]["command"] == "say done"            # other settings untouched
    assert {ev for ev in cl["hooks"]} == {"UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop"}
    stop_cc = [h for g in cl["hooks"]["Stop"] for h in g["hooks"] if "claimcheck" in h["command"] and h["command"].endswith(" hook")]
    assert stop_cc and "async" not in stop_cc[0] and (UH / ".claude" / "settings.json.claimcheck-backup").exists()
    assert set(json.loads((UH / ".gemini" / "settings.json").read_text())["hooks"]) == {"AfterTool", "AfterAgent"}
    cu = json.loads((UH / ".cursor" / "hooks.json").read_text())
    assert cu["version"] == 1 and set(cu["hooks"]) == {"postToolUse", "afterAgentResponse"}
    assert set(json.loads((UH / ".codex" / "hooks.json").read_text())["hooks"]) == {"UserPromptSubmit", "PostToolUse", "Stop"}
    snap = {p: p.read_text() for p in UH.rglob("*.json")}
    out2 = cc("init")
    assert "already wired" in out2 and {p: p.read_text() for p in UH.rglob("*.json")} == snap        # idempotent
    out3 = cc("init", "--remove")
    assert out3.count("removed from") == 4
    cl = json.loads((UH / ".claude" / "settings.json").read_text())
    assert cl == {"model": "opus", "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}]}}
    assert json.loads((UH / ".gemini" / "settings.json").read_text()) == {"theme": "dark"}
    assert "hooks" not in json.loads((UH / ".cursor" / "hooks.json").read_text())
    doc = cc("doctor")
    assert "NOT wired" in doc and "receipts: none yet" in doc


if __name__ == "__main__":
    test_init_wires_idempotently_and_removes_cleanly(); print("ok install")


def test_doctor_sees_the_quoted_path_form_as_wired():
    """With `claimcheck` on PATH the hook line is `"/abs/claimcheck" hook`; a text search for `claimcheck hook` misses it (0.1.0 doctor bug)."""
    home = Path(tempfile.mkdtemp(prefix="claimcheck-userhome2-"))
    (home / ".claude").mkdir(); (home / "bin").mkdir()
    exe = home / "bin" / "claimcheck"
    exe.write_text("#!/bin/sh\nexit 0\n"); exe.chmod(0o755)
    env = {**ENV, "CLAIMCHECK_USER_HOME": str(home), "CLAIMCHECK_HOME": str(home / ".claimcheck"), "PATH": f"{home / 'bin'}:/usr/bin:/bin"}
    run = lambda *a: subprocess.run([sys.executable, "-m", "claimcheck.cli", *a], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60).stdout
    assert "Claude Code: wired" in run("init")
    cmd = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd == f'"{exe}" hook'
    assert "Claude Code: wired" in run("doctor")
    assert "already wired" in run("init")
