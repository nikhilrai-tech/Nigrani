import asyncio
import base64
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.config import GcpCfg, load_config
from nigrani.gcp import Gcp, GcpError, build_filter, parse
from nigrani.prom import PromQueryError, series_key

NOW = time.time()
PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = PRIVATE.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
KEY = {"type": "service_account", "client_email": "nigrani@proj-12345.iam.gserviceaccount.com", "private_key": PEM, "token_uri": "https://oauth2.googleapis.com/token"}
METRIC = "cloudsql.googleapis.com/database/cpu/utilization"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def point(age, v):
    return {"interval": {"startTime": iso(NOW - age), "endTime": iso(NOW - age)}, "value": {"doubleValue": v}}


def db(name, *vals):   # points come newest first, as Google returns them
    return {"metric": {"type": METRIC, "labels": {}}, "resource": {"type": "cloudsql_database", "labels": {"database_id": name, "project_id": "proj-12345"}},
            "points": [point(60 * i, v) for i, v in enumerate(reversed(vals))]}


class Server:
    """A stand-in for Google's token endpoint and Cloud Monitoring (the shapes we use)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.status = 200
        self.token_status = 200
        self.series = [db("proj-12345:prod", 0.4, 0.55, 0.915), db("proj-12345:stage", 0.1, 0.12), db("proj-12345:idle")]
        self.pages = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.host == "oauth2.googleapis.com":
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            head, claims, sig = form["assertion"].split(".")
            pad = lambda t: t + "=" * (-len(t) % 4)  # noqa: E731
            PRIVATE.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())   # raises if forged
            c = json.loads(base64.urlsafe_b64decode(pad(claims)))
            assert c["iss"] == KEY["client_email"] and c["scope"].endswith("monitoring.read") and c["exp"] - c["iat"] == 3600
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3599, "token_type": "Bearer"})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "x"}})
        if req.headers.get("authorization") != "Bearer tok-1":
            return httpx.Response(401, json={})
        p = req.url.path
        if p.endswith("/metricDescriptors"):
            return httpx.Response(200, json={"metricDescriptors": []})
        if p == "/v3/projects/proj-12345/timeSeries":
            if "bad.metric/x" in req.url.params["filter"]:
                return httpx.Response(400, json={"error": {"message": "Cannot find metric(s) that match type = \"bad.metric/x\"."}})
            if self.pages and "pageToken" not in req.url.params:
                return httpx.Response(200, json={"timeSeries": self.series[:1], "nextPageToken": "p2"})
            if self.pages:
                return httpx.Response(200, json={"timeSeries": self.series[1:]})
            return httpx.Response(200, json={"timeSeries": self.series})
        return httpx.Response(404, json={"error": {"message": "nope"}})


def make(server=None, **kw):
    server = server or Server()
    kw.setdefault("credentials_json", json.dumps(KEY))
    cfg = GcpCfg(project_id="proj-12345", **kw)
    return Gcp(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler))), server


def run(c):
    return asyncio.run(c)


def test_parse_and_filter():
    q = parse(f'{METRIC} resource.label.database_id=proj-12345:prod* metric.label.state="a b" aligner=MAX period=60')
    assert q.aligner == "max" and q.period == 60
    f = build_filter(q)
    assert f.startswith(f'metric.type = "{METRIC}" AND resource.labels.database_id = monitoring.regex.full_match("proj\\\\-12345:prod.*")')
    assert 'metric.labels.state = "a b"' in f
    assert build_filter(parse(f"{METRIC} resource.label.zone=*")) == f'metric.type = "{METRIC}"'          # a bare * filters nothing
    assert '\\"' in build_filter(parse(f"{METRIC} resource.label.x='a\"b'"))                               # quotes are escaped
    for bad in ("", "not a metric", f"{METRIC} aligner=weird", f"{METRIC} period=5", f"{METRIC} foo=bar", f"{METRIC} loose", f'{METRIC} "unterminated'):
        with pytest.raises(PromQueryError):
            parse(bad)


def test_instant_takes_the_newest_value_per_series_and_signs_in_once():
    c, s = make()
    rows = run(c.instant(f"{METRIC} resource.label.database_id=*"))
    assert rows == [{"labels": {"database_id": "proj-12345:prod", "project_id": "proj-12345"}, "value": 0.915},
                    {"labels": {"database_id": "proj-12345:stage", "project_id": "proj-12345"}, "value": 0.12}]     # the idle one has no points
    run(c.instant(METRIC))
    assert s.token_calls == 1                                                                   # the token is reused
    req = [r for r in s.requests if r.url.path.endswith("/timeSeries")][0]
    assert req.url.params["aggregation.perSeriesAligner"] == "ALIGN_MEAN" and req.url.params["aggregation.alignmentPeriod"] == "300s"


def test_range_is_sorted_oldest_first():
    c, _ = make()
    rows = run(c.range(METRIC, NOW - 3600, NOW))
    assert [p[1] for p in rows[0]["points"]] == [0.4, 0.55, 0.915] and rows[0]["points"][0][0] < rows[0]["points"][-1][0]


def test_pagination_and_value_types():
    c, s = make()
    s.pages = True
    assert len(run(c.instant(METRIC))) == 2 and sum(r.url.path.endswith("/timeSeries") for r in s.requests) == 2
    s.pages = False
    s.series = [{"metric": {"type": "x.y/z", "labels": {"k": "v"}}, "resource": {"labels": {}}, "points": [
        {"interval": {"endTime": iso(NOW)}, "value": {"int64Value": "42"}}]},
        {"metric": {"type": "x.y/z"}, "resource": {"labels": {}}, "points": [{"interval": {"endTime": iso(NOW)}, "value": {"distributionValue": {}}}]}]
    assert run(c.instant("x.y/z")) == [{"labels": {"k": "v"}, "value": 42.0}]


@pytest.mark.parametrize("status,word", [(403, "Monitoring Viewer"), (401, "Monitoring Viewer"), (429, "rate limiting"), (503, "HTTP 503"), (404, "not found")])
def test_failures_are_infrastructure_errors(status, word):
    c, s = make()
    s.status = status
    with pytest.raises(GcpError, match=word):
        run(c.instant(METRIC))
    assert word in c.last_error


def test_rejected_query_refused_sign_in_and_bad_keys():
    c, s = make()
    with pytest.raises(PromQueryError, match="Cannot find metric"):
        run(c.instant("bad.metric/x"))
    s.token_status = 400
    with pytest.raises(GcpError, match="refused the service account"):
        run(make(s)[0].instant(METRIC))
    for raw, word in (("not json", "not valid JSON"), ("{}", "not valid JSON"), (json.dumps({**KEY, "private_key": "garbage"}), "private key")):
        with pytest.raises(GcpError, match=word):
            run(make(credentials_json=raw)[0].instant(METRIC))
    assert run(make()[0].healthy()) is True and run(make(s)[0].healthy()) is False


def test_key_file_and_metadata_server(tmp_path):
    f = tmp_path / "key.json"
    f.write_text(json.dumps(KEY))
    c, s = make(credentials_json="", credentials_file=str(f))
    assert len(run(c.instant(METRIC))) == 2
    c2, _ = make(credentials_json="", credentials_file=str(tmp_path / "missing.json"))
    with pytest.raises(GcpError, match="cannot read"):
        run(c2.instant(METRIC))

    def meta(req):
        if "metadata.google.internal" in req.url.host:
            assert req.headers["metadata-flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3000})
        return Server().handler(req)
    c3 = Gcp(GcpCfg(project_id="proj-12345"), client=httpx.AsyncClient(transport=httpx.MockTransport(meta)))
    assert len(run(c3.instant(METRIC))) == 2


def test_config(tmp_path):
    assert not GcpCfg().enabled and GcpCfg(project_id="my-project-1").enabled
    for bad in ({"project_id": "Bad Project"}, {"url": "ftp://x"}):
        with pytest.raises(ValueError):
            GcpCfg(**bad)
    f = tmp_path / "n.yaml"
    f.write_text("gcp: {project_id: proj-aaaaa}\nservers:\n  - {id: p}\n  - {id: e, gcp: {project_id: proj-bbbbb}}\n")
    v = {s.id: s for s in load_config(str(f)).server_views()}
    assert v["p"].gcp.project_id == "proj-aaaaa" and v["e"].gcp.project_id == "proj-bbbbb"


def app_with(tmp_path, g):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    return TestClient(create_app(cfg, prom=FakeProm(), gcp={"default": g}))


def test_api_rules_preview_and_incident_chart(tmp_path):
    g, _ = make()
    H = {"X-Requested-With": "nigrani"}
    with app_with(tmp_path, g) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert c.get("/api/me").json()["servers"][0]["datasources"] == ["prometheus", "gcp"]
        body = {"name": "Cloud SQL CPU", "expr": f"{METRIC} resource.label.database_id=*", "warn": 0.8, "crit": 0.9, "datasource": "gcp", "unit": "ratio"}
        prev = c.post("/api/rules/test", json=body, headers=H).json()
        assert [(r["value"], r["severity"]) for r in prev["series"]] == [(0.915, "critical"), (0.12, None)]
        made = c.post("/api/rules", json=body, headers=H).json()
        labels = {"database_id": "proj-12345:prod", "project_id": "proj-12345"}
        iid = c.app.state.store.create_incident(source_id=made["id"], series_key=series_key(labels), rule_name="Cloud SQL CPU", series="proj-12345:prod",
                                                labels=labels, severity="critical", started_at=time.time() - 60, server="default")
        d = c.get(f"/api/incidents/{iid}").json()
        assert d["chart"]["series"] and d["problems"] == []


def test_api_without_gcp_refuses_the_rule(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        r = c.post("/api/rules", json={"name": "G", "expr": METRIC, "crit": 1, "datasource": "gcp"}, headers={"X-Requested-With": "nigrani"})
        assert r.status_code == 422 and "gcp" in r.json()["detail"]
