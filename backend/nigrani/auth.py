"""Signed-cookie sessions. One admin account from config/env; no external deps."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

COOKIE = "nigrani_session"


class Auth:
    def __init__(self, username: str, password: str, secret: str, session_hours: int, enabled: bool = True):
        self.username, self.password = username, password
        self.secret = (secret or secrets.token_hex(32)).encode()
        self.ttl = session_hours * 3600
        self.enabled = enabled
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _sign(self, payload: bytes) -> str:
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    def make_token(self, user: str) -> str:
        payload = base64.urlsafe_b64encode(json.dumps({"u": user, "exp": int(time.time()) + self.ttl}).encode())
        return payload.decode() + "." + self._sign(payload)

    def verify_token(self, token: str | None) -> str | None:
        if not token or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(payload.encode())):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        except ValueError:
            return None
        return data["u"] if data.get("exp", 0) > time.time() else None

    def throttled(self, ip: str) -> bool:
        q = self._fails[ip]
        while q and q[0] < time.time() - 300:
            q.popleft()
        return len(q) >= 5

    def check_login(self, ip: str, username: str, password: str) -> bool:
        ok = hmac.compare_digest(username, self.username) & hmac.compare_digest(password, self.password)
        if not ok:
            self._fails[ip].append(time.time())
        return bool(ok)

    def user_from(self, request: Request) -> str | None:
        if not self.enabled:
            return "anonymous"
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else request.cookies.get(COOKIE)
        return self.verify_token(token)


def make_dependency(auth: Auth):
    def require(request: Request) -> str:
        user = auth.user_from(request)
        if not user:
            raise HTTPException(status_code=401, detail="not authenticated")
        # CSRF defence for cookie auth: state-changing calls must carry a custom header
        if request.method not in ("GET", "HEAD", "OPTIONS") and auth.enabled:
            if request.headers.get("x-requested-with") != "nigrani" and not request.headers.get("authorization"):
                raise HTTPException(status_code=403, detail="missing X-Requested-With header")
        return user

    return require
