"""claimcheck Hermes plugin — a signed receipt for every run.

post_tool_call  → append the call to the session's hash-chained run log (before storage truncation)
pre_api_request → keep what the model was given (system prompt, injected memory, user messages) as context
post_llm_call   → remember the turn's final report and the user's request
on_session_end  → split the report into claims, check each against the run log, sign, write
                  ~/.claimcheck/receipts/<session>/<turn>.json + .html; then claimcheck.cc if logged in (cloud.py)
subagent_stop   → fold the child's tool history into the parent's chain (metadata only; Hermes passes no args)

Everything is best-effort and fails open: a receipt problem never touches the agent's turn.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import claimcheck  # noqa: F401  (importable in this interpreter)
except ImportError:
    # Hermes runs its own Python. Find the `claimcheck` package next to this file: <repo>/hermes_plugin/ (a
    # source checkout) or <site-packages>/claimcheck/hermes/ (the wheel, symlinked into ~/.hermes/plugins/).
    for _p in Path(__file__).resolve().parents[1:3]:
        if (_p / "claimcheck" / "__init__.py").exists():
            sys.path.insert(0, str(_p))
            break
    import claimcheck  # noqa: F401

from claimcheck.capture import HOME, RunLog, turn_stem  # noqa: E402
from claimcheck.document import build  # noqa: E402
from claimcheck.engine import extract_claims, verify as verify_claim  # noqa: E402
from claimcheck.ledger import DB, ledger_from_events, load as load_stored, events_from_messages, redact  # noqa: E402

RECEIPTS = HOME / "receipts"

_turns: dict[str, dict] = {}       # session_id → {turn_id, asked, final, model, platform, started}
_turn_started: dict[tuple, float] = {}
_settings: dict = {}
_lock = threading.Lock()


# ---------- config ----------

def _cfg(ctx, key, default):
    try:
        v = ctx.get_config(key, default)
        return default if v is None else v
    except Exception:
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    v = os.environ.get(name)
    return [x.strip() for x in v.split(",") if x.strip()] if v else default


# ---------- hooks ----------

def on_post_tool_call(*, tool_name=None, args=None, result=None, session_id=None, tool_call_id=None,
                      turn_id=None, duration_ms=0, status=None, **_):
    if not session_id or not tool_name:
        return
    try:
        now = time.time()
        _turn_started.setdefault((session_id, turn_id), now)
        RunLog(session_id).append(tool=tool_name, args=args or {}, result=result, ts=now, turn_id=turn_id,
                                  tool_call_id=tool_call_id, status=status, duration_ms=duration_ms)
    except Exception as e:  # never hurt the turn
        logger.warning("claimcheck: capture failed for %s: %s", tool_name, e)


def on_pre_api_request(*, session_id=None, turn_id=None, system_prompt=None, request_messages=None, **_):
    """Whatever memory provider, context file or plugin fed the prompt, it is in this request. Repeats are
    cheap: a block the session already has is skipped by hash before any disk work."""
    if not session_id:
        return
    try:
        from claimcheck import context
        context.add_many(session_id, turn_id, context.blocks_from_messages(request_messages, system_prompt))
    except Exception as e:  # never hurt the turn
        logger.warning("claimcheck: context capture failed: %s", e)


def on_post_llm_call(*, session_id=None, turn_id=None, user_message=None, assistant_response=None,
                     model=None, platform=None, **_):
    if not session_id:
        return
    with _lock:
        _turns[session_id] = {"turn_id": turn_id, "asked": user_message or "", "final": assistant_response or "",
                              "model": model, "platform": platform,
                              "started": _turn_started.get((session_id, turn_id), time.time())}


def on_subagent_stop(*, parent_session_id=None, child_session_id=None, tool_call_history=None, parent_turn_id=None, **_):
    if not parent_session_id or not tool_call_history:
        return
    try:
        log = RunLog(parent_session_id)
        for h in tool_call_history:
            if isinstance(h, dict):
                log.append(tool=h.get("tool_name") or "?", args=h.get("tool_input") or {}, result=None, ts=time.time(),
                           turn_id=parent_turn_id or None, status=h.get("status"), agent_id=child_session_id)
    except Exception as e:
        logger.warning("claimcheck: subagent capture failed: %s", e)


def on_session_end(*, session_id=None, turn_id=None, completed=None, failed=None, interrupted=None,
                   model=None, platform=None, **_):
    if not session_id:
        return
    try:
        if platform and platform in _settings.get("skip_platforms", []):
            return
        with _lock:
            turn = _turns.pop(session_id, None)
            for k in [k for k in _turn_started if k[0] == session_id]:
                _turn_started.pop(k, None)
        doc, path = make_run_receipt(session_id, turn, turn_id=turn_id, model=model, platform=platform,
                                     privacy=_settings.get("privacy", "full"),
                                     chatty=bool(_settings.get("receipt_chatty", False)),
                                     interrupted=bool(interrupted))
        if not doc:
            return
        S = doc["summary"]
        logger.info("claimcheck: %s · %s verified · %s unverified · %s pre-existing · %s contradicted · %s → %s",
                    doc["id"], S["verified"], S["unverified"], S["pre_existing"], S["contradicted"], S["headline"], path)
        threading.Thread(target=_after, args=(doc, path), daemon=True).start()   # witness flush + share, off the turn
    except Exception as e:
        logger.warning("claimcheck: receipt failed for %s: %s", session_id, e)


# ---------- receipt ----------

def _session_row(session_id: str) -> dict:
    """Token/cost/title columns from state.db when readable; the run log has no usage numbers."""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=1)
        con.row_factory = sqlite3.Row
        r = con.execute("select * from sessions where id=?", (session_id,)).fetchone()
        con.close()
        return dict(r) if r else {}
    except Exception:
        return {}


def make_run_receipt(session_id: str, turn: dict | None, *, turn_id=None, model=None, platform=None,
                     privacy="full", chatty=False, interrupted=False, sign=True, out_root: Path | None = None):
    """Build, sign and write the receipt for one run. Returns (doc, path) or (None, None) when skipped."""
    log = RunLog(session_id)
    live = log.exists()
    turn = turn or {}
    tid = turn.get("turn_id") or turn_id
    if live:
        all_events = log.events()
        turn_events = [e for e in all_events if tid is None or e.get("turn_id") == tid] or all_events
        final, asked = turn.get("final", ""), turn.get("asked", "")
        capture = {"mode": "live", "chain_head": log.head(), "events": len(turn_events),
                   "log_ref": f"runs/{log.path.name}"}
        msgs = [{"role": "user", "content": asked}, {"role": "assistant", "content": final, "tool_calls": None}]
        session = {"id": session_id, "model": model or turn.get("model"), "source": platform or turn.get("platform"),
                   "started_at": turn.get("started") or (turn_events[0]["ts"] if turn_events else time.time()),
                   "ended_at": time.time()}
    else:  # plugin enabled after the run started, or a stored session: rebuild at rest
        try:
            session, stored = load_stored(session_id)
        except (SystemExit, Exception):   # no stored session (or no readable DB): nothing to receipt
            return None, None
        all_events = events_from_messages(stored)
        turn_events = all_events
        final = next((m["content"] for m in reversed(stored) if m["role"] == "assistant" and m["content"]), "") or ""
        asked = next((m["content"] for m in stored if m["role"] == "user" and m["content"]), "") or ""
        capture = None
        msgs = stored
    if not final and not interrupted:
        return None, None
    if not turn_events and not chatty:
        return None, None
    row = _session_row(session_id)
    for k in ("title", "input_tokens", "output_tokens", "reasoning_tokens", "estimated_cost_usd", "profile_name"):
        if row.get(k) is not None and session.get(k) is None:
            session[k] = row[k]
    L_all = ledger_from_events(all_events)          # claims may refer to earlier turns of the same session
    L_turn = ledger_from_events(list(turn_events)) if turn_events is not all_events else L_all
    L_all["inputs"] = [redact(asked)] if asked else []   # a cron job's prompt + injected script output; never proves work
    L_all["context"] = _context(session_id, tid)            # what the model was given: backs facts, never work
    claims = extract_claims(redact(final), use_llm=False) if final else []
    for c in claims:
        c["verdict"], c["evidence"] = verify_claim(c, L_all)
    doc = build(session, msgs, L_turn, claims, adapter="hermes", platform=platform or session.get("source") or "hermes",
                classifier="none", privacy=privacy, capture=capture, turn_id=tid)
    if sign:
        from claimcheck.sign import sign as _sign
        _sign(doc)
    out = (out_root or RECEIPTS) / session_id
    out.mkdir(parents=True, exist_ok=True)
    stem = turn_stem(tid)
    path = out / f"{stem}.json"
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    try:
        from claimcheck.page import render
        path.with_suffix(".html").write_text(render(doc, None))
    except Exception as e:
        logger.debug("claimcheck: page render failed: %s", e)
    return doc, path


def _context(session_id: str, turn_id) -> list[str]:
    try:
        from claimcheck import context
        return context.load(session_id, turn_id)
    except Exception as e:
        logger.debug("claimcheck: context load failed: %s", e)
        return []


def _after(doc: dict, path: Path) -> None:
    try:
        from claimcheck.cloud import after_receipt
        link = after_receipt(doc, path)
        if link:
            logger.info("claimcheck: shared %s → %s", doc["id"], link)
    except Exception as e:  # sharing is a convenience; the local receipt stands
        logger.warning("claimcheck: cloud: %s", e)


# ---------- entry ----------

def register(ctx) -> None:
    _settings.update({
        "privacy": _cfg(ctx, "privacy", os.environ.get("CLAIMCHECK_PRIVACY", "full")),
        "skip_platforms": _cfg(ctx, "skip_platforms", _env_list("CLAIMCHECK_SKIP_PLATFORMS", [])),
        "receipt_chatty": _cfg(ctx, "receipt_chatty", False),
    })
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("subagent_stop", on_subagent_stop)
    logger.debug("claimcheck plugin registered (privacy=%s)", _settings["privacy"])
