import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.config import DatadogCfg, load_config
from nigrani.datadog import Datadog, DatadogError, labels_of
from nigrani.prom import PromQueryError, series_key

NOW = time.time()


def pts(*vals):
    return [[(NOW - 60 * (len(vals) - i)) * 1000, v] for i, v in enumerate(vals)]


class Server:
    """A stand-in for Datadog's API (the endpoints and answer shapes we use)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.series = [
            {"metric": "system.cpu.user", "scope": "host:web-1", "tag_set": ["host:web-1"], "pointlist": pts(40, 55, 91.5)},
            {"metric": "system.cpu.user", "scope": "host:web-2", "tag_set": ["host:web-2"], "pointlist": pts(10, 12, None)},
            {"metric": "system.cpu.user", "scope": "host:web-3", "tag_set": ["host:web-3"], "pointlist": pts(None, None, None)},
        ]
        self.monitors = [
            {"id": 1, "name": "API latency", "type": "metric alert", "overall_state": "Alert", "tags": ["team:api", "env:prod"], "overall_state_modified": "2026-11-02T10:00:00+00:00"},
            {"id": 2, "name": "Disk", "type": "metric alert", "overall_state": "Warn", "tags": [], "overall_state_modified": "2026-11-02T11:00:00+00:00"},
            {"id": 3, "name": "Queue", "type": "query alert", "overall_state": "No Data", "tags": [], "overall_state_modified": None},
            {"id": 4, "name": "Fine", "type": "query alert", "overall_state": "OK", "tags": []},
        ]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": ["x"]})
        if req.headers.get("dd-api-key") != "api" or req.headers.get("dd-application-key") != "app":
            return httpx.Response(403, json={"errors": ["Forbidden"]})
        p = req.url.path
        if p == "/api/v1/validate":
            return httpx.Response(200, json={"valid": True})
        if p == "/api/v1/monitor":
            return httpx.Response(200, json=self.monitors)
        if p == "/api/v1/query":
            if "(" in req.url.params["query"] and "avg:" not in req.url.params["query"]:
                return httpx.Response(400, json={"status": "error", "error": "Error parsing query: unable to parse"})
            return httpx.Response(200, json={"status": "ok", "series": self.series, "query": req.url.params["query"]})
        return httpx.Response(404, json={})


def make(server=None, **kw):
    server = server or Server()
    cfg = DatadogCfg(api_key="api", app_key="app", **kw)
    return Datadog(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler))), server


def run(c):
    return asyncio.run(c)


def test_labels():
    assert labels_of({"tag_set": ["host:web-1", "env:prod", "flag"]}) == {"host": "web-1", "env": "prod", "flag": "true"}
    assert labels_of({"scope": "host:a,device:/", "tag_set": []}) == {"host": "a", "device": "/"}
    assert labels_of({"scope": "*", "metric": "system.load.1"}) == {"metric": "system.load.1"}


def test_instant_takes_the_latest_real_point_and_skips_empty_series():
    c, s = make()
    rows = run(c.instant("avg:system.cpu.user{*} by {host}"))
    assert rows == [{"labels": {"host": "web-1"}, "value": 91.5}, {"labels": {"host": "web-2"}, "value": 12}]   # web-2's newest point is null; web-3 has none
    req = s.requests[-1]
    assert req.headers["dd-api-key"] == "api" and req.headers["dd-application-key"] == "app"
    assert req.url.host == "api.datadoghq.com" and int(req.url.params["to"]) - int(req.url.params["from"]) == 600


def test_range_returns_points_in_seconds():
    c, s = make()
    rows = run(c.range("avg:system.cpu.user{*} by {host}", NOW - 3600, NOW, 60))
    assert rows[0]["labels"] == {"host": "web-1"} and [p[1] for p in rows[0]["points"]] == [40, 55, 91.5]
    assert abs(rows[0]["points"][-1][0] - (NOW - 60)) < 1


def test_site_and_url_selection():
    c, s = make(site="datadoghq.eu")
    run(c.instant("avg:system.cpu.user{*}"))
    assert s.requests[-1].url.host == "api.datadoghq.eu" and c.app == "https://app.datadoghq.eu"
    c2, s2 = make(url="http://proxy.test")
    run(c2.instant("avg:system.cpu.user{*}"))
    assert s2.requests[-1].url.host == "proxy.test"


def test_monitors_are_summarised_worst_first():
    c, _ = make()
    d = run(c.monitors())
    assert d["counts"] == {"Alert": 1, "Warn": 1, "No Data": 1, "OK": 1} and d["total"] == 4
    assert [m["name"] for m in d["monitors"]] == ["API latency", "Queue", "Disk"]
    assert d["monitors"][0]["url"] == "https://app.datadoghq.com/monitors/1" and d["monitors"][1]["since"] is None


@pytest.mark.parametrize("status,word", [(403, "refused the keys"), (401, "refused the keys"), (429, "rate limiting"), (503, "HTTP 503")])
def test_failures_are_infrastructure_errors(status, word):
    c, s = make()
    s.status = status
    with pytest.raises(DatadogError, match=word):
        run(c.instant("avg:system.cpu.user{*}"))
    assert word in c.last_error


def test_a_rejected_query_is_a_query_error_and_wrong_keys_are_refused():
    c, _ = make()
    with pytest.raises(PromQueryError, match="parse"):
        run(c.instant("nonsense("))
    bad = Datadog(DatadogCfg(api_key="x", app_key="y"), client=httpx.AsyncClient(transport=httpx.MockTransport(Server().handler)))
    with pytest.raises(DatadogError, match="timeseries_query"):
        run(bad.instant("avg:system.cpu.user{*}"))
    assert run(c.healthy()) is True and run(bad.healthy()) is False


def test_not_json_means_a_wrong_site():
    c = Datadog(DatadogCfg(api_key="a", app_key="b"), client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))))
    with pytest.raises(DatadogError, match="datadog.site"):
        run(c.instant("avg:x{*}"))


def test_config(tmp_path):
    assert not DatadogCfg(api_key="a").enabled and DatadogCfg(api_key="a", app_key="b").enabled
    for bad in ({"site": "evil/path"}, {"site": "x y.com"}, {"url": "ftp://x"}):
        with pytest.raises(ValueError):
            DatadogCfg(**bad)
    f = tmp_path / "n.yaml"
    f.write_text("datadog: {api_key: a, app_key: b}\nservers:\n  - {id: p}\n  - {id: e, datadog: {site: datadoghq.eu, api_key: c, app_key: d}}\n")
    v = {s.id: s for s in load_config(str(f)).server_views()}
    assert v["p"].datadog.site == "datadoghq.com" and v["e"].datadog.site == "datadoghq.eu"


def app_with(tmp_path, dd):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    return TestClient(create_app(cfg, prom=FakeProm(), datadog={"default": dd}))


def test_api_monitors_rules_and_incident_chart(tmp_path):
    dd, _ = make()
    H = {"X-Requested-With": "nigrani"}
    with app_with(tmp_path, dd) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        me = c.get("/api/me").json()["servers"][0]
        assert me["datadog"] is True and me["datasources"] == ["prometheus", "datadog"]
        m = c.get("/api/datadog/monitors").json()
        assert m["counts"]["Alert"] == 1 and m["app_url"] == "https://app.datadoghq.com"
        body = {"name": "CPU", "expr": "avg:system.cpu.user{*} by {host}", "warn": 80, "crit": 90, "datasource": "datadog"}
        prev = c.post("/api/rules/test", json=body, headers=H).json()
        assert prev["series"] == [{"series": "web-1", "value": 91.5, "severity": "critical"}, {"series": "web-2", "value": 12, "severity": None}]
        made = c.post("/api/rules", json=body, headers=H).json()
        labels = {"host": "web-1"}
        iid = c.app.state.store.create_incident(source_id=made["id"], series_key=series_key(labels), rule_name="CPU", series="web-1",
                                                labels=labels, severity="critical", started_at=time.time() - 60, server="default")
        d = c.get(f"/api/incidents/{iid}").json()
        assert d["chart"]["series"][0]["name"] == "web-1" and d["problems"] == []


def test_api_without_datadog_and_with_a_refused_key(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert c.get("/api/datadog/monitors").status_code == 404
        r = c.post("/api/rules", json={"name": "D", "expr": "avg:x{*}", "crit": 1, "datasource": "datadog"}, headers={"X-Requested-With": "nigrani"})
        assert r.status_code == 422 and "datadog" in r.json()["detail"]
    dd, s = make()
    s.status = 403
    with app_with(tmp_path / "b", dd) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        r = c.get("/api/datadog/monitors")
        assert r.status_code == 502 and "refused the keys" in r.json()["detail"]


def test_the_datadog_preset_loads(tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text("presets: [datadog]\n")
    rules = {r.id: r for r in load_config(str(f)).rules}
    assert set(rules) == {"dd-host-cpu", "dd-host-memory", "dd-host-disk"} and rules["dd-host-memory"].direction == "below"
    assert all(r.datasource == "datadog" for r in rules.values())
