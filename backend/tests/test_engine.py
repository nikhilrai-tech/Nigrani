import asyncio

from conftest import RecordingNotifier
from nigrani.engine import Engine, Signal, classify


def run(c):
    return asyncio.run(c)


def make(cfg, store, prom, clock):
    n = RecordingNotifier()
    store.save_rule({"id": "r", "name": "High CPU", "expr": "q", "direction": "above", "warn": 80, "crit": 90,
                     "for_seconds": 300, "unit": "percent", "summary": "s"})
    return Engine(store, prom, n, cfg, clock=lambda: clock[0]), n


def test_classify():
    assert classify(95, "above", 80, 90) == ("critical", 90)
    assert classify(85, "above", 80, 90) == ("warning", 80)
    assert classify(10, "above", 80, 90) == (None, None)
    assert classify(1, "below", 7, 2) == ("critical", 2)
    assert classify(5, "below", 7, 2) == ("warning", 7)


def test_pending_then_fire_then_resolve(cfg, store, prom):
    clock = [1000.0]
    eng, n = make(cfg, store, prom, clock)
    prom.values["q"] = [({"instance": "a"}, 95.0)]

    run(eng.evaluate_rules(clock[0]))
    assert n.kinds == []  # pending: must persist for 300s
    clock[0] += 301
    run(eng.evaluate_rules(clock[0]))
    assert n.kinds == ["firing"] and n.events[0]["severity"] == "critical"
    assert len(store.open_incidents()) == 1

    clock[0] += 60
    run(eng.evaluate_rules(clock[0]))
    assert n.kinds == ["firing"]  # deduped, no repeat

    prom.values["q"] = [({"instance": "a"}, 10.0)]
    clock[0] += 30
    run(eng.evaluate_rules(clock[0]))
    assert n.kinds == ["firing", "resolved"]
    assert store.open_incidents() == []
    assert store.recent_incidents()[0]["status"] == "resolved"


def test_blip_shorter_than_for_never_fires(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    prom.values["q"] = [({}, 95.0)]
    run(eng.evaluate_rules(clock[0]))
    prom.values["q"] = [({}, 5.0)]
    clock[0] += 100
    run(eng.evaluate_rules(clock[0]))
    prom.values["q"] = [({}, 95.0)]
    clock[0] += 250
    run(eng.evaluate_rules(clock[0]))
    assert n.kinds == []  # pending restarted after recovery


def test_escalation_and_reminder(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    prom.values["q"] = [({}, 85.0)]
    run(eng.evaluate_rules(clock[0]))
    clock[0] += 301
    run(eng.evaluate_rules(clock[0]))
    assert n.kinds == ["firing"] and n.events[0]["severity"] == "warning"
    prom.values["q"] = [({}, 95.0)]
    clock[0] += 30
    run(eng.evaluate_rules(clock[0]))
    assert n.kinds[-1] == "escalated"
    clock[0] += cfg.alerts.repeat_interval + 1
    run(eng.evaluate_rules(clock[0]))
    assert n.kinds[-1] == "reminder"


def test_silence_suppresses_notifications_but_records_incident(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    store.add_silence("r", None, 10_000, "maintenance")
    prom.values["q"] = [({}, 95.0)]
    run(eng.evaluate_rules(clock[0]))
    clock[0] += 301
    run(eng.evaluate_rules(clock[0]))
    assert n.kinds == []
    inc = store.open_incidents()
    assert len(inc) == 1 and inc[0]["silenced"]


def test_restart_does_not_renotify(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    prom.values["q"] = [({}, 95.0)]
    run(eng.evaluate_rules(clock[0]))
    clock[0] += 301
    run(eng.evaluate_rules(clock[0]))
    assert n.kinds == ["firing"]
    eng2 = Engine(store, prom, n, cfg, clock=lambda: clock[0])  # simulated restart
    clock[0] += 30
    run(eng2.evaluate_rules(clock[0]))
    assert n.kinds == ["firing"]


def test_resolved_message_after_a_restart_keeps_rule_and_series(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    prom.values["q"] = [({"name": "api"}, 95.0)]
    run(eng.evaluate_rules(clock[0]))
    clock[0] += 301
    run(eng.evaluate_rules(clock[0]))
    eng2 = Engine(store, prom, n, cfg, clock=lambda: clock[0])      # simulated restart
    prom.values["q"] = [({"name": "api"}, 10.0)]
    clock[0] += 30
    run(eng2.evaluate_rules(clock[0]))
    resolved = n.events[-1]
    assert resolved["kind"] == "resolved"
    assert "High CPU" in resolved["title"] and "api" in resolved["title"] and "Now: 10.0%" in resolved["body"]


def test_vanished_series_resolves_after_grace(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    prom.values["q"] = [({"name": "x"}, 95.0)]
    run(eng.evaluate_rules(clock[0]))
    clock[0] += 301
    run(eng.evaluate_rules(clock[0]))
    prom.values["q"] = []
    clock[0] += 60
    run(eng.evaluate_rules(clock[0]))
    assert store.open_incidents()  # still within grace
    clock[0] += cfg.alerts.absent_grace
    run(eng.evaluate_rules(clock[0]))
    assert store.open_incidents() == [] and n.kinds[-1] == "resolved"


def test_prometheus_outage_alerts_after_three_failures(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    prom.up = False
    for _ in range(3):
        run(eng.prometheus_health(clock[0]))
        clock[0] += 30
    assert n.kinds == ["firing"] and "Prometheus" in n.events[0]["rule"]
    prom.up = True
    run(eng.prometheus_health(clock[0]))
    assert n.kinds[-1] == "resolved"


def test_prom_error_does_not_resolve_firing(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    prom.values["q"] = [({}, 95.0)]
    run(eng.evaluate_rules(clock[0]))
    clock[0] += 301
    run(eng.evaluate_rules(clock[0]))
    prom.up = False
    clock[0] += 600
    run(eng.evaluate_rules(clock[0]))
    assert len(store.open_incidents()) == 1 and "r" in eng.rule_errors


def test_failed_delivery_is_retried_next_cycle(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    results = [{"wanted": 1, "ok": 0}, {"wanted": 1, "ok": 0}, {"wanted": 1, "ok": 1}]

    async def flaky(ev, *, only=None, now=None):
        n.events.append(ev)
        return results.pop(0) if results else {"wanted": 1, "ok": 1}

    n.send = flaky
    prom.values["q"] = [({}, 95.0)]
    run(eng.evaluate_rules(clock[0]))
    clock[0] += 301
    run(eng.evaluate_rules(clock[0]))          # firing -> all channels fail
    clock[0] += 30
    run(eng.evaluate_rules(clock[0]))          # retry -> fails again
    clock[0] += 30
    run(eng.evaluate_rules(clock[0]))          # retry -> delivered
    clock[0] += 30
    run(eng.evaluate_rules(clock[0]))          # nothing more to send
    assert n.kinds == ["firing", "firing", "firing"]


def test_dropping_a_rule_closes_its_incidents_quietly(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    prom.values["q"] = [({}, 95.0)]
    run(eng.evaluate_rules(clock[0]))
    clock[0] += 301
    run(eng.evaluate_rules(clock[0]))
    assert len(store.open_incidents()) == 1
    eng.drop_rule("r", clock[0])
    assert store.open_incidents() == [] and n.kinds == ["firing"]


def test_external_signals_resolve_when_dropped(cfg, store, prom):
    clock = [0.0]
    eng, n = make(cfg, store, prom, clock)
    sig = Signal("svc:container-down", "Container down", "api", "api", "critical", for_seconds=0)
    run(eng.sync_external("svc:", [sig], 0.0))
    assert n.kinds == ["firing"]
    run(eng.sync_external("svc:", [], 30.0))  # container forgotten
    assert n.kinds == ["firing", "resolved"]
