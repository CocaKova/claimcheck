"""`~/.claimcheck/config.json`: the few settings claimcheck keeps. The one that matters is the review model.

A review is a reading task: open the run log, find the line that settles a flagged claim. On a metered
plan the agent's *default* model is often the most expensive one, so claimcheck never lets a review fall
through to it. The model is pinned once, at setup, by asking the person; `claimcheck review` refuses to
run without it. A local server (vLLM, Ollama, LM Studio, llama.cpp…) is pinned the same way plus its URL,
and then no agent CLI is in the loop at all. Environment variables override the file.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .store import HOME

FILE = HOME / "config.json"

# key → (env vars that override it, one-line meaning). Anything else is refused, so a typo cannot silently do nothing.
KEYS: dict[str, tuple[tuple[str, ...], str]] = {
    "review.model": (("CLAIMCHECK_REVIEW_MODEL",), "model pinned for receipt reviews (chosen by the person at setup; never the agent's default)"),
    "review.agent": (("CLAIMCHECK_REVIEW_AGENT",), "what runs reviews: endpoint (direct HTTP to a local/OpenAI-compatible server) | claude-code | codex | gemini | hermes"),
    "review.endpoint": (("CLAIMCHECK_REVIEW_ENDPOINT", "RECEIPT_LLM_URL"), "base URL of an OpenAI-compatible server for reviews, e.g. http://127.0.0.1:8000/v1 (local = costs nothing)"),
    "review.api_key": (("CLAIMCHECK_REVIEW_API_KEY",), "API key for review.endpoint, only if the server wants one"),
    "cloud.key": (("CLAIMCHECK_CLOUD_KEY",), "claimcheck.cc API key (`claimcheck login`); unset = nothing leaves this machine"),
    "cloud.url": (("CLAIMCHECK_CLOUD_URL",), "hosted service, default https://claimcheck.cc"),
    "cloud.witness": (("CLAIMCHECK_WITNESS",), "on | off: send each event's fingerprint (hashes only) as a run happens"),
    "cloud.share": (("CLAIMCHECK_SHARE",), "off | flagged | all: share receipts automatically after each run"),
    "cloud.privacy": (("CLAIMCHECK_SHARE_PRIVACY",), "summary | full | hashes: what a shared receipt shows (default summary)"),
}
CHOICES = {"cloud.witness": ("on", "off"), "cloud.share": ("off", "flagged", "all"),
           "cloud.privacy": ("summary", "full", "hashes")}
REVIEW_AGENTS = ("endpoint", "claude-code", "codex", "gemini", "hermes")
SECRET_KEYS = ("review.api_key", "cloud.key")


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
    if (cfg.get("review") or {}).get("api_key") or (cfg.get("cloud") or {}).get("key"):
        try:
            os.chmod(FILE, 0o600)
        except OSError:
            pass


def _check(key: str) -> None:
    if key not in KEYS:
        raise SystemExit(f"unknown setting `{key}` (known: {', '.join(KEYS)})")


def _env(key: str) -> str | None:
    for var in KEYS[key][0]:
        if os.environ.get(var):
            return os.environ[var]
    return None


def get(key: str, cfg: dict | None = None) -> Any:
    """The env var wins, then the file, then None."""
    _check(key)
    env = _env(key)
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
    if value is not None:
        value = str(value).strip() or None
    if key in CHOICES and value and value not in CHOICES[key]:
        raise SystemExit(f"{key} must be one of {', '.join(CHOICES[key])}")
    if key == "review.agent" and value and value not in REVIEW_AGENTS:
        raise SystemExit(f"review.agent must be one of {', '.join(REVIEW_AGENTS)}")
    if key == "review.endpoint" and value:
        from .local import normalize
        value = normalize(value)
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


def effective_agent(cfg: dict | None = None) -> str | None:
    """What runs a review: the pinned agent, else `endpoint` when a URL is pinned, else None (decided per receipt)."""
    cfg = load() if cfg is None else cfg
    return get("review.agent", cfg) or ("endpoint" if get("review.endpoint", cfg) else None)


def describe() -> list[str]:
    """One line per setting, for `claimcheck config` and `doctor`. Secrets are masked."""
    cfg = load()
    out = []
    for key, (envs, meaning) in KEYS.items():
        val = get(key, cfg)
        if val and key in SECRET_KEYS:
            val = val[:3] + "…" + val[-2:] if len(val) > 8 else "set"
        hit = next((v for v in envs if os.environ.get(v)), None)
        src = f" (from ${hit})" if hit else ""
        out.append(f"{key} = {val if val else 'NOT SET'}{src} — {meaning}")
    return out


def review_model_line() -> str:
    """The sentence `init` and `doctor` print about the review model."""
    cfg = load()
    model = get("review.model", cfg)
    if model:
        agent = effective_agent(cfg)
        ep = get("review.endpoint", cfg)
        if agent == "endpoint" and ep:
            from .local import is_local_url
            where = f" at {ep}" + (" (local, costs nothing)" if is_local_url(ep) else "")
        else:
            where = f" via {agent}" if agent else ""
        return f"Review model: {model}{where} — `claimcheck review` uses it and nothing else"
    return ("Review model: NOT SET — reviews will not run until the person picks one. Ask which model they want "
            "reviewing receipts (a small one is enough; it reads a log; a local one costs nothing — `claimcheck local` "
            "lists any), then: claimcheck config review.model <model> [and claimcheck config review.endpoint <url>]")
