#!/usr/bin/env python3
"""Ledger: the tool log of one run, from the Hermes DB or a frozen fixture, with secrets redacted."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


DB = Path(os.environ.get("RECEIPT_DB", Path.home() / ".hermes" / "state.db"))
FIXTURES = Path(os.environ.get("CLAIMCHECK_FIXTURES", Path(__file__).resolve().parent.parent / "tests" / "fixtures"))
SESSION_KEYS = ("id", "title", "source", "model", "started_at", "ended_at", "last_activity_at",
                "input_tokens", "output_tokens", "reasoning_tokens", "estimated_cost_usd")
MSG_KEYS = ("id", "role", "content", "tool_call_id", "tool_calls", "timestamp")

WRITE_TOOLS = {"patch", "write_file", "edit_file", "create_file", "apply_patch"}
READ_TOOLS = {"read_file", "search_files", "skill_view"}
EXTERNAL_TOOLS = {"cronjob_manage", "skill_manage", "brain_edit", "send_message", "browser_exec", "delegate_task"}
PATH_RE = re.compile(r"(?:~(?=/)|/home/\w+|/tmp|/etc|/opt|/var|/Users/\w+)[\w./+-]*|(?<![/:\w])/(?:[\w.+-]+/)+[\w.+-]+|\b[\w.-]+\.(?:md|py|json|yaml|yml|kt|sh|toml|txt|db|cfg|ini|gd|tscn)\b")


# ---------- redaction ----------

_QUOTE = r"""(?:\\?["'])?"""  # an optional quote, JSON-escaped when the text is a dumped tool call

# (name or lead-in, value): the value goes, the lead-in stays so the receipt still says WHAT was set.
NAMED_SECRET_RES = [
    # password: x · "api_key": "x" · client_secret=x · Authorization: x
    re.compile(r"((?:password|passwd|pwd|secret|token(?!s)|api[_-]?key|apikey|auth(?!or)|authorization|bearer)[\w-]*\\?[\"']?\s*[=:]\s*" + _QUOTE + r")([^\s'\"\\,;}]{8,})", re.I),
    # env-style names the word list misses: STRIPE_KEY= · MY_PASS= · SENTRY_DSN= · BW_SESSION=
    re.compile(r"(\b[A-Z][A-Z0-9_]*_(?:KEY|PASS|PASSWORD|PASSWD|PWD|SECRET|TOKEN|PAT|CREDENTIALS?|DSN|SESSION)\\?[\"']?\s*[=:]\s*" + _QUOTE + r")([^\s'\"\\,;}]{6,})"),
    # --password hunter2 · --token x (the `=` form is caught above)
    re.compile(r"(--(?:password|passwd|token|auth-token|api-key|apikey|secret|client-secret)\s+" + _QUOTE + r")([^\s'\"\\]{6,})", re.I),
    re.compile(r"(\bsshpass\s+-p\s*" + _QUOTE + r")([^\s'\"\\]+)"),
    re.compile(r"(\bmysql(?:dump|admin)?\b[^\n|;&]*?\s-p)([^\s'\"\\]{4,})"),
    # scheme://user:PASSWORD@host
    re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@'\"\\]*:)([^@\s/'\"\\]{3,})(?=@)", re.I),
    # Authorization: Bearer x · Basic x · Token x (a credential has a digit or symbol; "Basic authentication" stays)
    re.compile(r"(\b(?:Bearer|Basic|Token)\s+)((?=[^\s'\"\\]*[0-9+/=_-])[A-Za-z0-9._~+/=-]{12,})"),
    # webhook URLs are the secret themselves
    re.compile(r"(hooks\.slack\.com/services/)([A-Za-z0-9/_-]{8,})"),
    re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/)([A-Za-z0-9_-]{8,})"),
]

# values that are a secret on sight, whatever surrounds them
TOKEN_RES = [
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}"),            # OpenAI, Anthropic (sk-ant-…), generic
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"),   # Stripe secret / restricted keys
    re.compile(r"\bwhsec_[A-Za-z0-9]{10,}"),                     # Stripe webhook secret
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),                 # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{15,}"),                   # GitLab
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),                        # Hugging Face
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),                     # Google API
    re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}"),              # Slack
    re.compile(r"\btskey-[a-z]+-[A-Za-z0-9-]{10,}"),             # Tailscale
    re.compile(r"\btk_[A-Za-z0-9]{20,}"),                        # ntfy
    re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}"),              # Telegram bot
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                # AWS
    re.compile(r"\bAGE-SECRET-KEY-1[0-9A-Z]{50,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
SECRET_RES = NAMED_SECRET_RES + TOKEN_RES

_PLACEHOLDER = ("$", "<", "...", "…", "***", "[REDACTED]", "%", "{{", "/", "~/", "./")  # a path to a secret file is not the secret


def _keep_name(m: re.Match) -> str:
    return m.group(0) if m.group(2).startswith(_PLACEHOLDER) else m.group(1) + "[REDACTED]"


def redact(text: str) -> str:
    """Secrets seen in tool logs must never reach a receipt (or a fixture). Key names stay, values go."""
    if not text:
        return text
    for rx in NAMED_SECRET_RES:
        text = rx.sub(_keep_name, text)
    for rx in TOKEN_RES:
        text = rx.sub("[REDACTED]", text)
    return text


# ---------- ledger ----------

def load(session_id: str, fixture: bool = False):
    """Session + messages from the Hermes DB, or from a frozen fixture (tests/fixtures/<id>.json.gz)."""
    fx = FIXTURES / f"{session_id}.json.gz"
    if fixture or (not DB.exists() and fx.exists()):
        import gzip
        d = json.loads(gzip.open(fx, "rt").read())
        return d["session"], d["messages"]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    s = con.execute("select * from sessions where id=?", (session_id,)).fetchone()
    if not s:
        sys.exit(f"no session {session_id}")
    msgs = con.execute("select * from messages where session_id=? order by id", (session_id,)).fetchall()
    return dict(s), [dict(m) for m in msgs]


def dump_fixture(session_id: str) -> Path:
    """Freeze the columns the receipt needs so tests don't depend on the live DB."""
    import gzip
    s, msgs = load(session_id)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / f"{session_id}.json.gz"
    with gzip.open(out, "wt") as f:
        json.dump({"session": {k: s.get(k) for k in SESSION_KEYS},
                   "messages": [{k: (redact(m.get(k)) if isinstance(m.get(k), str) else m.get(k)) for k in MSG_KEYS} for m in msgs]}, f)
    return out


def _written_paths(name: str, args: dict) -> list[str]:
    """Paths a tool actually wrote. skill_manage carries a list of ops; brain_edit a note ref."""
    if name in WRITE_TOOLS:
        return PATH_RE.findall(json.dumps(args))
    if name == "skill_manage":
        ops = args.get("operations", [])
        if isinstance(ops, str):
            try:
                ops = json.loads(ops)
            except json.JSONDecodeError:
                import ast
                try:
                    ops = ast.literal_eval(ops)
                except Exception:
                    ops = []
        return [f"skill:{op.get('name', '')}" + (f"/{op['file_path']}" if op.get('file_path') else "") for op in ops
                if isinstance(op, dict) and op.get("action") in ("patch", "write", "create", "edit", "append")]
    if name == "brain_edit":
        return [args.get("ref", "")] if args.get("ref") else []
    return []


def _event(name: str, args: dict, res: dict | None, ts: float) -> dict:
    out, exit_code = "", None
    if res and res["content"]:
        try:
            j = json.loads(res["content"])
            out = str(j.get("output") or j.get("content") or j.get("result") or j)
            exit_code = j.get("exit_code")
        except (json.JSONDecodeError, AttributeError):
            out = res["content"]
    ev = {"tool": name, "args": args, "output": redact(out), "exit_code": exit_code, "ts": ts}
    ev["paths"] = sorted(set(PATH_RE.findall(json.dumps(args))))
    ev["wrote"] = sorted(set(_written_paths(name, args)))
    ev["payload"] = redact(json.dumps(args)) if (ev["wrote"] or name in ("execute_code", "terminal")) else ""
    ev["command"] = redact(args.get("command") or args.get("code") or "")
    return ev


def event_from_hook(tool: str, args: dict, result, ts: float, **meta) -> dict:
    """One ledger event from a live hook payload (Hermes post_tool_call): the result is the same
    JSON string the platform stores, before any storage truncation."""
    if not isinstance(args, dict):
        args = {"_raw": args}
    if tool == "tool_call" and isinstance(args.get("calls"), list) and len(args["calls"]) == 1:  # batch wrapper
        sub = args["calls"][0]
        tool, args = sub.get("name", tool), sub.get("arguments") or {}
    content = result if isinstance(result, str) else (json.dumps(result, ensure_ascii=False) if result is not None else "")
    ev = _event(tool, args, {"content": content} if content else None, ts)
    ev.update({k: v for k, v in meta.items() if v is not None})
    return ev


def events_from_messages(msgs: list[dict]) -> list[dict]:
    """One event per tool call in a stored transcript, joined to its result by tool_call_id."""
    results = {m["tool_call_id"]: m for m in msgs if m["role"] == "tool" and m["tool_call_id"]}
    events = []
    for m in msgs:
        if m["role"] != "assistant" or not m["tool_calls"]:
            continue
        for tc in json.loads(m["tool_calls"]):
            fn = tc.get("function", {})
            name = fn.get("name", "?")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            if name == "tool_call" and isinstance(args.get("calls"), list):  # Hermes batch wrapper
                for sub in args["calls"]:
                    events.append(_event(sub.get("name", "?"), sub.get("arguments") or {}, results.get(tc.get("id") or tc.get("call_id")), m["timestamp"]))
                continue
            res = results.get(tc.get("id") or tc.get("call_id"))
            events.append(_event(name, args, res, m["timestamp"]))
    return events


def ledger_from_events(events: list[dict]) -> dict:
    """Counts and views over a list of events (any source: stored transcript or live run log)."""
    for i, e in enumerate(events):
        e["i"] = i
    commands = [e for e in events if e["tool"] in ("terminal", "execute_code")]
    written = Counter(p for e in events for p in e["wrote"])
    read = Counter(p for e in events if e["tool"] in READ_TOOLS for p in e["paths"])
    external = [e for e in events if e["tool"] in EXTERNAL_TOOLS and not e["wrote"]]
    remote = [e for e in commands if re.search(r"\b(ssh|scp|rsync|curl|wget|git push)\b", e["command"])]
    failed = [e for e in commands if e["exit_code"] not in (None, 0, "0")]
    return {"events": events, "commands": commands, "written": written, "read": read,
            "external": external, "remote": remote, "failed": failed,
            "by_tool": Counter(e["tool"] for e in events)}


def build_ledger(msgs: list[dict]) -> dict:
    """Ledger of a stored Hermes transcript (at-rest capture)."""
    return ledger_from_events(events_from_messages(msgs))
