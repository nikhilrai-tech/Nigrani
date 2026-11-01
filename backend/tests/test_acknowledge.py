import asyncio

import httpx
from fastapi.testclient import TestClient

from conftest import FakeProm, RecordingNotifier
from nigrani.api import create_app
from nigrani.config import load_config
from nigrani.engine import Engine
from nigrani.format import build_event
from nigrani.notifiers import Notifier

REPEAT = 4 * 3600


def run(c):
    return asyncio.run(c)


def firing(cfg, store, prom, notifier=None):
    """An engine with one critical incident on 'api' that fired at t=0."""
    store.save_rule({"id": "r", "name": "High CPU", "expr": "q", "direction": "above", "warn": 80, "crit": 90,
                     "for_seconds": 0, "unit": "percent", "summary": "busy"})
    prom.values["q"] = [({"name": "api"}, 95.0)]
    n = notifier or RecordingNotifier()
    eng = Engine(store, prom, n, cfg, clock=lambda: 0.0)
    run(eng.evaluate_rules(0.0))
    return eng, n, store.open_incidents()[0]["id"]


def test_ack_records_who_and_tells_the_channels_once(cfg, store, prom):
    eng, n, iid = firing(cfg, store, prom)
    inc = run(eng.acknowledge(iid, "nikhil", "restarting the worker", hours=2, now=100.0))
    assert (inc["acked_by"], inc["ack_note"], inc["ack_until"]) == ("nikhil", "restarting the worker", 100.0 + 7200)
    assert n.kinds == ["firing", "acknowledged"]
    assert "nikhil is on it: restarting the worker" in n.events[-1]["body"] and n.events[-1]["title"].startswith("✋ Acknowledged")
    run(eng.evaluate_rules(200))
    assert n.kinds == ["firing", "acknowledged"]                      # nothing more is sent


def test_reminder_is_suppressed_while_acked_and_resumes_after(cfg, store, prom):
    cfg.alerts.repeat_interval = 600
    eng, n, iid = firing(cfg, store, prom)
    run(eng.acknowledge(iid, "me", hours=1, now=10.0))
    run(eng.evaluate_rules(700))                    # reminder due (>600s) but acked until 3610
    run(eng.evaluate_rules(1500))
    assert "reminder" not in n.kinds
    run(eng.evaluate_rules(3700))                   # ack expired
    assert n.kinds[-1] == "reminder"


def test_ack_until_resolved_never_expires_and_unack_resumes(cfg, store, prom):
    cfg.alerts.repeat_interval = 600
    eng, n, iid = firing(cfg, store, prom)
    run(eng.acknowledge(iid, "me", hours=None, now=10.0))
    assert store.get_incident(iid)["ack_until"] is None and store.get_incident(iid)["acked_at"] == 10.0
    run(eng.evaluate_rules(50_000))
    assert "reminder" not in n.kinds
    assert eng.unacknowledge(iid)["acked_at"] is None
    run(eng.evaluate_rules(50_030))
    assert n.kinds[-1] == "reminder"


def test_getting_worse_cancels_the_ack_and_notifies(cfg, store, prom):
    store.save_rule({"id": "r", "name": "High CPU", "expr": "q", "direction": "above", "warn": 80, "crit": 90,
                     "for_seconds": 0, "unit": "percent", "summary": "busy"})
    prom.values["q"] = [({"name": "api"}, 85.0)]                     # warning
    n = RecordingNotifier()
    eng = Engine(store, prom, n, cfg, clock=lambda: 0.0)
    run(eng.evaluate_rules(0.0))
    iid = store.open_incidents()[0]["id"]
    run(eng.acknowledge(iid, "me", hours=None, now=5.0))
    prom.values["q"] = [({"name": "api"}, 97.0)]                     # now critical
    run(eng.evaluate_rules(30.0))
    assert n.kinds[-1] == "escalated"
    assert store.get_incident(iid)["acked_at"] is None               # whoever was on it must look again


def test_resolved_message_credits_the_person(cfg, store, prom):
    eng, n, iid = firing(cfg, store, prom)
    run(eng.acknowledge(iid, "nikhil", hours=None, now=5.0))
    prom.values["q"] = [({"name": "api"}, 10.0)]
    run(eng.evaluate_rules(60.0))
    assert n.kinds[-1] == "resolved" and "Acknowledged by nikhil." in n.events[-1]["body"]


def test_ack_survives_a_restart(cfg, store, prom):
    cfg.alerts.repeat_interval = 600
    eng, n, iid = firing(cfg, store, prom)
    run(eng.acknowledge(iid, "me", hours=None, now=5.0))
    eng2 = Engine(store, prom, n, cfg, clock=lambda: 0.0)
    before = len(n.kinds)
    run(eng2.evaluate_rules(5000))
    assert len(n.kinds) == before                                   # still acked: no reminder after the restart


def test_only_a_firing_incident_can_be_acknowledged(cfg, store, prom):
    eng, n, iid = firing(cfg, store, prom)
    prom.values["q"] = [({"name": "api"}, 1.0)]
    run(eng.evaluate_rules(30.0))                                    # resolves
    assert run(eng.acknowledge(iid, "me", now=40.0)) is None and eng.unacknowledge(iid) is None
    assert run(eng.acknowledge(9999, "me")) is None


def test_ack_message_goes_to_channels_like_any_other_event(cfg, store, prom):
    seen = []
    store.save_channel({"id": "s", "name": "S", "type": "slack", "settings": {"webhook_url": "https://hooks.slack/x"},
                        "min_severity": "critical", "body_template": "{status} by {acknowledged_by}: {note}"})
    notifier = Notifier(store, "UTC", httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (seen.append(r.read()), httpx.Response(200))[1])))
    eng, _, iid = firing(cfg, store, prom, notifier)
    seen.clear()
    run(eng.acknowledge(iid, "nikhil", "on it", hours=1, now=5.0))
    assert len(seen) == 1 and b"ACKNOWLEDGED by nikhil: on it" in seen[0]


def test_event_text_for_ack():
    ev = build_event("acknowledged", severity="critical", rule_name="High CPU", series="api", value=95.0, threshold=90.0,
                     unit="percent", summary="", started_at=0, now=60, environment="e", ack_by="nikhil", ack_note="")
    assert ev["status"] == "ACKNOWLEDGED" and ev["emoji"] == "✋" and ev["acknowledged_by"] == "nikhil"
    assert "nikhil is on it." in ev["body"]


def test_api_ack_and_unack(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        H = {"X-Requested-With": "nigrani"}
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        iid = c.app.state.store.create_incident(source_id="r", series_key="a", rule_name="X", severity="critical",
                                                started_at=1e10)
        assert c.post(f"/api/incidents/{iid}/ack", json={}).status_code == 403                  # CSRF header required
        r = c.post(f"/api/incidents/{iid}/ack", json={"hours": 4, "note": "  looking  "}, headers=H).json()
        assert r["acked_by"] == "admin" and r["ack_note"] == "looking" and r["ack_until"] > r["acked_at"]
        listed = next(i for i in c.get("/api/incidents?status=firing").json() if i["id"] == iid)
        assert listed["acked_by"] == "admin"
        assert c.post(f"/api/incidents/{iid}/ack", json={"hours": 0}, headers=H).status_code == 422
        assert c.delete(f"/api/incidents/{iid}/ack", headers=H).json()["acked_at"] is None
        c.app.state.store.resolve_incident(iid, 2e10, None)
        assert c.post(f"/api/incidents/{iid}/ack", json={}, headers=H).status_code == 409
        assert c.post("/api/incidents/999/ack", json={}, headers=H).status_code == 409
