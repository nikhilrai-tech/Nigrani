import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProm, RecordingNotifier
from nigrani.api import create_app
from nigrani.config import SentryCfg, load_config
from nigrani.engine import Engine
from nigrani.prom import PromQueryError
from nigrani.sentry import Sentry, SentryError, normalize

TOKEN = "sntrys_secret"


def issue(i, project="api", level="error", count="12", users=3, first="2026-11-02T10:00:00.000000Z", last="2026-11-02T11:30:00Z"):
    return {"id": str(i), "shortId": f"{project.upper()}-{i}", "title": f"ValueError: boom {i}", "culprit": "app.views in index",
            "permalink": f"https://sentry.io/organizations/acme/issues/{i}/", "level": level, "status": "unresolved",
            "count": count, "userCount": users, "firstSeen": first, "lastSeen": last,
            "project": {"id": {"api": "11", "web": "12"}.get(project, "99"), "name": project.title(), "slug": project}}


class Server:
    """A stand-in for Sentry's API (the endpoints and answer shapes we use)."""

    def __init__(self):
        self.issues = [issue(1), issue(2, "web"), issue(3, "web")]
        self.requests: list[httpx.Request] = []
        self.status = 200

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "x"})
        if req.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "Invalid token"})
        path = req.url.path
        if path == "/api/0/organizations/acme/":
            return httpx.Response(200, json={"slug": "acme"})
        if path == "/api/0/organizations/acme/projects/":
            return httpx.Response(200, json=[{"id": "11", "slug": "api"}, {"id": "12", "slug": "web"}])
        if path == "/api/0/organizations/acme/issues/":
            params = req.url.params
            ids = params.get_list("project")
            rows = [i for i in self.issues if not ids or i["project"]["id"] in ids]
            return httpx.Response(200, json=rows[: int(params.get("limit", 25))])
        return httpx.Response(404, json={})


def make(cfg=None, server=None):
    server = server or Server()
    cfg = cfg or SentryCfg(org="acme", token=TOKEN)
    return Sentry(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler))), server


def run(c):
    return asyncio.run(c)


def test_issues_are_requested_with_the_token_and_normalized():
    s, srv = make()
    rows = run(s.issues("is:unresolved level:error", limit=5, sort="freq"))
    req = srv.requests[0]
    assert req.headers["authorization"] == f"Bearer {TOKEN}" and req.url.path == "/api/0/organizations/acme/issues/"
    assert dict(req.url.params) == {"query": "is:unresolved level:error", "limit": "5", "sort": "freq"}
    first = rows[0]
    assert (first["short_id"], first["project"], first["count"], first["users"], first["level"]) == ("API-1", "api", 12, 3, "error")
    assert first["first_seen"] == pytest.approx(1793613600.0) and first["last_seen"] > first["first_seen"]
    assert first["url"].endswith("/issues/1/")


def test_limit_is_clamped_and_sort_validated():
    s, srv = make()
    run(s.issues(limit=5000))
    assert srv.requests[0].url.params["limit"] == "100"
    with pytest.raises(PromQueryError):
        run(s.issues(sort="chaos"))


def test_projects_are_filtered_by_id():
    s, srv = make(SentryCfg(org="acme", token=TOKEN, projects=["web"]))
    rows = run(s.issues())
    assert {r["project"] for r in rows} == {"web"}
    assert [r.url.path for r in srv.requests] == ["/api/0/organizations/acme/projects/", "/api/0/organizations/acme/issues/"]
    run(s.issues())                                                                # the id lookup is cached
    assert sum(r.url.path.endswith("/projects/") for r in srv.requests) == 1
    bad, _ = make(SentryCfg(org="acme", token=TOKEN, projects=["nope"]))
    with pytest.raises(PromQueryError, match="nope"):
        run(bad.issues())


def test_instant_is_one_series_per_project_with_the_issue_count():
    s, _ = make()
    assert run(s.instant("is:unresolved")) == [{"labels": {"project": "api"}, "value": 1.0}, {"labels": {"project": "web"}, "value": 2.0}]
    s2, srv2 = make()
    srv2.issues = []
    assert run(s2.instant("is:unresolved")) == []                                  # nothing matches: no series, i.e. healthy


@pytest.mark.parametrize("status,needle", [(401, "refused the token"), (403, "refused the token"), (404, "not found"),
                                           (429, "rate limiting"), (500, "HTTP 500"), (503, "HTTP 503")])
def test_http_errors_become_readable_messages(status, needle):
    s, srv = make()
    srv.status = status
    with pytest.raises(SentryError, match=needle):
        run(s.issues())
    assert needle in s.last_error


def test_a_bad_query_is_a_query_error_not_an_outage():
    s, srv = make()
    srv.status = 400
    with pytest.raises(PromQueryError):
        run(s.issues("is:nonsense:::"))


def test_wrong_token_unreachable_and_not_json():
    s, _ = make(SentryCfg(org="acme", token="wrong"))
    with pytest.raises(SentryError, match="refused"):
        run(s.issues())
    down = Sentry(SentryCfg(org="acme", token=TOKEN), client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("no route")))))
    with pytest.raises(SentryError, match="could not be reached"):
        run(down.issues())
    html = Sentry(SentryCfg(org="acme", token=TOKEN), client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="<html>login</html>"))))
    with pytest.raises(SentryError, match="not JSON"):
        run(html.issues())
    obj = Sentry(SentryCfg(org="acme", token=TOKEN), client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"detail": "surprise"}))))
    with pytest.raises(SentryError, match="unexpected"):
        run(obj.issues())


def test_health_search_url_and_no_time_series():
    s, _ = make()
    assert run(s.healthy()) is True
    assert s.search_url("is:unresolved firstSeen:-1h") == "https://sentry.io/organizations/acme/issues/?query=is%3Aunresolved%20firstSeen%3A-1h"
    bad, _ = make(SentryCfg(org="acme", token="x"))
    assert run(bad.healthy()) is False
    with pytest.raises(PromQueryError):
        run(s.range("q", 1, 2, 3))


def test_normalize_survives_odd_data():
    row = normalize({"id": 5, "count": None, "userCount": "n/a", "firstSeen": "garbage", "project": None})
    assert (row["id"], row["count"], row["users"], row["first_seen"], row["project"]) == ("5", 0, 0, None, "")


def test_config(tmp_path):
    assert not SentryCfg().enabled and SentryCfg(org="a", token="t").enabled and SentryCfg(url="https://s.example.com/").url == "https://s.example.com"
    with pytest.raises(ValueError):
        SentryCfg(url="ftp://x")
    f = tmp_path / "n.yaml"
    f.write_text("sentry: {org: acme, token: t}\nservers:\n  - {id: a}\n  - {id: b, sentry: {org: other, token: u, url: 'https://sentry.example.com'}}\n")
    a, b = load_config(str(f)).server_views()
    assert a.sentry.org == "acme" and b.sentry.org == "other" and b.sentry.url == "https://sentry.example.com"


# ------------------------------------------------------------------ rules through the engine
def test_a_sentry_rule_fires_per_project_and_resolves_the_moment_the_issues_are_gone(cfg, store, prom):
    s, srv = make()
    store.save_rule({"id": "new", "name": "New errors", "expr": "is:unresolved firstSeen:-1h", "direction": "above", "warn": 0,
                     "crit": 1, "for_seconds": 0, "unit": "short", "datasource": "sentry"})
    n = RecordingNotifier()
    eng = Engine(store, prom, n, cfg, clock=lambda: 0.0, sentries={"default": s})
    run(eng.evaluate_rules(0.0))
    inc = {i["series"]: i for i in store.open_incidents()}
    assert set(inc) == {"api", "web"} and inc["api"]["severity"] == "warning" and inc["web"]["severity"] == "critical"
    srv.issues = []                                                                   # all fixed / aged out
    run(eng.evaluate_rules(30.0))
    assert store.open_incidents() == [] and n.kinds.count("resolved") == 2            # no 5 minute grace


def test_a_sentry_outage_keeps_alerts_and_a_server_without_sentry_skips_the_rule(cfg, store, prom):
    s, srv = make()
    store.save_rule({"id": "new", "name": "New errors", "expr": "x", "direction": "above", "crit": 0, "for_seconds": 0,
                     "unit": "short", "datasource": "sentry"})
    n = RecordingNotifier()
    eng = Engine(store, prom, n, cfg, clock=lambda: 0.0, sentries={"default": s})
    run(eng.evaluate_rules(0.0))
    srv.status = 500
    run(eng.evaluate_rules(30.0))
    assert len(store.open_incidents()) == 2 and "HTTP 500" in eng.rule_error("new")   # unknown is not healthy
    bare = Engine(store, prom, RecordingNotifier(), cfg, clock=lambda: 0.0)           # no Sentry configured at all
    run(bare.evaluate_rules(60.0))
    assert bare.rule_error("new") is None                                          # skipped quietly, not an error


# ------------------------------------------------------------------ API
class FakeSentry:
    cfg = SentryCfg(org="acme", token=TOKEN, projects=["api"])

    def __init__(self):
        self.calls = []
        self.fail = None

    async def issues(self, query="is:unresolved", *, limit=25, sort="date"):
        self.calls.append((query, limit, sort))
        if self.fail:
            raise self.fail
        return [normalize(issue(1)), normalize(issue(2, "web"))][:limit]

    async def instant(self, expr, at=None):
        return [{"labels": {"project": "api"}, "value": 3.0}]

    def search_url(self, q):
        return "https://sentry.io/?query=" + q

    async def close(self): ...


def app_with_sentry(tmp_path, fake):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    return TestClient(create_app(cfg, prom=FakeProm(), sentry={"default": fake}))


def test_api_issue_list_summary_and_errors(tmp_path):
    fake = FakeSentry()
    with app_with_sentry(tmp_path, fake) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        r = c.get("/api/sentry/issues?query=level:error&sort=freq&limit=1").json()
        assert [i["short_id"] for i in r["issues"]] == ["API-1"] and r["org"] == "acme" and r["projects"] == ["api"]
        assert fake.calls[-1] == ("level:error", 1, "freq") and r["search_url"].endswith("level:error")
        assert c.get("/api/sentry/issues?sort=chaos").status_code == 400
        s = c.get("/api/sentry/summary").json()
        assert set(s) == {"unresolved", "new_1h", "new_24h", "fatal"} and s["unresolved"] == {"count": 2, "capped": False}
        assert [q for q, *_ in fake.calls[-4:]] == ["is:unresolved", "is:unresolved firstSeen:-1h", "is:unresolved firstSeen:-24h", "is:unresolved level:fatal"]
        fake.fail = SentryError("Sentry refused the token")
        assert c.get("/api/sentry/issues").status_code == 502 and "refused" in c.get("/api/sentry/issues").json()["detail"]
        servers = c.get("/api/me").json()["servers"][0]
        assert servers["sentry"] is True and servers["datasources"] == ["prometheus", "sentry"]


def test_api_without_sentry(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert c.get("/api/sentry/issues").status_code == 404 and c.get("/api/sentry/summary").status_code == 404
        me = c.get("/api/me").json()["servers"][0]
        assert me["sentry"] is False and me["datasources"] == ["prometheus"]
        r = c.post("/api/rules", json={"name": "S", "expr": "is:unresolved", "crit": 1, "datasource": "sentry"}, headers={"X-Requested-With": "nigrani"})
        assert r.status_code == 422 and "sentry" in r.json()["detail"]


def test_api_rule_preview_and_incident_page_for_a_sentry_rule(tmp_path):
    fake = FakeSentry()
    with app_with_sentry(tmp_path, fake) as c:
        H = {"X-Requested-With": "nigrani"}
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        body = {"name": "New errors", "expr": "is:unresolved firstSeen:-1h", "warn": 0, "crit": 5, "datasource": "sentry"}
        prev = c.post("/api/rules/test", json=body, headers=H).json()
        assert prev["count"] == 1 and prev["series"][0] == {"series": "api", "value": 3.0, "severity": "warning"}
        made = c.post("/api/rules", json=body, headers=H).json()
        iid = c.app.state.store.create_incident(source_id=made["id"], series_key="project=api", rule_name="New errors", series="api",
                                                labels={"project": "api"}, severity="warning", started_at=time.time() - 60, server="default")
        d = c.get(f"/api/incidents/{iid}").json()
        assert d["chart"] is None and [i["short_id"] for i in d["sentry"]["issues"]] == ["API-1", "WEB-2"]
        assert fake.calls[-1][0] == "is:unresolved firstSeen:-1h project:api"             # narrowed to the incident's project
        assert d["problems"] == []
        sample = {"nigrani_backup": 1, "rules": [{**body, "id": "x", "for_seconds": 0, "unit": "short"}]}
        assert c.post("/api/backup/import", json=sample, headers=H).json()["ok"] is True


def test_the_sentry_preset_loads(tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text("presets: [sentry]\n")
    rules = {r.id: r for r in load_config(str(f)).rules}
    assert rules["sentry-new-issues"].datasource == "sentry" and rules["sentry-fatal"].crit == 0
