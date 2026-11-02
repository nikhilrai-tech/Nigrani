import pytest

from nigrani.config import PRESET_DIR, ConfigError, load_config


def test_all_presets_load_and_substitute():
    names = [p.stem for p in PRESET_DIR.glob("*.yaml")]
    assert {"core", "django", "redis", "postgres", "rds", "mqtt", "celery"} <= set(names)
    cfg = load_config(None)
    cfg_all = cfg.model_copy()
    import tempfile, pathlib
    f = pathlib.Path(tempfile.mkdtemp()) / "p.yaml"
    f.write_text("presets: [" + ", ".join(names) + "]\n")
    cfg_all = load_config(str(f))
    for r in cfg_all.rules:
        assert "{{fs}}" not in r.expr
    assert any(r.id == "host-disk" and 'mountpoint!~"' in r.expr for r in cfg_all.rules)
    assert {p.id for p in cfg_all.pages} >= {"traffic", "redis", "postgres", "mqtt", "celery", "rds"}


def test_example_config_is_valid():
    from pathlib import Path
    cfg = load_config(str(Path(__file__).resolve().parents[2] / "nigrani.example.yaml"), explicit=True)
    assert {"core", "django", "redis", "celery"} == set(cfg.presets)
    assert any(r.id == "queue-depth" for r in cfg.rules)
    assert any(p.id == "devices" for p in cfg.pages)
    assert [c.type for c in cfg.channels] == ["slack", "teams"]


def test_multi_server_example_is_valid():
    from pathlib import Path
    cfg = load_config(str(Path(__file__).resolve().parents[2] / "examples" / "multi-server.yaml"), explicit=True)
    prod, staging = cfg.server_views()
    assert (prod.id, staging.id) == ("prod", "staging")
    assert prod.datasources.loki and not staging.datasources.loki
    assert [c.servers for c in cfg.channels] == [["prod"], []]
    assert next(r for r in cfg.rules if r.id == "staging-queue").servers == ["staging"]


def test_user_overrides_preset_by_id(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("rules:\n  - {id: host-cpu, name: Custom CPU, expr: vector(1), crit: 50}\n")
    cfg = load_config(str(f))
    r = next(r for r in cfg.rules if r.id == "host-cpu")
    assert r.name == "Custom CPU" and r.crit == 50


def test_disable_ids(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("disable: {rules: [host-cpu]}\n")
    assert all(r.id != "host-cpu" for r in load_config(str(f)).rules)


def test_typo_is_rejected(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("datasource: {prometheus: http://x}\n")
    with pytest.raises(ConfigError):
        load_config(str(f))


def test_unknown_preset(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("presets: [nope]\n")
    with pytest.raises(ConfigError, match="unknown preset"):
        load_config(str(f))


def test_env_expansion_but_channels_stay_literal(tmp_path, monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://prom:9090")
    f = tmp_path / "p.yaml"
    f.write_text(
        "datasources: {prometheus: '${PROM_URL}'}\n"
        "channels:\n  - {id: s, name: S, type: slack, settings: {webhook_url: '${SLACK}'}}\n")
    cfg = load_config(str(f))
    assert cfg.datasources.prometheus == "http://prom:9090"
    assert cfg.channels[0].settings["webhook_url"] == "${SLACK}"


def test_panel_shorthand():
    from nigrani.config import PanelCfg
    p = PanelCfg(title="t", expr="up", legend="x")
    assert p.queries[0].expr == "up"
