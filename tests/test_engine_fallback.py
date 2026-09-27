"""Without claimcheck-core the hook still writes a signed, schema-valid, ledger-only receipt."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="claimcheck-nocore-"))


def test_ledger_only_receipt_without_core():
    env = {**os.environ, "CLAIMCHECK_HOME": str(HOME), "PYTHONPATH": str(ROOT), "CLAIMCHECK_CORE_PATH": "/nonexistent"}
    # hide a sibling checkout too
    code = "import sys; sys.modules['claimcheck_core']=None; from claimcheck.hook import main; sys.exit(main())"
    for payload in ({"session_id": "nc1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "true"}, "tool_response": {"stdout": ""}},
                    {"session_id": "nc1", "hook_event_name": "Stop", "last_assistant_message": "Ran `true`."}):
        r = subprocess.run([sys.executable, "-c", code], input=json.dumps(payload), capture_output=True, text=True, env=env, cwd=ROOT)
        assert r.returncode == 0, r.stderr
    d = json.loads(next((HOME / "receipts" / "nc1").glob("*.json")).read_text())
    assert d["claims"] == [] and d["ledger"]["tool_calls"] == 1 and d["signature"]
    assert d["verifier"]["name"].startswith("none") and d["summary"]["headline"] == "unchecked"
    import jsonschema
    jsonschema.Draft202012Validator(json.loads((ROOT / "spec" / "receipt-v0.1.schema.json").read_text())).validate(d)


if __name__ == "__main__":
    test_ledger_only_receipt_without_core(); print("ok engine fallback")
