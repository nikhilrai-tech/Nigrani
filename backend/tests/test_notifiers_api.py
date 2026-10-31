import asyncio
import json
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.auth import Auth
from nigrani.format import build_event, fmt_bytes, fmt_duration, fmt_value
from nigrani.notifiers import Notifier, mask_settings


def ev(severity="critical", kind="firing"):
    return build_event(kind, severity=severity, rule_name="High CPU", series="web", value=95.0, threshold=90.0,
                       unit="percent", summary="busy", started_at=0, now=120, environment="prod", link="http://p")


def make_notifier(store, handler):
    return Notifier(store, "UTC", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_formatting():
    assert fmt_value(92.34, "percent") == "92.3%"
    assert fmt_bytes(1536) == "1.5 KB"
    assert fmt_duration(3700) == "1h 1m"
    assert fmt_value(0.42, "seconds") == "420 ms"
    e = ev()
    assert "CRITICAL" in e["title"] and "95.0%" in e["body"] and "threshold 90.0%" in e["body"]
    assert ev(kind="resolved")["title"].startswith("✅ Resolved")


def test_payloads_and_routing(store):
    seen = []

    def handler(req: httpx.Request):
        seen.append((str(req.url), req.content))
        return httpx.Response(200)

    store.save_channel({"id": "s", "name": "S", "type": "slack", "settings": {"webhook_url": "https://hooks.slack/x"}, "min_severity": "warning"})
    store.save_channel({"id": "t", "name": "T", "type": "teams", "settings": {"webhook_url": "https://teams/x"}, "min_severity": "critical"})
    store.save_channel({"id": "g", "name": "G", "type": "telegram", "settings": {"bot_token": "TOK", "chat_id": "1"}, "min_severity": "warning"})
    n = make_notifier(store, handler)
    asyncio.run(n.send(ev("warning")))  # teams only wants critical
    urls = [u for u, _ in seen]
    assert "https://hooks.slack/x" in urls and "https://teams/x" not in urls
    assert any("api.telegram.org/botTOK/sendMessage" in u for u in urls)
    seen.clear()
    asyncio.run(n.send(ev("critical")))
    teams = next(json.loads(b) for u, b in seen if "teams" in u)
    assert teams["attachments"][0]["content"]["type"] == "AdaptiveCard"


def test_quiet_hours_only_mute_non_critical(store):
    seen = []
    store.save_channel({"id": "s", "name": "S", "type": "webhook", "settings": {"url": "https://x/y", "secret": "k"},
                        "min_severity": "info", "quiet_start": 0, "quiet_end": 23})
    n = make_notifier(store, lambda r: (seen.append(r), httpx.Response(200))[1])
    noon = datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp()
    asyncio.run(n.send(ev("warning"), now=noon))
    assert seen == []
    asyncio.run(n.send(ev("critical"), now=noon))
    assert len(seen) == 1 and seen[0].headers["x-nigrani-signature"].startswith("sha256=")


def test_env_secret_expanded_at_send_time(store, monkeypatch):
    monkeypatch.setenv("HOOK", "https://hooks.example/abc")
    seen = []
    store.save_channel({"id": "s", "name": "S", "type": "discord", "settings": {"webhook_url": "${HOOK}"}})
    n = make_notifier(store, lambda r: (seen.append(str(r.url)), httpx.Response(204))[1])
    asyncio.run(n.send(ev()))
    assert seen == ["https://hooks.example/abc"]


def test_failure_recorded_on_channel(store):
    store.save_channel({"id": "s", "name": "S", "type": "slack", "settings": {"webhook_url": "https://h/x"}})
    n = make_notifier(store, lambda r: httpx.Response(500, text="boom"))
    import nigrani.notifiers as pn
    orig = asyncio.sleep

    async def fast(_):  # skip retry backoff
        return None
    pn.asyncio.sleep = fast
    try:
        err = asyncio.run(n.send_to(store.get_channel("s"), ev()))
    finally:
        pn.asyncio.sleep = orig
    assert "HTTP 500" in err and "HTTP 500" in store.get_channel("s")["last_error"]


def test_mask_settings():
    m = mask_settings("slack", {"webhook_url": "https://hooks.slack.com/services/AAA/BBB/secret1234"})
    assert m["webhook_url"].endswith("1234") and "hooks" not in m["webhook_url"]
    assert mask_settings("slack", {"webhook_url": "${HOOK}"})["webhook_url"] == "${HOOK}"


def test_auth_tokens():
    a = Auth("u", "p", "sec", 1)
    t = a.make_token("u")
    assert a.verify_token(t) == "u"
    assert a.verify_token(t[:-1] + ("0" if t[-1] != "0" else "1")) is None  # always a different signature
    assert a.verify_token("garbage") is None
    a.ttl = -10
    assert a.verify_token(a.make_token("u")) is None
    for _ in range(5):
        a.check_login("1.1.1.1", "u", "bad")
    assert a.throttled("1.1.1.1") and not a.throttled("2.2.2.2")


def test_api_flow(cfg, prom):
    prom.metrics = {"django_http_requests_total_by_method_total"}
    cfg.presets = ["core", "django"]
    from nigrani.config import load_config
    import pathlib, tempfile
    f = pathlib.Path(tempfile.mkdtemp()) / "p.yaml"
    f.write_text("presets: [core, django]\n")
    c2 = load_config(str(f))
    c2.data_dir, c2.auth.password = cfg.data_dir, "pw"
    cpu = next(t for t in c2.tiles if t.id == "cpu")
    prom.values[cpu.query] = [({}, 91.0)]

    with TestClient(create_app(c2, prom=prom)) as client:
        H = {"X-Requested-With": "nigrani"}
        assert client.get("/api/health").json()["ok"]
        assert client.get("/api/overview").status_code == 401
        assert client.post("/api/login", json={"username": "admin", "password": "no"}).status_code == 401
        assert client.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 200

        o = client.get("/api/overview").json()
        tile = next(t for t in o["tiles"] if t["id"] == "cpu")
        assert tile["status"] == "crit" and tile["value"] == 91.0

        pages = {p["id"]: p for p in client.get("/api/pages").json()}
        assert pages["traffic"]["available"] is True
        assert client.get("/api/pages/traffic?range=1h").json()["panels"]

        # CSRF header required for writes
        body = {"name": "My rule", "expr": "vector(1)", "crit": 5, "for_seconds": 0}
        assert client.post("/api/rules", json=body).status_code == 403
        r = client.post("/api/rules", json=body, headers=H)
        assert r.status_code == 200 and r.json()["id"] == "my-rule"
        assert client.post("/api/rules", json={**body, "warn": 9}, headers=H).status_code == 422  # warn above crit
        assert client.put("/api/rules/my-rule", json={**body, "crit": 7}, headers=H).json()["crit"] == 7
        assert any(x["id"] == "host-cpu" for x in client.get("/api/rules").json())

        ch = client.post("/api/channels", json={"name": "Ops", "type": "slack",
                         "settings": {"webhook_url": "https://hooks.slack.com/abcdefgh1234"}}, headers=H).json()
        assert ch["settings"]["webhook_url"].startswith("••••")
        again = client.post("/api/channels", json={**ch}, headers=H).json()  # masked value sent back keeps secret
        assert again["settings"]["webhook_url"].endswith("1234")
        from nigrani.api import create_app as _  # noqa
        assert client.post("/api/silences", json={"match": "web", "minutes": 5}, headers=H).status_code == 200
        assert client.get("/api/me").json()["features"]["auth"] is True
