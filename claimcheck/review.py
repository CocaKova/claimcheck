"""`claimcheck review`: a second opinion on flagged receipts, from the model the person pinned.

The verifier is code and can be wrong in one direction (a false flag: the fact was in the log, spelled
differently). A review reads the raw run log and says verifier-right or false-flag with the proof. That is
a small reading task, so it runs on the model pinned in `review.model` and never on the agent's default,
which on a metered plan is often the dearest one. No model pinned → no review, with the line that fixes it.

Reviews are written next to the receipts (`~/.claimcheck/reviews/<receipt-id>.md`) and never edit the
signed receipt. Every runner is read-only (plan / read-only sandbox) and gets the prompt on stdin.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .store import HOME, digest, is_flagged, iter_receipts

REVIEWS = HOME / "reviews"
TIMEOUT = int(__import__("os").environ.get("CLAIMCHECK_REVIEW_TIMEOUT", "600"))

STDIN_NOTE = "The receipt to review follows on stdin."

# agent → (binary, argv builder(model, prompt) -> (argv, stdin)). Read-only mode on every one of them.
RUNNERS = {
    "claude-code": ("claude", lambda m, p: (["claude", "-p", "--model", m, "--permission-mode", "plan",
                                             "--allowedTools", "Read", "Grep", "Glob", "--output-format", "text"], p)),
    "codex": ("codex", lambda m, p: (["codex", "exec", "-m", m, "-s", "read-only", "-"], p)),
    "gemini": ("gemini", lambda m, p: (["gemini", "-m", m, "--approval-mode", "plan", "-p", STDIN_NOTE], p)),
    "hermes": ("hermes", lambda m, p: (["hermes", "chat", "-Q", "--oneshot", "-m", m, "-q", p], "")),
}

INSTRUCTIONS = """You are reviewing a claimcheck receipt of an agent run. Code compared the agent's final report with
its tool log and flagged claims the log does not show. Decide, for each flagged claim, whether the verifier
was RIGHT (the report said something the log does not show) or WRONG (a false flag: the fact is in the log,
spelled differently, or in an earlier turn of the same session). Prove it from the raw run log, which is
one JSON line per tool call (`tool`, `args`, `result`, `status`, `turn_id`). Read only: do not run
anything, do not edit anything, never quote secrets.

Answer with ONE block per flagged claim, in exactly this shape and nothing else:

claim: <the flagged sentence, shortened>
verdict: verifier-right | false-flag
why: <one sentence pointing at the log line: tool, command or path, and what it shows>
rule: <false-flag only: what the verifier should have matched, e.g. "YAML `key: value` equals `key=value`">
"""

VERDICT_RE = re.compile(r"^verdict:\s*(verifier-right|false-flag)", re.I | re.M)


def _agent_for(doc: dict) -> str | None:
    pinned = config.get("review.agent")
    if pinned:
        return pinned
    adapter = doc.get("run", {}).get("adapter")
    if adapter in RUNNERS and shutil.which(RUNNERS[adapter][0]):
        return adapter
    return next((a for a, (b, _) in RUNNERS.items() if shutil.which(b)), None)


def _prompt(doc: dict, path: Path) -> str:
    sid = doc["run"]["session_id"]
    log = HOME / "runs" / f"{sid}.jsonl"
    where = f"run log: {log}" if log.exists() else f"run log: none on disk (receipt {path} carries the ledger summary only)"
    return INSTRUCTIONS + "\n" + digest(doc, path) + "\n" + where + "\n"


def pending(which: str | None = None, *, limit: int = 3) -> list[tuple[Path, dict]]:
    """Flagged receipts with no review yet, newest first (or the one receipt/session named)."""
    out = []
    for p, d in iter_receipts():
        rid = d.get("id")
        if which and which not in (rid, d["run"].get("session_id")):
            continue
        if not which and (not is_flagged(d) or (REVIEWS / f"{rid}.md").exists()):
            continue
        out.append((p, d))
        if len(out) >= limit:
            break
    return out


def review(which: str | None = None, *, limit: int = 3, dry_run: bool = False) -> list[str]:
    """Returns one plain line per receipt reviewed (or the reason nothing ran)."""
    model = config.get("review.model")
    if not model:
        raise SystemExit("no review model pinned, so nothing ran. Ask the person which model should review receipts "
                         "(a small one is enough; it reads a log), then: claimcheck config review.model <model>")
    items = pending(which, limit=limit)
    if not items:
        return [f"nothing to review: {'no flagged receipt without a review' if not which else 'no receipt ' + which}"]
    lines = []
    for path, doc in items:
        rid = doc["id"]
        agent = _agent_for(doc)
        if not agent:
            raise SystemExit("no agent CLI found to run the review (looked for claude, codex, gemini, hermes); "
                             "pin one with: claimcheck config review.agent <name>")
        binary, argv = RUNNERS[agent]
        exe = shutil.which(binary)
        if not exe:
            raise SystemExit(f"review.agent is {agent} but `{binary}` is not on PATH")
        prompt = _prompt(doc, path)
        cmd, stdin = argv(model, prompt)
        cmd = [exe, *cmd[1:]]
        if dry_run:
            shown = " ".join(a if len(a) < 60 else "<prompt>" for a in cmd)
            lines.append(f"{rid}: would run {agent} on {model}: {shown}  ({len(prompt)} chars of prompt)")
            continue
        t0 = time.time()
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            lines.append(f"{rid}: {agent} on {model} timed out after {TIMEOUT}s; nothing written")
            continue
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            err = (r.stderr or r.stdout or "").strip()[-300:]
            lines.append(f"{rid}: {agent} on {model} failed (exit {r.returncode}): {err or 'no output'}")
            continue
        verdicts = [v.lower() for v in VERDICT_RE.findall(out)]
        REVIEWS.mkdir(parents=True, exist_ok=True)
        head = (f"# review of {rid}\n\nmodel: {model} · agent: {agent} · {time.strftime('%Y-%m-%dT%H:%M:%S%z')} · "
                f"{time.time() - t0:.0f}s · receipt: {path}\n\n")
        (REVIEWS / f"{rid}.md").write_text(head + out + "\n")
        summary = ", ".join(f"{verdicts.count(v)} {v}" for v in ("verifier-right", "false-flag") if verdicts.count(v)) or "no verdict line found"
        lines.append(f"{rid}: {summary} — {REVIEWS / (rid + '.md')}")
    return lines


def main(argv=None) -> int:  # pragma: no cover - thin
    for line in review(*(argv or [None])):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
