"""Redaction: every common secret shape is gone; the literals claims are checked against are untouched."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from claimcheck.ledger import redact  # noqa: E402

# (text, the secret that must not survive). Fake values, shaped like the real thing; each prefix is split across two
# adjacent literals ("sk_" "live_…" is one string) so secret scanners don't flag this file.
LEAKS = [
    ("export STRIPE_KEY=sk" "_live_51Habcdefghijklmnopqrstuv", "sk" "_live_51Habcdefghijklmnopqrstuv"),
    ("sk" "_live_51Habcdefghijklmnopqrstuv", "sk" "_live_51Habcdefghijklmnopqrstuv"),
    ("rk" "_test_51Habcdefghijklmnopqrstuv", "rk" "_test_51Habcdefghijklmnopqrstuv"),
    ("wh" "sec_abcdefghijklmnopqrstuvwxyz12", "wh" "sec_abcdefghijklmnopqrstuvwxyz12"),
    ("sk" "-ant-api03-abcdefghijklmnopqrstuvwxyz", "sk" "-ant-api03-abcdefghijklmnopqrstuvwxyz"),
    ("export OPENAI_API_KEY=sk" "-proj-abcdefghijklmnop", "sk" "-proj-abcdefghijklmnop"),
    ("AI" "zaSyA1234567890abcdefghijklmnopqrstu", "AI" "zaSyA1234567890abcdefghijklmnopqrstu"),
    ("hf" "_abcdefghijklmnopqrstuvwxyzABCDEF", "hf" "_abcdefghijklmnopqrstuvwxyzABCDEF"),
    ("gl" "pat-abcdefghijklmnopqrst", "gl" "pat-abcdefghijklmnopqrst"),
    ("np" "m_abcdefghijklmnopqrstuvwxyz0123456789", "np" "m_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("gh" "p_abcdefghijklmnopqrstuvwxyz0123", "gh" "p_abcdefghijklmnopqrstuvwxyz0123"),
    ("gi" "thub_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz", "gi" "thub_pat_11ABCDEFG0123456789"),
    ("xo" "xb-1234567890-abcdefghij", "xo" "xb-1234567890-abcdefghij"),
    ("tailscale up --authkey ts" "key-auth-kAbCdEf1234-ZyXwVu987654", "ts" "key-auth-kAbCdEf1234-ZyXwVu987654"),
    ("curl -H 'Authorization: Bearer tk" "_abcdefghijklmnopqrstuvwxyz1' ntfy.sh/x", "tk" "_abcdefghijklmnopqrstuvwxyz1"),
    ("TELEGRAM=123456789:A" "Aabcdefghijklmnopqrstuvwxyz0123456", "123456789:A" "Aabcdefghijklmnopqrstuvwxyz0123456"),
    ("aws AK" "IAABCDEFGHIJKLMNOP", "AK" "IAABCDEFGHIJKLMNOP"),
    ("postgres://admin:Hunter2SuperSecret@db.internal:5432/prod", "Hunter2SuperSecret"),
    ("DATABASE_URL=postgres://admin:Hunter2SuperSecret@db/prod", "Hunter2SuperSecret"),
    ("redis://:s3cretpass@cache:6379", "s3cretpass"),
    ("curl -H 'Authorization: Bearer abcdefghijklmnop1234'", "abcdefghijklmnop1234"),
    ("curl -u x -H 'Authorization: Basic dXNlcjpwYXNzd29yZA=='", "dXNlcjpwYXNzd29yZA=="),
    ("curl -H 'X-Api-Key: abcdefghijklmnop1234'", "abcdefghijklmnop1234"),
    ("MY_PASS=correcthorsebattery", "correcthorsebattery"),
    ("SENTRY_DSN=https://abc123def456@o1.ingest.sentry.io/1", "abc123def456@o1"),
    ("export BW_SESSION=Zm9vYmFyYmF6cXV4", "Zm9vYmFyYmF6cXV4"),
    ('{"password": "hunter22hunter"}', "hunter22hunter"),
    ('client_secret: "abcdefghijklmnop"', "abcdefghijklmnop"),
    ("mysql --password hunter22hunter", "hunter22hunter"),
    ("cloudflared tunnel run --token abcdef1234567890", "abcdef1234567890"),
    ("sshpass -p hunter22hunter ssh x", "hunter22hunter"),
    ("mysql -u root -phunter22hunter db", "hunter22hunter"),
    ("https://ho" "oks.slack.com/services/T000/B000/abcdefghijklmnop", "abcdefghijklmnop"),
    ("https://di" "scord.com/api/webhooks/123456/abcdefghijklmnopqrstuv", "abcdefghijklmnopqrstuv"),
    ("ey" "JhbGciOiJIUzI1NiJ9.ey" "JzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop", "ey" "JzdWIiOiIxMjM0NTY3ODkwIn0"),
    ("-----BEGIN OP" "ENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OP" "ENSSH PRIVATE KEY-----", "b3BlbnNzaC1rZXktdjEAAAA"),
]

# literals a claim is checked against: redacting these would turn true claims into "unverified"
KEEPS = [
    "commit 4b94f40e6621ab24a74fe0d1e881",
    "md5 1ac73233b2f7d0c4e9a8f1b3c5d7e9f1",
    "max_workers=16",
    "sort_key=created_at",
    "docker run -p 8080:80 nginx",
    "git clone https://git@github.com/CocaKova/claimcheck",
    "https://my-box.tail1234.ts.net:9119/api",
    "Basic authentication is off",
    "max_tokens: 65536",
    "~/.ssh/id_ed25519.pub",
    "pk_live_51Habcdefghijklmnopqrstuv",  # Stripe publishable key: public by design
    "PASSWORD=$DB_PASS",
    "password: <your password>",
    "api_key: {{API_KEY}}",
    "window_width_override=1280",
    "the token budget is 4096",
    "author: CocaKova",
    '"input_tokens": 123456789',
    "--client-secret /path/to/client_secret.json",
]


@pytest.mark.parametrize("text,secret", LEAKS)
def test_secret_removed(text, secret):
    out = redact(text)
    assert secret not in out, out
    assert "[REDACTED]" in out


@pytest.mark.parametrize("text,secret", LEAKS)
def test_secret_removed_inside_dumped_tool_call(text, secret):
    """capture.py redacts json.dumps(args): quotes arrive escaped."""
    out = redact(json.dumps({"command": text}))
    assert secret not in out, out


@pytest.mark.parametrize("text", KEEPS)
def test_literal_kept(text):
    assert redact(text) == text


def test_name_kept_value_gone():
    assert redact("export STRIPE_KEY=abcdefgh1234") == "export STRIPE_KEY=[REDACTED]"
    assert redact("postgres://admin:Hunter2SuperSecret@db/prod") == "postgres://admin:[REDACTED]@db/prod"


def test_idempotent():
    for text, _ in LEAKS:
        once = redact(text)
        assert redact(once) == once
