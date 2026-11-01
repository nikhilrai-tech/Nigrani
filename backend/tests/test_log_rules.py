import asyncio

import httpx
from fastapi.testclient import TestClient

from conftest import FakeProm, RecordingNotifier
from nigrani.api import create_app
from nigrani.config import load_config
from nigrani.engine import Engine

YAML = """
auth: {password: pw}
presets: [core, logs]
servers:
  - {id: prod, name: Production, datasources: {prometheus: "http://p1:9090", loki: "http://loki:3100"}}
  - {id: staging, name: Staging, datasources: {prometheus: "http://p2:9090"}}
"""


def make_cfg(tmp_path, text=YAML):
    f = tmp_path / "n.yaml"
    f.write_text(text)
    cfg = load_config(str(f))
    cfg.data_dir = str(tmp_path)
    return cfg


def run(c):
    return asyncio.run(c)


def test_logs_preset_is_a_loki_rule(tmp_path):
    cfg = make_cfg(tmp_path)
    rule = next(r for r in cfg.rules if r.id == "log-errors")
    assert rule.datasource == "loki" and "count_over_time" in rule.expr and rule.crit == 200


def test_datasource_roundtrips_and_defaults_to_prometheus(store):
    store.save_rule({"id": "a", "name": "A", "expr": "up", "direction": "above", "warn": 1, "crit": 2,
                     "for_seconds": 0, "unit": "short"})
    store.save_rule({"id": "b", "name": "B", "expr": "x", "direction": "above", "warn": 1, "crit": 2,
                     "for_seconds": 0, "unit": "short", "datasource": "loki"})
    assert store.get_rule("a")["datasource"] == "prometheus"
    assert store.get_rule("b")["datasource"] == "loki"


def test_log_rule_is_evaluated_against_loki_only(tmp_path, store):
    cfg = make_cfg(tmp_path)
    prom_prod, prom_stg, loki_prod = FakeProm(), FakeProm(), FakeProm()
    store.save_rule({"id": "log", "name": "Error spike", "expr": "LOGQL", "direction": "above", "warn": 50,
                     "crit": 200, "for_seconds": 0, "unit": "short", "datasource": "loki"})
    loki_prod.values["LOGQL"] = [({"container": "api"}, 300.0), ({"container": "web"}, 5.0)]
    prom_prod.values["LOGQL"] = [({"container": "wrong-source"}, 999.0)]   # must never be consulted
    n = RecordingNotifier()
    eng = Engine(store, {"prod": prom_prod, "staging": prom_stg}, n, cfg, clock=lambda: 1000.0,
                 lokis={"prod": loki_prod})
    run(eng.evaluate_rules(1000.0))
    inc = store.open_incidents()
    assert [(i["server"], i["series"], i["severity"]) for i in inc] == [("prod", "api", "critical")]
    assert n.events[0]["rule"] == "Error spike"
    assert eng.rule_error("log") is None               # staging has no Loki: skipped quietly, not an error


def test_log_rule_error_is_reported_per_rule(tmp_path, store):
    cfg = make_cfg(tmp_path)
    loki = FakeProm()
    loki.up = False
    store.save_rule({"id": "log", "name": "L", "expr": "q", "direction": "above", "warn": 1, "crit": 2,
                     "for_seconds": 0, "unit": "short", "datasource": "loki"})
    eng = Engine(store, {"prod": FakeProm(), "staging": FakeProm()}, RecordingNotifier(), cfg, lokis={"prod": loki})
    run(eng.evaluate_rules(1000.0))
    assert "Production" in eng.rule_error("log")


def test_api_preview_and_validation(tmp_path):
    cfg = make_cfg(tmp_path)
    loki = FakeProm()
    loki.values["sum(count_over_time(x))"] = [({"container": "api"}, 120.0)]
    with TestClient(create_app(cfg, prom={"prod": FakeProm(), "staging": FakeProm()}, loki={"prod": loki})) as c:
        H = {"X-Requested-With": "nigrani"}
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        body = {"name": "L", "expr": "sum(count_over_time(x))", "warn": 50, "crit": 200, "datasource": "loki"}

        ok = c.post("/api/rules/test?server=prod", json=body, headers=H).json()
        assert ok["count"] == 1 and ok["series"][0]["series"] == "api" and ok["series"][0]["severity"] == "warning"
        no = c.post("/api/rules/test?server=staging", json=body, headers=H)
        assert no.status_code == 400 and "no loki configured" in no.json()["detail"]

        made = c.post("/api/rules", json=body, headers=H).json()
        assert made["datasource"] == "loki"
        assert next(r for r in c.get("/api/rules").json() if r["id"] == made["id"])["datasource"] == "loki"
        assert next(r for r in c.get("/api/rules").json() if r["id"] == "host-cpu")["datasource"] == "prometheus"


def test_cannot_create_a_log_rule_when_no_server_has_loki(tmp_path):
    cfg = make_cfg(tmp_path, "auth: {password: pw}\n")
    with TestClient(create_app(cfg, prom=FakeProm(), loki={})) as c:
        H = {"X-Requested-With": "nigrani"}
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        r = c.post("/api/rules", json={"name": "L", "expr": "q", "crit": 1, "datasource": "loki"}, headers=H)
        assert r.status_code == 422 and "loki" in r.json()["detail"]


def test_log_alert_resolves_as_soon_as_the_lines_stop(tmp_path, store):
    """No matching log lines make LogQL drop the series (not return 0), which must count as healthy right away."""
    cfg = make_cfg(tmp_path)
    loki, prom = FakeProm(), FakeProm()
    store.save_rule({"id": "log", "name": "Error spike", "expr": "LOGQL", "direction": "above", "warn": 50,
                     "crit": 200, "for_seconds": 0, "unit": "short", "datasource": "loki"})
    n = RecordingNotifier()
    eng = Engine(store, {"prod": prom, "staging": FakeProm()}, n, cfg, clock=lambda: 1000.0, lokis={"prod": loki})
    loki.values["LOGQL"] = [({"container": "api"}, 300.0)]
    run(eng.evaluate_rules(1000.0))
    assert n.kinds == ["firing"]
    loki.values["LOGQL"] = []                              # lines stopped: series disappears
    run(eng.evaluate_rules(1030.0))                        # next cycle, long before absent_grace (300s)
    assert n.kinds == ["firing", "resolved"] and store.open_incidents() == []
    resolved = n.events[-1]
    assert "Error spike" in resolved["title"] and "api" in resolved["title"]
    assert "Now:" not in resolved["body"]                  # no stale "Now: 300" for a series that no longer exists


def test_log_outage_does_not_resolve_anything(tmp_path, store):
    cfg = make_cfg(tmp_path)
    loki = FakeProm()
    store.save_rule({"id": "log", "name": "Error spike", "expr": "LOGQL", "direction": "above", "warn": 50,
                     "crit": 200, "for_seconds": 0, "unit": "short", "datasource": "loki"})
    n = RecordingNotifier()
    eng = Engine(store, {"prod": FakeProm(), "staging": FakeProm()}, n, cfg, clock=lambda: 1000.0, lokis={"prod": loki})
    loki.values["LOGQL"] = [({"container": "api"}, 300.0)]
    run(eng.evaluate_rules(1000.0))
    loki.up = False                                        # Loki down: unknown, not healthy
    run(eng.evaluate_rules(1030.0))
    assert n.kinds == ["firing"] and len(store.open_incidents()) == 1


def test_logs_endpoint_returns_the_logql_and_label_it_ran(tmp_path, monkeypatch):
    """The UI turns the current search into a rule from exactly this query + label."""
    cfg = make_cfg(tmp_path, "auth: {password: pw}\ndatasources: {loki: 'http://loki:3100', loki_container_label: service}\n")
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"data": {"result": [
            {"stream": {"service": "api"}, "values": [["1700000000000000000", "ERROR boom"]]}]}})

    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        r = c.get("/api/logs?container=api&level=error&q=boom").json()
    assert r["label"] == "service"
    assert r["query"].startswith('{service="api"}') and "|~" in r["query"] and '|= "boom"' in r["query"]
    assert seen["params"]["query"] == r["query"]
    assert r["rows"][0]["line"] == "ERROR boom" and r["rows"][0]["container"] == "api"
