import asyncio
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProm, RecordingNotifier
from nigrani.api import create_app
from nigrani.config import ConfigError, load_config
from nigrani.engine import Engine, Signal
from nigrani.format import build_event
from nigrani.notifiers import Notifier
from nigrani.store import Store

YAML = """
app: {name: Fleet, environment: production}
presets: [core]
auth: {password: pw}
datasources: {prometheus: "http://prom-default:9090"}
checks: [{id: web, name: Web, type: http, target: "http://x"}]
servers:
  - {id: prod, name: Production, datasources: {prometheus: "http://prom-prod:9090", loki: "http://loki-prod:3100"}}
  - {id: staging, name: Staging, environment: staging, checks: []}
"""


def make_cfg(tmp_path, text=YAML):
    f = tmp_path / "nigrani.yaml"
    f.write_text(text)
    cfg = load_config(str(f))
    cfg.data_dir = str(tmp_path)
    return cfg


def run(c):
    return asyncio.run(c)


def test_single_server_is_implicit():
    cfg = load_config(None)
    (v,) = cfg.server_views()
    assert v.id == "default" and v.datasources.prometheus == cfg.datasources.prometheus


def test_servers_inherit_and_override(tmp_path):
    prod, staging = make_cfg(tmp_path).server_views()
    assert prod.datasources.prometheus == "http://prom-prod:9090" and prod.datasources.loki
    assert [c.id for c in prod.checks] == ["web"]            # inherited from the top level
    assert staging.datasources.prometheus == "http://prom-default:9090"  # inherited
    assert staging.checks == [] and staging.environment == "staging"      # explicit empty list wins


def test_config_validation(tmp_path):
    with pytest.raises(ConfigError, match="duplicate server"):
        make_cfg(tmp_path, "servers: [{id: a}, {id: a}]")
    with pytest.raises(ConfigError, match="unknown server"):
        make_cfg(tmp_path, "servers: [{id: a}]\nrules: [{id: r, name: R, expr: up, crit: 1, servers: [nope]}]")
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, "servers: [{id: Bad Id}]")


def engine_for(cfg, proms, store):
    n = RecordingNotifier()
    store.save_rule({"id": "r", "name": "High CPU", "expr": "q", "direction": "above", "warn": 80, "crit": 90,
                     "for_seconds": 0, "unit": "percent", "summary": "s"})
    return Engine(store, proms, n, cfg, clock=lambda: 1000.0), n


def test_each_server_alerts_independently(tmp_path, store):
    cfg = make_cfg(tmp_path)
    prod, staging = FakeProm(), FakeProm()
    prod.values["q"] = [({"instance": "a"}, 95.0)]
    staging.values["q"] = [({"instance": "a"}, 10.0)]
    eng, n = engine_for(cfg, {"prod": prod, "staging": staging}, store)
    run(eng.evaluate_rules(1000.0))
    inc = store.open_incidents()
    assert [(i["server"], i["rule_name"]) for i in inc] == [("prod", "High CPU")]
    assert n.events[0]["server"] == "Production" and n.events[0]["server_id"] == "prod"
    assert "[Production]" in n.events[0]["title"]

    staging.values["q"] = [({"instance": "a"}, 95.0)]   # same series key on the other server
    run(eng.evaluate_rules(1030.0))
    assert sorted(i["server"] for i in store.open_incidents()) == ["prod", "staging"]

    prod.values["q"] = [({"instance": "a"}, 5.0)]       # recovering prod must not touch staging
    run(eng.evaluate_rules(1060.0))
    assert [i["server"] for i in store.open_incidents()] == ["staging"]
    assert n.kinds == ["firing", "firing", "resolved"]


def test_rule_scoped_to_servers(tmp_path, store):
    cfg = make_cfg(tmp_path)
    prod, staging = FakeProm(), FakeProm()
    prod.values["q"] = staging.values["q"] = [({}, 95.0)]
    eng, n = engine_for(cfg, {"prod": prod, "staging": staging}, store)
    store.save_rule({**store.get_rule("r"), "servers": ["prod"]})
    run(eng.evaluate_rules(1000.0))
    assert [i["server"] for i in store.open_incidents()] == ["prod"]

    store.save_rule({**store.get_rule("r"), "servers": ["staging"]})   # scope moved: prod incident is closed quietly
    run(eng.evaluate_rules(1030.0))
    assert [i["server"] for i in store.open_incidents()] == ["staging"]


def test_prometheus_outage_is_per_server(tmp_path, store):
    cfg = make_cfg(tmp_path)
    prod, staging = FakeProm(), FakeProm()
    eng, n = engine_for(cfg, {"prod": prod, "staging": staging}, store)
    staging.up = False
    for t in range(3):
        run(eng.prometheus_health(1000.0 + t))
    assert [i["server"] for i in store.open_incidents()] == ["staging"]


def test_sync_external_is_scoped_to_its_server(tmp_path, store):
    cfg = make_cfg(tmp_path)
    eng, n = engine_for(cfg, {"prod": FakeProm(), "staging": FakeProm()}, store)
    down = lambda: Signal("svc:container-down", "Container down", "api", "api", "critical")  # noqa: E731
    run(eng.sync_external("svc:", [down()], 1000.0, server="prod"))
    run(eng.sync_external("svc:", [down()], 1000.0, server="staging"))
    run(eng.sync_external("svc:", [], 1030.0, server="prod"))      # prod's container is gone / forgotten
    assert [i["server"] for i in store.open_incidents()] == ["staging"]


def test_incidents_of_a_removed_server_are_closed_on_startup(tmp_path, store):
    cfg = make_cfg(tmp_path)
    store.create_incident(source_id="r", series_key="a", rule_name="Old", severity="critical", started_at=1.0,
                          server="default")   # e.g. left over from before the config gained `servers:`
    store.create_incident(source_id="r", series_key="a", rule_name="Live", severity="critical", started_at=1.0,
                          server="prod")
    Engine(store, {"prod": FakeProm(), "staging": FakeProm()}, RecordingNotifier(), cfg)
    assert [i["rule_name"] for i in store.open_incidents()] == ["Live"]


def test_silence_can_target_one_server(store):
    store.add_silence("r", "web", 5000.0, "maintenance", server="staging")
    assert store.is_silenced("r", "web-1", 1000.0, "staging")
    assert not store.is_silenced("r", "web-1", 1000.0, "prod")


def test_channel_scoped_to_servers(store):
    seen = []
    store.save_channel({"id": "oncall", "name": "Oncall", "type": "webhook", "settings": {"url": "https://x/y"},
                        "min_severity": "info", "servers": ["prod"]})
    n = Notifier(store, "UTC", httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (seen.append(r), httpx.Response(200))[1])))

    def ev(server_id):
        e = build_event("firing", severity="critical", rule_name="X", series="", value=1, threshold=0, unit="short",
                        summary="", started_at=0, now=1, environment="e")
        e["server_id"] = server_id
        return e

    assert run(n.send(ev("staging")))["wanted"] == 0 and seen == []
    assert run(n.send(ev("prod")))["ok"] == 1 and len(seen) == 1
    assert run(n.send({**ev(None), "kind": "test"}))["ok"] == 1   # digest/test events carry no server


def test_store_migrates_a_database_from_the_first_release():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE rules (id TEXT PRIMARY KEY, name TEXT NOT NULL, expr TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'above',
        warn REAL, crit REAL, for_seconds INTEGER NOT NULL DEFAULT 300, unit TEXT NOT NULL DEFAULT 'short',
        summary TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'custom', updated_at REAL);
      CREATE TABLE channels (id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}',
        min_severity TEXT NOT NULL DEFAULT 'warning', quiet_start INTEGER, quiet_end INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
        last_error TEXT, last_sent_at REAL);
      CREATE TABLE incidents (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, series_key TEXT NOT NULL,
        rule_name TEXT NOT NULL, series TEXT NOT NULL DEFAULT '', labels TEXT NOT NULL DEFAULT '{}', severity TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'firing', started_at REAL NOT NULL, resolved_at REAL, last_value REAL, peak_value REAL,
        threshold REAL, unit TEXT NOT NULL DEFAULT 'short', summary TEXT NOT NULL DEFAULT '', last_notified REAL,
        silenced INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE silences (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, match TEXT, until REAL NOT NULL,
        reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
      INSERT INTO rules(id,name,expr) VALUES('old','Old','up');
      INSERT INTO incidents(source_id,series_key,rule_name,severity,started_at) VALUES('old','a','Old','critical',1);
    """)
    c.commit()
    c.close()
    s = Store(path)
    assert s.get_rule("old")["servers"] == []
    assert s.open_incidents()[0]["server"] == "default"
    s.add_silence(None, None, 9e9, "x", server="prod")  # new columns are usable


def test_api_routes_to_the_selected_server(tmp_path):
    cfg = make_cfg(tmp_path)
    prod, staging = FakeProm(), FakeProm()
    cpu = next(t for t in cfg.tiles if t.id == "cpu")
    prod.values[cpu.query] = [({}, 91.0)]
    staging.values[cpu.query] = [({}, 12.0)]
    staging.metrics = {"django_http_requests_total_by_method_total"}

    with TestClient(create_app(cfg, prom={"prod": prod, "staging": staging})) as c:
        H = {"X-Requested-With": "nigrani"}
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        me = c.get("/api/me").json()
        assert [s["id"] for s in me["servers"]] == ["prod", "staging"]
        assert me["default_server"] == "prod" and me["features"]["multi"] and me["servers"][0]["logs"]

        def cpu_of(overview):
            return next(t for t in overview["tiles"] if t["id"] == "cpu")["value"]

        assert cpu_of(c.get("/api/overview").json()) == 91.0                 # default = first server
        assert cpu_of(c.get("/api/overview?server=staging").json()) == 12.0
        assert c.get("/api/overview?server=nope").status_code == 404

        fleet = c.get("/api/fleet").json()
        assert [(f["server"]["id"], cpu_of(f)) for f in fleet] == [("prod", 91.0), ("staging", 12.0)]

        # rules/channels validate server ids
        body = {"name": "R", "expr": "vector(1)", "crit": 5, "servers": ["nope"]}
        assert c.post("/api/rules", json=body, headers=H).status_code == 422
        ok = c.post("/api/rules", json={**body, "servers": ["staging"]}, headers=H).json()
        assert ok["servers"] == ["staging"]
        assert c.post("/api/channels", json={"name": "C", "type": "slack", "servers": ["x"]}, headers=H).status_code == 422

        # incidents are tagged and filterable by server
        store = c.app.state.store
        store.create_incident(source_id="r", series_key="a", rule_name="A", severity="critical", started_at=time.time() - 60, server="prod")
        store.create_incident(source_id="r", series_key="a", rule_name="B", severity="warning", started_at=time.time() - 30, server="staging")
        assert {i["rule_name"] for i in c.get("/api/incidents").json()} == {"A", "B"}
        assert [i["rule_name"] for i in c.get("/api/incidents?server=staging").json()] == ["B"]
        ov = c.get("/api/overview?server=prod").json()
        assert ov["incident_counts"] == {"critical": 1, "warning": 0}         # only this server's alerts

        assert c.post("/api/silences", json={"server": "nope"}, headers=H).status_code == 422
        assert c.post("/api/silences", json={"server": "prod", "minutes": 5}, headers=H).status_code == 200


def test_public_status_never_leaks_internal_rule_names(tmp_path):
    cfg = make_cfg(tmp_path, YAML + "status_page: {enabled: true}\n")
    prod, staging = FakeProm(), FakeProm()
    with TestClient(create_app(cfg, prom={"prod": prod, "staging": staging})) as c:
        store = c.app.state.store
        store.create_incident(source_id="host-disk", series_key="a", rule_name="Disk filling up", series="/data",
                              severity="critical", started_at=1e10, server="prod")
        store.create_incident(source_id="check:web", series_key="web", rule_name="Check failing: Web", series="http://x",
                              severity="critical", started_at=1e10, server="prod")
        d = c.get("/api/public/status").json()           # no login
        names = [i["rule"] for i in d["incidents"]]
        assert names == ["Web"] and "Disk filling up" not in str(d)
        assert all(" · " in ch["name"] for ch in d["checks"])  # server-prefixed when several servers


def test_excluded_containers_are_ignored(tmp_path, store):
    from nigrani.containers import collect_containers

    class Stub:
        async def instant(self, q):
            def rows(v):
                return [{"labels": {"name": n}, "value": v} for n in ("web-1", "other-project-db")]
            if "container_cpu_usage" in q:
                return rows(5.0)
            if "working_set" in q or "spec_memory" in q:
                return rows(1e6)
            if "changes(" in q:
                return rows(0.0)
            if "container_start_time" in q:
                return rows(5000.0)
            if "network" in q:
                return rows(1.0)
            return rows(3.0)  # container_last_seen age

    cfg = make_cfg(tmp_path, "containers: {exclude: ['^other-']}\n")
    got = run(collect_containers(Stub(), cfg, store))
    assert [c["name"] for c in got] == ["web-1"]
    assert not store.kv_get("known_containers", {}).get("other-project-db")   # never auto-tracked either
