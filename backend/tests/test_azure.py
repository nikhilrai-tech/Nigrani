import asyncio
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.azure import Azure, AzureError, _interval_for, parse
from nigrani.config import AzureCfg, load_config
from nigrani.prom import PromQueryError, series_key

SUB = "sub-1"
NOW = time.time()


def rid(rg, name, kind="Microsoft.Compute/virtualMachines"):
    return f"/subscriptions/{SUB}/resourceGroups/{rg}/providers/{kind}/{name}"


def data(*vals, field="maximum", step=300):
    out = []
    for i, v in enumerate(vals):
        t = datetime.fromtimestamp(NOW - step * (len(vals) - i), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"timeStamp": t, **({field: v} if v is not None else {})})
    return out


class Server:
    """A stand-in for Microsoft Entra ID and Azure Resource Manager (the endpoints and answer shapes we use)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.tokens = 0
        self.expire_next = False
        self.values = {"web-1": [40, 60, 92], "web-2": [10, 12, None], "batch-1": [90, 91, 99]}
        self.rtype_rows = {"Microsoft.Compute/virtualMachines": [
            {"id": rid("prod-rg", "web-1"), "name": "web-1", "type": "Microsoft.Compute/virtualMachines"},
            {"id": rid("prod-rg", "web-2"), "name": "web-2", "type": "Microsoft.Compute/virtualMachines"},
            {"id": rid("dev-rg", "batch-1"), "name": "batch-1", "type": "Microsoft.Compute/virtualMachines"}]}
        self.paged = False
        self.fail: tuple[int, dict] | None = None
        self.secret_ok = True

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        p = req.url.path
        if p.endswith("/oauth2/v2.0/token"):
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            if not self.secret_ok or form["client_secret"] != "s3cret":
                return httpx.Response(401, json={"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret"})
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": 3600, "token_type": "Bearer"})
        if not req.headers.get("authorization", "").startswith("Bearer tok"):
            return httpx.Response(401, json={})
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"error": {"code": "ExpiredAuthenticationToken"}})
        if self.fail:
            return httpx.Response(self.fail[0], json=self.fail[1])
        if p == f"/subscriptions/{SUB}":
            return httpx.Response(200, json={"subscriptionId": SUB})
        if p == f"/subscriptions/{SUB}/resources":
            rtype = req.url.params["$filter"].split("'")[1]
            rows = self.rtype_rows.get(rtype, [])
            if self.paged:
                if "page2" not in req.url.params:
                    return httpx.Response(200, json={"value": rows[:1], "nextLink": f"https://management.azure.com{p}?page2=1&api-version=2021-04-01&%24filter=resourceType%20eq%20%27{rtype}%27"})
                rows = rows[1:]
            return httpx.Response(200, json={"value": rows})
        if p.endswith("/providers/microsoft.insights/metrics"):
            name = p.split("/")[-4]
            if req.url.params["metricnames"] == "nope":
                return httpx.Response(400, json={"error": {"code": "BadRequest", "message": "Failed to find metric configuration for provider"}})
            agg = req.url.params["aggregation"].lower()
            vals = self.values.get(name, [])
            return httpx.Response(200, json={"value": [{"name": {"value": req.url.params["metricnames"]}, "unit": "Percent",
                                                        "timeseries": [{"data": data(*vals, field=agg)}]}]})
        return httpx.Response(404, json={})


def make(server=None, **kw):
    server = server or Server()
    cfg = AzureCfg(tenant_id="tenant", client_id="client", client_secret="s3cret", subscription_id=SUB,
                   authority_url="https://login.test", management_url="https://management.azure.com", **kw)
    return Azure(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler))), server


def run(c):
    return asyncio.run(c)


def by(rows):
    return {r["labels"]["resource"]: r["value"] for r in rows}


def test_parse():
    q = parse('Microsoft.Compute/virtualMachines "Percentage CPU" aggregation=Maximum interval=PT5M resourcegroup=prod-rg resource=web-*')
    assert (q.rtype, q.metric, q.aggregation, q.interval, q.group, q.resource) == ("Microsoft.Compute/virtualMachines", "Percentage CPU", "maximum", "PT5M", "prod-rg", "web-*")
    assert parse("Microsoft.Sql/servers/databases cpu_percent").aggregation == "average"
    for bad in ("", "cpu_percent", "VMs cpu", 'Microsoft.Compute/virtualMachines "unclosed', "Microsoft.X/y m aggregation=Median",
                "Microsoft.X/y m interval=PT2M", "Microsoft.X/y m zone=1"):
        with pytest.raises(PromQueryError):
            parse(bad)


def test_intervals_follow_the_step_but_never_go_below_the_rule():
    assert _interval_for(0, None) == ("PT1M", 60) and _interval_for(200, None) == ("PT5M", 300)
    assert _interval_for(30, "PT15M") == ("PT15M", 900) and _interval_for(100000, None) == ("P1D", 86400)


def test_instant_covers_every_resource_and_uses_the_latest_point():
    c, s = make()
    rows = run(c.instant('Microsoft.Compute/virtualMachines "Percentage CPU" aggregation=Maximum interval=PT5M'))
    assert by(rows) == {"batch-1": 99, "web-1": 92, "web-2": 12}                         # web-2's newest point is empty
    assert {r["labels"]["resource_group"] for r in rows} == {"prod-rg", "dev-rg"}
    m = [r for r in s.requests if r.url.path.endswith("/metrics")][0]
    assert m.url.params["metricnames"] == "Percentage CPU" and m.url.params["aggregation"] == "Maximum" and m.url.params["interval"] == "PT5M"
    start, end = m.url.params["timespan"].split("/")
    assert datetime.fromisoformat(end.replace("Z", "+00:00")) > datetime.fromisoformat(start.replace("Z", "+00:00"))
    list_req = [r for r in s.requests if r.url.path.endswith("/resources")][0]
    assert list_req.url.params["$filter"] == "resourceType eq 'Microsoft.Compute/virtualMachines'"


def test_filters_by_group_name_and_configured_groups():
    c, _ = make()
    assert set(by(run(c.instant('Microsoft.Compute/virtualMachines cpu resourcegroup=prod-rg')))) == {"web-1", "web-2"}
    assert set(by(run(c.instant('Microsoft.Compute/virtualMachines "Percentage CPU" resourcegroup=PROD-RG resource=web-1')))) == {"web-1"}
    c2, _ = make(resource_groups=["dev-rg"])
    assert set(by(run(c2.instant('Microsoft.Compute/virtualMachines "Percentage CPU"')))) == {"batch-1"}


def test_listing_is_followed_across_pages_and_cached():
    c, s = make()
    s.paged = True
    assert len(run(c.instant('Microsoft.Compute/virtualMachines "Percentage CPU"'))) == 3
    n = len([r for r in s.requests if r.url.path.endswith("/resources") or "page2" in str(r.url)])
    run(c.instant('Microsoft.Compute/virtualMachines "Percentage CPU"'))
    assert len([r for r in s.requests if r.url.path.endswith("/resources") or "page2" in str(r.url)]) == n    # cached for five minutes


def test_range_returns_points():
    c, _ = make()
    rows = run(c.range('Microsoft.Compute/virtualMachines "Percentage CPU" resource=web-1 aggregation=Maximum', NOW - 3600, NOW, 300))
    assert rows[0]["labels"] == {"resource": "web-1", "resource_group": "prod-rg"} and [p[1] for p in rows[0]["points"]] == [40, 60, 92]


def test_the_token_is_reused_and_refreshed_after_a_401():
    c, s = make()
    run(c.instant('Microsoft.Compute/virtualMachines "Percentage CPU"'))
    run(c.instant('Microsoft.Compute/virtualMachines "Percentage CPU"'))
    assert s.tokens == 1
    s.expire_next = True
    assert len(run(c.instant('Microsoft.Compute/virtualMachines "Percentage CPU"'))) == 3 and s.tokens == 2


def test_refused_credentials_and_roles_are_readable_infrastructure_errors():
    c, s = make()
    s.secret_ok = False
    with pytest.raises(AzureError, match="client_secret") as e:
        run(c.instant('Microsoft.Compute/virtualMachines "Percentage CPU"'))
    assert "invalid_client" in str(e.value) and "s3cret" not in str(e.value)
    c2, s2 = make()
    s2.fail = (403, {"error": {"code": "AuthorizationFailed"}})
    with pytest.raises(AzureError, match="Monitoring Reader"):
        run(c2.instant('Microsoft.Compute/virtualMachines "Percentage CPU"'))
    for status, word in ((429, "rate limiting"), (503, "HTTP 503")):
        s2.fail = (status, {})
        with pytest.raises(AzureError, match=word):
            run(c2.instant('Microsoft.Compute/virtualMachines "Percentage CPU"'))


def test_a_bad_metric_name_is_a_query_error():
    c, _ = make()
    with pytest.raises(PromQueryError, match="Failed to find metric"):
        run(c.instant("Microsoft.Compute/virtualMachines nope"))


def test_a_type_with_no_resources_is_just_empty():
    c, _ = make()
    assert run(c.instant("Microsoft.Web/serverFarms CpuPercentage")) == []


def test_not_json_means_a_wrong_management_url():
    def h(req):
        if "oauth2" in req.url.path:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, text="<html>")
    c = Azure(AzureCfg(tenant_id="t", client_id="c", client_secret="s", subscription_id=SUB), client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    with pytest.raises(AzureError, match="management_url"):
        run(c.instant('Microsoft.Compute/virtualMachines "Percentage CPU"'))


def test_healthy_and_config(tmp_path):
    c, s = make()
    assert run(c.healthy()) is True
    s.secret_ok = False
    c2, s2 = make(); s2.secret_ok = False
    assert run(c2.healthy()) is False
    assert not AzureCfg(tenant_id="a").enabled and AzureCfg(tenant_id="a", client_id="b", client_secret="c", subscription_id="d").enabled
    with pytest.raises(ValueError):
        AzureCfg(management_url="ftp://x")
    f = tmp_path / "n.yaml"
    f.write_text("azure: {tenant_id: t, client_id: c, client_secret: s, subscription_id: one}\nservers:\n  - {id: a}\n  - {id: b, azure: {tenant_id: t, client_id: c, client_secret: s, subscription_id: two}}\n")
    v = {x.id: x for x in load_config(str(f)).server_views()}
    assert v["a"].azure.subscription_id == "one" and v["b"].azure.subscription_id == "two"


def app_with(tmp_path, az):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    return TestClient(create_app(cfg, prom=FakeProm(), azure={"default": az}))


def test_api_rule_preview_incident_chart_and_missing_config(tmp_path):
    az, _ = make()
    H = {"X-Requested-With": "nigrani"}
    with app_with(tmp_path, az) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert c.get("/api/me").json()["servers"][0]["datasources"] == ["prometheus", "azure"]
        body = {"name": "VM CPU", "expr": 'Microsoft.Compute/virtualMachines "Percentage CPU" aggregation=Maximum', "warn": 80, "crit": 95, "datasource": "azure"}
        prev = c.post("/api/rules/test", json=body, headers=H).json()
        got = {x["series"]: (x["value"], x["severity"]) for x in prev["series"]}
        assert got == {"web-1 prod-rg": (92, "warning"), "web-2 prod-rg": (12, None), "batch-1 dev-rg": (99, "critical")}
        made = c.post("/api/rules", json=body, headers=H).json()
        labels = {"resource": "web-1", "resource_group": "prod-rg"}
        iid = c.app.state.store.create_incident(source_id=made["id"], series_key=series_key(labels), rule_name="VM CPU", series="web-1",
                                                labels=labels, severity="warning", started_at=time.time() - 60, server="default")
        d = c.get(f"/api/incidents/{iid}").json()
        assert d["chart"]["series"][0]["points"] and d["problems"] == []
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path / "x"), "pw"
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        r = c.post("/api/rules", json={"name": "A", "expr": "Microsoft.X/y m", "crit": 1, "datasource": "azure"}, headers=H)
        assert r.status_code == 422 and "azure" in r.json()["detail"]


def test_the_azure_preset_loads(tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text("presets: [azure]\n")
    rules = {r.id: r for r in load_config(str(f)).rules}
    assert len(rules) == 6 and all(r.datasource == "azure" for r in rules.values())
    for r in rules.values():
        parse(r.expr)
