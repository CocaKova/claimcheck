"""`claimcheck init` / `doctor` / `open`: find the agents on this machine and wire their hooks.

Every agent gets the same two lines: after each tool call → `claimcheck hook`; at the end of a turn →
`claimcheck hook`. The command reads the agent's own JSON payload, so nothing here is agent-specific
except the file the hooks live in and the names of the two events. Edits are idempotent (a second
`init` changes nothing), backed up once (`<file>.claimcheck-backup`), and reversible (`init --remove`).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .capture import HOME

MARK = "claimcheck hook"


def _ours(cmd) -> bool:
    """Both spellings of our hook line: `.../claimcheck hook` and `python -m claimcheck.cli hook`."""
    c = (cmd or "").strip()
    return "claimcheck" in c and c.endswith(" hook")
HOMEDIR = Path(os.environ.get("CLAIMCHECK_USER_HOME", Path.home()))


def hook_command() -> str:
    """The shell line every agent will run. Prefer the installed `claimcheck` script; fall back to this Python."""
    exe = shutil.which("claimcheck")
    if exe:
        return f'"{exe}" hook'
    # not on PATH (source checkout, or a venv the agent's shell won't see): pin the interpreter and the package
    pkg_root = Path(__file__).resolve().parent.parent
    return f'env PYTHONPATH="{pkg_root}" "{sys.executable}" -m claimcheck.cli hook'


# ---------- one adapter per agent: (present?, file, apply(cfg)->cfg, strip(cfg)->cfg, describe) ----------

def _cc_style(post_events: tuple[str, ...], stop_events: tuple[str, ...], *, stop_async: bool, prompt_events: tuple[str, ...] = ()):
    """Claude Code's `hooks` block, also spoken by Codex and Gemini CLI."""
    def apply(cfg: dict) -> dict:
        cmd = hook_command()
        hooks = cfg.setdefault("hooks", {})
        for ev in prompt_events + post_events + stop_events:
            groups = hooks.setdefault(ev, [])
            entry = {"type": "command", "command": cmd, "timeout": 30}
            if ev in stop_events and stop_async:
                entry["async"] = True
            ours = [h for g in groups for h in g.get("hooks", []) if _ours(h.get("command"))]
            if ours:
                for h in ours:  # already wired: refresh the line in case the install moved or the shape changed
                    h.clear(); h.update(entry)
                continue
            groups.append({"matcher": "", "hooks": [entry]})
        return cfg

    def strip(cfg: dict) -> dict:
        hooks = cfg.get("hooks") or {}
        for ev in list(hooks):
            groups = []
            for g in hooks[ev]:
                g["hooks"] = [h for h in g.get("hooks", []) if not _ours(h.get("command"))]
                if g["hooks"]:
                    groups.append(g)
            if groups:
                hooks[ev] = groups
            else:
                hooks.pop(ev)
        if not hooks:
            cfg.pop("hooks", None)
        return cfg
    return apply, strip


def _cursor_apply(cfg: dict) -> dict:
    cmd = hook_command()
    cfg.setdefault("version", 1)
    hooks = cfg.setdefault("hooks", {})
    for ev in ("postToolUse", "afterAgentResponse"):
        lst = hooks.setdefault(ev, [])
        ours = [h for h in lst if _ours(h.get("command"))]
        for h in ours:
            h["command"] = cmd
        if not ours:
            lst.append({"command": cmd})
    return cfg


def _cursor_strip(cfg: dict) -> dict:
    hooks = cfg.get("hooks") or {}
    for ev in list(hooks):
        hooks[ev] = [h for h in hooks[ev] if not _ours(h.get("command"))]
        if not hooks[ev]:
            hooks.pop(ev)
    if not hooks:
        cfg.pop("hooks", None)
    return cfg


AGENTS = {
    "claude-code": {
        "label": "Claude Code",
        "dir": HOMEDIR / ".claude",
        "file": HOMEDIR / ".claude" / "settings.json",
        # Stop is synchronous on purpose: an async hook gets no stdin payload, and the receipt takes milliseconds
        "adapter": _cc_style(("PostToolUse", "PostToolUseFailure"), ("Stop",), stop_async=False, prompt_events=("UserPromptSubmit",)),
    },
    "codex": {
        "label": "Codex CLI",
        "dir": HOMEDIR / ".codex",
        "file": HOMEDIR / ".codex" / "hooks.json",
        "adapter": _cc_style(("PostToolUse",), ("Stop",), stop_async=False, prompt_events=("UserPromptSubmit",)),
    },
    "gemini": {
        "label": "Gemini CLI",
        "dir": HOMEDIR / ".gemini",
        "file": HOMEDIR / ".gemini" / "settings.json",
        "adapter": _cc_style(("AfterTool",), ("AfterAgent",), stop_async=False),
    },
    "cursor": {
        "label": "Cursor",
        "dir": HOMEDIR / ".cursor",
        "file": HOMEDIR / ".cursor" / "hooks.json",
        "adapter": (_cursor_apply, _cursor_strip),
    },
}


def detect() -> list[str]:
    found = [k for k, a in AGENTS.items() if a["dir"].is_dir()]
    if (HOMEDIR / ".hermes").is_dir():
        found.append("hermes")
    return found


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text() or "{}")
    except ValueError as e:
        raise SystemExit(f"{path} is not valid JSON ({e}); fix it or move it aside, then run `claimcheck init` again")


def _write(path: Path, cfg: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = path.with_name(path.name + ".claimcheck-backup")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, path)


def wired(agent: str) -> bool:
    if agent == "hermes":
        return (HOMEDIR / ".hermes" / "plugins" / "claimcheck" / "plugin.yaml").exists()
    a = AGENTS[agent]
    if not a["file"].exists():
        return False
    try:
        return _any_ours(json.loads(a["file"].read_text()))
    except ValueError:  # not JSON we can read: fall back to the two spellings as text
        txt = a["file"].read_text()
        return MARK in txt or "claimcheck.cli hook" in txt


def _any_ours(obj) -> bool:
    """True if any string anywhere in a settings tree is our hook line (the quoted-path form hides `claimcheck hook` from a text search)."""
    if isinstance(obj, dict):
        return any(_any_ours(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_any_ours(v) for v in obj)
    return isinstance(obj, str) and _ours(obj)


def wire(agent: str, *, remove: bool = False, dry_run: bool = False) -> str:
    """Returns a one-line, plain-English result."""
    if agent == "hermes":
        return _wire_hermes(remove=remove, dry_run=dry_run)
    a = AGENTS[agent]
    apply, strip = a["adapter"]
    cfg = _read(a["file"])
    before = json.dumps(cfg, sort_keys=True)
    cfg = strip(cfg) if remove else apply(cfg)
    if json.dumps(cfg, sort_keys=True) == before:
        return f"{a['label']}: already {'clean' if remove else 'wired'} ({a['file']})"
    if not dry_run:
        _write(a["file"], cfg)
    verb = "removed from" if remove else "wired into"
    return f"{a['label']}: {'would be ' if dry_run else ''}{verb} {a['file']}"


def _wire_hermes(*, remove: bool, dry_run: bool) -> str:
    dst = HOMEDIR / ".hermes" / "plugins" / "claimcheck"
    here = Path(__file__).resolve().parent
    src = here / "hermes" if (here / "hermes" / "plugin.yaml").exists() else here.parent / "hermes_plugin"  # wheel, else source checkout
    hermes = shutil.which("hermes")
    if remove:
        if dry_run:
            return f"Hermes Agent: would disable the plugin and remove {dst}"
        if hermes:
            subprocess.run([hermes, "plugins", "disable", "claimcheck"], capture_output=True, text=True, timeout=60)
        if dst.is_symlink() or dst.exists():
            (dst.unlink() if dst.is_symlink() else shutil.rmtree(dst))
        return "Hermes Agent: plugin disabled and removed"
    if not (src / "plugin.yaml").exists():
        return "Hermes Agent: found, but this claimcheck install has no plugin folder (reinstall from PyPI or from the repo)"
    if dry_run:
        return f"Hermes Agent: would link {dst} → {src} and enable the plugin"
    if not (dst.is_symlink() or dst.exists()):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
    if hermes:
        r = subprocess.run([hermes, "plugins", "enable", "claimcheck"], capture_output=True, text=True, timeout=120)
        note = "enabled" if r.returncode == 0 else f"linked; enable failed: {(r.stdout + r.stderr).strip()[-120:]}"
    else:
        note = "linked; run `hermes plugins enable claimcheck`"
    return f"Hermes Agent: plugin {note}"


def init(agents: list[str] | None = None, *, remove: bool = False, dry_run: bool = False) -> list[str]:
    from .sign import KEY_FILE, keygen, kid, load_key, pub_raw
    targets = agents or detect()
    lines = []
    if not targets:
        return ["No supported agent found in this home folder (looked for Claude Code, Codex, Gemini CLI, Cursor, Hermes)."]
    for a in targets:
        if a not in AGENTS and a != "hermes":
            lines.append(f"{a}: not a known agent (known: {', '.join([*AGENTS, 'hermes'])})")
            continue
        lines.append(wire(a, remove=remove, dry_run=dry_run))
    if not remove and not dry_run:
        HOME.mkdir(parents=True, exist_ok=True)
        keygen(KEY_FILE)
        lines.append(f"Signing key: {KEY_FILE} (id {kid(pub_raw(load_key()))}) — receipts from this machine carry it")
        lines.append(f"Receipts will appear in {HOME / 'receipts'} — `claimcheck open` shows the latest, `claimcheck flagged` lists the ones worth a look")
    return lines


def doctor() -> list[str]:
    from .sign import BACKEND, KEY_FILE
    from .store import iter_receipts, is_flagged
    lines = [f"claimcheck home: {HOME}", f"python: {sys.version.split()[0]} · signing backend: {BACKEND} · key: {'present' if KEY_FILE.exists() else 'missing (made on first receipt)'}"]
    for a in [*AGENTS, "hermes"]:
        present = (AGENTS[a]["dir"] if a in AGENTS else HOMEDIR / ".hermes").is_dir()
        if not present:
            continue
        lines.append(f"{AGENTS[a]['label'] if a in AGENTS else 'Hermes Agent'}: {'wired' if wired(a) else 'found, NOT wired — run `claimcheck init`'}")
    docs = list(iter_receipts())
    flagged = sum(1 for _, d in docs if is_flagged(d))
    if docs:
        newest = docs[0][1]
        lines.append(f"receipts: {len(docs)} ({flagged} flagged) · newest {newest['created_at']} from {newest['run']['agent'].get('platform')}")
    else:
        lines.append("receipts: none yet — run any agent turn that uses a tool, then look again")
    log = HOME / "hook.log"
    if log.exists():
        errs = [l for l in log.read_text().splitlines()[-200:] if " ERROR " in l]
        lines.append(f"hook log: {log} · {len(errs)} error(s) in the last 200 lines" + (f" · last: {errs[-1][:160]}" if errs else ""))
    return lines


def open_latest(which: str | None = None) -> str:
    import webbrowser
    from .store import iter_receipts
    for p, d in iter_receipts():
        if which and which not in (d["id"], d["run"]["session_id"]):
            continue
        page = p.with_suffix(".html")
        if not page.exists():
            from .page import render
            page.write_text(render(d, None))
        webbrowser.open(page.as_uri())
        return f"opened {page}"
    return "no receipt to open yet"
