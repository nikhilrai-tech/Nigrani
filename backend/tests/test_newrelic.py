import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.config import NewRelicCfg, load_config
from nigrani.newrelic import NewRelic, NewRelicError
from nigrani.prom import PromQueryError, series_key

NOW = time.time()


class Server:
    """A stand-in for NerdGraph (the request and answer shapes we use)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.queries: list[str] = []
        self.status = 200
        self.errors = None

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.status != 200:
            return httpx.Response(self.status, json={})
        if req.headers.get("api-key") != "NRAK-key":
            return httpx.Response(403, json={})
        body = json.loads(req.content)
        if "nrql(" not in body["query"]:
            return httpx.Response(200, json={"data": {"actor": {"account": {"id": body["variables"]["account"]}}}})
        q = body["variables"]["q"]
        self.queries.append(q)
        if self.errors:
            return httpx.Response(200, json={"data": {"actor": {"account": None}}, "errors": self.errors})
        if "SELEKT" in q:
            return httpx.Response(200, json={"data": None, "errors": [{"message": "NRQL Syntax Error: Error at line 1", "extensions": {"errorClass": "NRQL_SYNTAX_ERROR"}}]})
        if "TIMESERIES" in q:
            rows = [{"beginTimeSeconds": int(NOW) - 120, "endTimeSeconds": int(NOW) - 60, "facet": "web-1", "average.cpuPercent": 40.0},
                    {"beginTimeSeconds": int(NOW) - 60, "endTimeSeconds": int(NOW), "facet": "web-1", "average.cpuPercent": 91.5}]
        elif "FACET hostname, region" in q:
            rows = [{"facet": ["web-1", "eu"], "average.cpuPercent": 91.5}]
        elif "FACET" in q:
            rows = [{"facet": "web-1", "average.cpuPercent": 91.5}, {"facet": "web-2", "average.cpuPercent": 12}, {"facet": "web-3", "average.cpuPercent": None}]
        elif "count(*)" in q and "average" in q:
            rows = [{"count": 120, "average.duration": 0.4}]
        else:
            rows = [{"count": 7}]
        return httpx.Response(200, json={"data": {"actor": {"account": {"nrql": {"results": rows}}}}})


def make(server=None, **kw):
    server = server or Server()
    cfg = NewRelicCfg(account_id="123", api_key="NRAK-key", **kw)
    return NewRelic(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler))), server


def run(c):
    return asyncio.run(c)


def test_instant_gives_one_series_per_facet_and_skips_nulls():
    c, s = make()
    rows = run(c.instant("SELECT average(cpuPercent) FROM SystemSample FACET hostname SINCE 5 minutes ago"))
    assert rows == [{"labels": {"facet": "web-1"}, "value": 91.5}, {"labels": {"facet": "web-2"}, "value": 12.0}]
    req = s.requests[-1]
    assert req.headers["api-key"] == "NRAK-key" and req.url.host == "api.newrelic.com"
    assert json.loads(req.content)["variables"]["account"] == 123


def test_without_a_facet_and_with_several_columns_and_facets():
    c, _ = make()
    assert run(c.instant("SELECT count(*) FROM Transaction")) == [{"labels": {"metric": "count"}, "value": 7.0}]
    both = run(c.instant("SELECT count(*), average(duration) FROM Transaction"))
    assert {tuple(r["labels"].items()): r["value"] for r in both} == {(("metric", "count"),): 120.0, (("metric", "average.duration"),): 0.4}
    assert run(c.instant("SELECT average(cpuPercent) FROM SystemSample FACET hostname, region")) == [
        {"labels": {"facet1": "web-1", "facet2": "eu"}, "value": 91.5}]


def test_range_replaces_the_time_clauses_and_groups_points_by_facet():
    c, s = make()
    rows = run(c.range("SELECT average(cpuPercent) FROM SystemSample FACET hostname SINCE 5 minutes ago", NOW - 3600, NOW, 60))
    q = s.queries[-1]
    assert q.count("SINCE") == 1 and "5 minutes" not in q and q.endswith("TIMESERIES") and f"SINCE {int(NOW - 3600)} UNTIL {int(NOW)}" in q
    assert rows == [{"labels": {"facet": "web-1"}, "points": [[int(NOW) - 120.0, 40.0], [int(NOW) - 60.0, 91.5]]}]


def test_region_and_url_selection():
    c, s = make(region="eu")
    run(c.instant("SELECT count(*) FROM Transaction"))
    assert s.requests[-1].url.host == "api.eu.newrelic.com"
    c2, s2 = make(url="http://proxy.test/graphql")
    run(c2.instant("SELECT count(*) FROM Transaction"))
    assert s2.requests[-1].url.host == "proxy.test"


@pytest.mark.parametrize("status,word", [(403, "refused the API key"), (401, "refused the API key"), (429, "rate limiting"), (503, "HTTP 503")])
def test_failures_are_infrastructure_errors(status, word):
    c, s = make()
    s.status = status
    with pytest.raises(NewRelicError, match=word):
        run(c.instant("SELECT count(*) FROM Transaction"))
    assert word in c.last_error


def test_graphql_errors_split_into_a_bad_query_and_a_refused_account():
    c, s = make()
    with pytest.raises(PromQueryError, match="Syntax Error"):
        run(c.instant("SELEKT 1"))
    s.errors = [{"message": "Not authorized to access account 123", "extensions": {"errorClass": "FORBIDDEN"}}]
    with pytest.raises(NewRelicError, match="account_id"):
        run(c.instant("SELECT count(*) FROM Transaction"))
    wrong = NewRelic(NewRelicCfg(account_id="123", api_key="x"), client=httpx.AsyncClient(transport=httpx.MockTransport(Server().handler)))
    assert run(c.healthy()) is True and run(wrong.healthy()) is False


def test_not_json_means_a_wrong_url():
    c = NewRelic(NewRelicCfg(account_id="1", api_key="k"), client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))))
    with pytest.raises(NewRelicError, match="newrelic.url"):
        run(c.instant("SELECT count(*) FROM Transaction"))


def test_config(tmp_path):
    assert not NewRelicCfg(api_key="a").enabled and NewRelicCfg(api_key="a", account_id="9").enabled
    for bad in ({"account_id": "abc"}, {"region": "asia"}, {"url": "ftp://x"}):
        with pytest.raises(ValueError):
            NewRelicCfg(**bad)
    f = tmp_path / "n.yaml"
    f.write_text("newrelic: {account_id: '1', api_key: a}\nservers:\n  - {id: p}\n  - {id: e, newrelic: {account_id: '2', api_key: b, region: eu}}\n")
    v = {s.id: s for s in load_config(str(f)).server_views()}
    assert v["p"].newrelic.region == "us" and v["e"].newrelic.region == "eu" and v["e"].newrelic.account_id == "2"


def app_with(tmp_path, nr):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    return TestClient(create_app(cfg, prom=FakeProm(), newrelic={"default": nr}))


def test_api_rules_preview_and_incident_chart(tmp_path):
    nr, _ = make()
    H = {"X-Requested-With": "nigrani"}
    with app_with(tmp_path, nr) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        me = c.get("/api/me").json()["servers"][0]
        assert me["datasources"] == ["prometheus", "newrelic"]
        body = {"name": "CPU", "expr": "SELECT average(cpuPercent) FROM SystemSample FACET hostname SINCE 5 minutes ago",
                "warn": 80, "crit": 90, "datasource": "newrelic"}
        prev = c.post("/api/rules/test", json=body, headers=H).json()
        assert prev["series"] == [{"series": "web-1", "value": 91.5, "severity": "critical"}, {"series": "web-2", "value": 12.0, "severity": None}]
        made = c.post("/api/rules", json=body, headers=H).json()
        labels = {"facet": "web-1"}
        iid = c.app.state.store.create_incident(source_id=made["id"], series_key=series_key(labels), rule_name="CPU", series="web-1",
                                                labels=labels, severity="critical", started_at=time.time() - 60, server="default")
        d = c.get(f"/api/incidents/{iid}").json()
        assert d["chart"]["series"][0]["name"] == "web-1" and d["problems"] == []


def test_api_without_newrelic_refuses_the_rule(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        r = c.post("/api/rules", json={"name": "N", "expr": "SELECT count(*) FROM Transaction", "crit": 1, "datasource": "newrelic"}, headers={"X-Requested-With": "nigrani"})
        assert r.status_code == 422 and "newrelic" in r.json()["detail"]
