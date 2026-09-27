"""`~/.claimcheck/config.json`: the few settings claimcheck keeps. The one that matters is the review model.

A review is a reading task: open the run log, find the line that settles a flagged claim. On a metered
plan the agent's *default* model is often the most expensive one, so claimcheck never lets a review fall
through to it. The model is pinned once, at setup, by asking the person; `claimcheck review` refuses to
run without it. Environment variables override the file (`CLAIMCHECK_REVIEW_MODEL`, `CLAIMCHECK_REVIEW_AGENT`).
"""
from __future__ import annotations

import json
import os
from typing import Any

from .store import HOME

FILE = HOME / "config.json"

# key → (env var, one-line meaning). Anything else is refused, so a typo cannot silently do nothing.
KEYS: dict[str, tuple[str, str]] = {
    "review.model": ("CLAIMCHECK_REVIEW_MODEL", "model pinned for receipt reviews (chosen by the person at setup; never the agent's default)"),
    "review.agent": ("CLAIMCHECK_REVIEW_AGENT", "which agent runs reviews: claude-code | codex | gemini | hermes"),
}
REVIEW_AGENTS = ("claude-code", "codex", "gemini", "hermes")


def load() -> dict:
    if not FILE.exists():
        return {}
    try:
        return json.loads(FILE.read_text() or "{}")
    except ValueError as e:
        raise SystemExit(f"{FILE} is not valid JSON ({e}); fix it or move it aside")


def save(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, FILE)


def _check(key: str) -> None:
    if key not in KEYS:
        raise SystemExit(f"unknown setting `{key}` (known: {', '.join(KEYS)})")


def get(key: str, cfg: dict | None = None) -> Any:
    """The env var wins, then the file, then None."""
    _check(key)
    env = os.environ.get(KEYS[key][0])
    if env:
        return env
    node: Any = load() if cfg is None else cfg
    for part in key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node or None


def put(key: str, value: str | None) -> dict:
    """Set (or clear with None) one dotted key and write the file."""
    _check(key)
    if key == "review.agent" and value and value not in REVIEW_AGENTS:
        raise SystemExit(f"review.agent must be one of {', '.join(REVIEW_AGENTS)}")
    cfg = load()
    node = cfg
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    if value is None:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value
    save(cfg)
    return cfg


def describe() -> list[str]:
    """One line per setting, for `claimcheck config` and `doctor`."""
    cfg = load()
    out = []
    for key, (env, meaning) in KEYS.items():
        val = get(key, cfg)
        src = " (from $" + env + ")" if os.environ.get(env) else ""
        out.append(f"{key} = {val if val else 'NOT SET'}{src} — {meaning}")
    return out


def review_model_line() -> str:
    """The sentence `init` and `doctor` print about the review model."""
    model = get("review.model")
    if model:
        agent = get("review.agent")
        return f"Review model: {model}" + (f" via {agent}" if agent else "") + " — `claimcheck review` uses it and nothing else"
    return ("Review model: NOT SET — reviews will not run until the person picks one. Ask which model they want "
            "reviewing receipts (a small one is enough; it reads a log), then: claimcheck config review.model <model>")
