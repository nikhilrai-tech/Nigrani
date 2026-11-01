"""Password hashing with scrypt from the standard library (no extra dependency)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

N, R, P = 2**14, 8, 1
MIN_LENGTH = 10


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return "scrypt${}${}${}${}${}".format(N, R, P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# verified against when the username does not exist, so a missing user takes as long as a wrong password
DUMMY_HASH = hash_password("not-a-real-password")


def password_problem(password: str) -> str | None:
    if len(password) < MIN_LENGTH:
        return f"password must be at least {MIN_LENGTH} characters"
    if len(password) > 200:
        return "password is too long"
    return None
