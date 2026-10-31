import asyncio
import json
import os
import sqlite3
import tempfile

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani import templates
from nigrani.api import create_app
from nigrani.config import ConfigError, load_config
from nigrani.format import build_event
from nigrani.notifiers import Notifier
from nigrani.store import Store


def event(kind="firing", server="Production"):
    return build_event(kind, severity="critical", rule_name="High CPU", series="web-1", value=93.4, threshold=90.0,
                       unit="percent", summary="Check the Containers page.", started_at=1000.0, now=1000.0 + 125,
                       environment="production", link="https://n.example.com/#/alerts", server=server)


def test_event_carries_everything_a_placeholder_needs():
    ev = event()
    assert (ev["status"], ev["emoji"], ev["value_text"], ev["threshold_text"], ev["duration"]) == \
        ("FIRING", "🔴", "93.4%", "90.0%", "2m")
    assert ev["started_text"].endswith("UTC") and ev["summary"] == "Check the Containers page."
    done = event("resolved")
    assert (done["status"], done["emoji"]) == ("RESOLVED", "✅")


def test_render_fills_every_placeholder_and_leaves_unknown_ones_alone():
    ev = event()
    text = "{emoji} {status} {rule} on {server}/{series}: {value} (limit {threshold}) for {duration} - {summary} {link}"
    assert templates.render(text, ev) == \
        "🔴 FIRING High CPU on Production/web-1: 93.4% (limit 90.0%) for 2m - Check the Containers page. https://n.example.com/#/alerts"
    assert templates.render("{nope} {rule}", ev) == "{nope} High CPU"
    assert templates.render("{title}", ev) == ev["title"] and templates.render("{body}", ev) == ev["body"]


def test_validation():
    assert templates.validate("{rule}: {value}", "{summary}") is None
    assert templates.validate(None, None) is None
    err = templates.validate("{rule} {sevrity}", None)
    assert "{sevrity}" in err and "{severity}" in err        # names the typo and lists the valid ones
    assert "longer than" in templates.validate("x" * 601, None)


def test_apply_only_touches_what_is_templated_and_never_digests():
    ev = event()
    ch = {"title_template": "{rule}!", "body_template": None}
    out = templates.apply(ch, ev)
    assert out["title"] == "High CPU!" and out["body"] == ev["body"]
    assert templates.apply({}, ev) is ev                        # no template: the very same event
    digest = {**ev, "kind": "digest"}
    assert templates.apply({"title_template": "x"}, digest) is digest


def test_each_channel_sends_its_own_text(store):
    seen = {}

    def handler(req):
        seen[str(req.url)] = json.loads(req.content)
        return httpx.Response(200)

    store.save_channel({"id": "slack", "name": "S", "type": "slack", "settings": {"webhook_url": "https://hooks.slack/x"},
                        "min_severity": "info", "title_template": "{emoji} {rule} on {server}",
                        "body_template": "{value} over {threshold} for {duration}. Runbook: https://wiki/cpu"})
    store.save_channel({"id": "plain", "name": "P", "type": "discord", "settings": {"webhook_url": "https://discord/x"},
                        "min_severity": "info"})
    n = Notifier(store, "UTC", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(n.send(event()))
    slack = seen["https://hooks.slack/x"]
    assert slack["text"] == "🔴 High CPU on Production"
    assert "93.4% over 90.0% for 2m. Runbook: https://wiki/cpu" in slack["attachments"][0]["blocks"][0]["text"]["text"]
    discord = seen["https://discord/x"]["embeds"][0]
    assert discord["title"] == event()["title"]                 # the other channel keeps the default text


def test_templates_survive_a_save_round_trip_and_are_not_env_expanded(store, monkeypatch):
    monkeypatch.setenv("HOME_SECRET", "leak")
    store.save_channel({"id": "c", "name": "C", "type": "slack", "settings": {}, "title_template": "{rule} ${HOME_SECRET}"})
    assert store.get_channel("c")["title_template"] == "{rule} ${HOME_SECRET}"


def test_database_from_before_templates_is_migrated():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE channels (id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}',
        min_severity TEXT NOT NULL DEFAULT 'warning', quiet_start INTEGER, quiet_end INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
        last_error TEXT, last_sent_at REAL, servers TEXT NOT NULL DEFAULT '[]');
      INSERT INTO channels(id,name,type) VALUES('old','Old','slack');
    """)
    c.commit()
    c.close()
    s = Store(path)
    ch = s.get_channel("old")
    assert ch["title_template"] is None and ch["body_template"] is None
    s.save_channel({**ch, "title_template": "{rule}"})
    assert s.get_channel("old")["title_template"] == "{rule}"


def test_config_rejects_a_bad_template(tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text("channels:\n  - {id: s, name: S, type: slack, title_template: '{rulee}'}\n")
    with pytest.raises(ConfigError, match="rulee"):
        load_config(str(f))
    f.write_text("channels:\n  - {id: s, name: S, type: slack, title_template: '{rule}', body_template: '{value}'}\n")
    assert load_config(str(f)).channels[0].title_template == "{rule}"


def test_api_validates_previews_and_stores(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        H = {"X-Requested-With": "nigrani"}
        c.post("/api/login", json={"username": "admin", "password": "pw"})

        names = [p["name"] for p in c.get("/api/message-placeholders").json()]
        assert {"rule", "series", "value", "duration", "server"} <= set(names)

        ok = c.post("/api/channels/preview", json={"title_template": "{emoji} {rule}: {value}", "body_template": "{summary}"}, headers=H)
        assert ok.status_code == 200 and ok.json() == {
            "title": "🔴 High CPU: 93.4%", "body": "Check which container is busy on the Containers page."}
        resolved = c.post("/api/channels/preview", json={"title_template": "{status} {rule}", "kind": "resolved"}, headers=H).json()
        assert resolved["title"] == "RESOLVED High CPU"
        bad = c.post("/api/channels/preview", json={"title_template": "{ruel}"}, headers=H)
        assert bad.status_code == 422 and "{ruel}" in bad.json()["detail"]

        saved = c.post("/api/channels", json={"name": "Ops", "type": "slack", "settings": {"webhook_url": "https://h/x"},
                                              "title_template": "{rule}", "body_template": "{value}"}, headers=H).json()
        assert saved["title_template"] == "{rule}" and saved["body_template"] == "{value}"
        assert c.post("/api/channels", json={"name": "Bad", "type": "slack", "title_template": "{nope}"}, headers=H).status_code == 422
        assert next(x for x in c.get("/api/channels").json() if x["id"] == saved["id"])["body_template"] == "{value}"
