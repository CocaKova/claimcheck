"""ed25519 signatures over the canonical receipt. One key per machine, in ~/.claimcheck/key.

kid = first 16 hex of sha256(raw public key). The signature covers canon(doc minus `signature`), so
`id` is inside the signed bytes and a signed receipt cannot be re-identified.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from .document import canon, now_iso, sha256

# Backend: `cryptography` when installed, else PyNaCl, else the vendored pure-Python RFC 8032 code, so a
# hook installed with nothing but python3 still signs. All three produce the same bytes.
_FORCE = os.environ.get("CLAIMCHECK_SIGN_BACKEND")  # cryptography | pynacl | pure (tests / debugging)
try:
    if _FORCE and _FORCE != "cryptography":
        raise ImportError
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    BACKEND = "cryptography"
except ImportError:  # pragma: no cover
    try:
        if _FORCE and _FORCE != "pynacl":
            raise ImportError
        import nacl.signing
        import nacl.exceptions
        BACKEND = "pynacl"
    except ImportError:
        from . import _ed25519
        BACKEND = "pure"


class _Key:
    """32 raw secret bytes; sign/public through whichever backend loaded."""

    def __init__(self, raw: bytes):
        self.raw = raw

    def public(self) -> bytes:
        if BACKEND == "cryptography":
            return Ed25519PrivateKey.from_private_bytes(self.raw).public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if BACKEND == "pynacl":
            return bytes(nacl.signing.SigningKey(self.raw).verify_key)
        return _ed25519.public_key(self.raw)

    def sign(self, msg: bytes) -> bytes:
        if BACKEND == "cryptography":
            return Ed25519PrivateKey.from_private_bytes(self.raw).sign(msg)
        if BACKEND == "pynacl":
            return nacl.signing.SigningKey(self.raw).sign(msg).signature
        return _ed25519.sign(self.raw, msg)


def _verify_raw(pub: bytes, msg: bytes, sig: bytes) -> bool:
    try:
        if BACKEND == "cryptography":
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
            return True
        if BACKEND == "pynacl":
            nacl.signing.VerifyKey(pub).verify(msg, sig)
            return True
        return _ed25519.verify(pub, msg, sig)
    except Exception:
        return False

HOME = Path(os.environ.get("CLAIMCHECK_HOME", Path.home() / ".claimcheck"))
KEY_FILE = HOME / "key"


def keygen(path: Path = KEY_FILE, overwrite: bool = False) -> Path:
    if path.exists() and not overwrite:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(raw.hex() + "\n")
    return path


def load_key(path: Path = KEY_FILE) -> _Key:
    if not path.exists():
        keygen(path)
    return _Key(bytes.fromhex(path.read_text().strip()))


def pub_raw(key: _Key) -> bytes:
    return key.public()


def kid(pub: bytes) -> str:
    return sha256(pub)[:16]


def sign(doc: dict, key: _Key | None = None, embed_pub: bool = True) -> dict:
    key = key or load_key()
    body = {k: v for k, v in doc.items() if k != "signature"}
    signed_at = now_iso()
    pub = pub_raw(key)
    sig = key.sign(canon(body))
    doc["signature"] = {"alg": "ed25519", "kid": kid(pub), "sig": base64.b64encode(sig).decode(),
                        "signed_at": signed_at, "canon": "jcs", **({"pub": base64.b64encode(pub).decode()} if embed_pub else {})}
    return doc


def verify_signature(doc: dict, pub: bytes | None = None) -> tuple[bool, str]:
    s = doc.get("signature")
    if not s:
        return False, "unsigned"
    if pub is None:
        if "pub" not in s:
            return False, "no public key embedded and none supplied"
        pub = base64.b64decode(s["pub"])
    if kid(pub) != s.get("kid"):
        return False, "kid does not match the public key"
    body = {k: v for k, v in doc.items() if k != "signature"}
    try:
        ok = _verify_raw(pub, canon(body), base64.b64decode(s["sig"]))
    except (ValueError, TypeError):
        ok = False
    if not ok:
        return False, "signature invalid: bad signature"
    return True, f"signed by {s['kid']} at {s['signed_at']}"
