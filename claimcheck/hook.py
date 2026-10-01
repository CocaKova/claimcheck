"""`claimcheck hook` — one stdin-JSON hook for every agent that has hooks.

Claude Code, Codex CLI, Gemini CLI, Cursor, GitHub Copilot CLI, Cline and Hermes' shell hooks all hand a
hook one JSON object on stdin. The field names differ; the facts don't: which session, which tool, what
went in, what came out, and (at the end of a turn) what the agent finally said. This module reads any of
those shapes, appends tool calls to the session's hash-chained run log, and turns the end of a turn into a
signed receipt. It never blocks the agent: every path exits 0, and any error goes to the log file.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from .capture import HOME, RunLog, turn_stem

logger = logging.getLogger("claimcheck.hook")
LOG_FILE = HOME / "hook.log"

# Which canonical event a platform's event name means.
POST_TOOL = {"posttooluse", "posttoolusefailure", "aftertool", "aftershellexecution", "aftermcpexecution",
             "afterfileedit", "post_tool_call", "postmcptooluse", "afterfilewrite"}
PROMPT = {"userpromptsubmit", "beforeagent", "user_prompt_submit"}
TURN_END = {"stop", "afteragent", "afteragentresponse", "agentstop", "taskcomplete", "on_session_end",
            "sessionend", "session_end", "post_cascade_response", "post_cascade_response_with_transcript"}

# Platform tool names → the canonical names the ledger and verifier reason about.
TOOL_MAP = {
    # commands
    "bash": "terminal", "shell": "terminal", "run_shell_command": "terminal", "execute_command": "terminal",
    "run_terminal_cmd": "terminal", "terminal": "terminal", "powershell": "terminal", "exec": "terminal",
    "local_shell": "terminal", "container.exec": "terminal", "execute_code": "execute_code",
    # writes
    "write": "write_file", "write_file": "write_file", "create_file": "write_file", "writefile": "write_file",
    "edit": "patch", "multiedit": "patch", "notebookedit": "patch", "replace": "patch", "apply_patch": "patch",
    "patch": "patch", "edit_file": "patch", "search_replace": "patch", "str_replace_editor": "patch",
    "write_to_file": "write_file", "apply_diff": "patch", "insert_content": "patch",
    # reads
    "read": "read_file", "read_file": "read_file", "readfile": "read_file", "cat": "read_file", "view": "read_file",
    "grep": "search_files", "glob": "search_files", "search_files": "search_files", "list_dir": "search_files",
    "ls": "search_files", "list_directory": "search_files", "codebase_search": "search_files", "read_many_files": "read_file",
    "search_file_content": "search_files", "glob_search": "search_files",
}


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _text(x) -> str:
    """Tool output as text, whatever the platform wrapped it in."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        parts = []
        for k in ("stdout", "output", "llmContent", "textResultForLlm", "content", "text", "result", "returnDisplay", "message"):
            v = x.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
            elif isinstance(v, list):
                parts.extend(_text(i) for i in v)
        if x.get("stderr"):
            parts.append(str(x["stderr"]))
        if x.get("error"):
            parts.append(f"error: {x['error']}")
        return "\n".join(p for p in parts if p) or json.dumps(x, ensure_ascii=False)[:20000]
    if isinstance(x, list):
        return "\n".join(_text(i) for i in x)
    return str(x)


def _exit_code(resp, event: str, status: str | None):
    if isinstance(resp, dict):
        for k in ("exit_code", "exitCode", "returncode", "return_code", "code"):
            if isinstance(resp.get(k), int):
                return resp[k]
        if isinstance(resp.get("metadata"), dict) and isinstance(resp["metadata"].get("exit_code"), int):
            return resp["metadata"]["exit_code"]
    if isinstance(resp, str) and resp.lstrip().startswith("{"):
        try:
            return _exit_code(json.loads(resp), event, status)
        except ValueError:
            pass
    if event == "posttoolusefailure" or status == "error":
        return 1
    return None


def normalize(p: dict) -> dict:
    """One dict for every platform: platform, event, session_id, turn_id, cwd, tool, args, output,
    exit_code, status, tool_call_id, final, asked, transcript_path, agent_id."""
    ev_raw = str(_first(p, "hook_event_name", "hookName", "event", "agent_action_name", default="")).strip()
    ev = ev_raw.lower()
    tool = _first(p, "tool_name", "toolName", "tool", default="")
    args = _first(p, "tool_input", "toolArgs", "args", "parameters", "tool_args", default={})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"command": args} if TOOL_MAP.get(str(tool).lower()) == "terminal" else {"_raw": args}
    extra = p.get("extra") if isinstance(p.get("extra"), dict) else {}
    resp = _first(p, "tool_response", "tool_output", "toolResult", "tool_result", "result", "response",
                  default=_first(extra, "result", default=None))
    if isinstance(resp, str) and resp.lstrip().startswith("{") and ev in POST_TOOL:
        try:  # Hermes shell hooks and some MCP wrappers pass the JSON result as a string
            resp = json.loads(resp)
        except ValueError:
            pass
    tool_info = p.get("tool_info") if isinstance(p.get("tool_info"), dict) else {}
    if tool_info:  # Windsurf/Devin desktop
        tool = tool or ("terminal" if "command_line" in tool_info else "mcp")
        args = args or {"command": tool_info.get("command_line", "")}
        resp = resp if resp is not None else tool_info.get("mcp_result") or tool_info.get("response")
    platform = _platform(p, ev)
    status = _first(p, "status", default=None)
    if status is None and isinstance(p.get("success"), bool):
        status = "ok" if p["success"] else "error"
    if ev == "posttoolusefailure":
        status = "error"
    canon_tool = TOOL_MAP.get(str(tool).lower(), str(tool) or "?")
    if canon_tool == "terminal" and isinstance(args, dict):
        cmd = args.get("command")
        if isinstance(cmd, list):
            args = {**args, "command": " ".join(str(c) for c in cmd)}
        elif cmd is None and args.get("cmd"):
            args = {**args, "command": str(args["cmd"])}
    final = _first(p, "last_assistant_message", "prompt_response", "text", "final_response", "assistant_response", default="")
    if not final and isinstance(tool_info.get("response"), str):
        final = tool_info["response"]
    return {
        "platform": platform,
        "event": "post_tool" if ev in POST_TOOL else ("turn_end" if ev in TURN_END else ("prompt" if ev in PROMPT else ev or "unknown")),
        "event_raw": ev_raw,
        "session_id": str(_first(p, "session_id", "conversation_id", "sessionId", "taskId", "trajectory_id", default="") or ""),
        "turn_id": _first(p, "turn_id", "generation_id", "prompt_id", "execution_id", default=None),
        "cwd": _first(p, "cwd", default=None),
        "tool": canon_tool,
        "tool_raw": str(tool),
        "args": args if isinstance(args, dict) else {"_raw": args},
        "output": _text(resp),
        "exit_code": _exit_code(resp, ev, status),
        "status": status or ("ok" if ev in POST_TOOL else None),
        "tool_call_id": _first(p, "tool_use_id", "tool_call_id", "callID", "call_id", default=None),
        "final": final if isinstance(final, str) else _text(final),
        "asked": _first(p, "prompt", "user_message", "user_prompt", default="") or "",
        "transcript_path": _first(p, "transcript_path", default=None),
        "agent_id": _first(p, "agent_id", "agent_type", "parent_session_id", default=None),
        "model": _first(p, "model", "model_name", default=None),
    }


def _platform(p: dict, ev: str) -> str:
    if "conversation_id" in p and "generation_id" in p:
        return "cursor"
    if "hookName" in p and "clineVersion" in p:
        return "cline"
    if "trajectory_id" in p or "agent_action_name" in p:
        return "windsurf"
    if "sessionId" in p or "toolName" in p or "toolArgs" in p:
        return "copilot"
    if ev in ("aftertool", "afteragent", "beforetool", "beforeagent") or "prompt_response" in p:
        return "gemini"
    if "extra" in p and "args" in p:
        return "hermes"
    if "turn_id" in p and "permission_mode" in p:
        return "codex"
    if "transcript_path" in p or "stop_hook_active" in p or "tool_use_id" in p or "permission_mode" in p:
        return "claude-code"
    return os.environ.get("CLAIMCHECK_PLATFORM", "other")


# ---------- the two actions ----------

def record_tool(n: dict) -> dict | None:
    if not n["session_id"] or n["tool"] in ("?", ""):
        return None
    payload = {"output": n["output"]}
    if n["exit_code"] is not None:
        payload["exit_code"] = n["exit_code"]
    return RunLog(n["session_id"]).append(tool=n["tool"], args=n["args"], result=json.dumps(payload, ensure_ascii=False),
                                          ts=time.time(), turn_id=n["turn_id"], tool_call_id=n["tool_call_id"],
                                          status=n["status"], agent_id=n["agent_id"], exit_code=n["exit_code"])


def finish_turn(n: dict, *, privacy: str = "full", sign: bool = True, chatty: bool = False):
    """Receipt for the session's most recent turn. Claims come from the final message when the platform
    hands it over (Claude Code, Codex, Gemini, Cursor); otherwise the receipt is ledger-only."""
    from .document import build
    from .engine import extract_claims, verify as verify_claim
    from .ledger import ledger_from_events, redact
    from .page import render
    from .store import RECEIPTS

    sid = n["session_id"]
    if not sid:
        return None, None
    log = RunLog(sid)
    all_events = log.events()
    tid = n["turn_id"]
    turn_events = [e for e in all_events if e.get("turn_id") == tid] if tid else all_events
    if tid and not turn_events:
        turn_events = all_events
    if not turn_events and not chatty:
        return None, None
    # mark the turn boundary so the next turn's ledger starts fresh even when the platform has no turn ids
    final = redact(n["final"] or "")
    L_all = ledger_from_events(all_events)
    L_turn = ledger_from_events(list(turn_events)) if turn_events is not all_events else L_all
    asked = n["asked"] or recall_prompt(sid)
    L_all["inputs"] = [redact(asked)] if asked else []   # what it was given backs claims about its input, never work
    claims = extract_claims(final, use_llm=False) if final else []
    for c in claims:
        c["verdict"], c["evidence"] = verify_claim(c, L_all)
    started = turn_events[0]["ts"] if turn_events else time.time()
    session = {"id": sid, "model": n["model"], "source": n["platform"], "started_at": started, "ended_at": time.time()}
    msgs = [{"role": "user", "content": asked, "tool_calls": None}, {"role": "assistant", "content": final, "tool_calls": None}]
    doc = build(session, msgs, L_turn, claims, adapter=n["platform"], platform=n["platform"], classifier="none",
                privacy=privacy, turn_id=tid, capture={"mode": "live", "chain_head": log.head(),
                                                       "events": len(turn_events), "log_ref": f"runs/{log.path.name}"})
    if n["cwd"]:
        doc["run"]["cwd"] = str(n["cwd"])[:300]
        doc["id"] = __import__("claimcheck.document", fromlist=["content_id"]).content_id(doc)
    if sign:
        from .sign import sign as _sign
        _sign(doc)
    out = RECEIPTS / sid
    out.mkdir(parents=True, exist_ok=True)
    stem = turn_stem(tid)
    path = out / f"{stem}.json"
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    try:
        path.with_suffix(".html").write_text(render(doc, None))
    except Exception as e:  # the JSON is the receipt; the page is a convenience
        logger.debug("page render failed: %s", e)
    _turn_marker(sid, tid)
    return doc, path


def _asked_path(sid: str) -> Path:
    return HOME / "runs" / f"{RunLog(sid).path.stem}.asked"


def remember_prompt(n: dict):
    if n["session_id"] and n["asked"]:
        try:
            _asked_path(n["session_id"]).parent.mkdir(parents=True, exist_ok=True)
            _asked_path(n["session_id"]).write_text(json.dumps({"turn_id": n["turn_id"], "asked": redact_text(n["asked"])[:4000]}))
        except OSError:
            pass


def redact_text(s: str) -> str:
    from .ledger import redact
    return redact(s)


def recall_prompt(sid: str) -> str:
    try:
        return json.loads(_asked_path(sid).read_text()).get("asked", "")
    except (OSError, ValueError):
        return ""


def _turn_marker(sid: str, tid):
    """Platforms without turn ids (Cursor stop, Cline) get a per-session counter so each receipt covers new events."""
    p = HOME / "runs" / f"{RunLog(sid).path.stem}.turns"
    try:
        with open(p, "a") as f:
            f.write(json.dumps({"turn_id": tid, "events": len(RunLog(sid).records()), "at": time.time()}) + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    """Entry for `claimcheck hook`. Reads one JSON object from stdin; always exits 0."""
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        HOME.chmod(0o700)  # run logs and receipts are this user's business, not every local account's
        logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    except OSError:
        pass
    try:
        raw = sys.stdin.read()
        p = json.loads(raw) if raw.strip() else {}
        if not isinstance(p, dict) or not p:
            logger.info("empty payload (%d bytes) — nothing to record", len(raw))
            return 0
        n = normalize(p)
        logger.info("%s %s session=%s tool=%s", n["platform"], n["event_raw"] or n["event"], n["session_id"][-12:], n["tool_raw"] or "-")
        if os.environ.get("CLAIMCHECK_DEBUG"):
            logger.info("payload %s", json.dumps(p)[:2000])
        if n["event"] == "post_tool":
            record_tool(n)
        elif n["event"] == "prompt":
            remember_prompt(n)
        elif n["event"] == "turn_end":
            doc, path = finish_turn(n, privacy=os.environ.get("CLAIMCHECK_PRIVACY", "full"),
                                    chatty=os.environ.get("CLAIMCHECK_CHATTY") == "1")
            if doc:
                S = doc["summary"]
                logger.info("%s %s · %s verified · %s unverified · %s pre-existing · %s contradicted → %s",
                            n["platform"], doc["id"], S["verified"], S["unverified"], S["pre_existing"], S["contradicted"], path)
    except Exception as e:  # never surface to the agent
        logger.exception("hook failed: %s", e)
    return 0
