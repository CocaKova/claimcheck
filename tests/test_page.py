"""Receipt page: consequential actions the report never mentions, and a headline that owns its unchecked claims."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from claimcheck.page import render, unreported  # noqa: E402

DEMO = json.loads((Path(__file__).resolve().parent.parent / "site" / "demo-receipt.json").read_text())


def _items(*cmds):
    return [{"i": i, "text": c} for i, c in enumerate(cmds)]


def test_silent_push_and_restart_are_listed():
    items = _items("cd ~/app && git push -q origin main", "systemctl --user restart hermes-gateway.service", "ls -la")
    got = unreported(items, "Fixed the parser and added a test.")
    assert ("pushed", "git push -q origin main") in got
    assert ("stopped", "systemctl --user restart hermes-gateway.service") in got
    assert len(got) == 2


def test_mentioned_actions_are_not_listed():
    items = _items("git push -q origin main", "systemctl --user restart hermes-gateway.service")
    assert unreported(items, "Pushed to main and restarted the gateway.") == []
    # naming the thing is enough
    assert unreported(_items("rm -f ~/workspace/app/notes.md"), "notes.md was stale, so it's gone.") == []


def test_housekeeping_is_not_listed():
    items = _items("rm -rf /tmp/build-123", "rm -rf $S/venv", "rm -rf build dist __pycache__", "chmod +x run.sh",
                   "python3 -m http.server 8765 & echo $! > srv.pid", "kill $(cat srv.pid)", "pkill -f 'http.server 8765'",
                   "sudo -n true 2>/dev/null")
    assert unreported(items, "Built the page.") == []


def test_real_deletes_still_listed():
    got = unreported(_items("rm -f ~/workspace/omarchy/.git/reply-round3.md", "git rm -rq notes"), "Posted the reply.")
    assert [k for k, _ in got] == ["deleted", "deleted"]


def _doc(verdicts, items=()):
    """The site's demo receipt with these claims and commands."""
    d = copy.deepcopy(DEMO)
    d["claims"] = [{"text": f"claim {i}", "kind": "other", "targets": [], "verdict": v, "evidence": ""} for i, v in enumerate(verdicts)]
    d["ledger"]["commands"].update({"total": len(items), "failed": 0, "remote": 0, "items": list(items)})
    d["report"]["text"] = "Did the thing."
    return d


def test_headline_owns_unchecked_claims():
    page = render(_doc(["verified", "verified", "unchecked", "unchecked", "unchecked"]))
    assert "Every claim it could check checks out: 2 of 5." in page
    assert "Every claim in this run checks out." in render(_doc(["verified", "verified"]))


def test_page_lists_silent_actions():
    page = render(_doc(["verified"], _items("git push -q origin main")))
    assert "didn&#x27;t mention" in page or "didn't mention" in page
    assert "git push -q origin main" in page
    assert "Things it did but didn" not in render(_doc(["verified"], _items("ls")))
