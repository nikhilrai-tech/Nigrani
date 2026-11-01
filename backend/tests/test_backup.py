import json
import time

from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.config import load_config

H = {"X-Requested-With": "nigrani"}
SLACK = "https://hooks.slack.com/services/T000/B000/literal-secret-value"


def app_for(tmp_path, name, yaml_text="presets: [core]\n"):
    d = tmp_path / name
    d.mkdir()
    f = d / "n.yaml"
    f.write_text(yaml_text)
    cfg = load_config(str(f))
    cfg.data_dir, cfg.auth.password = str(d), "pw"
    return create_app(cfg, prom=FakeProm())


def login(c):
    c.post("/api/login", json={"username": "admin", "password": "pw"})


def seed(c):
    c.post("/api/rules", json={"name": "Queue deep", "expr": "vector(1)", "warn": 5, "crit": 20, "unit": "short"}, headers=H)
    c.post("/api/channels", json={"id": "slack", "name": "Slack", "type": "slack", "settings": {"webhook_url": SLACK},
                                  "title_template": "{emoji} {rule}", "min_severity": "critical"}, headers=H)
    c.post("/api/channels", json={"id": "tg", "name": "Telegram", "type": "telegram",
                                  "settings": {"bot_token": "${TG_TOKEN}", "chat_id": "42"}}, headers=H)
    c.post("/api/silences", json={"match": "web", "minutes": 60, "reason": "deploy"}, headers=H)


def test_export_masks_literal_secrets_but_keeps_env_references(tmp_path):
    with TestClient(app_for(tmp_path, "a")) as c:
        login(c)
        seed(c)
        data = c.get("/api/backup").json()
        assert data["nigrani_backup"] == 1 and data["includes_secrets"] is False
        slack = next(x for x in data["channels"] if x["id"] == "slack")
        assert slack["settings"]["webhook_url"] is None and slack["secrets_omitted"] == ["webhook_url"]
        assert slack["title_template"] == "{emoji} {rule}" and slack["min_severity"] == "critical"
        tg = next(x for x in data["channels"] if x["id"] == "tg")
        assert tg["settings"] == {"bot_token": "${TG_TOKEN}", "chat_id": "42"} and "secrets_omitted" not in tg
        assert SLACK not in json.dumps(data)                       # the literal secret appears nowhere
        assert any(r["id"] == "queue-deep" for r in data["rules"]) and any(r["id"] == "host-cpu" for r in data["rules"])
        assert [s["match"] for s in data["silences"]] == ["web"]

        full = c.get("/api/backup?include_secrets=true").json()
        assert full["includes_secrets"] is True
        assert next(x for x in full["channels"] if x["id"] == "slack")["settings"]["webhook_url"] == SLACK
        assert "attachment" in c.get("/api/backup").headers["content-disposition"]


def test_dry_run_changes_nothing_and_describes_the_plan(tmp_path):
    with TestClient(app_for(tmp_path, "a")) as a, TestClient(app_for(tmp_path, "b")) as b:
        login(a); login(b); seed(a)
        backup = a.get("/api/backup").json()
        plan = b.post("/api/backup/import", json=backup, headers=H).json()      # dry_run is the default
        assert plan["ok"] and plan["summary"]["rules"]["create"] == 1                 # queue-deep is new; presets exist
        assert plan["summary"]["rules"]["skip"] >= 1 and plan["summary"]["channels"]["create"] == 2
        assert plan["needs_secrets"] == [{"channel": "slack", "keys": ["webhook_url"]}]
        assert b.get("/api/channels").json() == [] and not any(r["id"] == "queue-deep" for r in b.get("/api/rules").json())


def test_round_trip_into_a_fresh_instance(tmp_path):
    with TestClient(app_for(tmp_path, "a")) as a, TestClient(app_for(tmp_path, "b")) as b:
        login(a); login(b); seed(a)
        full = a.get("/api/backup?include_secrets=true").json()
        done = b.post("/api/backup/import?dry_run=false", json=full, headers=H).json()
        assert done["applied"] and done["ok"] and done["needs_secrets"] == []
        chans = {x["id"]: x for x in b.get("/api/channels").json()}
        assert chans["slack"]["enabled"] and chans["slack"]["title_template"] == "{emoji} {rule}"
        assert chans["slack"]["settings"]["webhook_url"].startswith("••••")        # stored; masked in the API
        assert chans["tg"]["settings"]["bot_token"] == "${TG_TOKEN}"
        rule = next(r for r in b.get("/api/rules").json() if r["id"] == "queue-deep")
        assert (rule["warn"], rule["crit"], rule["source"]) == (5, 20, "custom")
        assert [s["match"] for s in b.get("/api/silences").json()] == ["web"]
        again = b.post("/api/backup/import?dry_run=false", json=full, headers=H).json()   # idempotent
        assert again["summary"]["channels"] == {"create": 0, "update": 0, "skip": 2}
        assert again["summary"]["silences"]["create"] == 0 and len(b.get("/api/silences").json()) == 1


def test_secretless_backup_creates_the_channel_disabled_and_keeps_stored_secrets_on_overwrite(tmp_path):
    with TestClient(app_for(tmp_path, "a")) as a, TestClient(app_for(tmp_path, "b")) as b:
        login(a); login(b); seed(a)
        safe = a.get("/api/backup").json()                                          # secrets omitted
        res = b.post("/api/backup/import?dry_run=false", json=safe, headers=H).json()
        assert res["needs_secrets"] == [{"channel": "slack", "keys": ["webhook_url"]}]
        slack = next(x for x in b.get("/api/channels").json() if x["id"] == "slack")
        assert slack["enabled"] is False and "webhook_url" not in slack["settings"]

        # an instance that already has the secret keeps it when the file leaves it out
        a.post("/api/channels", json={"id": "slack", "name": "Slack renamed", "type": "slack", "min_severity": "info",
                                      "settings": {"webhook_url": SLACK}}, headers=H)
        res = a.post("/api/backup/import?dry_run=false&mode=overwrite", json=safe, headers=H).json()
        assert res["needs_secrets"] == []
        after = a.get("/api/backup?include_secrets=true").json()
        assert next(x for x in after["channels"] if x["id"] == "slack")["settings"]["webhook_url"] == SLACK
        assert next(x for x in after["channels"] if x["id"] == "slack")["name"] == "Slack"      # overwritten from the file


def test_merge_never_overwrites_and_overwrite_never_deletes(tmp_path):
    with TestClient(app_for(tmp_path, "a")) as a, TestClient(app_for(tmp_path, "b")) as b:
        login(a); login(b); seed(a)
        b.post("/api/rules", json={"id": "queue-deep", "name": "Mine", "expr": "vector(9)", "crit": 99}, headers=H)
        b.post("/api/rules", json={"name": "Only on b", "expr": "vector(1)", "crit": 1}, headers=H)
        backup = a.get("/api/backup").json()
        merged = b.post("/api/backup/import?dry_run=false", json=backup, headers=H).json()
        assert next(i for i in merged["items"] if i["id"] == "queue-deep")["action"] == "skip"
        assert next(r for r in b.get("/api/rules").json() if r["id"] == "queue-deep")["name"] == "Mine"
        b.post("/api/backup/import?dry_run=false&mode=overwrite", json=backup, headers=H)
        rules = {r["id"]: r for r in b.get("/api/rules").json()}
        assert rules["queue-deep"]["name"] == "Queue deep"           # overwritten
        assert "only-on-b" in rules                                     # but nothing was deleted


def test_one_bad_item_blocks_everything_and_lists_the_problems(tmp_path):
    with TestClient(app_for(tmp_path, "a")) as a, TestClient(app_for(tmp_path, "b")) as b:
        login(a); login(b); seed(a)
        bad = a.get("/api/backup").json()
        bad["rules"].append({"id": "inverted", "name": "X", "expr": "up", "warn": 90, "crit": 10, "for_seconds": 0})
        bad["rules"].append({"id": "elsewhere", "name": "Y", "expr": "up", "crit": 1, "servers": ["mars"]})
        bad["rules"].append({"id": "logs", "name": "Z", "expr": "q", "crit": 1, "datasource": "loki"})
        bad["channels"].append({"id": "typo", "name": "T", "type": "slack", "settings": {}, "title_template": "{rulee}"})
        bad["channels"].append({"id": "weird", "name": "W", "type": "carrier-pigeon", "settings": {}})
        r = b.post("/api/backup/import?dry_run=false", json=bad, headers=H)
        assert r.status_code == 422
        body = r.json()
        assert body["applied"] is False and len(body["errors"]) == 5
        text = " | ".join(body["errors"])
        assert "less severe" in text and "unknown server" in text and "no server has it configured" in text and "{rulee}" in text
        assert b.get("/api/channels").json() == []                     # the good items were not applied either
        assert not any(x["id"] == "queue-deep" for x in b.get("/api/rules").json())


def test_rejects_things_that_are_not_a_backup(tmp_path):
    with TestClient(app_for(tmp_path, "a")) as c:
        login(c)
        for junk in ({}, {"nigrani_backup": 2}, [1, 2], {"nigrani_backup": 1, "rules": "no"}):
            r = c.post("/api/backup/import", json=junk, headers=H).json()
            assert r["ok"] is False and r["errors"]
        assert c.post("/api/backup/import", content=b"not json", headers={**H, "Content-Type": "application/json"}).status_code == 400
        assert c.post("/api/backup/import", json={}).status_code == 403                    # CSRF header required


def test_expired_silences_are_skipped_and_preset_rules_stay_presets(tmp_path):
    with TestClient(app_for(tmp_path, "a")) as c:
        login(c)
        data = c.get("/api/backup").json()
        data["silences"] = [{"server": None, "source_id": None, "match": "old", "until": time.time() - 10, "reason": ""},
                            {"server": None, "source_id": "host-cpu", "match": None, "until": time.time() + 600, "reason": "x"}]
        data["rules"] = [{**next(r for r in data["rules"] if r["id"] == "host-cpu"), "crit": 99}]
        res = c.post("/api/backup/import?dry_run=false&mode=overwrite", json=data, headers=H).json()
        assert res["summary"]["silences"] == {"create": 1, "update": 0, "skip": 1}
        cpu = next(r for r in c.get("/api/rules").json() if r["id"] == "host-cpu")
        assert cpu["crit"] == 99 and cpu["source"] == "preset"     # preset is enabled here, so it stays a preset
        custom = {**data["rules"][0], "id": "not-in-this-config", "source": "preset"}
        c.post("/api/backup/import?dry_run=false", json={"nigrani_backup": 1, "rules": [custom]}, headers=H)
        assert next(r for r in c.get("/api/rules").json() if r["id"] == "not-in-this-config")["source"] == "custom"
