"""`claimcheck review`: a second opinion on flagged receipts, from the model the person pinned.

The verifier is code and can be wrong in one direction (a false flag: the fact was in the log, spelled
differently). A review reads the raw run log and says verifier-right or false-flag with the proof. That is
a small reading task, so it runs on the model pinned in `review.model` and never on the agent's default,
which on a metered plan is often the dearest one. No model pinned → no review, with the line that fixes it.

Two ways to run it:
- `endpoint`: one HTTP call to an OpenAI-compatible server (a local vLLM/Ollama/LM Studio/llama.cpp, or any
  URL). A bare chat completion cannot open files, so claimcheck inlines the evidence itself: the turn's run-log
  lines, already redacted, the ones mentioning the flagged literals first, under a size budget.
- an agent CLI (claude-code, codex, gemini, hermes) in its read-only mode, told where the log is.

Reviews are written next to the receipts (`~/.claimcheck/reviews/<receipt-id>.md`) and never edit the
signed receipt.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, local
from .capture import RunLog
from .store import HOME, digest, flagged_claims, is_flagged, iter_receipts

REVIEWS = HOME / "reviews"
TIMEOUT = int(os.environ.get("CLAIMCHECK_REVIEW_TIMEOUT", "600"))
EVIDENCE_BUDGET = int(os.environ.get("CLAIMCHECK_REVIEW_EVIDENCE_CHARS", "28000"))
STDIN_NOTE = "The receipt to review follows on stdin."

INSTRUCTIONS = """You are reviewing a claimcheck receipt of an agent run. Code compared the agent's final report with
its tool log and flagged claims the log does not show. Decide, for each flagged claim, whether the verifier
was RIGHT (the report said something the log does not show) or WRONG (a false flag: the fact is in the log,
spelled differently, or in an earlier turn of the same session). Prove it from the raw run log, which is
one record per tool call (`tool`, `args`, `result`, `status`, `turn_id`). Read only: do not run
anything, do not edit anything, never quote secrets.

Answer with ONE block per flagged claim, in exactly this shape and nothing else:

claim: <the flagged sentence, shortened>
verdict: verifier-right | false-flag
why: <one sentence pointing at the log line: tool, command or path, and what it shows>
rule: <false-flag only: what the verifier should have matched, e.g. "YAML `key: value` equals `key=value`">
"""

VERDICT_RE = re.compile(r"^\s*\**verdict\**:\s*\**(verifier-right|false-flag)", re.I | re.M)
THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


# ---------------------------------------------------------------- CLI agents

def _codex_argv(model: str, prompt: str) -> tuple[list[str], str]:
    """Codex reaches a local model only through --oss; pick its provider from the pinned endpoint's port."""
    argv = ["codex", "exec", "-m", model, "-s", "read-only"]
    ep = config.get("review.endpoint")
    if ep and local.is_local_url(ep):
        argv.append("--oss")
        port = ep.split("//", 1)[-1].split("/", 1)[0].rsplit(":", 1)[-1]
        provider = {"11434": "ollama", "1234": "lmstudio"}.get(port)
        if provider:
            argv += ["--local-provider", provider]
    return argv + ["-"], prompt


# agent → (binary, argv builder(model, prompt) -> (argv, stdin)). Read-only mode on every one of them.
RUNNERS = {
    "claude-code": ("claude", lambda m, p: (["claude", "-p", "--model", m, "--permission-mode", "plan",
                                             "--allowedTools", "Read", "Grep", "Glob", "--output-format", "text"], p)),
    "codex": ("codex", _codex_argv),
    "gemini": ("gemini", lambda m, p: (["gemini", "-m", m, "--approval-mode", "plan", "-p", STDIN_NOTE], p)),
    "hermes": ("hermes", lambda m, p: (["hermes", "chat", "-Q", "--oneshot", "-m", m, "-q", p], "")),
}


def _agent_for(doc: dict) -> str | None:
    pinned = config.effective_agent()
    if pinned:
        return pinned
    adapter = doc.get("run", {}).get("adapter")
    if adapter in RUNNERS and shutil.which(RUNNERS[adapter][0]):
        return adapter
    return next((a for a, (b, _) in RUNNERS.items() if shutil.which(b)), None)


def _log_path(doc: dict) -> Path:
    return RunLog(doc["run"]["session_id"]).path


def _cli_prompt(doc: dict, path: Path) -> str:
    log = _log_path(doc)
    where = f"run log: {log}" if log.exists() else f"run log: none on disk (receipt {path} carries the ledger summary only)"
    return INSTRUCTIONS + "\n" + digest(doc, path) + "\n" + where + "\n"


# ---------------------------------------------------------------- endpoint: inline the evidence

def _literals(doc: dict) -> list[str]:
    out = []
    for c in flagged_claims(doc):
        for t in c.get("targets") or []:
            s = str(t).strip().strip("`'\"")
            s = re.sub(r"(…|\.{2,3})$", "", s)
            if len(s) >= 3:
                out.append(s.lower())
    return sorted(set(out), key=len, reverse=True)


def _fmt(rec: dict, room: int) -> str:
    args = json.dumps(rec.get("args"), ensure_ascii=False)
    res = rec.get("result")
    res = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
    head = f"[{rec.get('ts', '')[:19]}] {rec.get('tool')} status={rec.get('status') or '?'}"
    a = args if len(args) <= 600 else args[:600] + f"… (+{len(args) - 600} chars)"
    cap = max(200, min(1500, room - len(head) - len(a) - 40))
    r = res if len(res) <= cap else res[:cap] + f"… (+{len(res) - cap} chars)"
    return f"{head}\n  args: {a}\n  result: {r}"


def evidence(doc: dict) -> tuple[str, str]:
    """(text, note). The turn's records plus any earlier record of the session that mentions a flagged literal;
    literal hits first, then the turn in order, under EVIDENCE_BUDGET chars. Records are already redacted."""
    log = RunLog(doc["run"]["session_id"])
    if not log.exists():
        return "", "no run log on disk for this session; only the receipt's own evidence lines are available"
    try:
        recs = log.records()
    except (OSError, ValueError) as e:
        return "", f"run log unreadable ({e.__class__.__name__})"
    tid = doc["run"].get("turn_id")
    lits = _literals(doc)

    def hit(r: dict) -> bool:
        blob = (json.dumps(r.get("args"), ensure_ascii=False) + " " + str(r.get("result"))).lower()
        return any(l in blob for l in lits)

    turn = [r for r in recs if tid is None or r.get("turn_id") == tid] or recs
    hits = [r for r in recs if hit(r)]
    order = hits + [r for r in turn if r not in hits]
    out, used, skipped = [], 0, 0
    for r in order:
        block = _fmt(r, EVIDENCE_BUDGET - used)
        if used + len(block) > EVIDENCE_BUDGET:
            skipped += 1
            continue
        out.append(block); used += len(block) + 2
    note = (f"{len(hits)} record(s) mention a flagged literal; {len(turn)} record(s) in this turn; "
            f"{len(out)} shown" + (f", {skipped} left out for size" if skipped else ""))
    return "\n\n".join(out), note


def _endpoint_prompt(doc: dict, path: Path) -> str:
    ev, note = evidence(doc)
    body = digest(doc, path) + f"\n\nrun log excerpt ({note}):\n\n" + (ev or "(none)") + "\n"
    return body


def _post(endpoint: str, api_key: str | None, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "claimcheck"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def run_endpoint(model: str, body: str, *, endpoint: str, api_key: str | None, timeout: int = TIMEOUT) -> tuple[str, str]:
    """(text, error). Exactly one of them is non-empty."""
    payload = {"model": model, "temperature": 0, "max_tokens": 4096,
               "messages": [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": body}]}
    try:
        resp = _post(endpoint, api_key, payload, timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300] if e.fp else ""
        if e.code in (401, 403):
            return "", f"{endpoint} wants an API key (HTTP {e.code}); pin it with `claimcheck config review.api_key <key>`"
        if e.code in (400, 404) and "model" in detail.lower():
            ep = local.probe(endpoint, api_key=api_key)
            have = ", ".join(ep.models[:8]) if ep and ep.models else "nothing listed"
            return "", f"{endpoint} does not serve a model called `{model}` (HTTP {e.code}); it serves: {have}. Pin one with `claimcheck config review.model <model>`"
        return "", f"{endpoint} answered HTTP {e.code}: {detail or e.reason}"
    except urllib.error.URLError as e:
        return "", f"nothing answering at {endpoint} ({e.reason}); is the local server running? `claimcheck local` lists what is"
    except (TimeoutError, OSError) as e:
        return "", f"{endpoint} did not answer within {timeout}s ({e.__class__.__name__})"
    except ValueError:
        return "", f"{endpoint} returned something that is not JSON"
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        err = resp.get("error") if isinstance(resp, dict) else None
        return "", f"{endpoint} returned no choices" + (f": {json.dumps(err)[:200]}" if err else "")
    text = THINK_RE.sub("", (msg.get("content") or "")).strip()
    if not text:
        if msg.get("reasoning_content") or msg.get("reasoning"):
            return "", f"`{model}` spent its whole answer thinking and returned no text; a larger max_tokens or a non-reasoning model fixes it"
        return "", f"`{model}` returned an empty answer"
    return text, ""


# ---------------------------------------------------------------- the command

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


def _write(rid: str, path: Path, model: str, how: str, text: str, t0: float) -> str:
    verdicts = [v.lower() for v in VERDICT_RE.findall(text)]
    REVIEWS.mkdir(parents=True, exist_ok=True)
    head = (f"# review of {rid}\n\nmodel: {model} · {how} · {time.strftime('%Y-%m-%dT%H:%M:%S%z')} · "
            f"{time.time() - t0:.0f}s · receipt: {path}\n\n")
    (REVIEWS / f"{rid}.md").write_text(head + text + "\n")
    summary = ", ".join(f"{verdicts.count(v)} {v}" for v in ("verifier-right", "false-flag") if verdicts.count(v)) or "no verdict line found"
    return f"{rid}: {summary} — {REVIEWS / (rid + '.md')}"


def review(which: str | None = None, *, limit: int = 3, dry_run: bool = False) -> list[str]:
    """Returns one plain line per receipt reviewed (or the reason nothing ran)."""
    model = config.get("review.model")
    if not model:
        raise SystemExit("no review model pinned, so nothing ran. Ask the person which model should review receipts "
                         "(a small one is enough; it reads a log; a local one costs nothing — `claimcheck local` lists any), "
                         "then: claimcheck config review.model <model>")
    items = pending(which, limit=limit)
    if not items:
        return [f"nothing to review: {'no flagged receipt without a review' if not which else 'no receipt ' + which}"]
    lines = []
    for path, doc in items:
        rid = doc["id"]
        agent = _agent_for(doc)
        if agent == "endpoint":
            endpoint = config.get("review.endpoint")
            if not endpoint:
                raise SystemExit("review.agent is endpoint but no review.endpoint is pinned; "
                                 "`claimcheck local` lists local servers, then: claimcheck config review.endpoint <url>")
            body = _endpoint_prompt(doc, path)
            if dry_run:
                lines.append(f"{rid}: would POST to {endpoint}/chat/completions model={model} ({len(body)} chars of evidence)")
                continue
            t0 = time.time()
            text, err = run_endpoint(model, body, endpoint=endpoint, api_key=config.get("review.api_key"))
            if err:
                lines.append(f"{rid}: {err}")
                continue
            lines.append(_write(rid, path, model, f"endpoint: {endpoint}", text, t0))
            continue
        if not agent:
            raise SystemExit("nothing to run the review with: no agent CLI on PATH (claude, codex, gemini, hermes) and no "
                             "review.endpoint pinned. A local server works with no agent at all: `claimcheck local`, then "
                             "claimcheck config review.endpoint <url>")
        binary, argv = RUNNERS[agent]
        exe = shutil.which(binary)
        if not exe:
            raise SystemExit(f"review.agent is {agent} but `{binary}` is not on PATH")
        prompt = _cli_prompt(doc, path)
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
        out = THINK_RE.sub("", r.stdout or "").strip()
        if r.returncode != 0 or not out:
            err = (r.stderr or r.stdout or "").strip()[-300:]
            lines.append(f"{rid}: {agent} on {model} failed (exit {r.returncode}): {err or 'no output'}")
            continue
        lines.append(_write(rid, path, model, f"agent: {agent}", out, t0))
    return lines


def main(argv=None) -> int:  # pragma: no cover - thin
    for line in review(*(argv or [None])):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
