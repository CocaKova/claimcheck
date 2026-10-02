"""`claimcheck ci`: receipts in a pull request are checked offline, the gate follows the rules, outages never decide."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CLAIMCHECK_HOME", tempfile.mkdtemp(prefix="claimcheck-test-"))

from claimcheck import ci  # noqa: E402
from claimcheck.document import content_id  # noqa: E402
from claimcheck.sign import keygen, kid, load_key, pub_raw, sign  # noqa: E402

DEMO = json.loads((ROOT / "site" / "demo-receipt.json").read_text())
KEYS = Path(tempfile.mkdtemp(prefix="claimcheck-ci-keys-"))
KEY_A, KEY_B = load_key(keygen(KEYS / "a.key")), load_key(keygen(KEYS / "b.key"))


def receipt(verdicts=("verified",), key=KEY_A, text="Added `foo=1` to bar.yaml") -> dict:
    d = json.loads(json.dumps(DEMO))
    d.pop("signature", None)
    d["claims"] = [{"i": i, "text": f"{text} #{i}", "kind": "file_changed", "targets": ["foo=1"], "verdict": v,
                    "evidence": "seen"} for i, v in enumerate(verdicts)]
    n = {k: sum(1 for v in verdicts if v == k) for k in ("verified", "unverified", "pre-existing", "contradicted", "unchecked")}
    d["summary"] = {"verified": n["verified"], "unverified": n["unverified"], "pre_existing": n["pre-existing"],
                    "contradicted": n["contradicted"], "unchecked": n["unchecked"],
                    "headline": next((v for v in ("contradicted", "pre-existing", "unverified") if n[v]), "verified")}
    d["id"] = content_id(d)
    if key is not None:
        sign(d, key)
    return d


def git(repo: Path, *args: str, msg_env=None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README").write_text("x\n")
    git(tmp_path, "add", "."); git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "feature")
    monkeypatch.chdir(tmp_path)
    for k in ("GITHUB_ACTIONS", "GITHUB_EVENT_PATH", "GITHUB_REPOSITORY", "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_CI",
              "CLAIMCHECK_API_KEY", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "GITHUB_RUN_ID"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def add(repo: Path, doc: dict | str, name: str | None = None, msg: str = "work") -> None:
    d = repo / ".claimcheck" / "receipts"; d.mkdir(parents=True, exist_ok=True)
    body = doc if isinstance(doc, str) else json.dumps(doc, indent=1)
    (d / (name or f"{json.loads(body)['id']}.json")).write_text(body)
    git(repo, "add", "."); git(repo, "commit", "-qm", msg)


def run(*args: str) -> tuple[int, str]:
    out = Path(tempfile.mkdtemp()) / "c.md"
    with pytest.raises(SystemExit) as ex:
        from claimcheck.cli import main
        main(["ci", "check", "--base", "main", "--markdown", str(out), *args])
    return ex.value.code, out.read_text() if out.exists() else ""


def test_clean_receipt_passes_and_counts(repo):
    add(repo, receipt(("verified", "verified", "unverified")))
    code, md = run()
    assert code == 0
    assert md.startswith(ci.MARKER) and "merge gate passed" in md and "3 claims: 2 verified · 1 unverified" in md
    assert "Flagged claims (1)" in md


def test_contradicted_fails_by_default_and_rules_are_configurable(repo):
    add(repo, receipt(("verified", "contradicted")))
    code, md = run()
    assert code == 1 and "1 contradicted claim" in md and "merge gate **failed**" in md
    assert run("--fail-on", "none")[0] == 0
    add(repo, receipt(("unverified",), text="other"))
    assert run("--fail-on", "contradicted")[0] == 1
    assert run("--fail-on", "unverified")[0] == 1
    assert run("--fail-on", "pre-existing")[0] == 0


def test_edited_and_unsigned_receipts_fail(repo):
    d = receipt()
    d["claims"][0]["verdict"] = "verified"; d["summary"]["contradicted"] = 0
    d["claims"][0]["text"] = "something nicer"   # edited after signing
    add(repo, d, name="edited.json")
    code, md = run()
    assert code == 1 and "edited after it was made" in md
    git(repo, "rm", "-q", ".claimcheck/receipts/edited.json"); git(repo, "commit", "-qm", "rm")
    add(repo, receipt(key=None))
    code, md = run()
    assert code == 1 and "unsigned" in md


def test_not_a_receipt_fails(repo):
    add(repo, "{\"hello\": 1}", name="x.json")
    assert run()[0] == 1


def test_pinned_keys_come_from_the_base_branch(repo):
    pa = pub_raw(KEY_A)
    import base64
    git(repo, "checkout", "-q", "main")
    (repo / ".claimcheck").mkdir()
    (repo / ".claimcheck" / "trusted-keys").write_text(f"# team\n{kid(pa)} {base64.b64encode(pa).decode()}\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "pin")
    git(repo, "checkout", "-q", "feature"); git(repo, "merge", "-q", "main")
    add(repo, receipt(key=KEY_A))
    code, md = run()
    assert code == 0 and "(pinned)" in md
    # a PR can't pin its own key: the file is read from the base branch
    pb = pub_raw(KEY_B)
    with open(repo / ".claimcheck" / "trusted-keys", "a") as f:
        f.write(f"{kid(pb)} {base64.b64encode(pb).decode()}\n")
    add(repo, receipt(key=KEY_B, text="other"))
    code, md = run()
    assert code == 1 and "isn't in .claimcheck/trusted-keys on the base branch" in md


def test_receipts_required_for_ai_commits(repo):
    (repo / "a.py").write_text("print(1)\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "feat: a\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    assert run()[0] == 0                                  # default: never required
    code, md = run("--require-receipts", "ai")
    assert code == 1 and "1 commit(s) say an AI agent co-wrote them" in md
    add(repo, receipt())
    assert run("--require-receipts", "ai")[0] == 0


def test_human_commits_need_no_receipt_under_ai_rule(repo):
    (repo / "a.py").write_text("print(1)\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "a human change")
    assert run("--require-receipts", "ai")[0] == 0
    assert run("--require-receipts", "always")[0] == 1


def test_claim_text_cannot_inject_markdown_or_mentions(repo):
    add(repo, receipt(("unverified",), text="ping @octocat see [x](https://evil.example) <img src=x> | col"))
    md = run()[1]
    assert "@octocat" not in md and "](https://evil.example)" not in md and "<img" not in md
    assert "@​octocat" in md


# ------------------------------------------------------------------ against a fake GitHub / claimcheck.cc

class Fake(BaseHTTPRequestHandler):
    comments: list = []
    calls: list = []
    mode: dict = {}
    pr: dict = {}

    def log_message(self, *a):
        pass

    def _out(self, code, body, raw=False):
        b = body if raw else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_GET(self):
        Fake.calls.append(("GET", self.path))
        p = self.path.split("?")[0]
        if Fake.mode.get("down"):
            return self._out(502, {"message": "bad gateway"})
        if p.endswith("/issues/7/comments"):
            return self._out(200, Fake.comments if "page=1" in self.path else [])
        if p.endswith("/pulls/7"):
            return self._out(200, Fake.pr["info"])
        if p.endswith("/pulls/7/files"):
            return self._out(200, Fake.pr["files"] if "page=1" in self.path else [])
        if p.endswith("/pulls/7/commits"):
            return self._out(200, Fake.pr["commits"] if "page=1" in self.path else [])
        if "/contents/" in p:
            path = p.split("/contents/", 1)[1]
            b = Fake.pr["blobs"].get((path, self.path.split("ref=")[1]))
            return self._out(200, b, raw=True) if b is not None else self._out(404, {"message": "Not Found"})
        self._out(404, {"message": "Not Found"})

    def do_POST(self):
        body = self._body()
        Fake.calls.append(("POST", self.path))
        if Fake.mode.get("down") or Fake.mode.get("post_down"):
            return self._out(502, {"message": "bad gateway"})
        if self.path.endswith("/issues/7/comments"):
            if Fake.mode.get("fork"):
                return self._out(403, {"message": "Resource not accessible by integration"})
            Fake.comments.append({"id": len(Fake.comments) + 1, "body": body["body"], "user": {"login": "github-actions[bot]"}})
            return self._out(201, Fake.comments[-1])
        if "/statuses/" in self.path:
            Fake.mode["status"] = body
            return self._out(201, {})
        if self.path == "/v1/receipts":
            if self.headers.get("Authorization") != "Bearer cck_repo_test":
                return self._out(401, {"error": "missing or unknown API key"})
            return self._out(200, {"url": "https://claimcheck.cc/r/tok_" + body["id"][-8:], "witness": Fake.mode.get("witness", "witnessed")})
        self._out(404, {})

    def do_PATCH(self):
        body = self._body()
        Fake.calls.append(("PATCH", self.path))
        cid = int(self.path.rsplit("/", 1)[1])
        for c in Fake.comments:
            if c["id"] == cid:
                c["body"] = body["body"]
                return self._out(200, c)
        self._out(404, {})


@pytest.fixture
def fake(monkeypatch, repo, tmp_path_factory):
    Fake.comments, Fake.calls, Fake.mode, Fake.pr = [], [], {}, {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    ev = tmp_path_factory.mktemp("ev") / "event.json"
    ev.write_text(json.dumps({"pull_request": {"number": 7, "base": {"sha": git(repo, "rev-parse", "main")},
                                               "head": {"sha": "", "repo": {"full_name": "o/r"}}}}))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(ev))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("GITHUB_API_URL", url)
    monkeypatch.setenv("CLAIMCHECK_URL", url)
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    yield url
    srv.shutdown()


def test_one_sticky_comment_is_updated_not_spammed(fake, repo):
    add(repo, receipt(("verified",)))
    assert run("--comment")[0] == 0
    assert len(Fake.comments) == 1 and "merge gate passed" in Fake.comments[0]["body"]
    assert run("--comment")[0] == 0                               # same content: no write at all
    assert [c for c in Fake.calls if c[0] in ("POST", "PATCH")] == [("POST", "/repos/o/r/issues/7/comments")]
    add(repo, receipt(("contradicted",), text="new"))
    assert run("--comment")[0] == 1
    assert len(Fake.comments) == 1 and "merge gate **failed**" in Fake.comments[0]["body"]
    assert ("PATCH", "/repos/o/r/issues/comments/1") in Fake.calls


def test_github_down_fails_open_by_default_and_closed_on_request(fake, repo, capsys):
    add(repo, receipt(("verified",)))
    Fake.mode["down"] = True
    code, md = run("--comment", "--status")
    assert code == 0 and "failing open" in capsys.readouterr().out
    assert "GitHub answered 502" in md
    assert run("--comment", "--on-outage", "closed")[0] == 3
    # an outage never turns a failing verdict into a pass, or the other way round
    add(repo, receipt(("contradicted",), text="new"))
    assert run("--comment")[0] == 1
    assert run("--comment", "--on-outage", "closed")[0] == 1


def test_fork_pr_read_only_token_is_a_notice_not_a_failure(fake, repo, capsys):
    add(repo, receipt(("verified",)))
    Fake.mode["fork"] = True
    assert run("--comment")[0] == 0
    assert "comment not posted" in capsys.readouterr().out


def test_status_is_set(fake, repo):
    add(repo, receipt(("contradicted",)))
    assert run("--status")[0] == 1
    assert Fake.mode["status"]["state"] == "failure" and Fake.mode["status"]["context"] == "claimcheck"


def test_repo_key_adds_links_and_witness(fake, repo, monkeypatch):
    add(repo, receipt(("verified",)))
    monkeypatch.setenv("CLAIMCHECK_API_KEY", "cck_repo_test")
    code, md = run()
    assert code == 0 and "[open](https://claimcheck.cc/r/tok_" in md and "witnessed" in md
    Fake.mode["witness"] = "none"
    code, md = run("--require-witness")
    assert code == 1 and "not witnessed live" in md
    Fake.mode["witness"] = "rewritten"
    assert run()[0] == 1                                          # a rewritten log fails even without the rule


def test_claimcheck_down_with_repo_key_keeps_the_offline_verdict(fake, repo, monkeypatch):
    add(repo, receipt(("verified",)))
    monkeypatch.setenv("CLAIMCHECK_API_KEY", "cck_repo_test")
    Fake.mode["post_down"] = True
    code, md = run()
    assert code == 0 and "claimcheck.cc" in md and "no link" in md
    assert run("--on-outage", "closed")[0] == 3
    assert run("--require-witness")[0] == 0                       # witness unknown: the outage policy decides
    assert run("--require-witness", "--on-outage", "closed")[0] == 3


def test_from_github_reads_the_pr_without_a_checkout(fake, repo, monkeypatch):
    good, bad = receipt(("verified",)), receipt(("contradicted",), text="bad")
    Fake.pr = {"info": {"base": {"sha": "b" * 40}, "head": {"sha": "h" * 40, "repo": {"full_name": "fork/r"}}},
               "files": [{"filename": f".claimcheck/receipts/{good['id']}.json", "status": "added"},
                         {"filename": "src/x.py", "status": "modified"}],
               "commits": [{"commit": {"message": "x\n\nCo-Authored-By: Claude <noreply@anthropic.com>"}}],
               "blobs": {(f".claimcheck/receipts/{good['id']}.json", "h" * 40): json.dumps(good).encode()}}
    out = Path(tempfile.mkdtemp()) / "c.md"
    from claimcheck.cli import main
    with pytest.raises(SystemExit) as ex:
        main(["ci", "check", "--from-github", "--pr", "7", "--comment", "--require-receipts", "ai", "--markdown", str(out)])
    assert ex.value.code == 0 and len(Fake.comments) == 1 and good["id"] in Fake.comments[0]["body"]
    Fake.pr["blobs"][(f".claimcheck/receipts/{good['id']}.json", "h" * 40)] = json.dumps(bad).encode()
    with pytest.raises(SystemExit) as ex:
        main(["ci", "check", "--from-github", "--pr", "7", "--comment"])
    assert ex.value.code == 1


def test_share_links_from_other_hosts_are_never_fetched(fake):
    outages = []
    assert ci.fetch_shared("https://evil.example/r/abcdefghijkl", None, outages) is None
    assert ci.fetch_shared("http://169.254.169.254/r/abcdefghijkl", None, outages) is None
    assert not [c for c in Fake.calls if c[0] == "GET"] and not outages


def test_attach_writes_a_signed_receipt_at_the_chosen_privacy(repo, monkeypatch, tmp_path):
    from claimcheck import sign as signmod
    monkeypatch.setattr(signmod, "KEY_FILE", KEYS / "a.key")
    monkeypatch.setattr(signmod, "load_key", lambda p=KEYS / "a.key": KEY_A)
    full = receipt(("verified",))
    full["privacy"] = "full"; full.pop("signature"); full["id"] = content_id(full); sign(full, KEY_A)
    f = tmp_path / "r.json"; f.write_text(json.dumps(full))
    from claimcheck.cli import main
    with pytest.raises(SystemExit) as ex:
        main(["ci", "attach", str(f), "--privacy", "hashes"])
    assert ex.value.code == 0
    (p,) = (repo / ".claimcheck" / "receipts").glob("*.json")
    doc = json.loads(p.read_text())
    assert doc["privacy"] == "hashes" and "text" not in doc["report"]
    git(repo, "add", "."); git(repo, "commit", "-qm", "receipt")
    assert run()[0] == 0


def test_setup_problems_exit_2_not_1(repo):
    from claimcheck.cli import main
    with pytest.raises(SystemExit) as ex:
        main(["ci", "check", "--base", "no-such-branch"])
    assert ex.value.code == 2
