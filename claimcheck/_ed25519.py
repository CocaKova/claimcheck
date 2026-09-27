"""Pure-Python Ed25519 (RFC 8032), used only when neither `cryptography` nor PyNaCl is importable.

Slow (~50 ms per operation on a laptop, more on a phone) but dependency-free, so a hook installed by a
non-technical user signs receipts with nothing but python3. Not constant-time: keys made here are
per-machine receipt keys, not secrets protecting money.
"""
from __future__ import annotations

import hashlib
import os

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _I % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = 2 * t1 * t2 * _D % _P
    d = 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _compress(P) -> bytes:
    zi = _inv(P[2])
    x, y = P[0] * zi % _P, P[1] * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _equal(P, Q) -> bool:
    return (P[0] * Q[2] - Q[0] * P[2]) % _P == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _P == 0


def _h(*parts: bytes) -> int:
    return int.from_bytes(hashlib.sha512(b"".join(parts)).digest(), "little")


def _secret_expand(sk: bytes):
    h = hashlib.sha512(sk).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def generate() -> bytes:
    return os.urandom(32)


def public_key(sk: bytes) -> bytes:
    a, _ = _secret_expand(sk)
    return _compress(_mul(a, _G))


def sign(sk: bytes, msg: bytes) -> bytes:
    a, prefix = _secret_expand(sk)
    A = _compress(_mul(a, _G))
    r = _h(prefix, msg) % _L
    R = _compress(_mul(r, _G))
    k = _h(R, A, msg) % _L
    s = (r + k * a) % _L
    return R + int.to_bytes(s, 32, "little")


def verify(pk: bytes, msg: bytes, sig: bytes) -> bool:
    if len(sig) != 64:
        return False
    A = _decompress(pk)
    R = _decompress(sig[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _L:
        return False
    k = _h(sig[:32], pk, msg) % _L
    return _equal(_mul(s, _G), _add(R, _mul(k, A)))
