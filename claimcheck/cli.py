"""claimcheck CLI.

  claimcheck receipt <hermes-session-id> [--fixture] [--classify none|local] [--privacy full|summary|hashes] [--sign] [--out DIR] [--md]
  claimcheck page <receipt.json> [--name NAME] > page.html
  claimcheck verify <receipt.json>
  claimcheck keygen
  claimcheck hook                        # stdin JSON from any agent's hook system → run log / receipt
  claimcheck flagged [--all] [--json]    # receipts whose headline is not verified (newest first)
  claimcheck chain <session-id>          # recompute the live run log's hash chain
  claimcheck init [agent...] [--remove] [--dry-run]   # wire every agent found here (claude-code codex gemini cursor hermes)
  claimcheck doctor                      # what is wired, how many receipts, any hook errors
  claimcheck open [receipt-id|session]   # open the latest (or named) receipt page in the browser
  claimcheck dump-fixture <hermes-session-id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .document import build, check_id
from .engine import extract_claims, verify as verify_claim
from .ledger import build_ledger, dump_fixture, load

OUT_DIR = Path(os.environ.get("RECEIPT_OUT", os.environ.get("CLAIMCHECK_OUT", "receipts")))
SCHEMA = Path(__file__).resolve().parent.parent / "spec" / "receipt-v0.1.schema.json"


def make_receipt(sid: str, *, fixture=False, classify="none", privacy="full", sign=False, adapter="hermes") -> dict:
    s, msgs = load(sid, fixture=fixture)
    L = build_ledger(msgs)
    final = next((m["content"] for m in reversed(msgs) if m["role"] == "assistant" and m["content"]), "")
    claims = extract_claims(final, use_llm=(classify != "none")) if final else []
    for c in claims:
        c["verdict"], c["evidence"] = verify_claim(c, L)
    doc = build(s, msgs, L, claims, adapter=adapter, platform=adapter, classifier=classify, privacy=privacy)
    if sign:
        from .sign import sign as _sign
        _sign(doc)
    return doc


def cmd_receipt(a):
    doc = make_receipt(a.session_id, fixture=a.fixture, classify=a.classify, privacy=a.privacy, sign=a.sign)
    out = Path(a.out or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{a.session_id}.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    if a.md:
        from .render_md import render
        s, msgs = load(a.session_id, fixture=a.fixture)
        L = build_ledger(msgs)
        cl = [{"text": c["text"], "_verdict": (c["verdict"], c["evidence"])} for c in doc["claims"]]
        p.with_suffix(".md").write_text(render(s, msgs, L, cl))
    S = doc["summary"]
    print(f"{doc['id']} · {S['verified']} verified · {S['unverified']} unverified · {S['pre_existing']} pre-existing · "
          f"{S['contradicted']} contradicted · {S['unchecked']} unchecked · {S['headline']}"
          + (f" · signed {doc['signature']['kid']}" if doc.get("signature") else "") + f"\n→ {p}", file=sys.stderr)


def cmd_page(a):
    from .page import render
    d = json.loads(Path(a.receipt).read_text())
    sys.stdout.write(render(d, a.name))


def cmd_verify(a):
    d = json.loads(Path(a.receipt).read_text())
    ok = True
    if SCHEMA.exists():
        try:
            import jsonschema
            errs = sorted(jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text())).iter_errors(d), key=lambda e: list(e.path))
            print(("ok " if not errs else "FAIL") + " schema" + ("" if not errs else ": " + "; ".join(f"{'/'.join(map(str, e.path))}: {e.message[:80]}" for e in errs[:5])))
            ok &= not errs
        except ImportError:
            print("skip schema (pip install jsonschema)")
    idok = check_id(d)
    print(("ok " if idok else "FAIL") + f" id {d.get('id')}" + ("" if idok else " (content changed)"))
    ok &= idok
    from .sign import verify_signature
    sok, why = verify_signature(d)
    print(("ok " if sok else ("--  " if why == "unsigned" else "FAIL")) + f" signature: {why}")
    ok &= sok or why == "unsigned"
    sys.exit(0 if ok else 1)


def cmd_flagged(a):
    from .store import digest, is_flagged, iter_receipts
    rows = [(p, d) for p, d in iter_receipts() if a.all or is_flagged(d)]
    if a.json:
        print(json.dumps([{"id": d["id"], "headline": d["summary"]["headline"], "session_id": d["run"]["session_id"],
                           "turn_id": d["run"].get("turn_id"), "platform": d["run"]["agent"].get("platform"),
                           "created_at": d["created_at"], "path": str(p)} for p, d in rows], indent=1))
        return
    if not rows:
        print("no flagged receipts" if not a.all else "no receipts yet")
    for p, d in rows[: a.limit]:
        print(digest(d, p) + "\n")


def cmd_chain(a):
    from .capture import RunLog, verify_chain
    log = RunLog(a.session_id)
    if not log.exists():
        sys.exit(f"no run log at {log.path}")
    recs = log.records()
    ok, info = verify_chain(recs)
    print(("ok " if ok else "FAIL") + f" chain: {len(recs)} events · " + (f"head {info}" if ok else info))
    sys.exit(0 if ok else 1)


def cmd_init(a):
    from .install import init
    for line in init(a.agents or None, remove=a.remove, dry_run=a.dry_run):
        print(line)


def cmd_doctor(a):
    from .install import doctor
    print("\n".join(doctor()))


def cmd_open(a):
    from .install import open_latest
    print(open_latest(a.which))


def cmd_keygen(a):
    from .sign import KEY_FILE, keygen, kid, load_key, pub_raw
    p = keygen(KEY_FILE, overwrite=a.force)
    print(f"{p} · kid {kid(pub_raw(load_key(p)))}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claimcheck", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"claimcheck {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("receipt"); r.add_argument("session_id"); r.add_argument("--fixture", action="store_true")
    r.add_argument("--classify", choices=["none", "local"], default="none"); r.add_argument("--privacy", choices=["full", "summary", "hashes"], default="full")
    r.add_argument("--sign", action="store_true"); r.add_argument("--out"); r.add_argument("--md", action="store_true"); r.set_defaults(f=cmd_receipt)
    p = sub.add_parser("page"); p.add_argument("receipt"); p.add_argument("--name"); p.set_defaults(f=cmd_page)
    v = sub.add_parser("verify"); v.add_argument("receipt"); v.set_defaults(f=cmd_verify)
    k = sub.add_parser("keygen"); k.add_argument("--force", action="store_true"); k.set_defaults(f=cmd_keygen)
    hk = sub.add_parser("hook"); hk.set_defaults(f=lambda a: sys.exit(__import__("claimcheck.hook", fromlist=["main"]).main()))
    fl = sub.add_parser("flagged"); fl.add_argument("--all", action="store_true"); fl.add_argument("--json", action="store_true")
    fl.add_argument("--limit", type=int, default=20); fl.set_defaults(f=cmd_flagged)
    c = sub.add_parser("chain"); c.add_argument("session_id"); c.set_defaults(f=cmd_chain)
    i = sub.add_parser("init"); i.add_argument("agents", nargs="*"); i.add_argument("--remove", action="store_true")
    i.add_argument("--dry-run", action="store_true"); i.set_defaults(f=cmd_init)
    sub.add_parser("doctor").set_defaults(f=cmd_doctor)
    o = sub.add_parser("open"); o.add_argument("which", nargs="?"); o.set_defaults(f=cmd_open)
    d = sub.add_parser("dump-fixture"); d.add_argument("session_id"); d.set_defaults(f=lambda a: print(dump_fixture(a.session_id)))
    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
