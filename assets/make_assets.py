#!/usr/bin/env python3
"""Regenerate the README header art (hero-light.svg, hero-dark.svg). Stdlib only."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# GitHub-like neutrals; accent and verdict colors are the ones the receipt page uses (claimcheck/page.py).
THEMES = {
    "dark": dict(bg="#0d1117", panel="#161b22", line="#30363d", text="#e6edf3", dim="#8b949e", accent="#9db6dd",
                 verified="#5cc48e", unverified="#e2b04a", pre="#f08a5a", contradicted="#f27b70", unchecked="#8b949e"),
    "light": dict(bg="#ffffff", panel="#f6f8fa", line="#d0d7de", text="#1f2328", dim="#656d76", accent="#23406e",
                  verified="#1f7a4d", unverified="#a16207", pre="#c2561b", contradicted="#b42318", unchecked="#656d76"),
}
FONT = "ui-sans-serif, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"
ALT = ("claimcheck: the hook records each tool call into a redacted, hash-chained run log; at the end of the turn "
       "the verifier checks every claim in the agent's final message against that log and writes a signed receipt")


def box(x, y, w, h, label, sub, c, stroke):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{c["panel"]}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x + w / 2}" y="{y + 30}" text-anchor="middle" font-family="{FONT}" font-size="17" font-weight="600" fill="{c["text"]}">{label}</text>'
            f'<text x="{x + w / 2}" y="{y + 52}" text-anchor="middle" font-family="{FONT}" font-size="12.5" fill="{c["dim"]}">{sub}</text>')


def arrow(x1, y1, x2, y2, color):
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2 - 7}" y2="{y2}" stroke="{color}" stroke-width="1.8"/>'
            f'<path d="M{x2 - 8},{y2 - 5} L{x2},{y2} L{x2 - 8},{y2 + 5} Z" fill="{color}"/>')


def chip(x, y, label, color, c):
    w = 16 + 8.4 * len(label)
    return (w, f'<rect x="{x}" y="{y}" width="{w:.0f}" height="26" rx="13" fill="none" stroke="{color}" stroke-width="1.4"/>'
               f'<text x="{x + w / 2:.1f}" y="{y + 17.5}" text-anchor="middle" font-family="{MONO}" font-size="13.5" '
               f'font-weight="600" fill="{color}">{label}</text>')


def hero(c):
    W, H = 1200, 320
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{ALT}">',
         f'<rect width="{W}" height="{H}" rx="16" fill="{c["bg"]}" stroke="{c["line"]}"/>',
         f'<text x="60" y="78" font-family="{MONO}" font-size="38" font-weight="700" fill="{c["text"]}">claimcheck</text>',
         f'<text x="60" y="112" font-family="{FONT}" font-size="18" fill="{c["dim"]}">'
         'Receipts for AI agent runs: what it touched, what it claimed, whether the two match.</text>']
    y, w, gap = 150, 232, 52
    nodes = [("agent turn", "tool calls, then a final message", c["line"]),
             ("hook", "redact, hash-chain each event", c["line"]),
             ("verifier", "claims vs. the run log, in code", c["line"]),
             ("signed receipt", "ed25519, id = content hash", c["accent"])]
    for i, (label, sub, stroke) in enumerate(nodes):
        x = 60 + i * (w + gap)
        s.append(box(x, y, w, 70, label, sub, c, stroke))
        if i:
            s.append(arrow(x - gap, y + 35, x, y + 35, c["dim"]))
    # every claim in the final message gets exactly one verdict
    y2 = 262
    s.append(f'<text x="60" y="{y2 + 18}" font-family="{FONT}" font-size="14" fill="{c["dim"]}">each claim is marked</text>')
    x = 212
    for label, key in (("verified", "verified"), ("unverified", "unverified"), ("pre-existing", "pre"),
                       ("contradicted", "contradicted"), ("unchecked", "unchecked")):
        cw, svg = chip(x, y2, label, c[key], c)
        s.append(svg)
        x += cw + 12
    s.append(f'<text x="{x + 6}" y="{y2 + 18}" font-family="{FONT}" font-size="14" fill="{c["dim"]}">'
             'by code, never by a model</text>')
    s.append("</svg>")
    return "".join(s)


for theme, colors in THEMES.items():
    with open(os.path.join(HERE, f"hero-{theme}.svg"), "w", encoding="utf-8") as f:
        f.write(hero(colors))
print("ok")
