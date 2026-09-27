"""The review model is pinned by the person; a review never falls through to the agent's default model."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UH = Path(tempfile.mkdtemp(prefix="claimcheck-userhome-"))
CH = UH / ".claimcheck"
ENV = {k: v for k, v in os.environ.items() if not k.startswith("CLAIMCHECK_REVIEW")}
ENV.update({"CLAIMCHECK_USER_HOME": str(UH), "CLAIMCHECK_HOME": str(CH), "PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})


def cc(*args, ok=True, stdin=""):
    r = subprocess.run([sys.executable, "-m", "claimcheck.cli", *args], capture_output=True, text=True, env=ENV, cwd=ROOT,
                       timeout=60, input=stdin)
    if ok:
        assert r.returncode == 0, r.stderr
    return r


def _fake_flagged_receipt():
    d = CH / "receipts" / "sess1"
    d.mkdir(parents=True, exist_ok=True)
    doc = {"claimcheck": "0.1", "id": "rcpt_test0001", "created_at": "2026-09-27T00:00:00Z",
           "run": {"adapter": "claude-code", "session_id": "sess1", "turn_id": "t1", "agent": {"platform": "claude-code"}, "asked": "do x"},
           "summary": {"verified": 0, "unverified": 1, "pre_existing": 0, "contradicted": 0, "unchecked": 0, "headline": "unverified"},
           "claims": [{"text": "ran `foo --bar`", "kind": "command_ran", "targets": ["`foo --bar`"], "verdict": "unverified",
                       "evidence": "`foo --bar` not found in any write, command or output"}]}
    (d / "t1.json").write_text(json.dumps(doc))


def test_init_pins_the_model_and_doctor_shows_it():
    (UH / ".claude").mkdir(parents=True, exist_ok=True)
    out = cc("init", "claude-code", "--review-model", "haiku", "--no-input").stdout
    assert "Review model: haiku" in out
    cfg = json.loads((CH / "config.json").read_text())
    assert cfg["review"]["model"] == "haiku"
    assert "Review model: haiku" in cc("doctor").stdout
    assert cc("config", "review.model").stdout.strip() == "haiku"


def test_init_without_a_model_says_so_and_review_refuses():
    cc("config", "review.model", "--unset")
    assert "NOT SET" in cc("init", "claude-code", "--no-input").stdout
    _fake_flagged_receipt()
    r = cc("review", ok=False)
    assert r.returncode != 0 and "no review model pinned" in r.stderr and "claimcheck config review.model" in r.stderr
    assert not (CH / "reviews").exists()


def test_review_dry_run_uses_only_the_pinned_model_and_read_only_mode():
    cc("config", "review.model", "haiku")
    cc("config", "review.agent", "claude-code")
    _fake_flagged_receipt()
    fake_bin = UH / "bin"; fake_bin.mkdir(exist_ok=True)
    (fake_bin / "claude").write_text("#!/bin/sh\necho stub\n"); (fake_bin / "claude").chmod(0o755)
    env = {**ENV, "PATH": f"{fake_bin}:{ENV['PATH']}"}
    r = subprocess.run([sys.executable, "-m", "claimcheck.cli", "review", "--dry-run"], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "--model haiku" in r.stdout and "--permission-mode plan" in r.stdout and "rcpt_test0001" in r.stdout


def test_review_writes_the_verdict_file_and_skips_it_next_time():
    cc("config", "review.model", "haiku"); cc("config", "review.agent", "claude-code")
    _fake_flagged_receipt()
    fake_bin = UH / "bin"; fake_bin.mkdir(exist_ok=True)
    (fake_bin / "claude").write_text("#!/bin/sh\ncat >/dev/null\nprintf 'claim: ran foo\\nverdict: false-flag\\nwhy: the log has it\\nrule: x\\n'\n")
    (fake_bin / "claude").chmod(0o755)
    env = {**ENV, "PATH": f"{fake_bin}:{ENV['PATH']}"}
    r = subprocess.run([sys.executable, "-m", "claimcheck.cli", "review"], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "1 false-flag" in r.stdout
    f = CH / "reviews" / "rcpt_test0001.md"
    assert f.exists() and "model: haiku" in f.read_text() and "verdict: false-flag" in f.read_text()
    r2 = subprocess.run([sys.executable, "-m", "claimcheck.cli", "review"], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
    assert "nothing to review" in r2.stdout


def test_config_refuses_unknown_keys():
    r = cc("config", "review.modle", "x", ok=False)
    assert r.returncode != 0 and "unknown setting" in r.stderr
