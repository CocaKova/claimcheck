"""Spec v0.1 document tests: schema-valid at every privacy level, content id stable, signature detects tampering."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CLAIMCHECK_HOME", tempfile.mkdtemp(prefix="claimcheck-test-"))

import jsonschema  # noqa: E402

import gzip  # noqa: E402

FIX = Path(tempfile.mkdtemp(prefix="claimcheck-fixture-"))
os.environ["CLAIMCHECK_FIXTURES"] = str(FIX)
from claimcheck.cli import make_receipt  # noqa: E402
from claimcheck.document import apply_privacy, canon, check_id, content_id  # noqa: E402
from claimcheck.sign import sign, verify_signature  # noqa: E402
import claimcheck.ledger as _ledger  # noqa: E402

_ledger.FIXTURES = FIX  # the env var only counts if this file imports claimcheck first; another test may have

SCHEMA = json.loads((ROOT / "spec" / "receipt-v0.1.schema.json").read_text())
V = jsonschema.Draft202012Validator(SCHEMA)
SID = "synthetic_1"
_MSGS = [
    {"id": 1, "role": "user", "content": "add PORT to config and run the tests", "tool_call_id": None, "tool_calls": None, "timestamp": 1790000000.0},
    {"id": 2, "role": "assistant", "content": None, "tool_call_id": None, "timestamp": 1790000001.0,
     "tool_calls": json.dumps([{"id": "c1", "function": {"name": "read_file", "arguments": json.dumps({"path": "/home/dev/app/settings.yaml"})}}])},
    {"id": 3, "role": "tool", "content": "window_width_override: 1280\n", "tool_call_id": "c1", "tool_calls": None, "timestamp": 1790000002.0},
    {"id": 4, "role": "assistant", "content": None, "tool_call_id": None, "timestamp": 1790000003.0,
     "tool_calls": json.dumps([{"id": "c2", "function": {"name": "write_file", "arguments": json.dumps({"path": "/home/dev/app/config.py", "content": "PORT = 8080\n"})}}])},
    {"id": 5, "role": "tool", "content": "ok", "tool_call_id": "c2", "tool_calls": None, "timestamp": 1790000004.0},
    {"id": 6, "role": "assistant", "content": None, "tool_call_id": None, "timestamp": 1790000005.0,
     "tool_calls": json.dumps([{"id": "c3", "function": {"name": "terminal", "arguments": json.dumps({"command": "pytest -q"})}}])},
    {"id": 7, "role": "tool", "content": json.dumps({"output": "9 passed", "exit_code": 0}), "tool_call_id": "c3", "tool_calls": None, "timestamp": 1790000006.0},
    {"id": 8, "role": "assistant", "content": "Done.\n- Wrote `/home/dev/app/config.py` with PORT = 8080.\n- Added window_width_override=1280 to settings.yaml.\n- Ran `pytest -q`: 9 passed.\n", "tool_call_id": None, "tool_calls": None, "timestamp": 1790000007.0},
]
with gzip.open(FIX / f"{SID}.json.gz", "wt") as _f:
    json.dump({"session": {"id": SID, "title": "synthetic", "source": "cli", "model": "m", "started_at": 1790000000.0, "ended_at": 1790000007.0,
                           "input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 0, "estimated_cost_usd": 0.0012}, "messages": _MSGS}, _f)


def _doc(**kw):
    return make_receipt(SID, fixture=True, **kw)


def test_schema_all_privacy_levels():
    for level in ("full", "summary", "hashes"):
        d = _doc(privacy=level, sign=True)
        errs = list(V.iter_errors(d))
        assert not errs, (level, [e.message for e in errs][:3])
        assert check_id(d)
    h = _doc(privacy="hashes")
    assert "text" not in h["report"] and "items" not in h["ledger"]["commands"] and "asked" not in h["run"]


def test_id_is_content_addressed():
    a, b = _doc(), _doc()
    assert a["id"] == b["id"]                          # created_at differs; the id must not
    a.pop("created_at"); b.pop("created_at")
    assert canon(a) == canon(b)


def test_signature_and_tamper():
    d = sign(_doc())
    ok, why = verify_signature(d)
    assert ok, why
    t = copy.deepcopy(d)
    t["summary"]["pre_existing"] = 0                     # make the run look clean (the synthetic run has one)
    ok, why = verify_signature(t)
    assert not ok and "invalid" in why
    assert not check_id(t)
    u = copy.deepcopy(d); u.pop("signature")
    assert verify_signature(u) == (False, "unsigned")


def test_no_floats_in_canon():
    d = _doc()
    canon(d)                                            # raises on any float
    assert d["ledger"]["usage"].get("cost_usd") is None or isinstance(d["ledger"]["usage"]["cost_usd"], str)


if __name__ == "__main__":
    test_schema_all_privacy_levels(); test_id_is_content_addressed(); test_signature_and_tamper(); test_no_floats_in_canon()
    print("ok document")
