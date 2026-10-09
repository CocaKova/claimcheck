"""The context store: dedup by content, bounded loads, transcript reading from an offset."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CLAIMCHECK_HOME", tempfile.mkdtemp(prefix="claimcheck-ctx-"))

from claimcheck import context  # noqa: E402


def test_blocks_dedupe_within_and_across_sessions():
    sysp = "You are a careful agent. " * 50
    assert context.add_many("ctx_a", "t1", [("system", sysp), ("user", "please check the backups tonight")]) == 2
    assert context.add_many("ctx_a", "t2", [("system", sysp), ("user", "please check the backups tonight")]) == 0
    assert context.add_many("ctx_b", "t1", [("system", sysp)]) == 1          # indexed per session…
    assert len(list(context.BLOBS.rglob(f"{context.add('ctx_b', 't1', 'system', sysp)}*"))) == 1   # …stored once
    assert context.add("ctx_a", "t1", "user", "ok") is None                  # too short to rest a claim on


def test_secrets_are_redacted_before_storage():
    sha = context.add("ctx_s", "t1", "user", "deploy with STRIPE_KEY=sk_live_abcdefghijklmnop1234 to prod please")
    blob = context.load("ctx_s")[0]
    assert "sk_live_abcdefghijklmnop1234" not in blob and "[REDACTED]" in blob and sha


def test_load_stops_at_the_turn_and_at_the_cap(monkeypatch=None):
    for i in range(1, 4):
        context.add("ctx_t", f"t{i}", "user", f"turn {i} says the deploy window is {i}0 minutes long")
    assert len(context.load("ctx_t", "t2")) == 2 and "turn 3" not in " ".join(context.load("ctx_t", "t2"))
    assert len(context.load("ctx_t", "unknown-turn")) == 3
    old = context.MAX_LOAD
    context.MAX_LOAD = 60
    try:
        got = context.load("ctx_t")
        assert len(got) == 1 and "turn 3" in got[0]                          # newest first under the cap
    finally:
        context.MAX_LOAD = old


def test_message_shapes_and_roles():
    msgs = [{"role": "system", "content": "sys prompt text that is long enough"},
            {"role": "user", "content": [{"type": "text", "text": "memory: the NAS is at 10.0.0.5"}, {"type": "image_url"}]},
            {"role": "assistant", "content": "I think the NAS is at 10.9.9.9"},
            {"role": "tool", "content": "tool output lives in the run log"}]
    got = list(context.blocks_from_messages(msgs, system_prompt="instructions block, long enough to keep"))
    assert [s for s, _ in got] == ["system", "system", "user"]
    assert "10.9.9.9" not in json.dumps(got)


def test_transcript_reads_only_new_lines():
    d = Path(tempfile.mkdtemp())
    tr = d / "t.jsonl"
    lines = [
        {"type": "attachment", "attachment": {"type": "instructions"}, "rendered": [{"content": "MEMORY.md: deploy host is build-host-01"}]},
        {"type": "user", "message": {"content": "what's the deploy host again?"}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "a tool result"}]}},
        {"type": "assistant", "message": {"content": "it's build-host-01"}},
    ]
    tr.write_text("".join(json.dumps(x) + "\n" for x in lines))
    got = list(context.blocks_from_transcript(tr, "ctx_tr"))
    assert [s for s, _ in got] == ["attachment:instructions", "user"]
    assert list(context.blocks_from_transcript(tr, "ctx_tr")) == []          # nothing new
    with open(tr, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "and the backup host?"}}) + "\n")
        f.write('{"type": "user", "message": {"content": "half a li')          # still being written
    assert [t for _, t in context.blocks_from_transcript(tr, "ctx_tr")] == ["and the backup host?"]
