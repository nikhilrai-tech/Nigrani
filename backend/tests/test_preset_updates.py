from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.config import load_config
from nigrani.fingerprint import fingerprint
from nigrani.legacy_presets import SHIPPED_IN_0_1_0
from nigrani.store import Store

# container-restarts exactly as v0.1.0 shipped it (it counted changes of the start time, which docker's in-place restarts
# never touch)
OLD = {"id": "container-restarts", "name": "Container restarting", "expr": 'max by (name) (changes(container_start_time_seconds{name!=""}[10m]))',
       "direction": "above", "warn": 2, "crit": 4, "for_seconds": 0, "unit": "short", "summary": "old text", "enabled": True,
       "servers": [], "datasource": "prometheus"}
NEW = {**OLD, "expr": "count by (name) (...) - 1", "summary": "new text"}


def test_the_embedded_fingerprints_match_what_v0_1_0_shipped():
    assert fingerprint(OLD) in SHIPPED_IN_0_1_0 and len(SHIPPED_IN_0_1_0) == 19
    cfg = load_config(None)
    now = next(r for r in cfg.rules if r.id == "container-restarts")
    assert fingerprint(now.model_dump()) != fingerprint(OLD)                 # the preset really changed
    assert "container_label_restartcount" in now.expr


def test_unedited_preset_rules_follow_the_preset(store):
    assert store.sync_preset_rule(OLD) == "created"
    assert store.sync_preset_rule(OLD) == "kept"
    store.save_rule({**store.get_rule("container-restarts"), "enabled": False, "servers": []})   # a toggle is not an edit
    assert store.sync_preset_rule(NEW) == "updated"
    row = store.get_rule("container-restarts")
    assert row["expr"] == NEW["expr"] and row["summary"] == "new text"
    assert row["enabled"] is False                                              # the user's switch survives
    assert store.sync_preset_rule(NEW) == "kept"                                # and it is now in sync


def test_an_edited_preset_rule_is_never_overwritten(store):
    store.sync_preset_rule(OLD)
    store.save_rule({**store.get_rule("container-restarts"), "warn": 10, "crit": 50})            # the user tuned it
    assert store.sync_preset_rule(NEW) == "kept"
    assert store.get_rule("container-restarts")["crit"] == 50 and store.get_rule("container-restarts")["expr"] == OLD["expr"]


def test_rows_from_v0_1_0_have_no_hash_and_are_judged_by_what_was_shipped(store):
    store.seed_rule(OLD)                                                         # how v0.1.0 stored it: no preset_hash
    assert store.get_rule("container-restarts")["preset_hash"] is None
    assert store.sync_preset_rule(NEW, SHIPPED_IN_0_1_0) == "updated"           # still the shipped definition: upgraded

    store2 = Store(":memory:")
    store2.seed_rule({**OLD, "crit": 7})                                         # someone edited it in v0.1.0
    assert store2.sync_preset_rule(NEW, SHIPPED_IN_0_1_0) == "kept"
    assert store2.get_rule("container-restarts")["crit"] == 7


def test_custom_rules_are_never_touched(store):
    store.save_rule({**OLD, "source": "custom"})
    assert store.sync_preset_rule(NEW, SHIPPED_IN_0_1_0) == "kept"
    assert store.get_rule("container-restarts")["expr"] == OLD["expr"]


def test_upgrading_an_old_install_fixes_the_rule_on_startup(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    Store(str(tmp_path / "nigrani.db")).seed_rule(OLD)                          # an install created by v0.1.0
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        rule = next(r for r in c.get("/api/rules").json() if r["id"] == "container-restarts")
        assert "container_label_restartcount" in rule["expr"] and rule["warn"] == 2
        untouched = next(r for r in c.get("/api/rules").json() if r["id"] == "host-cpu")
        assert untouched["source"] == "preset"
