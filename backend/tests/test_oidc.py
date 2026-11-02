import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.config import ConfigError, load_config
from nigrani.oidc import OidcError, b64url, validate_claims

ISSUER = "https://idp.example.com"
CLIENT = "nigrani-client"


def jwt(claims):
    enc = lambda d: b64url(json.dumps(d).encode())  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class Provider:
    """A tiny OpenID provider: discovery plus a token endpoint that mints the ID token for the nonce it was given."""

    def __init__(self):
        self.claims = {"email": "dev@example.com", "email_verified": True, "name": "Dev Eloper"}
        self.override: dict = {}
        self.token_requests: list[dict] = []
        self.fail_token = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
                                             "token_endpoint": f"{ISSUER}/token"})
        if req.url.path == "/token":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.token_requests.append(form)
            if self.fail_token:
                return httpx.Response(400, json={"error": "invalid_grant"})
            claims = {"iss": ISSUER, "aud": CLIENT, "exp": time.time() + 600, "nonce": self.nonce, **self.claims, **self.override}
            return httpx.Response(200, json={"id_token": jwt(claims), "access_token": "x"})
        return httpx.Response(404)


YAML = """
auth:
  password: root-password-123
  oidc:
    issuer: https://idp.example.com
    client_id: nigrani-client
    client_secret: s3cret
    label: Sign in with Google
    allowed_domains: [example.com]
    admin_emails: [boss@example.com]
    editor_emails: [lead@example.com]
app: {public_url: "https://nigrani.example.com"}
"""


def make(tmp_path, text=YAML):
    f = tmp_path / "n.yaml"
    f.write_text(text)
    cfg = load_config(str(f))
    cfg.data_dir = str(tmp_path)
    app = create_app(cfg, prom=FakeProm())
    provider = Provider()
    app.state.oidc.client = httpx.AsyncClient(transport=httpx.MockTransport(provider.handler))
    return app, provider


def sign_in(app, provider, **claims):
    """Walk the browser through login -> provider -> callback. Returns (client, callback response)."""
    c = TestClient(app, follow_redirects=False)
    login = c.get("/api/auth/oidc/login")
    assert login.status_code == 302
    q = parse_qs(urlparse(login.headers["location"]).query)
    provider.nonce = q["nonce"][0]
    provider.override = claims
    cb = c.get("/api/auth/oidc/callback", params={"code": "abc", "state": q["state"][0]})
    return c, cb, q


def test_config_needs_a_public_url_and_someone_allowed(tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text(YAML.replace('app: {public_url: "https://nigrani.example.com"}\n', ""))
    with pytest.raises(ConfigError, match="public_url"):
        load_config(str(f))
    f.write_text(YAML.replace("    allowed_domains: [example.com]\n    admin_emails: [boss@example.com]\n    editor_emails: [lead@example.com]\n", ""))
    with pytest.raises(ConfigError, match="allowed_domains"):
        load_config(str(f))


def test_login_redirects_with_pkce_state_and_nonce(tmp_path):
    app, provider = make(tmp_path)
    c = TestClient(app, follow_redirects=False)
    assert c.get("/api/auth/config").json() == {"oidc": {"label": "Sign in with Google", "login_url": "/api/auth/oidc/login"}}
    r = c.get("/api/auth/oidc/login")
    url = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/authorize"
    assert q["client_id"] == CLIENT and q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://nigrani.example.com/api/auth/oidc/callback" and "openid" in q["scope"]
    assert q["state"] and q["nonce"] and "nigrani_oidc" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]


def test_successful_sign_in_creates_a_user_with_the_default_role(tmp_path):
    app, provider = make(tmp_path)
    c, cb, q = sign_in(app, provider)
    assert cb.status_code == 302 and cb.headers["location"] == "/"
    me = c.get("/api/me", follow_redirects=True).json()
    assert (me["user"], me["name"], me["role"], me["provider"]) == ("dev@example.com", "Dev Eloper", "viewer", "oidc")
    # the code verifier that was sent hashes to the challenge from the authorize URL (PKCE), with the client secret
    sent = provider.token_requests[0]
    assert b64url(hashlib.sha256(sent["code_verifier"].encode()).digest()) == q["code_challenge"][0]
    assert sent["client_secret"] == "s3cret" and sent["grant_type"] == "authorization_code" and sent["code"] == "abc"
    users = [u for u in app.state.store.list_users()]
    assert users[0]["provider"] == "oidc" and users[0]["password_hash"] is None
    assert any(a["action"] == "login.sso" for a in app.state.store.list_audit())


def test_roles_from_the_configured_lists_and_database_roles_win_afterwards(tmp_path):
    app, provider = make(tmp_path)
    assert sign_in(app, provider, email="boss@example.com")[0].get("/api/me").json()["role"] == "admin"
    assert sign_in(app, provider, email="lead@example.com")[0].get("/api/me").json()["role"] == "editor"
    sign_in(app, provider)                                                         # dev, first time: viewer
    dev = app.state.store.get_user_by_name("dev@example.com")
    app.state.store.update_user(dev["id"], role="editor")                           # an admin promoted them
    assert sign_in(app, provider)[0].get("/api/me").json()["role"] == "editor"      # next sign-in keeps it


def test_only_allowed_accounts_get_in(tmp_path):
    app, provider = make(tmp_path)
    c, cb, _ = sign_in(app, provider, email="stranger@gmail.com")
    assert cb.headers["location"].startswith("/?sso_error=") and "not+allowed" in cb.headers["location"]
    assert c.get("/api/me").status_code == 401 and app.state.store.list_users() == []
    # allowed by domain, case-insensitively
    assert sign_in(app, provider, email="Someone@Example.COM")[1].headers["location"] == "/"


@pytest.mark.parametrize("override,why", [
    ({"email_verified": False}, "not verified"), ({"email_verified": "false"}, "not verified"),
    ({"aud": "someone-else"}, "different application"), ({"iss": "https://evil.example.com"}, "different provider"),
    ({"exp": time.time() - 3600}, "expired"), ({"nonce": "forged"}, "does not match"), ({"email": ""}, "e-mail"),
    ({"aud": [CLIENT, "other"], "azp": "other"}, "different application"),
])
def test_bad_id_tokens_are_refused(tmp_path, override, why):
    app, provider = make(tmp_path)
    c, cb, _ = sign_in(app, provider, **override)
    assert cb.headers["location"].startswith("/?sso_error=")
    assert why.replace(" ", "+") in cb.headers["location"] or why in cb.headers["location"].replace("+", " ")
    assert c.get("/api/me").status_code == 401 and app.state.store.list_users() == []


def test_flow_failures(tmp_path):
    app, provider = make(tmp_path)
    c = TestClient(app, follow_redirects=False)
    # the callback without having started here (no cookie), and with a wrong state
    assert "/?sso_error=" in c.get("/api/auth/oidc/callback", params={"code": "x", "state": "y"}).headers["location"]
    q = parse_qs(urlparse(c.get("/api/auth/oidc/login").headers["location"]).query)
    assert "expired" in c.get("/api/auth/oidc/callback", params={"code": "x", "state": "WRONG"}).headers["location"]
    assert "refused" in c.get("/api/auth/oidc/callback", params={"error": "access_denied"}).headers["location"]
    provider.fail_token = True
    q = parse_qs(urlparse(c.get("/api/auth/oidc/login").headers["location"]).query)   # a failed attempt clears the cookie: start again
    provider.nonce = q["nonce"][0]
    assert "rejected" in c.get("/api/auth/oidc/callback", params={"code": "x", "state": q["state"][0]}).headers["location"]
    assert c.get("/api/me").status_code == 401


def test_disabled_and_password_accounts_are_not_taken_over(tmp_path):
    app, provider = make(tmp_path)
    sign_in(app, provider)
    dev = app.state.store.get_user_by_name("dev@example.com")
    app.state.store.update_user(dev["id"], disabled=True)
    assert "disabled" in sign_in(app, provider)[1].headers["location"]
    app.state.store.create_user(username="lead@example.com", role="admin", password_hash="scrypt$x", provider="local")
    assert "already+exists" in sign_in(app, provider, email="lead@example.com")[1].headers["location"]


def test_sso_session_follows_database_changes(tmp_path):
    app, provider = make(tmp_path)
    c, _, _ = sign_in(app, provider)
    dev = app.state.store.get_user_by_name("dev@example.com")
    app.state.store.update_user(dev["id"], disabled=True)
    assert c.get("/api/me").status_code == 401


def test_without_oidc_nothing_is_exposed(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "root-password-123"
    c = TestClient(create_app(cfg, prom=FakeProm()), follow_redirects=False)
    assert c.get("/api/auth/config").json() == {"oidc": None}
    assert c.get("/api/auth/oidc/login").status_code == 404 and c.get("/api/auth/oidc/callback").status_code == 404


def test_validate_claims_accepts_a_good_token():
    good = {"iss": ISSUER + "/", "aud": [CLIENT], "exp": time.time() + 60, "nonce": "n", "email": "a@b.c", "email_verified": True}
    validate_claims(good, issuer=ISSUER, client_id=CLIENT, nonce="n")        # trailing slash tolerated, aud may be a list
    with pytest.raises(OidcError):
        validate_claims({**good, "nonce": "other"}, issuer=ISSUER, client_id=CLIENT, nonce="n")
