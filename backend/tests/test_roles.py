import sqlite3

from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.auth import Identity, required_role
from nigrani.config import load_config
from nigrani.passwords import hash_password, verify_password

H = {"X-Requested-With": "nigrani"}
PW = "a-long-enough-password"


def make_app(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "root-password-123"
    return create_app(cfg, prom=FakeProm())


class Session:
    """A browser: its own cookie jar."""

    def __init__(self, app, username, password):
        self.c = TestClient(app)
        r = self.c.post("/api/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        self.role = r.json()["role"]

    def get(self, path, **kw): return self.c.get(path, **kw)
    def post(self, path, json=None, **kw): return self.c.post(path, json=json if json is not None else {}, headers=H, **kw)
    def put(self, path, json, **kw): return self.c.put(path, json=json, headers=H, **kw)
    def delete(self, path): return self.c.delete(path, headers=H)


def team(tmp_path):
    app = make_app(tmp_path)
    admin = Session(app, "admin", "root-password-123")
    for name, role in (("vera", "viewer"), ("eddie", "editor"), ("alice", "admin")):
        assert admin.post("/api/users", {"username": name, "role": role, "password": PW, "name": name.title()}).status_code == 200
    return app, admin, Session(app, "vera", PW), Session(app, "eddie", PW), Session(app, "alice", PW)


def test_required_role_table():
    assert required_role("GET", "/api/overview") == "viewer"
    assert required_role("POST", "/api/rules/test") == "viewer" and required_role("POST", "/api/channels/preview") == "viewer"
    assert required_role("POST", "/api/rules") == "editor" and required_role("DELETE", "/api/silences/3") == "editor"
    assert required_role("POST", "/api/incidents/1/ack") == "editor"
    assert required_role("GET", "/api/backup") == "editor" and required_role("GET", "/api/backup", {"include_secrets": "true"}) == "admin"
    assert required_role("POST", "/api/backup/import") == "admin"
    for p in ("/api/users", "/api/users/3", "/api/audit"):
        assert required_role("GET", p) == "admin"
    assert required_role("POST", "/api/some-future-route") == "editor"          # forgotten routes fail closed


def test_scrypt_passwords():
    h = hash_password(PW)
    assert h.startswith("scrypt$") and PW not in h and h != hash_password(PW)    # salted
    assert verify_password(PW, h) and not verify_password("wrong", h) and not verify_password(PW, "garbage")


def test_what_each_role_can_do(tmp_path):
    app, admin, viewer, editor, admin2 = team(tmp_path)
    rule = {"name": "R", "expr": "vector(1)", "crit": 5, "for_seconds": 0}

    # viewer: read everything, change nothing
    assert viewer.get("/api/overview").status_code == 200 and viewer.get("/api/rules").status_code == 200
    assert viewer.post("/api/rules/test", rule).status_code == 200                 # previews are read-only
    for r in (viewer.post("/api/rules", rule), viewer.post("/api/silences", {"minutes": 5}), viewer.post("/api/incidents/1/ack"),
              viewer.post("/api/digest/send"), viewer.get("/api/backup"), viewer.get("/api/users"), viewer.get("/api/audit")):
        assert r.status_code == 403, r.request.url
    assert "viewer" in viewer.post("/api/rules", rule).json()["detail"]

    # editor: alerts, channels, silences, plain backup export
    made = editor.post("/api/rules", rule)
    assert made.status_code == 200 and editor.put(f"/api/rules/{made.json()['id']}", {**rule, "crit": 9}).status_code == 200
    assert editor.post("/api/silences", {"minutes": 5}).status_code == 200
    assert editor.get("/api/backup").status_code == 200
    for r in (editor.get("/api/backup?include_secrets=true"), editor.post("/api/backup/import?dry_run=false", {"nigrani_backup": 1}),
              editor.get("/api/users"), editor.post("/api/users", {"username": "x" * 5, "password": PW}), editor.get("/api/audit")):
        assert r.status_code == 403

    # admin: everything
    assert admin2.get("/api/users").status_code == 200 and admin2.get("/api/audit").status_code == 200
    assert admin2.get("/api/backup?include_secrets=true").status_code == 200
    assert admin2.post("/api/backup/import", {"nigrani_backup": 1}).status_code == 200


def test_role_changes_and_disabling_apply_to_open_sessions_at_once(tmp_path):
    app, admin, viewer, editor, _ = team(tmp_path)
    users = {u["username"]: u for u in admin.get("/api/users").json()}
    assert viewer.post("/api/silences", {"minutes": 5}).status_code == 403
    admin.put(f"/api/users/{users['vera']['id']}", {"role": "editor"})
    assert viewer.post("/api/silences", {"minutes": 5}).status_code == 200            # no new login needed
    admin.put(f"/api/users/{users['eddie']['id']}", {"disabled": True})
    assert editor.get("/api/overview").status_code == 401                              # session is dead
    assert TestClient(app).post("/api/login", json={"username": "eddie", "password": PW}).status_code == 401


def test_the_configured_admin_is_always_there_and_protected(tmp_path):
    app, admin, *_ = team(tmp_path)
    users = admin.get("/api/users").json()
    assert users[0]["provider"] == "config" and users[0]["role"] == "admin" and users[0]["id"] is None
    assert admin.post("/api/users", {"username": "ADMIN", "password": PW}).status_code == 409       # name taken
    assert admin.post("/api/users", {"username": "vera", "password": PW}).status_code == 409        # duplicate (any case)
    assert admin.post("/api/users", {"username": "VERA", "password": PW}).status_code == 409


def test_user_validation_and_lockout_protection(tmp_path):
    app, admin, viewer, editor, alice = team(tmp_path)
    assert admin.post("/api/users", {"username": "bob", "password": "short"}).status_code == 422
    assert admin.post("/api/users", {"username": "b o b", "password": PW}).status_code == 422
    assert admin.post("/api/users", {"username": "bob", "role": "root", "password": PW}).status_code == 422
    users = {u["username"]: u for u in admin.get("/api/users").json()}
    # an administrator can't lock themselves out or delete themselves
    assert alice.put(f"/api/users/{users['alice']['id']}", {"role": "viewer"}).status_code == 409
    assert alice.put(f"/api/users/{users['alice']['id']}", {"disabled": True}).status_code == 409
    assert alice.delete(f"/api/users/{users['alice']['id']}").status_code == 409
    # ...but can manage others, and a password reset takes effect
    assert alice.put(f"/api/users/{users['vera']['id']}", {"password": "another-long-password"}).status_code == 200
    assert TestClient(app).post("/api/login", json={"username": "vera", "password": PW}).status_code == 401
    assert TestClient(app).post("/api/login", json={"username": "vera", "password": "another-long-password"}).status_code == 200
    assert alice.put(f"/api/users/{users['vera']['id']}", {"password": "short"}).status_code == 422
    assert alice.delete(f"/api/users/{users['vera']['id']}").status_code == 200
    assert alice.delete("/api/users/9999").status_code == 404


def test_passwords_are_stored_hashed_and_never_returned(tmp_path):
    app, admin, *_ = team(tmp_path)
    listed = str(admin.get("/api/users").json())
    assert "scrypt" not in listed and "password_hash" not in listed
    row = sqlite3.connect(str(tmp_path / "nigrani.db")).execute("select password_hash from users where username='vera'").fetchone()[0]
    assert row.startswith("scrypt$") and PW not in row


def test_failed_logins_are_throttled_and_audited(tmp_path):
    app = make_app(tmp_path)
    c = TestClient(app)
    for _ in range(5):
        assert c.post("/api/login", json={"username": "nobody", "password": "x"}).status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "root-password-123"}).status_code == 429
    audit = app.state.store.list_audit()
    assert [a["action"] for a in audit].count("login.failed") == 5


def test_audit_log_records_who_did_what(tmp_path):
    app, admin, viewer, editor, alice = team(tmp_path)
    editor.post("/api/rules", {"name": "Disk rule", "expr": "vector(1)", "crit": 5, "for_seconds": 0})
    editor.post("/api/silences", {"minutes": 30, "match": "web"})
    viewer.post("/api/rules", {"name": "nope", "expr": "vector(1)", "crit": 5})           # denied
    alice.get("/api/backup?include_secrets=true")
    alice.post("/api/backup/import", {"nigrani_backup": 1})                                 # dry run: not logged
    alice.post("/api/rules/test", {"name": "p", "expr": "vector(1)", "crit": 1})            # preview: not logged
    log = admin.get("/api/audit").json()
    seen = {(a["user"], a["action"]): a for a in log}
    assert seen[("eddie", "rule.create")]["detail"] == "Disk rule" and seen[("eddie", "rule.create")]["status"] == 200
    assert ("eddie", "silence.create") in seen and "web" in seen[("eddie", "silence.create")]["detail"]
    assert ("vera", "denied: rule.create") in seen and seen[("vera", "denied: rule.create")]["status"] == 403
    assert seen[("alice", "backup.export")]["detail"] == "including credentials"
    assert ("alice", "backup.import") not in seen and not any(a["action"] == "rule.test" for a in log)
    assert ("admin", "login") in seen or any(a["action"] == "login" for a in log)
    assert ("admin", "user.create") in seen and "as editor" in str([a["detail"] for a in log if a["action"] == "user.create"])
    mine = admin.get("/api/audit?user=eddie").json()
    assert {a["user"] for a in mine} == {"eddie"} and {"login", "rule.create", "silence.create"} <= {a["action"] for a in mine}


def test_acknowledging_credits_the_display_name(tmp_path):
    app, admin, viewer, editor, _ = team(tmp_path)
    iid = app.state.store.create_incident(source_id="r", series_key="a", rule_name="X", severity="critical", started_at=1e10)
    assert editor.post(f"/api/incidents/{iid}/ack", {"hours": 1}).json()["acked_by"] == "Eddie"
    assert admin.get("/api/me").json()["can"] == {"edit": True, "admin": True}
    me = viewer.get("/api/me").json()
    assert (me["role"], me["can"], me["provider"]) == ("viewer", {"edit": False, "admin": False}, "local")


def test_sessions_from_before_roles_were_the_admin(tmp_path):
    app = make_app(tmp_path)
    old = app.state.auth.sign_blob({"u": "admin"}, 3600)                              # no provider field, as before
    c = TestClient(app)
    c.cookies.set("nigrani_session", old)
    assert c.get("/api/me").json()["role"] == "admin"


def test_auth_disabled_means_everyone_is_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGRANI_ALLOW_NO_AUTH", "1")
    cfg = load_config(None)
    cfg.data_dir = str(tmp_path)
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        assert c.get("/api/users").status_code == 200 and c.get("/api/me").json()["role"] == "admin"


def test_identity_can():
    assert Identity("a", "a", "editor", "local").can("viewer") and not Identity("a", "a", "editor", "local").can("admin")
