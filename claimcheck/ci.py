"""Pull-request receipts and the merge gate. The logic lives here so it runs in any CI or a pre-push hook;
the GitHub Action (action.yml) is a thin wrapper around `claimcheck ci check`.

  claimcheck ci check [--base REF] [--head REF] [--comment] [--status] ...   exit 0 pass · 1 gate failed · 3 outage (fail-closed)
  claimcheck ci attach [receipt...] [--privacy summary|full|hashes]          copy this branch's receipts into .claimcheck/receipts/
  claimcheck ci trust                                                        pin this machine's signing key for the repo
  claimcheck ci key create <owner/repo> | list | revoke <prefix>             Team: a repo key for CI (upload-only)

Receipts are DATA: read as git blobs (or through the GitHub API), verified offline (content id + ed25519 signature,
and the signer against .claimcheck/trusted-keys as it is on the BASE branch, so a PR can't pin its own key). Nothing
from the pull request is executed. The verdicts inside a receipt were made on the machine that ran the agent, where
the run log lives; CI proves the receipt wasn't edited after signing and applies the merge rules to it.

Outages (GitHub API, claimcheck.cc) never decide a verdict: everything the gate needs is in the repo. With
--on-outage open (default) a failed comment, status or link is reported loudly (annotation + job summary) and the
offline verdict stands; with closed, an outage fails the check with exit 3 so it can't be mistaken for a flag.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .document import check_id, parse_iso
from .sign import verify_signature

DIR = ".claimcheck/receipts"
TRUSTED = ".claimcheck/trusted-keys"
MARKER = "<!-- claimcheck:pr-receipts -->"
CONTEXT = "claimcheck"
FLAGS = ("contradicted", "pre-existing", "unverified")
MAX_BYTES = 2 * 1024 * 1024
MAX_RECEIPTS = 200
OUTAGE_NOTE = "claimcheck.cc didn't answer"
DOCS = "https://github.com/CocaKova/claimcheck/blob/main/docs/ci.md"
AI_RE = re.compile(r"^(?:co-authored-by|assisted-by|generated-by):[^\n]*\b(claude|anthropic|codex|openai|chatgpt|copilot|"
                   r"cursor|gemini|aider|devin|openhands|jules|hermes|windsurf|cline)\b|generated with \[?(?:claude code|codex|"
                   r"cursor|aider|copilot)", re.I | re.M)
TRAILER_RE = re.compile(r"^claimcheck-receipt:[ \t]*(\S+)[ \t]*$", re.I | re.M)


def _usage(msg: str):
    """A setup problem: exit 2, never 1, so it can't be read as the gate's verdict."""
    print(msg, file=sys.stderr)
    raise SystemExit(2)


class Outage(Exception):
    """A service didn't answer (unreachable, 5xx, 429). Never a verdict."""


class Denied(Exception):
    """The service answered no (401/403/404 on a write): a setup matter, e.g. a fork PR's read-only token."""


# ------------------------------------------------------------------ http

def _http(method: str, url: str, body=None, headers: dict | None = None, timeout: float = 15, what: str = "GitHub",
          raw: bool = False, retries: int = 1):
    """(status, json|bytes). Raises Outage after one retry on no answer / 5xx / 429."""
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", f"claimcheck-ci/{__version__}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                return r.status, (b if raw else json.loads(b or b"{}"))
        except urllib.error.HTTPError as e:
            b = e.read() or b""
            if e.code >= 500 or e.code == 429:
                err = f"{what} answered {e.code}"
            else:
                try:
                    return e.code, json.loads(b or b"{}")
                except ValueError:
                    return e.code, {"message": b[:200].decode("utf-8", "replace")}
        except (urllib.error.URLError, OSError, ValueError) as e:
            err = f"can't reach {what}: {getattr(e, 'reason', e)}"
        if attempt < retries:
            time.sleep(2)
    raise Outage(err)


class GitHub:
    def __init__(self, repo: str, token: str | None, api: str | None = None):
        self.repo, self.token = repo, token
        self.api = (api or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def call(self, method: str, path: str, body=None, raw: bool = False, accept: str = "application/vnd.github+json"):
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return _http(method, self.api + path, body, h, raw=raw, what="GitHub")

    def pages(self, path: str, limit: int = 30) -> list:
        out = []
        for page in range(1, limit + 1):
            code, rows = self.call("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            if code != 200:
                raise Denied(f"GitHub {code} on {path}: {rows.get('message', '') if isinstance(rows, dict) else ''}")
            out += rows
            if len(rows) < 100:
                break
        return out


# ------------------------------------------------------------------ where the receipts come from

def _git(*args: str, cwd: str | None = None) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


class GitSource:
    """The PR as this clone sees it: commits in base..head, receipt files added or changed in merge-base..head."""

    def __init__(self, base: str | None, head: str | None):
        self.head = self._need(head or "HEAD")
        self.base_tip = self._need(base or self._default_base())
        self.base = _git("merge-base", self.base_tip, self.head) or self.base_tip

    @staticmethod
    def _default_base() -> str:
        for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
            if _git("rev-parse", "--verify", "-q", ref + "^{commit}"):
                return ref
        _usage("claimcheck ci: no base branch found here; pass --base <ref>")

    @staticmethod
    def _need(ref: str) -> str:
        sha = _git("rev-parse", "--verify", "-q", ref + "^{commit}")
        if not sha and re.fullmatch(r"[0-9a-f]{7,40}", ref):   # a shallow CI clone: fetch just that commit once
            _git("fetch", "--no-tags", "-q", "origin", ref)
            sha = _git("rev-parse", "--verify", "-q", ref + "^{commit}")
        if not sha:
            _usage(f"claimcheck ci: commit {ref} isn't in this clone (on GitHub use actions/checkout "
                             "with fetch-depth: 0)")
        return sha

    def changed(self, d: str) -> list[str]:
        out = _git("diff", "--name-only", "--no-renames", "--diff-filter=AM", f"{self.base}..{self.head}", "--", d)
        return [p for p in out.splitlines() if p.endswith(".json")]

    def blob(self, path: str, ref: str | None = None) -> bytes | None:
        r = subprocess.run(["git", "cat-file", "blob", f"{ref or self.head}:{path}"], capture_output=True)
        return r.stdout if r.returncode == 0 else None

    def messages(self) -> list[str]:
        out = subprocess.run(["git", "log", "--format=%B%x00", f"{self.base_tip}..{self.head}"],
                             capture_output=True, text=True).stdout
        return [m.strip() for m in out.split("\0") if m.strip()]

    def trusted(self) -> bytes | None:
        return self.blob(TRUSTED, self.base_tip)


class GitHubSource:
    """The PR through the GitHub API, for a job that must not check out the PR (comments on fork PRs)."""

    def __init__(self, gh: GitHub, pr: int):
        self.gh, self.pr = gh, pr
        code, info = gh.call("GET", f"/repos/{gh.repo}/pulls/{pr}")
        if code != 200:
            raise Denied(f"GitHub {code} reading PR #{pr}: {info.get('message', '')}")
        self.base_tip, self.head = info["base"]["sha"], info["head"]["sha"]
        self.base = self.base_tip

    def changed(self, d: str) -> list[str]:
        files = self.gh.pages(f"/repos/{self.gh.repo}/pulls/{self.pr}/files")
        return [f["filename"] for f in files if f.get("status") in ("added", "modified", "renamed", "changed")
                and f["filename"].startswith(d.rstrip("/") + "/") and f["filename"].endswith(".json")]

    def blob(self, path: str, ref: str | None = None) -> bytes | None:
        code, b = self.gh.call("GET", f"/repos/{self.gh.repo}/contents/{urllib.parse.quote(path)}?ref={ref or self.head}",
                               raw=True, accept="application/vnd.github.raw")
        return b if code == 200 else None

    def messages(self) -> list[str]:
        return [c["commit"]["message"] for c in self.gh.pages(f"/repos/{self.gh.repo}/pulls/{self.pr}/commits", limit=3)]

    def trusted(self) -> bytes | None:
        return self.blob(TRUSTED, self.base_tip)


# ------------------------------------------------------------------ checking one receipt

def parse_pins(raw: bytes | None) -> dict | None:
    """kid -> pinned public key (base64) or '' when only the kid is pinned. None = the repo pins nothing."""
    if raw is None:
        return None
    pins = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        parts = line.split("#", 1)[0].split()
        if parts and re.fullmatch(r"[0-9a-f]{16}", parts[0]):
            pins[parts[0]] = parts[1] if len(parts) > 1 else ""
    return pins


def check_receipt(raw: bytes, name: str, pins: dict | None) -> dict:
    e: dict = {"path": name, "problems": []}
    if len(raw) > MAX_BYTES:
        e["problems"].append("larger than 2 MB")
        return e
    try:
        doc = json.loads(raw)
    except ValueError:
        e["problems"].append("not valid JSON")
        return e
    if not isinstance(doc, dict) or "claimcheck" not in doc or not isinstance(doc.get("summary"), dict):
        e["problems"].append("not a claimcheck receipt")
        return e
    run = doc.get("run") or {}
    agent = run.get("agent") or {}
    e.update(id=doc.get("id"), title=run.get("title") or "", agent=" · ".join(x for x in (agent.get("platform"), agent.get("model")) if x),
             summary={k: doc["summary"].get(k, 0) for k in ("verified", "unverified", "pre_existing", "contradicted", "unchecked")},
             headline=doc["summary"].get("headline", ""), privacy=doc.get("privacy", "full"), claims=len(doc.get("claims") or []))
    e["_doc"] = doc
    if not check_id(doc):
        e["problems"].append("edited after it was made (the content id doesn't match)")
    ok, why = verify_signature(doc)
    sig = doc.get("signature") or {}
    e["signer"] = sig.get("kid")
    if not ok:
        e["problems"].append("unsigned" if why == "unsigned" else why)
    elif pins is not None:
        pin = pins.get(sig.get("kid"))
        if pin is None:
            e["problems"].append(f"signed by key {sig.get('kid')}, which isn't in {TRUSTED} on the base branch")
        elif pin and pin != sig.get("pub"):
            e["problems"].append(f"key {sig.get('kid')} doesn't match the public key pinned in {TRUSTED}")
        else:
            e["pinned"] = True
    hashed = doc.get("privacy") == "hashes"
    e["flagged"] = [{"verdict": c.get("verdict"), "text": "(text withheld: hashes privacy)" if hashed else c.get("text", ""),
                     "evidence": c.get("evidence", "")} for c in doc.get("claims") or [] if c.get("verdict") in FLAGS]
    return e


# ------------------------------------------------------------------ hosted layer (Team: repo key)

def _site() -> str:
    """The API (CLAIMCHECK_URL in CI, else the configured cloud.url, else api.claimcheck.cc)."""
    from .cloud import base_url
    return (os.environ.get("CLAIMCHECK_URL") or base_url()).rstrip("/")


def link_receipts(entries: list[dict], key: str, outages: list[str]) -> None:
    """Share each sound receipt with the repo key: a page link plus the live-witness state. Idempotent server-side."""
    from .cloud import api
    for e in entries:
        if e["problems"] or "_doc" not in e or e.get("url"):
            continue
        for attempt in (0, 1):   # one retry, like every other call here
            code, out = api("POST", "/v1/receipts", e["_doc"], timeout=20, api_key=key, url=_site())
            if not (code == 0 or code >= 500 or code == 429) or attempt:
                break
            time.sleep(2)
        if code == 200:
            e["url"], e["witness"] = out.get("url"), out.get("witness")
        elif code == 0 or code >= 500 or code == 429:
            outages.append(f"claimcheck.cc: {out.get('error') or code} (no link for {e['id']})")
            e["link_note"] = OUTAGE_NOTE
        else:
            e["link_note"] = out.get("error") or f"claimcheck.cc said {code}"


def fetch_shared(url: str, pins: dict | None, outages: list[str]) -> dict | None:
    """A receipt named by its share link in a commit trailer. Only claimcheck.cc links are fetched."""
    site = urllib.parse.urlsplit(_site())
    u = urllib.parse.urlsplit(url)
    hosts = {site.netloc, "claimcheck.cc", "www.claimcheck.cc"}   # share links live on the site, which forwards /r/ to the API
    if u.scheme != "https" and u.netloc != site.netloc or u.netloc not in hosts or not re.fullmatch(r"/r/[\w-]{10,40}", u.path):
        return None
    try:
        code, out = _http("GET", f"{site.scheme}://{site.netloc}{u.path}.json", what="claimcheck.cc")
    except Outage as ex:
        outages.append(f"{ex} (couldn't fetch {url})")
        return None
    if code != 200 or not isinstance(out, dict) or not isinstance(out.get("receipt"), dict):
        return {"path": url, "problems": [f"the link doesn't open ({code})"], "url": url}
    e = check_receipt(json.dumps(out["receipt"]).encode(), url, pins)
    e["url"], e["witness"] = url, (out.get("witness") or {}).get("state")
    return e


# ------------------------------------------------------------------ the gate

def parse_fail_on(s: str) -> tuple[str, ...]:
    s = (s or "").strip().lower()
    if s in ("", "none", "never"):
        return ()
    if s == "flagged":
        return FLAGS
    out = tuple(x.strip() for x in s.split(",") if x.strip())
    bad = [x for x in out if x not in FLAGS]
    if bad:
        _usage(f"--fail-on: unknown verdict {bad[0]!r} (contradicted, pre-existing, unverified, flagged, none)")
    return out


def gate(entries: list[dict], *, fail_on, require: str, ai_commits: int, require_witness: bool,
         outages: list[str], on_outage: str) -> tuple[int, list[str]]:
    reasons = []
    for e in entries:
        name = e.get("id") or e["path"]
        if e["problems"]:
            reasons.append(f"{name}: " + "; ".join(e["problems"]))
            continue
        for v in fail_on:
            n = e["summary"].get(v.replace("-", "_"), 0)
            if n:
                reasons.append(f"{name}: {n} {v} claim{'s' if n > 1 else ''}")
        w = e.get("witness")
        if w == "rewritten":
            reasons.append(f"{name}: claimcheck.cc saw the run log rewritten after the run")
        elif require_witness and w != "witnessed" and not (w is None and e.get("link_note") == OUTAGE_NOTE):
            # unknown because claimcheck.cc didn't answer: the outage policy decides (open: stands, closed: exit 3)
            reasons.append(f"{name}: not witnessed live ({w or e.get('link_note') or 'no claimcheck.cc key'})")
    if not entries and (require == "always" or (require == "ai" and ai_commits)):
        reasons.append("no receipts in this pull request" + (f", and {ai_commits} commit(s) say an AI agent co-wrote them"
                                                             if ai_commits else ""))
    if reasons:
        return 1, reasons
    if outages and on_outage == "closed":
        return 3, []
    return 0, []


# ------------------------------------------------------------------ the comment

_MD = re.compile(r"([\\`*_{}\[\]()#+!|~>])")


def _md(s, n: int = 220) -> str:
    s = " ".join(str(s or "").split())
    s = s[:n] + ("…" if len(s) > n else "")
    return _MD.sub(r"\\\1", html.escape(s, quote=False)).replace("@", "@​")


def _code(s, n: int = 60) -> str:
    """Text for a `code span`: markdown escapes don't apply inside one, so only backticks and newlines go."""
    return " ".join(str(s or "").replace("`", "'").split())[:n].replace("|", "\\|")


def render(entries: list[dict], code: int, reasons: list[str], *, ai_commits: int, rules: str, outages: list[str],
           notes: list[str]) -> str:
    head = {0: "passed", 1: "**failed**", 3: "couldn't finish (fail-closed)"}[code]
    tot = {k: sum(e.get("summary", {}).get(k, 0) for e in entries) for k in
           ("verified", "unverified", "pre_existing", "contradicted", "unchecked")}
    claims = sum(e.get("claims", 0) for e in entries)
    out = [MARKER, f"### claimcheck: merge gate {head}", ""]
    if entries:
        out.append(f"{len(entries)} receipt{'s' if len(entries) != 1 else ''} · {claims} claims: {tot['verified']} verified · "
                   f"{tot['unverified']} unverified · {tot['pre_existing']} pre-existing · {tot['contradicted']} contradicted · "
                   f"{tot['unchecked']} unchecked")
        out += ["", "| Receipt | Agent | Claims | Flags | Signature | Page |", "|---|---|---|---|---|---|"]
        for e in entries:
            s = e.get("summary", {})
            flags = ", ".join(f"{s.get(k.replace('-', '_'))} {k}" for k in FLAGS if s.get(k.replace("-", "_"))) or "none"
            if e["problems"]:
                sig = "FAILS: " + _md("; ".join(e["problems"]), 160)
            else:
                sig = f"ok · `{e.get('signer')}`" + (" (pinned)" if e.get("pinned") else "")
            page = (f"[open]({e['url']})" + (f" · {e['witness']}" if e.get("witness") else "")) if e.get("url") else _md(e.get("link_note") or "–", 80)
            title = (" " + _md(e["title"], 70)) if e.get("title") else ""
            out.append(f"| `{_code(e.get('id') or e['path'])}`{title} | {_md(e.get('agent') or '–', 50)} | {e.get('claims', 0)} | "
                       f"{flags} | {sig} | {page} |")
        flagged = [(e, c) for e in entries for c in e.get("flagged", [])]
        if flagged:
            out += ["", f"<details><summary>Flagged claims ({len(flagged)})</summary>", ""]
            for e, c in flagged[:40]:
                out.append(f"- **{c['verdict']}** · {_md(c['text'])}" + (f"  \n  _{_md(c['evidence'], 200)}_" if c.get("evidence") else ""))
            if len(flagged) > 40:
                out.append(f"- … and {len(flagged) - 40} more (open the receipts)")
            out += ["", "</details>"]
    else:
        out.append("No receipts in this pull request." + (f" {ai_commits} commit(s) say an AI agent co-wrote them." if ai_commits else ""))
        out.append("")
        out.append("To add one: `claimcheck ci attach` on the machine that ran the agent, then commit `.claimcheck/receipts/`.")
    if reasons:
        out += ["", "**Why it failed**", ""] + [f"- {_md(r, 300)}" for r in reasons]
    if outages:
        out += ["", "**Couldn't reach a service** (the verdict above didn't depend on it):", ""] + [f"- {_md(o, 200)}" for o in outages]
    for n in notes:
        out += ["", _md(n, 300)]
    out += ["", f"<sub>Checked offline from the receipts in this pull request: content ids and ed25519 signatures"
                f"{' against the keys pinned on the base branch' if any(e.get('pinned') for e in entries) else ''}. "
                f"The verdicts were made on the machine that ran the agent. Rules: {rules}. "
                f"claimcheck {__version__} · [how this works]({DOCS})</sub>"]
    return "\n".join(out)


# ------------------------------------------------------------------ posting

def github_comment(gh: GitHub, pr: int, body: str) -> str:
    """One sticky comment: update ours if it exists, else post. Returns posted | updated | unchanged."""
    comments = gh.pages(f"/repos/{gh.repo}/issues/{pr}/comments", limit=10)
    ours = [c for c in comments if MARKER in (c.get("body") or "")]
    ours.sort(key=lambda c: not (c.get("user") or {}).get("login", "").endswith("[bot]"))
    for c in ours:
        if c.get("body") == body:
            return "unchanged"
        code, _ = gh.call("PATCH", f"/repos/{gh.repo}/issues/comments/{c['id']}", {"body": body})
        if code == 200:
            return "updated"
    code, out = gh.call("POST", f"/repos/{gh.repo}/issues/{pr}/comments", {"body": body})
    if code == 201:
        return "posted"
    raise Denied(f"GitHub {code}: {out.get('message', '') if isinstance(out, dict) else ''}")


def github_status(gh: GitHub, sha: str, code: int, reasons: list[str], target: str | None) -> None:
    state = {0: "success", 1: "failure", 3: "error"}[code]
    desc = {0: "receipts check out", 1: (reasons[0] if reasons else "gate failed"), 3: "a service didn't answer (fail-closed)"}[code]
    body = {"state": state, "context": CONTEXT, "description": desc[:139]}
    if target:
        body["target_url"] = target
    c, out = gh.call("POST", f"/repos/{gh.repo}/statuses/{sha}", body)
    if c != 201:
        raise Denied(f"GitHub {c}: {out.get('message', '') if isinstance(out, dict) else ''}")


def gitlab_comment(body: str) -> str:
    api, pid, iid = os.environ.get("CI_API_V4_URL"), os.environ.get("CI_PROJECT_ID"), os.environ.get("CI_MERGE_REQUEST_IID")
    token = os.environ.get("GITLAB_TOKEN") or os.environ.get("CLAIMCHECK_GITLAB_TOKEN")
    if not (api and pid and iid):
        raise Denied("not a GitLab merge request pipeline")
    if not token:
        raise Denied("set GITLAB_TOKEN (a project access token with api scope) to post the comment")
    h = {"PRIVATE-TOKEN": token}
    base = f"{api.rstrip('/')}/projects/{urllib.parse.quote(pid, safe='')}/merge_requests/{iid}/notes"
    code, notes = _http("GET", base + "?per_page=100&sort=desc", headers=h, what="GitLab")
    if code != 200:
        raise Denied(f"GitLab {code}")
    for n in notes:
        if MARKER in (n.get("body") or ""):
            if n.get("body") == body:
                return "unchanged"
            code, _ = _http("PUT", f"{base}/{n['id']}", {"body": body}, h, what="GitLab")
            if code == 200:
                return "updated"
    code, _ = _http("POST", base, {"body": body}, h, what="GitLab")
    if code == 201:
        return "posted"
    raise Denied(f"GitLab {code}")


# ------------------------------------------------------------------ check

def _event() -> dict:
    p = os.environ.get("GITHUB_EVENT_PATH")
    try:
        return json.loads(Path(p).read_text()) if p else {}
    except (OSError, ValueError):
        return {}


def _pr_for_workflow_run(gh: GitHub, ev: dict) -> int | None:
    """workflow_run.pull_requests is empty for fork PRs: find the open PR whose head is exactly that commit."""
    wr = ev.get("workflow_run") or {}
    if wr.get("pull_requests"):
        return wr["pull_requests"][0]["number"]
    owner = ((wr.get("head_repository") or {}).get("owner") or {}).get("login")
    if not owner or not wr.get("head_branch"):
        return None
    for p in gh.pages(f"/repos/{gh.repo}/pulls?state=open&head={owner}:{urllib.parse.quote(wr['head_branch'])}", limit=1):
        if p["head"]["sha"] == wr.get("head_sha"):
            return p["number"]
    return None


def _out(kind: str, msg: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{kind} title=claimcheck::{msg.replace(chr(10), ' ')}")
    else:
        print(f"{kind}: {msg}", file=sys.stderr)


def cmd_check(a) -> int:
    gh_env = os.environ.get("GITHUB_ACTIONS") == "true"
    ev = _event() if gh_env else {}
    pr_ev = ev.get("pull_request") or {}
    repo = a.repo or os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    outages: list[str] = []
    notes: list[str] = []
    gh = GitHub(repo, token) if repo else None
    pr = a.pr or pr_ev.get("number")
    try:
        if a.from_github:
            if not gh:
                _usage("--from-github needs --repo (or GITHUB_REPOSITORY)")
            pr = pr or (_pr_for_workflow_run(gh, ev) if ev.get("workflow_run") else None)
            if not pr:
                print("claimcheck ci: no open pull request for this run; nothing to do")
                return 0
            src = GitHubSource(gh, int(pr))
        else:
            base = a.base or (pr_ev.get("base") or {}).get("sha") or os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
            head = a.head or (pr_ev.get("head") or {}).get("sha")
            src = GitSource(base, head)
        pins = parse_pins(src.trusted()) if not a.trusted_keys else parse_pins(Path(a.trusted_keys).read_bytes())
        paths = src.changed(a.dir)
        msgs = src.messages()
    except Outage as ex:   # the API source has nothing to verify without GitHub
        outages.append(str(ex))
        _out("warning", f"{ex}: couldn't read the pull request")
        return 3 if a.on_outage == "closed" else 0
    except Denied as ex:
        _out("warning", str(ex))
        return 0
    entries, seen = [], set()
    for p in paths[:MAX_RECEIPTS]:
        raw = src.blob(p)
        if raw is None:
            continue
        e = check_receipt(raw, p, pins)
        if e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        entries.append(e)
    if len(paths) > MAX_RECEIPTS:
        notes.append(f"Only the first {MAX_RECEIPTS} of {len(paths)} receipt files were checked.")
    ai_commits = sum(1 for m in msgs if AI_RE.search(m))
    for m in msgs:
        for ref in TRAILER_RE.findall(m):
            if ref.startswith("http"):
                e = fetch_shared(ref, pins, outages)
                if e and e.get("id") not in seen:
                    seen.add(e.get("id"))
                    entries.append(e)
            elif ref not in seen:
                notes.append(f"A commit names receipt {ref}, but it isn't in this pull request.")
    key = a.api_key or os.environ.get("CLAIMCHECK_API_KEY")
    if key and entries:
        link_receipts(entries, key, outages)
    elif a.require_witness and not key:
        notes.append("--require-witness needs a claimcheck.cc repo key (CLAIMCHECK_API_KEY); fork pull requests get no secrets.")
    fail_on = parse_fail_on(a.fail_on)
    code, reasons = gate(entries, fail_on=fail_on, require=a.require_receipts, ai_commits=ai_commits,
                         require_witness=a.require_witness, outages=outages, on_outage=a.on_outage)
    rules = (f"fail on {', '.join(fail_on) or 'nothing flagged'}, tampered or unsigned receipts"
             + (f", keys not pinned in {TRUSTED}" if pins is not None else "")
             + {"never": "", "ai": " · receipts required when a commit says an AI co-wrote it",
                "always": " · a receipt required"}[a.require_receipts]
             + (" · live witness required" if a.require_witness else "") + f" · outages fail {a.on_outage}")

    def body() -> str:
        return render(entries, code, reasons, ai_commits=ai_commits, rules=rules, outages=outages, notes=notes)

    is_fork = bool(pr_ev) and ((pr_ev.get("head") or {}).get("repo") or {}).get("full_name") != repo
    if a.comment:
        try:
            if os.environ.get("GITLAB_CI") == "true":
                print(f"comment {gitlab_comment(body())}")
            elif not (gh and pr):
                print("no pull request here: comment skipped")
            elif not token:
                _out("notice", "no GITHUB_TOKEN: comment skipped")
            else:
                print(f"comment {github_comment(gh, int(pr), body())}")
        except Outage as ex:
            outages.append(f"{ex} (comment not posted)")
        except Denied as ex:
            _out("notice", f"comment not posted: {ex}" + (" (fork pull request: the token is read-only; the comment "
                                                           "workflow in docs/ci.md posts it)" if is_fork else ""))
    if a.status and gh and token:
        sha = getattr(src, "head", None)
        run = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
               if os.environ.get("GITHUB_RUN_ID") else None)
        try:
            github_status(gh, sha, code if code != 0 or not (outages and a.on_outage == "closed") else 3, reasons, run)
        except Outage as ex:
            outages.append(f"{ex} (status not set)")
        except Denied as ex:
            _out("notice", f"status not set: {ex}")
    if outages and a.on_outage == "closed" and code == 0:
        code = 3
    text = body()
    for r in reasons:
        _out("error", r)
    for o in outages:
        _out("warning", o + (" — failing closed" if a.on_outage == "closed" else " — failing open: the offline verdict stands"))
    if a.markdown:
        Path(a.markdown).write_text(text)
    if a.json:
        Path(a.json).write_text(json.dumps({"result": {0: "pass", 1: "fail", 3: "outage"}[code], "exit": code,
                                            "reasons": reasons, "outages": outages, "ai_commits": ai_commits,
                                            "receipts": [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries]}, indent=1))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text.replace(MARKER, "") + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"result={ {0: 'pass', 1: 'fail', 3: 'outage'}[code] }\nreceipts={len(entries)}\n")
    print(f"claimcheck ci: {len(entries)} receipt(s), {ai_commits} AI-co-authored commit(s): "
          + {0: "PASS", 1: "FAIL", 3: "OUTAGE (fail-closed)"}[code])
    return code


# ------------------------------------------------------------------ attach / trust / key

def cmd_attach(a) -> int:
    from .cloud import view
    from .store import iter_receipts
    root = Path(_git("rev-parse", "--show-toplevel") or ".")
    dest = root / a.dir
    picked = []
    if a.which:
        for w in a.which:
            if Path(w).is_file():
                picked.append(json.loads(Path(w).read_text()))
                continue
            d = next((d for _, d in iter_receipts() if w in (d["id"], d["run"]["session_id"])), None)
            if not d:
                raise SystemExit(f"no receipt matches {w!r}")
            picked.append(d)
    else:
        g = GitSource(a.base, "HEAD")
        since = int(_git("show", "-s", "--format=%ct", g.base) or 0)
        names = {Path(p).name for p in (_git("diff", "--name-only", g.base).splitlines()
                                        + _git("ls-files", "--others", "--exclude-standard").splitlines())}
        for _, d in iter_receipts():
            try:
                made = parse_iso(d["created_at"]).timestamp()
            except (KeyError, ValueError):
                continue
            wrote = {Path(p).name for p in d.get("ledger", {}).get("files", {}).get("written", [])}
            if made >= since and wrote & names:
                picked.append(d)
        if not picked:
            print("no receipts on this machine touch the files this branch changes; name one: claimcheck ci attach <receipt-id>")
            return 1
    dest.mkdir(parents=True, exist_ok=True)
    for d in picked:
        try:
            v = view(d, a.privacy)
        except ValueError as ex:
            print(f"skip {d['id']}: {ex}")
            continue
        p = dest / f"{v['id']}.json"
        if p.exists():
            print(f"have {p.relative_to(root)}")
            continue
        p.write_text(json.dumps(v, indent=1, ensure_ascii=False) + "\n")
        s = v["summary"]
        print(f"wrote {p.relative_to(root)} · {s['headline']} · privacy {v.get('privacy')}")
    if subprocess.run(["git", "check-ignore", "-q", str(dest / "x.json")], cwd=root).returncode == 0:
        print(f"WARNING: .gitignore hides {a.dir}/ (a 'receipts/' rule?), so the receipts won't be committed. "
              f"Add '!{a.dir}/' to .gitignore, or anchor the rule ('/receipts/').")
    print(f"commit {a.dir}/ with the change; at '{a.privacy}' a receipt carries the agent's report and claims "
          + ("and file names" if a.privacy != "hashes" else "as hashes only"))
    return 0


def cmd_trust(a) -> int:
    from .sign import KEY_FILE, keygen, kid, load_key, pub_raw
    if not KEY_FILE.exists():
        keygen(KEY_FILE)
    pub = pub_raw(load_key(KEY_FILE))
    root = Path(_git("rev-parse", "--show-toplevel") or ".")
    p = root / TRUSTED
    line = f"{kid(pub)} {base64.b64encode(pub).decode()}" + (f"  # {a.note}" if a.note else "")
    have = p.read_text() if p.exists() else ("# claimcheck: receipts in pull requests must be signed by one of these keys.\n"
                                             "# One per line: <kid> <public key>. CI reads this file from the base branch.\n")
    if kid(pub) in have:
        print(f"{kid(pub)} is already pinned in {TRUSTED}")
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(have.rstrip("\n") + "\n" + line + "\n")
    print(f"pinned {kid(pub)} in {TRUSTED}: merge it to the base branch first, then PRs signed by other keys fail the gate")
    return 0


def cmd_key(a) -> int:
    from .cloud import api, key
    if not key():
        raise SystemExit("log in first: claimcheck login <key> (the account that owns the repo key)")
    if a.action == "create":
        if not a.arg:
            raise SystemExit("claimcheck ci key create <owner/repo>")
        code, out = api("POST", "/v1/repo-keys", {"repo": a.arg})
        if code != 200:
            raise SystemExit(out.get("error") or f"claimcheck.cc said {code}")
        print(f"{out['key']}\nshown once: add it to {a.arg} as the secret CLAIMCHECK_API_KEY. It can only share receipts "
              "(no history, no deleting, no witness).")
    elif a.action == "list":
        code, out = api("GET", "/v1/repo-keys")
        if code != 200:
            raise SystemExit(out.get("error") or f"claimcheck.cc said {code}")
        for k in out.get("keys", []):
            print(f"{k['prefix']}…  {k['repo']}  created {k['created_at'][:10]}" + ("  REVOKED" if k.get("revoked") else ""))
        if not out.get("keys"):
            print("no repo keys")
    else:
        code, out = api("DELETE", f"/v1/repo-keys/{urllib.parse.quote(a.arg or '')}")
        print("revoked" if code == 200 else (out.get("error") or f"not found ({code})"))
    return 0


def add_parser(sub) -> None:
    ci = sub.add_parser("ci", help="pull-request receipts and the merge gate (any CI, or a pre-push hook)")
    cs = ci.add_subparsers(dest="ci_cmd", required=True)
    c = cs.add_parser("check", help="verify the receipts in a pull request, comment, and exit 0 pass / 1 fail / 3 outage")
    c.add_argument("--base", help="base ref (default: the PR's base in CI, else origin/HEAD)")
    c.add_argument("--head", help="head ref (default: the PR's head in CI, else HEAD)")
    c.add_argument("--dir", default=DIR, help=f"where receipts are committed (default {DIR})")
    c.add_argument("--fail-on", default="contradicted", help="verdicts that fail the gate: contradicted (default), "
                   "pre-existing, unverified, comma list, flagged = all three, none")
    c.add_argument("--require-receipts", choices=["never", "ai", "always"], default="never",
                   help="fail when the PR has no receipt: never (default), ai = when a commit says an AI co-wrote it, always")
    c.add_argument("--require-witness", action="store_true", help="Team: fail unless claimcheck.cc witnessed each run live")
    c.add_argument("--on-outage", choices=["open", "closed"], default="open",
                   help="when GitHub/claimcheck.cc don't answer: open (default) = offline verdict stands, loudly; closed = exit 3")
    c.add_argument("--trusted-keys", help=f"pinned keys file (default: {TRUSTED} on the base branch)")
    c.add_argument("--comment", action="store_true", help="upsert one sticky PR/MR comment (GitHub or GitLab)")
    c.add_argument("--status", action="store_true", help=f"also set a '{CONTEXT}' commit status on GitHub")
    c.add_argument("--from-github", action="store_true", help="read the PR through the API instead of this clone (no checkout)")
    c.add_argument("--repo", help="owner/repo (default GITHUB_REPOSITORY)")
    c.add_argument("--pr", type=int, help="pull request number (default: from the CI event)")
    c.add_argument("--api-key", help="Team: claimcheck.cc repo key for links + witness (default CLAIMCHECK_API_KEY)")
    c.add_argument("--markdown", help="write the comment here too")
    c.add_argument("--json", help="write the result as JSON here")
    c.set_defaults(f=lambda a: sys.exit(cmd_check(a)))
    at = cs.add_parser("attach", help="copy this branch's receipts into .claimcheck/receipts/ to commit with it")
    at.add_argument("which", nargs="*", help="receipt ids, session ids or files (default: receipts that wrote files this branch changes)")
    at.add_argument("--privacy", choices=["full", "summary", "hashes"], default="summary")
    at.add_argument("--base"); at.add_argument("--dir", default=DIR)
    at.set_defaults(f=lambda a: sys.exit(cmd_attach(a)))
    tr = cs.add_parser("trust", help=f"pin this machine's signing key in {TRUSTED}")
    tr.add_argument("--note", help="a label for the key, e.g. whose machine")
    tr.set_defaults(f=lambda a: sys.exit(cmd_trust(a)))
    k = cs.add_parser("key", help="Team: repo keys for CI (create <owner/repo> | list | revoke <prefix>)")
    k.add_argument("action", choices=["create", "list", "revoke"]); k.add_argument("arg", nargs="?")
    k.set_defaults(f=lambda a: sys.exit(cmd_key(a)))
