import json
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.config import ConfigError, GrafanaCfg, load_config
from nigrani.grafana import dashboard_links, explore_url

G = GrafanaCfg(url="https://grafana.example.com/", prometheus_datasource="prom-uid", loki_datasource="loki-uid", dashboards=[
    {"title": "Containers", "url": "/d/abc/containers?var-container={container}&from={from}&to={to}", "where": ["incident", "container"]},
    {"title": "Host", "url": "/d/xyz/node", "where": ["host", "overview"]},
    {"title": "External", "url": "https://other.example.com/d/q?server={server}", "where": ["incident"]},
])


def test_explore_url_carries_the_query_datasource_and_range():
    url = explore_url(G, "prometheus", 'up{job="a b"} == 0', 1_700_000_000, 1_700_003_600)
    assert url.startswith("https://grafana.example.com/explore?schemaVersion=1&panes=")
    pane = json.loads(unquote(parse_qs(urlparse(url).query)["panes"][0]))["a"]
    assert pane["datasource"] == "prom-uid" and pane["range"] == {"from": "1700000000000", "to": "1700003600000"}
    q = pane["queries"][0]
    assert q["expr"] == 'up{job="a b"} == 0' and q["datasource"] == {"type": "prometheus", "uid": "prom-uid"}
    logs = json.loads(unquote(parse_qs(urlparse(explore_url(G, "loki", '{c="x"} |= "e"', 1, 2)).query)["panes"][0]))["a"]
    assert logs["datasource"] == "loki-uid" and logs["queries"][0]["queryType"] == "range"


def test_explore_needs_a_url_a_datasource_and_a_query():
    assert explore_url(GrafanaCfg(), "prometheus", "up", 1, 2) is None
    assert explore_url(GrafanaCfg(url="https://g"), "prometheus", "up", 1, 2) is None        # no datasource uid
    assert explore_url(G, "prometheus", "", 1, 2) is None


def test_dashboard_links_fill_and_encode_placeholders():
    links = dashboard_links(G, "incident", container="my app/1", server="prod", **{"from": 100, "to": 200})
    assert links[0] == {"title": "Containers",
                        "url": "https://grafana.example.com/d/abc/containers?var-container=my%20app%2F1&from=100000&to=200000"}
    assert links[1] == {"title": "External", "url": "https://other.example.com/d/q?server=prod"}      # absolute stays as is


def test_links_that_need_a_missing_value_are_left_out_and_context_filters():
    assert [l["title"] for l in dashboard_links(G, "incident", container=None, server="prod", **{"from": 1, "to": 2})] == ["External"]
    assert [l["title"] for l in dashboard_links(G, "host", server="prod")] == ["Host"]
    assert [l["title"] for l in dashboard_links(G, "overview")] == ["Host"]
    assert dashboard_links(GrafanaCfg(), "host") == []


def test_config_validation(tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text("grafana: {url: 'ftp://nope'}\n")
    with pytest.raises(ConfigError, match="http"):
        load_config(str(f))
    f.write_text("grafana: {url: 'https://g', dashboards: [{title: T, url: '/d/x?v={contanier}'}]}\n")
    with pytest.raises(ConfigError, match="contanier"):
        load_config(str(f))
    f.write_text("grafana: {url: 'https://g', dashboards: [{title: T, url: '/d/x?v={container}', where: [incident, container]}]}\n"
                 "servers:\n  - {id: a}\n  - {id: b, grafana: {url: 'https://other'}}\n")
    a, b = load_config(str(f)).server_views()
    assert a.grafana.url == "https://g" and b.grafana.url == "https://other" and b.grafana.dashboards == []


def test_api_endpoint_and_incident_payload(tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text("auth: {password: pw}\ngrafana:\n  url: https://g.example.com\n  prometheus_datasource: p\n"
                 "  dashboards:\n    - {title: Containers, url: '/d/c?var-c={container}', where: [incident, container]}\n"
                 "    - {title: Host, url: '/d/h', where: [host]}\n")
    cfg = load_config(str(f))
    cfg.data_dir = str(tmp_path)
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert c.get("/api/me").json()["servers"][0]["grafana"] is True
        r = c.get("/api/grafana-links?context=container&container=api").json()
        assert r["enabled"] and r["explore"] is None and r["links"] == [{"title": "Containers", "url": "https://g.example.com/d/c?var-c=api"}]
        assert [l["title"] for l in c.get("/api/grafana-links?context=host").json()["links"]] == ["Host"]
        e = c.get("/api/grafana-links?expr=up&minutes=30").json()
        assert e["explore"].startswith("https://g.example.com/explore?")
        assert c.get("/api/grafana-links?container=bad%20name").status_code == 400

        store = c.app.state.store
        store.save_rule({"id": "cpu", "name": "High CPU", "expr": "up == 0", "direction": "above", "crit": 1, "for_seconds": 0, "unit": "short"})
        iid = store.create_incident(source_id="cpu", series_key="name=api", rule_name="High CPU", series="api",
                                    labels={"name": "api"}, severity="critical", started_at=1e9)
        g = c.get(f"/api/incidents/{iid}").json()["grafana"]
        assert g["explore"].startswith("https://g.example.com/explore?") and "up%20%3D%3D%200" in g["explore"]
        assert g["links"] == [{"title": "Containers", "url": "https://g.example.com/d/c?var-c=api"}]


def test_without_grafana_nothing_is_offered(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert c.get("/api/me").json()["servers"][0]["grafana"] is False
        assert c.get("/api/grafana-links?expr=up").json() == {"enabled": False, "explore": None, "links": []}
