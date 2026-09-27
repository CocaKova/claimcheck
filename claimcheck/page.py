#!/usr/bin/env python3
"""Render a receipt document (spec v0.1) as a standalone HTML page.

  claimcheck page receipts/<session>.json [--name NAME] > out.html
"""
from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from .document import parse_iso

ORDER = ["contradicted", "pre-existing", "unverified", "verified", "unchecked"]
LABEL = {"verified": "Verified", "unverified": "Unverified", "pre-existing": "Pre-existing", "contradicted": "Contradicted", "unchecked": "Unchecked"}
PLAIN = {
    "verified": "Confirmed by the record.",
    "unverified": "Reported, but nothing in the record shows it happened.",
    "pre-existing": "Was already there before the agent started. It took credit for it.",
    "contradicted": "The record shows the opposite.",
    "unchecked": "Nothing in the record to check this against.",
}
EXPLAIN = {
    "verified": "The log contains what the agent said it did.",
    "unverified": "The agent reported this, but nothing in the log shows it. Ask before you trust it.",
    "pre-existing": "The agent took credit for something that was already there before it touched anything.",
    "contradicted": "The log shows the opposite of what the agent said.",
    "unchecked": "Nothing literal to check against, so it counts neither for nor against the agent.",
}


def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def fmt_dur(sec: float) -> str:
    m = int(sec // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def render(d: dict, name: str | None = None) -> str:
    r = d["run"]; L = d["ledger"]; U = L.get("usage", {})
    start = parse_iso(r["started_at"]).timestamp(); end = parse_iso(r["ended_at"]).timestamp()
    claims = sorted(d["claims"], key=lambda c: ORDER.index(c["verdict"]))
    counts = {k: sum(1 for c in claims if c["verdict"] == k) for k in ORDER}
    asked = r.get("asked", ""); final = d["report"].get("text", "")
    title = r.get("title") or asked.strip().split("\n")[0][:80] or r["session_id"]
    cost = f"${float(U['cost_usd']):.2f}" if U.get("cost_usd") else "$0 · local model"
    tools = sorted(L["by_tool"].items(), key=lambda kv: -kv[1])
    items = L["commands"].get("items", [])
    n_cmd, n_failed, n_remote = L["commands"]["total"], L["commands"]["failed"], L["commands"]["remote"]
    written, read = L["files"]["written"], L["files"]["read"]
    sig = d.get("signature")
    worst = next((k for k in ORDER[:4] if counts[k]), "verified" if counts["verified"] else "unchecked")
    headline = {
        "unchecked": "This run made no checkable claims.",
        "contradicted": "This run contradicts its own log.",
        "pre-existing": "This run took credit for existing work.",
        "unverified": "This run reported something it never observed.",
        "verified": "Every claim in this run checks out.",
    }[worst]

    n_claims = len(claims)
    checked = n_claims - counts["unchecked"]
    plain_bits = []
    if counts["verified"]:
        plain_bits.append(f"{counts['verified']} check out against the record")
    if counts["pre-existing"]:
        plain_bits.append(f"{counts['pre-existing']} {'was' if counts['pre-existing']==1 else 'were'} already in place before it started, so it took credit for work it did not do")
    if counts["unverified"]:
        plain_bits.append(f"{counts['unverified']} {'is' if counts['unverified']==1 else 'are'} reported but not backed by anything in the record, so ask before you rely on {'it' if counts['unverified']==1 else 'them'}")
    if counts["contradicted"]:
        plain_bits.append(f"{counts['contradicted']} {'is' if counts['contradicted']==1 else 'are'} contradicted by the record")
    plain = (f"The agent worked for {fmt_dur(end - start)} on this request. It ran {n_cmd} commands"
             f"{f' ({n_failed} failed)' if n_failed else ''}, "
             f"changed {len(written)} file{'s' if len(written)!=1 else ''}"
             f"{f', reached {n_remote} other machine' + ('s' if n_remote!=1 else '') if n_remote else ''}, "
             f"and cost {cost.split(' ·')[0]}. It reported {n_claims} things it did"
             f"{f', {checked} of which could be checked' if checked != n_claims else ''}: " + "; ".join(plain_bits) + ".")

    def claim_row(c):
        v = c["verdict"]
        return f"""
      <li class="claim {v}">
        <div class="stripe" aria-hidden="true"></div>
        <div class="claim-body">
          <div class="claim-head"><span class="pill {v}">{LABEL[v]}</span></div>
          <p class="claim-text">{e(c['text'])}</p>
          <p class="evidence tech">{e(c['evidence'])}</p>
          <p class="evidence plain">{PLAIN[v]}</p>
        </div>
      </li>"""

    failed_rows = "".join(f"<li><code>exit {e(it['exit_code'])}</code> {e(it['text'][:140])}</li>" for it in items if it.get("exit_code") not in (None, 0))[:6000]
    remote_rows = "".join(f"<li>{e(it['text'][:140])}</li>" for it in items if it.get("remote"))[:8000]
    written_rows = "".join(f"<li>{e(p)}</li>" for p in written[:12])
    tool_rows = "".join(f"<span class='tool'>{e(k)} <b>×{v}</b></span>" for k, v in tools)

    short = name or " ".join(re.sub(r"[^\w\s-]", "", title).split()[:3]) or "Agent run"
    return f"""<title>{e(short)} Receipt</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {{
  --ground:#FAFAF7; --paper:#FFFFFF; --ink:#1B1F24; --muted:#6B7078; --rule:#E3E1DA; --rule-strong:#C9C6BD;
  --accent:#23406E; --accent-soft:#E9EEF6;
  --ok:#1F7A4D; --ok-soft:#E4F2EA; --warn:#A16207; --warn-soft:#FBF1D9; --pre:#C2561B; --pre-soft:#FBE7DC; --bad:#B42318; --bad-soft:#FBE2DF;
  --display:"Bricolage Grotesque", "Archivo", system-ui, sans-serif;
  --body:"IBM Plex Sans", system-ui, -apple-system, sans-serif;
  --mono:"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  color-scheme:dark; --ground:#14171C; --paper:#1B1F25; --ink:#E6E4DF; --muted:#9AA0A8; --rule:#2B3038; --rule-strong:#3B414B;
  --accent:#9DB6DD; --accent-soft:#1E2A3D;
  --ok:#5CC48E; --ok-soft:#153224; --warn:#E2B04A; --warn-soft:#3A2E12; --pre:#F08A5A; --pre-soft:#3C2216; --bad:#F27B70; --bad-soft:#3E1B18;
}} }}
:root[data-theme="dark"] {{
  color-scheme:dark; --ground:#14171C; --paper:#1B1F25; --ink:#E6E4DF; --muted:#9AA0A8; --rule:#2B3038; --rule-strong:#3B414B;
  --accent:#9DB6DD; --accent-soft:#1E2A3D;
  --ok:#5CC48E; --ok-soft:#153224; --warn:#E2B04A; --warn-soft:#3A2E12; --pre:#F08A5A; --pre-soft:#3C2216; --bad:#F27B70; --bad-soft:#3E1B18;
}}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--ground); color:var(--ink); font-family:var(--body); font-size:15px; line-height:1.5; padding-block:32px 64px; padding-inline:16px; }}
.sheet {{ max-width:760px; margin:0 auto; background:var(--paper); border:1px solid var(--rule); border-top:3px solid var(--accent); padding:28px clamp(16px,4vw,40px) 36px; }}
.eyebrow {{ font-family:var(--mono); font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); display:flex; flex-wrap:wrap; gap:6px 16px; }}
h1 {{ font-family:var(--display); font-weight:700; font-size:clamp(24px,4.5vw,34px); line-height:1.15; margin:10px 0 6px; text-wrap:balance; letter-spacing:-.01em; }}
.headline {{ font-family:var(--display); font-weight:500; font-size:18px; color:var(--{ {'verified':'ok','unverified':'warn','pre-existing':'pre','contradicted':'bad'}[worst] }); margin:0 0 22px; }}
.verdicts {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin:0 0 26px; }}
.verdict {{ border:1px solid var(--rule); padding:12px 12px 10px; }}
.verdict .n {{ font-family:var(--display); font-weight:700; font-size:30px; line-height:1; font-variant-numeric:tabular-nums; }}
.verdict .l {{ font-family:var(--mono); font-size:11px; letter-spacing:.06em; text-transform:uppercase; margin-top:6px; }}
.verdict.verified {{ color:var(--ok); background:var(--ok-soft); border-color:transparent }}
.verdict.unverified {{ color:var(--warn); background:var(--warn-soft); border-color:transparent }}
.verdict.pre-existing {{ color:var(--pre); background:var(--pre-soft); border-color:transparent }}
.verdict.contradicted {{ color:var(--bad); background:var(--bad-soft); border-color:transparent }}
.verdict.zero {{ color:var(--muted); background:transparent; border-color:var(--rule) }}
@media (max-width:520px) {{ .verdicts {{ grid-template-columns:repeat(2,1fr) }} }}
h2 {{ font-family:var(--mono); font-size:12px; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); margin:30px 0 10px; padding-bottom:6px; border-bottom:1px solid var(--rule-strong); }}
.asked {{ margin:0; padding:0 0 0 14px; border-left:2px solid var(--accent); color:var(--ink); }}
.asked small {{ display:block; font-family:var(--mono); font-size:11px; color:var(--muted); letter-spacing:.06em; text-transform:uppercase; margin-bottom:4px }}
dl.ledger {{ margin:0; display:grid; grid-template-columns:max-content 1fr; column-gap:18px; row-gap:0; }}
dl.ledger dt, dl.ledger dd {{ margin:0; padding:9px 0; border-bottom:1px solid var(--rule); }}
dl.ledger dt {{ font-family:var(--mono); font-size:12px; color:var(--muted); letter-spacing:.04em; text-transform:uppercase; padding-top:11px }}
dl.ledger dd {{ font-variant-numeric:tabular-nums }}
dl.ledger dd b {{ font-weight:600 }}
.tool {{ display:inline-block; font-family:var(--mono); font-size:12px; margin:0 10px 4px 0; color:var(--muted) }}
.tool b {{ color:var(--ink); font-weight:500 }}
ul.mono {{ list-style:none; margin:0; padding:0; font-family:var(--mono); font-size:12.5px; }}
ul.mono li {{ padding:6px 0; border-bottom:1px solid var(--rule); overflow-x:auto; white-space:nowrap; }}
ul.mono code {{ color:var(--bad); margin-right:8px }}
ol.claims {{ list-style:none; margin:0; padding:0; display:grid; gap:10px }}
.claim {{ display:grid; grid-template-columns:4px 1fr; gap:14px; border:1px solid var(--rule); background:var(--paper) }}
.claim .stripe {{ background:var(--rule-strong) }}
.claim.verified .stripe {{ background:var(--ok) }} .claim.unverified .stripe {{ background:var(--warn) }}
.claim.pre-existing .stripe {{ background:var(--pre) }} .claim.contradicted .stripe {{ background:var(--bad) }} .claim.unchecked {{ opacity:.7 }}
.pill.unchecked {{ color:var(--muted); background:var(--ground) }} .legend .unchecked b {{ color:var(--muted) }}
.claim-body {{ padding:12px 14px 12px 0 }}
.pill {{ display:inline-block; font-family:var(--mono); font-size:11px; letter-spacing:.06em; text-transform:uppercase; padding:2px 8px; border-radius:2px; }}
.pill.verified {{ color:var(--ok); background:var(--ok-soft) }} .pill.unverified {{ color:var(--warn); background:var(--warn-soft) }}
.pill.pre-existing {{ color:var(--pre); background:var(--pre-soft) }} .pill.contradicted {{ color:var(--bad); background:var(--bad-soft) }}
.claim-text {{ margin:8px 0 6px; font-weight:500 }}
.evidence {{ margin:0; font-family:var(--mono); font-size:12px; color:var(--muted); overflow-wrap:anywhere }}
.legend {{ display:grid; gap:6px; margin:14px 0 0; padding:0; list-style:none; font-size:13px; color:var(--muted) }}
.legend b {{ font-family:var(--mono); font-size:11px; letter-spacing:.06em; text-transform:uppercase; font-weight:500; margin-right:6px }}
.legend .verified b {{ color:var(--ok) }} .legend .unverified b {{ color:var(--warn) }} .legend .pre-existing b {{ color:var(--pre) }} .legend .contradicted b {{ color:var(--bad) }}
details {{ margin-top:8px }}
summary {{ cursor:pointer; font-family:var(--mono); font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:var(--accent) }}
summary:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px }}
blockquote.final {{ margin:12px 0 0; padding:14px 16px; background:var(--ground); border:1px solid var(--rule); white-space:pre-wrap; font-size:14px; overflow-x:auto }}
.views {{ display:flex; gap:0; margin:0 0 18px; border:1px solid var(--rule-strong); width:max-content; }}
.views button {{ font:inherit; font-family:var(--mono); font-size:12px; letter-spacing:.06em; text-transform:uppercase; padding:7px 14px; border:0; background:transparent; color:var(--muted); cursor:pointer; }}
.views button[aria-pressed="true"] {{ background:var(--accent); color:var(--paper); }}
.views button:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px }}
.plain {{ display:none }} :root[data-view="plain"] .plain {{ display:block }} :root[data-view="plain"] .tech {{ display:none }}
:root[data-view="plain"] .evidence.plain {{ font-family:var(--body); font-size:14px; color:var(--ink) }}
.statement {{ font-size:17px; line-height:1.55; margin:0 0 22px; max-width:62ch }}
.foot {{ margin-top:30px; padding-top:12px; border-top:1px dashed var(--rule-strong); font-family:var(--mono); font-size:11px; color:var(--muted); letter-spacing:.04em; display:flex; flex-wrap:wrap; gap:6px 18px }}
</style>

<article class="sheet">
  <div class="eyebrow">
    <span>Receipt</span><span>{e(datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M'))}</span><span>{e(fmt_dur(end - start))}</span><span>{e(r['agent'].get('platform'))}</span><span>{e(r['agent'].get('model') or '')}</span>
  </div>
  <h1>{e(title)}</h1>
  <p class="headline">{e(headline)}</p>
  <div class="views" role="group" aria-label="View">
    <button type="button" id="view-plain" aria-pressed="false">Plain</button>
    <button type="button" id="view-tech" aria-pressed="true">Technical</button>
  </div>
  <p class="statement plain">{e(plain)}</p>

  <div class="verdicts">
    {''.join(f'<div class="verdict {k}{" zero" if not counts[k] else ""}"><div class="n">{counts[k]}</div><div class="l">{LABEL[k]}</div></div>' for k in ["verified","unverified","pre-existing","contradicted"])}
  </div>

  {f'<p class="evidence" style="margin:-16px 0 20px">{counts["unchecked"]} further claim{"s" if counts["unchecked"]!=1 else ""} had nothing literal to check.</p>' if counts["unchecked"] else ''}
  <blockquote class="asked"><small>Asked</small>{e(asked.strip()[:400])}</blockquote>

  <div class="tech">
  <h2>Ledger</h2>
  <dl class="ledger">
    <dt>Tool calls</dt><dd><b>{L['tool_calls']}</b><br>{tool_rows}</dd>
    <dt>Commands</dt><dd><b>{n_cmd}</b> run · <b>{n_failed}</b> failed · <b>{n_remote}</b> reached another machine</dd>
    <dt>Files</dt><dd><b>{len(written)}</b> written · <b>{len(read)}</b> read</dd>
    <dt>External</dt><dd><b>{L['external']}</b> actions on cron, skills, memory or browser</dd>
    <dt>Tokens</dt><dd>{U.get('input_tokens',0):,} in · {U.get('output_tokens',0):,} out · {U.get('reasoning_tokens',0):,} reasoning</dd>
    <dt>Cost</dt><dd>{e(cost)}</dd>
  </dl>
  {f'<h2>Files written</h2><ul class="mono">{written_rows}</ul>' if written_rows else ''}
  {f'<h2>Commands that failed</h2><ul class="mono">{failed_rows}</ul>' if failed_rows else ''}
  {f'<h2>Reached other machines</h2><ul class="mono">{remote_rows}</ul>' if remote_rows else ''}
  </div>

  <h2><span class="tech">Claims, checked against the ledger</span><span class="plain">What it said it did</span></h2>
  <ol class="claims">{''.join(claim_row(c) for c in claims)}</ol>
  <ul class="legend tech">
    {''.join(f'<li class="{k}"><b>{LABEL[k]}</b>{EXPLAIN[k]}</li>' for k in ORDER)}
  </ul>

  <h2 class="tech">In the agent's own words</h2>
  <details class="tech"><summary>Show the final report</summary><blockquote class="final">{e(final.strip()[:4000])}</blockquote></details>

  <div class="foot"><span>receipt {e(d.get('id',''))}</span><span>session {e(r['session_id'])}</span><span>claims split by code · every verdict computed from the tool log</span>{f'<span>signed {e(sig["kid"])} · verify at claimcheck.cc</span>' if sig else '<span>unsigned</span>'}</div>
</article>
<script>
(function(){{
  var root=document.documentElement, bp=document.getElementById('view-plain'), bt=document.getElementById('view-tech');
  function set(v){{ root.dataset.view=v; bp.setAttribute('aria-pressed', v==='plain'); bt.setAttribute('aria-pressed', v==='tech'); try{{localStorage.setItem('receipt-view',v)}}catch(e){{}} }}
  var v='tech'; try{{ v=localStorage.getItem('receipt-view')||v }}catch(e){{}}
  if(location.hash==='#plain') v='plain'; if(location.hash==='#tech') v='tech';
  set(v); bp.onclick=function(){{set('plain')}}; bt.onclick=function(){{set('tech')}};
}})();
</script>
"""


if __name__ == "__main__":
    d = json.loads(Path(sys.argv[1]).read_text())
    sys.stdout.write(render(d, sys.argv[2] if len(sys.argv) > 2 else None))  # legacy entry; prefer `claimcheck page`
