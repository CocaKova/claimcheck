"""Find local model servers on this machine, so setup can say "you have a free model at X, pin it".

Probes the usual OpenAI-compatible ports with short timeouts and reads `/v1/models`. Nothing is cached and
nothing is written. `CLAIMCHECK_LOCAL_URLS` (comma-separated base URLs) replaces the default list, which is
also how the tests point it at a fake server.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# (base URL, what usually listens there)
DEFAULT_URLS = [
    ("http://127.0.0.1:8000", "vLLM / SGLang / TensorRT-LLM"),
    ("http://127.0.0.1:11434", "Ollama"),
    ("http://127.0.0.1:1234", "LM Studio"),
    ("http://127.0.0.1:8080", "llama.cpp server"),
    ("http://127.0.0.1:1337", "Jan"),
    ("http://127.0.0.1:5000", "text-generation-webui"),
    ("http://127.0.0.1:4000", "LiteLLM proxy"),
]
TIMEOUT = float(os.environ.get("CLAIMCHECK_LOCAL_TIMEOUT", "1.5"))


@dataclass
class Endpoint:
    base: str                 # normalized, ends in /v1
    label: str
    models: list[str] = field(default_factory=list)
    needs_key: bool = False   # answered 401/403: alive, wants an API key
    note: str = ""

    def line(self) -> str:
        if self.needs_key:
            return f"{self.base} ({self.label}) — answers but wants an API key; pin with `claimcheck config review.api_key <key>`"
        shown = ", ".join(self.models[:5]) + (f" (+{len(self.models) - 5} more)" if len(self.models) > 5 else "")
        return f"{self.base} ({self.label}) — {shown or 'no model listed'}"


def normalize(base: str) -> str:
    """`http://host:port`, `…/v1`, `…/v1/` → `http://host:port/v1`. Anything else is left alone."""
    b = base.strip().rstrip("/")
    if not b.startswith(("http://", "https://")):
        b = "http://" + b
    for tail in ("/chat/completions", "/models"):
        if b.endswith(tail):
            b = b[: -len(tail)]
    return b if b.endswith("/v1") else b + "/v1"


def is_local_url(url: str) -> bool:
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":")[0].lower().strip("[]")
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0") or host.endswith(".local") or host.startswith(("10.", "192.168.", "172."))


def probe(base: str, label: str = "", api_key: str | None = None, timeout: float = TIMEOUT) -> Endpoint | None:
    """One GET on /v1/models. None = nothing listening (or not an OpenAI-shaped server)."""
    ep = Endpoint(normalize(base), label or "local server")
    req = urllib.request.Request(ep.base + "/models", headers={"Accept": "application/json", "User-Agent": "claimcheck"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            ep.needs_key = True
            return ep
        return None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    data = body.get("data") if isinstance(body, dict) else body
    if isinstance(data, list):
        ep.models = [str(m.get("id") or m.get("name") or m) for m in data if m]
    elif isinstance(body, dict) and "models" in body:   # Ollama's native /api/tags shape, in case a proxy forwards it
        ep.models = [str(m.get("name") or m.get("model")) for m in body["models"] if isinstance(m, dict)]
    else:
        return None
    return ep


def candidates() -> list[tuple[str, str]]:
    env = os.environ.get("CLAIMCHECK_LOCAL_URLS")
    if env:
        return [(u.strip(), "from $CLAIMCHECK_LOCAL_URLS") for u in env.split(",") if u.strip()]
    out = list(DEFAULT_URLS)
    for var, label in (("OPENAI_BASE_URL", "$OPENAI_BASE_URL"), ("OLLAMA_HOST", "$OLLAMA_HOST"), ("RECEIPT_LLM_URL", "$RECEIPT_LLM_URL")):
        v = os.environ.get(var)
        if v and is_local_url(v if "://" in v else "http://" + v):
            out.insert(0, (v, label))
    seen, uniq = set(), []
    for u, l in out:
        n = normalize(u)
        if n not in seen:
            seen.add(n); uniq.append((u, l))
    return uniq


def detect(api_key: str | None = None) -> list[Endpoint]:
    """Every local server that answered, in candidate order."""
    found = []
    for base, label in candidates():
        ep = probe(base, label, api_key=api_key)
        if ep:
            found.append(ep)
    return found


def lines(found: list[Endpoint] | None = None) -> list[str]:
    found = detect() if found is None else found
    if not found:
        return ["local models: none answering on the usual ports (vLLM :8000, Ollama :11434, LM Studio :1234, llama.cpp :8080, Jan :1337, LiteLLM :4000)"]
    return ["local models (cost nothing to review with):"] + ["  " + ep.line() for ep in found]
