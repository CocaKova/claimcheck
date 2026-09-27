"""Review bridge: a flagged receipt becomes exactly one kanban card (fake `hermes` records the calls)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="claimcheck-bridge-"))
FAKE = HOME / "hermes"
FAKE.write_text("#!/bin/sh\necho \"$@\" >> \"$CC_CALLS\"\n[ \"$2\" = create ] && cat >/dev/null && echo '{\"id\": \"t_fake1\", \"status\": \"ready\"}'\nexit 0\n")
FAKE.chmod(0o755)


def _receipt(sid, headline, verdict, adapter="hermes"):
    d = HOME / "receipts" / sid; d.mkdir(parents=True)
    doc = {"id": f"rcpt_{sid}", "created_at": "2026-09-26T00:00:00.000Z",
           "run": {"session_id": sid, "turn_id": f"{sid}:x:abc", "adapter": adapter, "agent": {"platform": "matrix"}, "asked": "do the thing"},
           "summary": {"verified": 1, "unverified": 0, "pre_existing": 0, "contradicted": 0, "unchecked": 0, "headline": headline},
           "claims": [{"i": 0, "text": "Added foo=1 to bar.yaml", "kind": "file_changed", "targets": ["foo=1"], "verdict": verdict, "evidence": "`foo=1` appears only in content the agent read"}]}
    doc["summary"][verdict.replace("-", "_")] = 1 if verdict != "verified" else 0
    (d / "abc.json").write_text(json.dumps(doc))


def _run():
    env = {**os.environ, "CLAIMCHECK_HOME": str(HOME), "HERMES_BIN": str(FAKE), "CC_CALLS": str(HOME / "calls"), "CLAIMCHECK_REVIEW_CHAT": "!room:x"}
    return subprocess.run([sys.executable, str(ROOT / "hermes_plugin" / "review_bridge.py")], capture_output=True, text=True, env=env)


def test_digest_one_card_strong_flags_only_own_platform():
    _receipt("s_clean", "verified", "verified")
    _receipt("s_weak1", "unverified", "unverified")                      # weak alone: no card
    _receipt("s_cc", "pre-existing", "pre-existing", adapter="claude-code")  # other agent's chat: listed, never carded
    r = _run()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "" and not (HOME / "calls").exists()
    _receipt("s_strong", "pre-existing", "pre-existing")
    _receipt("s_weak2", "unverified", "unverified")
    r = _run()
    calls = (HOME / "calls").read_text()
    assert calls.count("kanban create") == 1 and "--skill claimcheck-review" in calls
    assert "notify-subscribe t_fake1 --platform matrix --chat-id !room:x" in calls
    assert "review card t_fake1: 2 receipt(s)" in r.stdout          # strong + the pending weak, one card
    r2 = _run()
    assert r2.stdout.strip() == "" and (HOME / "calls").read_text().count("kanban create") == 1
    state = json.loads((HOME / "review-bridge.json").read_text())
    card = next(iter(state["cards"].values()))
    assert set(card["receipts"]) == {"rcpt_s_strong", "rcpt_s_weak2"} and "rcpt_s_cc" in state["seen"]


if __name__ == "__main__":
    test_digest_one_card_strong_flags_only_own_platform(); print("ok bridge")
