"""The verdict engine: `claimcheck-core` when installed, ledger-only mode when it is not.

The public package captures, chains, redacts, signs and renders. Splitting a report into claims and deciding
verdicts lives in the separately distributed `claimcheck_core` (binary wheels). Without it a receipt still
carries the full ledger and the report; `claims` is empty and `verifier.name` says why.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# dev convenience: a sibling checkout of the private core repo, or CLAIMCHECK_CORE_PATH
for _p in [os.environ.get("CLAIMCHECK_CORE_PATH"), str(Path(__file__).resolve().parent.parent.parent / "claimcheck-core")]:
    if _p and Path(_p, "claimcheck_core").is_dir() and _p not in sys.path:
        sys.path.append(_p)

try:
    import claimcheck_core as _core
    AVAILABLE = True
    VERSION = _core.__version__
    RULES_SHA256 = _core.RULES_SHA256
    MISSING = ""
except ImportError as e:  # pragma: no cover
    _core = None
    AVAILABLE = False
    VERSION = "0"
    RULES_SHA256 = "0" * 64
    MISSING = f"claimcheck-core not installed ({e.__class__.__name__}); run: pip install claimcheck-core"


def extract_claims(report: str, use_llm: bool = False) -> list[dict]:
    if not AVAILABLE or not report:
        return []
    return _core.extract_claims(report, use_llm=use_llm)


def verify(claim: dict, ledger: dict) -> tuple[str, str]:
    if not AVAILABLE:
        return "unchecked", MISSING
    return _core.verify(claim, ledger)


def verifier_block(classifier: str = "none", classifier_model: str | None = None) -> dict:
    """The receipt's `verifier` member."""
    if AVAILABLE:
        return {"name": "claimcheck-core", "version": VERSION, "rules_sha256": RULES_SHA256,
                "classifier": classifier, "classifier_model": classifier_model}
    return {"name": "none (claimcheck-core not installed)", "version": "0", "rules_sha256": RULES_SHA256, "classifier": "none"}
