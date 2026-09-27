"""Markdown rendering of a receipt (CLI stdout)."""
from __future__ import annotations

from collections import Counter
from datetime import datetime


def fmt_dur(sec: float) -> str:
    m = int(sec // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def render(s: dict, msgs: list[dict], L: dict, claims: list[dict]) -> str:
    first_user = next((m["content"] for m in msgs if m["role"] == "user" and m["content"]), "")
    final = next((m["content"] for m in reversed(msgs) if m["role"] == "assistant" and m["content"]), "")
    start = s["started_at"]; end = s.get("ended_at") or s.get("last_activity_at") or start
    counts = Counter(v for v, _ in (c["_verdict"] for c in claims))
    score = (f"{counts['verified']} verified · {counts['unverified']} unverified · {counts['pre-existing']} pre-existing · "
             f"{counts['contradicted']} contradicted" + (f" · {counts['unchecked']} unchecked" if counts['unchecked'] else ""))

    o = []
    o.append(f"# Receipt · {s.get('title') or first_user.strip()[:70] or s['id']}")
    o.append(f"*{datetime.fromtimestamp(start):%Y-%m-%d %H:%M} · {fmt_dur(end - start)} · {s.get('source')} · {s.get('model')}*\n")
    o.append(f"**Asked:** {first_user.strip()[:300]}\n")
    o.append(f"**Claims:** {score}\n")
    o.append("## Ledger\n")
    o.append(f"- **{len(L['events'])} tool calls** — " + ", ".join(f"{k} ×{v}" for k, v in L["by_tool"].most_common()))
    o.append(f"- **{len(L['commands'])} commands**, {len(L['failed'])} non-zero exit, {len(L['remote'])} touched another machine (ssh/scp/curl)")
    o.append(f"- **{len(L['written'])} files written/edited**, {len(L['read'])} read")
    o.append(f"- **{len(L['external'])} external actions** (cron / skills / brain / browser)")
    o.append(f"- **tokens** {s.get('input_tokens', 0):,} in · {s.get('output_tokens', 0):,} out · {s.get('reasoning_tokens', 0):,} reasoning"
             + (f" · **cost** ${s['estimated_cost_usd']:.2f}" if s.get("estimated_cost_usd") else " · local model, $0"))
    if L["written"]:
        o.append("\n**Files written:** " + ", ".join(f"`{p}`" for p in L["written"]))
    if L["failed"]:
        o.append("\n**Commands that failed:**")
        for e in L["failed"][:8]:
            o.append(f"- exit {e['exit_code']}: `{e['command'][:110]}`")
    if L["remote"]:
        o.append("\n**Touched other machines:**")
        for e in L["remote"][:8]:
            o.append(f"- `{e['command'][:110]}`")

    o.append("\n## Claims vs. ledger\n")
    o.append("| | Claim | Evidence |")
    o.append("|---|---|---|")
    icon = {"verified": "✅", "unverified": "⚠️", "pre-existing": "🟠", "contradicted": "❌", "unchecked": "◌"}
    for c in sorted(claims, key=lambda c: ["contradicted", "pre-existing", "unverified", "verified", "unchecked"].index(c["_verdict"][0])):
        v, ev = c["_verdict"]
        o.append(f"| {icon[v]} | {c['text'][:140]} | {ev[:160]} |")

    o.append("\n## What the agent said\n")
    o.append("> " + final.strip()[:1800].replace("\n", "\n> "))
    return "\n".join(o) + "\n"

