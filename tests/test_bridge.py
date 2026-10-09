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
FAKE.write_text("#!/bin/sh\necho \"$@\" >> \"$CC_CALLS\"\n[ \"$2\" = create ] && cat >> \"$CC_CALLS.body\" && echo '{\"id\": \"t_fake1\", \"status\": \"ready\"}'\nexit 0\n")
FAKE.chmod(0o755)


def _receipt(sid, headline, verdict, adapter="hermes", n_claims=1):
    d = HOME / "receipts" / sid; d.mkdir(parents=True)
    doc = {"id": f"rcpt_{sid}", "created_at": "2026-09-26T00:00:00.000Z",
           "run": {"session_id": sid, "turn_id": f"{sid}:x:abc", "adapter": adapter, "agent": {"platform": "matrix"}, "asked": "do the thing"},
           "summary": {"verified": 1, "unverified": 0, "pre_existing": 0, "contradicted": 0, "unchecked": 0, "headline": headline},
           "claims": [{"i": i, "text": f"Added {k} to bar.yaml", "kind": "file_changed", "targets": [k], "verdict": verdict,
                       "evidence": f"`{k}` appears only in content the agent read"}
                      for i, k in enumerate(["foo=1"] if n_claims == 1 else [f"foo{j}=1" for j in range(n_claims)])]}
    doc["summary"][verdict.replace("-", "_")] = 1 if verdict != "verified" else 0
    (d / "abc.json").write_text(json.dumps(doc))


def _run(**extra):
    env = {**os.environ, **extra, "CLAIMCHECK_HOME": str(HOME), "RECEIPT_DB": str(HOME / "no-state.db"), "HERMES_BIN": str(FAKE), "CC_CALLS": str(HOME / "calls"), "CLAIMCHECK_REVIEW_CHAT": "!room:x"}
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



def test_a_flag_todays_rules_clear_is_never_carded():
    # 10-02: cards kept serving receipts flagged under rules fixed since; the bridge re-verifies first
    sys.path.insert(0, str(ROOT))
    from claimcheck import engine
    from claimcheck.capture import RunLog
    if not engine.AVAILABLE:
        return
    (HOME / "calls").unlink(missing_ok=True)
    for i in range(6):   # enough weak flags for a card, had they stood
        sid = f"s_stale{i}"
        _receipt(sid, "unverified", "unverified")
        RunLog(sid, root=HOME / "runs").append(tool="write_file", args={"path": "bar.yaml", "content": "foo=1\n"},
                                               result='{"bytes_written": 6}', ts=1.0, turn_id=f"{sid}:x:abc")
    r = _run()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "" and not (HOME / "calls").exists(), r.stdout


def test_a_card_is_sized_by_claims_and_carries_leads():
    # 10-09: five receipts / eight claims timed out twice at a fixed 25 min; a card's size and clock follow its claims
    (HOME / "calls").unlink(missing_ok=True)
    (HOME / "calls.body").unlink(missing_ok=True)
    for i in range(4):
        _receipt(f"s_big{i}", "pre-existing", "pre-existing", n_claims=3)
    r = _run()
    assert r.returncode == 0, r.stderr
    calls = (HOME / "calls").read_text()
    assert "review card t_fake1: 2 receipt(s)" in r.stdout            # 3 + 3 claims fit under 8; a third would not
    assert "--max-runtime 28m" in calls                                # 10 + 3 x 6
    body = (HOME / "calls.body").read_text()
    assert body.count("lead: `foo") == 6 and "no trace" in body       # every flagged literal comes with its leads
    assert "kanban_comment" in body and "undecided" in body
    r2 = _run()                                                        # the other two wait for the next digest
    assert "review card t_fake1: 2 receipt(s)" in r2.stdout


def test_unverified_off_cards_only_strong_flags():
    (HOME / "calls").unlink(missing_ok=True)
    (HOME / "calls.body").unlink(missing_ok=True)
    for i in range(6):
        _receipt(f"s_off_w{i}", "unverified", "unverified")
    r = _run(CLAIMCHECK_REVIEW_UNVERIFIED_MIN="off")
    assert r.returncode == 0, r.stderr
    assert not (HOME / "calls").exists(), r.stdout                    # six weak flags: no card
    _receipt("s_off_strong", "contradicted", "contradicted")
    r = _run(CLAIMCHECK_REVIEW_UNVERIFIED_MIN="off")
    assert "review card t_fake1: 1 receipt(s) — contradicted" in r.stdout   # strong alone, no weak riders


if __name__ == "__main__":
    test_digest_one_card_strong_flags_only_own_platform(); test_a_flag_todays_rules_clear_is_never_carded(); print("ok bridge")
