import asyncio
import time

import httpx
from fastapi.testclient import TestClient

from conftest import FakeProm, RecordingNotifier
from nigrani.api import create_app
from nigrani.config import load_config
from nigrani.engine import Engine


def run(c):
    return asyncio.run(c)


def test_every_step_of_an_incident_is_recorded(cfg, store, prom):
    store.save_rule({"id": "r", "name": "High CPU", "expr": "q", "direction": "above", "warn": 80, "crit": 90,
                     "for_seconds": 0, "unit": "percent", "summary": "busy"})
    n = RecordingNotifier()

    async def send(ev, *, only=None, now=None):
        n.events.append(ev)
        return {"wanted": 2, "ok": 1}
    n.send = send
    eng = Engine(store, prom, n, cfg, clock=lambda: 0.0)
    prom.values["q"] = [({"name": "api"}, 85.0)]
    run(eng.evaluate_rules(0.0))
    iid = store.open_incidents()[0]["id"]
    run(eng.acknowledge(iid, "nikhil", "on it", hours=1, now=10.0))
    prom.values["q"] = [({"name": "api"}, 97.0)]
    run(eng.evaluate_rules(30.0))                                  # escalates: cancels the ack
    prom.values["q"] = [({"name": "api"}, 5.0)]
    run(eng.evaluate_rules(60.0))
    kinds = [(e["kind"], e["detail"]) for e in store.list_events(iid)]
    assert [k for k, _ in kinds if k != "notified"] == [
        "firing", "acknowledged", "severity", "unacknowledged", "resolved"]
    detail = dict(kinds)
    assert detail["firing"] == "warning: 85.0% (threshold 80.0%)" and detail["acknowledged"] == "nikhil: on it"
    assert detail["resolved"] == "back to 5.0%"
    assert any(d == "firing: sent to 1 of 2 channels" for k, d in kinds if k == "notified")
    assert [e["ts"] for e in store.list_events(iid)] == sorted(e["ts"] for e in store.list_events(iid))


def test_a_silenced_notification_leaves_a_trace(cfg, store, prom):
    store.save_rule({"id": "r", "name": "X", "expr": "q", "direction": "above", "crit": 1, "for_seconds": 0, "unit": "short"})
    store.add_silence("r", None, 1e12, "maintenance")
    prom.values["q"] = [({}, 5.0)]
    eng = Engine(store, prom, RecordingNotifier(), cfg, clock=lambda: 0.0)
    run(eng.evaluate_rules(0.0))
    iid = store.open_incidents()[0]["id"]
    assert ("suppressed", "firing: silenced, no message sent") in [(e["kind"], e["detail"]) for e in store.list_events(iid)]


class Lab(FakeProm):
    """Answers the queries the incident page makes, with data around t=1000."""

    async def range(self, q, start, end, step):
        from nigrani.prom import PromUnavailable
        if not self.up:
            raise PromUnavailable("down")
        self.ranges = getattr(self, "ranges", []) + [(q, start, end)]
        if "container_cpu" in q:
            return [{"labels": {}, "points": [[start, 10.0], [end, 40.0]]}]
        if "container_memory" in q:
            return [{"labels": {}, "points": [[start, 1e8], [end, 2e8]]}]
        if q.startswith("container_start_time_seconds"):
            # docker restarted the container in place: cAdvisor reports one series per RestartCount
            return [{"labels": {"container_label_restartcount": "3"}, "points": [[start, 1.0], [start + 300, 1.0]]},
                    {"labels": {"container_label_restartcount": "4"}, "points": [[start + 1200, 1.0], [end, 1.0]]}]
        if q == "RULE_EXPR":
            return [{"labels": {"name": "other"}, "points": [[start, 1.0]]},
                    {"labels": {"name": "api"}, "points": [[start, 60.0], [end, 95.0]]}]
        return []

    async def instant(self, q, at=None):
        if q.startswith("max by (name) (container_start_time_seconds") and ") >" in q:   # only the incident page query
            self.restart_at = at
            return [{"labels": {"name": "api"}, "value": time.time() - 700}, {"labels": {"name": "db"}, "value": time.time() - 5000},
                    {"labels": {"name": "cache"}, "value": time.time() - 650}]
        return []


def make_app(tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text("auth: {password: pw}\ngithub: {repo: o/r}\n")
    cfg = load_config(str(f))
    cfg.data_dir = str(tmp_path)
    prom = Lab()
    return create_app(cfg, prom=prom), prom


def test_endpoint_collects_what_was_going_on(tmp_path):
    app, prom = make_app(tmp_path)
    with TestClient(app) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        store = c.app.state.store
        now = time.time()
        store.save_rule({"id": "cpu", "name": "High CPU", "expr": "RULE_EXPR", "direction": "above", "warn": 80, "crit": 90,
                         "for_seconds": 0, "unit": "percent"})
        iid = store.create_incident(source_id="cpu", series_key="name=api", rule_name="High CPU", series="api",
                                    labels={"name": "api"}, severity="critical", started_at=now - 600, value=95.0,
                                    threshold=90.0, unit="percent")
        other = store.create_incident(source_id="x", series_key="a", rule_name="Disk", severity="warning",
                                      started_at=now - 700, value=1.0)
        far = store.create_incident(source_id="y", series_key="a", rule_name="Long ago", severity="warning",
                                    started_at=now - 86400, value=1.0)
        store.add_event(iid, now - 600, "firing", "critical: 95.0%")
        rt = c.app.state.runtimes["default"]
        rt.github._cache = (now, [
            {"id": 1, "name": "Deploy", "sha": "abc1234", "conclusion": "success", "finished_at": now - 900, "started_at": now - 960},
            {"id": 2, "name": "Deploy", "sha": "old0000", "conclusion": "success", "finished_at": now - 86400, "started_at": now - 86500}])

        d = c.get(f"/api/incidents/{iid}").json()
        assert d["incident"]["rule_name"] == "High CPU" and [e["kind"] for e in d["events"]] == ["firing"]
        assert d["window"]["start"] <= now - 600 - 1200 and d["window"]["end"] <= time.time()
        ch = d["chart"]
        assert (ch["warn"], ch["crit"], ch["unit"]) == (80, 90, "percent")
        assert [s["name"] for s in ch["series"]] == ["api"]                      # only this incident's series, not "other"
        assert d["container"]["name"] == "api" and d["container"]["cpu"][-1][1] == 40.0 and d["container"]["mem"]
        assert [(r["container"], r.get("count")) for r in d["restarts"]] == [("cache", None), ("api", "4")]
        assert d["restarts"][1]["ts"] == d["window"]["start"] + 1200               # when the "restart #4" series first appeared
        assert prom.restart_at == d["window"]["end"]                              # evaluated at the end of the window
        assert [x["sha"] for x in d["deploys"]] == ["abc1234"]                    # the 24h-old deploy is out of the window
        assert [r["id"] for r in d["related"]] == [other] and far not in [r["id"] for r in d["related"]]
        assert d["problems"] == []

        assert c.get("/api/incidents/99999").status_code == 404


def test_incident_without_a_rule_still_has_a_page(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        iid = c.app.state.store.create_incident(source_id="check:web", series_key="web", rule_name="Check failing: Web",
                                                severity="critical", started_at=time.time() - 120, server="default")
        d = c.get(f"/api/incidents/{iid}").json()
        assert d["chart"] is None and d["container"] is None and d["events"] == []


def test_prometheus_errors_are_reported_not_fatal(tmp_path):
    app, prom = make_app(tmp_path)
    with TestClient(app) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        c.app.state.store.save_rule({"id": "cpu", "name": "C", "expr": "RULE_EXPR", "direction": "above", "crit": 1,
                                     "for_seconds": 0, "unit": "short"})
        iid = c.app.state.store.create_incident(source_id="cpu", series_key="a", rule_name="C", severity="critical",
                                                started_at=time.time() - 60)
        prom.up = False
        d = c.get(f"/api/incidents/{iid}").json()
        assert d["chart"] is None and any(p.startswith("metric:") for p in d["problems"])


def test_logs_endpoint_takes_an_explicit_time_window(tmp_path, monkeypatch):
    f = tmp_path / "n.yaml"
    f.write_text("auth: {password: pw}\ndatasources: {loki: 'http://loki:3100'}\n")
    cfg = load_config(str(f))
    cfg.data_dir = str(tmp_path)
    seen = {}

    def handler(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={"data": {"result": []}})

    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert c.get("/api/logs?container=api&start=1000&end=2000").status_code == 200
    assert (seen["start"], seen["end"]) == (str(int(1000e9)), str(int(2000e9)))
