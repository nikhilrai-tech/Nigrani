"""OpenID Connect sign-in (authorization code flow with PKCE) without any extra dependency.

The ID token is received straight from the provider's token endpoint over a connection this server opened, so,
as OpenID Connect Core 3.1.3.7 step 6 allows, the TLS connection authenticates the issuer and the token's signature is
not re-checked; the claims (issuer, audience, expiry, nonce, verified e-mail) are.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import OidcCfg


class OidcError(Exception):
    """A problem that is safe to show to the person signing in."""


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_login() -> tuple[str, str, str]:
    """-> (state, nonce, pkce verifier)"""
    return secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)


def decode_claims(id_token: str) -> dict[str, Any]:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as e:
        raise OidcError("the provider returned an unreadable ID token") from e


def validate_claims(claims: dict[str, Any], *, issuer: str, client_id: str, nonce: str, now: float | None = None) -> None:
    now = now or time.time()
    if str(claims.get("iss", "")).rstrip("/") != issuer.rstrip("/"):
        raise OidcError("the ID token was issued by a different provider")
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if client_id not in audiences:
        raise OidcError("the ID token is for a different application")
    if len(audiences) > 1 and claims.get("azp") != client_id:
        raise OidcError("the ID token is for a different application")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - 60:
        raise OidcError("the ID token has expired")
    if claims.get("nonce") != nonce:
        raise OidcError("the sign-in response does not match this sign-in attempt")
    if not claims.get("email"):
        raise OidcError("the provider did not share an e-mail address")
    if str(claims.get("email_verified")).lower() != "true":
        raise OidcError("the e-mail address is not verified at the provider")


class Oidc:
    def __init__(self, cfg: OidcCfg, redirect_uri: str, client: httpx.AsyncClient | None = None):
        self.cfg, self.redirect_uri = cfg, redirect_uri
        self.client = client or httpx.AsyncClient(timeout=10)
        self._doc: tuple[float, dict[str, Any]] | None = None

    async def discovery(self) -> dict[str, Any]:
        if self._doc and time.time() - self._doc[0] < 3600:
            return self._doc[1]
        try:
            r = await self.client.get(self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration")
            r.raise_for_status()
            doc = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OidcError("the sign-in provider could not be reached") from e
        if str(doc.get("issuer", "")).rstrip("/") != self.cfg.issuer.rstrip("/"):
            raise OidcError("the provider reports a different issuer than the one configured")
        self._doc = (time.time(), doc)
        return doc

    async def authorize_url(self, state: str, nonce: str, verifier: str) -> str:
        doc = await self.discovery()
        return doc["authorization_endpoint"] + "?" + urlencode({
            "response_type": "code", "client_id": self.cfg.client_id, "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.cfg.scopes), "state": state, "nonce": nonce,
            "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
        })

    async def finish(self, code: str, verifier: str, nonce: str) -> dict[str, Any]:
        doc = await self.discovery()
        try:
            r = await self.client.post(doc["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                "client_id": self.cfg.client_id, "client_secret": self.cfg.client_secret, "code_verifier": verifier})
            r.raise_for_status()
            token = r.json()["id_token"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise OidcError("the provider rejected the sign-in") from e
        claims = decode_claims(token)
        validate_claims(claims, issuer=self.cfg.issuer, client_id=self.cfg.client_id, nonce=nonce)
        return claims

    def allowed(self, email: str) -> bool:
        e = email.lower()
        c = self.cfg
        domain = e.rsplit("@", 1)[-1]
        return (domain in [d.lower() for d in c.allowed_domains] or e in [x.lower() for x in c.allowed_emails]
                or e in [x.lower() for x in c.admin_emails] or e in [x.lower() for x in c.editor_emails])

    def role_for_new_user(self, email: str) -> str:
        e = email.lower()
        if e in [x.lower() for x in self.cfg.admin_emails]:
            return "admin"
        if e in [x.lower() for x in self.cfg.editor_emails]:
            return "editor"
        return self.cfg.default_role
