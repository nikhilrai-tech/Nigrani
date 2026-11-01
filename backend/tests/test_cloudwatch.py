import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProm
from nigrani.api import create_app
from nigrani.cloudwatch import CloudWatch, CloudWatchError, parse
from nigrani.config import CloudWatchCfg, ConfigError, load_config
from nigrani.prom import PromQueryError
from nigrani.sigv4 import Credentials, sign

NS = "http://monitoring.amazonaws.com/doc/2010-08-01/"
CREDS = dict(access_key_id="AKIATEST", secret_access_key="s3cret/key")


def metric(*dims):
    inner = "".join(f"<member><Name>{k}</Name><Value>{v}</Value></member>" for k, v in dims)
    return f"<member><Namespace>AWS/RDS</Namespace><MetricName>CPUUtilization</MetricName><Dimensions>{inner}</Dimensions></member>"


def xml(body):
    return httpx.Response(200, text=body, headers={"content-type": "text/xml"})


class Server:
    """A stand-in for the CloudWatch Query API (ListMetrics and GetMetricData)."""

    def __init__(self):
        self.requests: list[tuple[httpx.Request, dict[str, str]]] = []
        self.dbs = {"db-a": [(1000.0, 40.0), (1060.0, 95.5)], "db-b": [(1000.0, 10.0), (1060.0, 11.0)]}
        self.error: tuple[int, str, str] | None = None
        self.pages = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        self.requests.append((req, form))
        if self.error:
            status, code, msg = self.error
            return httpx.Response(status, text=f"<ErrorResponse><Error><Type>Sender</Type><Code>{code}</Code><Message>{msg}</Message></Error></ErrorResponse>")
        if form["Action"] == "ListMetrics":
            rows = [metric(("DBInstanceIdentifier", n)) for n in self.dbs] + [metric(("DBInstanceIdentifier", "x"), ("Role", "WRITER"))]
            if self.pages and "NextToken" not in form:
                return xml(f"<ListMetricsResponse xmlns='{NS}'><ListMetricsResult><Metrics>{rows[0]}</Metrics><NextToken>t2</NextToken></ListMetricsResult></ListMetricsResponse>")
            if self.pages:
                rows = rows[1:]
            return xml(f"<ListMetricsResponse xmlns='{NS}'><ListMetricsResult><Metrics>{''.join(rows)}</Metrics></ListMetricsResult></ListMetricsResponse>")
        out = ""
        i = 1
        while f"MetricDataQueries.member.{i}.Id" in form:
            p = f"MetricDataQueries.member.{i}.MetricStat.Metric.Dimensions.member.1.Value"
            pts = self.dbs.get(form[p], [])
            ts = "".join(f"<member>{datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}</member>" for t, _ in pts)
            vs = "".join(f"<member>{v}</member>" for _, v in pts)
            out += f"<member><Id>m{i}</Id><Label>x</Label><Timestamps>{ts}</Timestamps><Values>{vs}</Values><StatusCode>Complete</StatusCode></member>"
            i += 1
        return xml(f"<GetMetricDataResponse xmlns='{NS}'><GetMetricDataResult><MetricDataResults>{out}</MetricDataResults></GetMetricDataResult></GetMetricDataResponse>")


def make(server=None, **kw):
    server = server or Server()
    cfg = CloudWatchCfg(region="ap-south-1", endpoint_url="http://cw.test", **{**CREDS, **kw})
    return CloudWatch(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler))), server


def run(c):
    return asyncio.run(c)


# ---- signing ----
def test_sigv4_matches_the_aws_test_vector():
    h = sign("GET", "https://example.amazonaws.com/", {}, b"", region="us-east-1", service="service",
             creds=Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"),
             now=datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc))
    assert h["Authorization"] == ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
                                  "SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")


def test_sigv4_signs_the_session_token_and_body():
    h = sign("POST", "https://monitoring.ap-south-1.amazonaws.com/", {"Content-Type": "application/x-www-form-urlencoded"}, b"a=1",
             region="ap-south-1", service="monitoring", creds=Credentials("AK", "SK", "TOKEN"))
    assert h["X-Amz-Security-Token"] == "TOKEN" and "x-amz-security-token" in h["Authorization"]
    other = sign("POST", "https://monitoring.ap-south-1.amazonaws.com/", {"Content-Type": "application/x-www-form-urlencoded"}, b"a=2",
                 region="ap-south-1", service="monitoring", creds=Credentials("AK", "SK", "TOKEN"))
    assert h["Authorization"].split("Signature=")[1] != other["Authorization"].split("Signature=")[1] or h["X-Amz-Date"] != other["X-Amz-Date"]


# ---- expressions ----
def test_parse_expressions():
    q = parse('AWS/RDS CPUUtilization{DBInstanceIdentifier="*"} stat=Maximum period=60')
    assert (q.namespace, q.metric, q.dims, q.stat, q.period) == ("AWS/RDS", "CPUUtilization", [("DBInstanceIdentifier", "*")], "Maximum", 60)
    q = parse("AWS/SQS NumberOfMessagesSent")
    assert q.dims == [] and q.stat == "Average" and q.period == 300
    assert parse('AWS/ApplicationELB HTTPCode_Target_5XX_Count{LoadBalancer="app/x/1", TargetGroup="tg"} stat=Sum').dims == [("LoadBalancer", "app/x/1"), ("TargetGroup", "tg")]
    for bad in ("", "CPUUtilization", 'AWS/RDS CPU{DB=x}', "AWS/RDS CPU period=61", "AWS/RDS CPU stat=Median", "AWS/RDS CPU nonsense"):
        with pytest.raises(PromQueryError):
            parse(bad)


# ---- queries ----
def test_instant_expands_wildcards_and_takes_the_latest_point():
    c, s = make()
    rows = run(c.instant('AWS/RDS CPUUtilization{DBInstanceIdentifier="*"} stat=Maximum'))
    assert rows == [{"labels": {"DBInstanceIdentifier": "db-a"}, "value": 95.5}, {"labels": {"DBInstanceIdentifier": "db-b"}, "value": 11.0}]
    form = s.requests[-1][1]
    assert form["Action"] == "GetMetricData" and form["MetricDataQueries.member.1.MetricStat.Stat"] == "Maximum"
    assert form["MetricDataQueries.member.1.MetricStat.Period"] == "300"
    listed = s.requests[0][1]
    assert listed["Action"] == "ListMetrics" and listed["Dimensions.member.1.Name"] == "DBInstanceIdentifier" and "Dimensions.member.1.Value" not in listed


def test_the_wildcard_skips_metrics_with_extra_dimensions_and_follows_pages():
    c, s = make()
    s.pages = True
    names = [r["labels"]["DBInstanceIdentifier"] for r in run(c.instant('AWS/RDS CPUUtilization{DBInstanceIdentifier="*"}'))]
    assert names == ["db-a", "db-b"]  # the "x"/WRITER metric has another dimension set, so it is a different series


def test_a_named_resource_needs_no_listing_and_a_missing_one_has_no_value():
    c, s = make()
    assert run(c.instant('AWS/RDS CPUUtilization{DBInstanceIdentifier="db-b"}'))[0]["value"] == 11.0
    assert [f["Action"] for _, f in s.requests] == ["GetMetricData"]
    assert run(c.instant('AWS/RDS CPUUtilization{DBInstanceIdentifier="gone"}')) == []


def test_range_returns_points_and_period_follows_the_step():
    c, s = make()
    rows = run(c.range('AWS/RDS CPUUtilization{DBInstanceIdentifier="db-a"} period=60', 900, 1100, 90))
    assert rows == [{"labels": {"DBInstanceIdentifier": "db-a"}, "points": [[1000.0, 40.0], [1060.0, 95.5]]}]
    assert s.requests[-1][1]["MetricDataQueries.member.1.MetricStat.Period"] == "120"


def test_requests_are_signed_and_carry_the_session_token():
    c, s = make(session_token="TOK")
    run(c.instant('AWS/RDS CPUUtilization{DBInstanceIdentifier="db-a"}'))
    req, _ = s.requests[0]
    assert req.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIATEST/") and "/ap-south-1/monitoring/aws4_request" in req.headers["authorization"]
    assert req.headers["x-amz-security-token"] == "TOK" and "s3cret" not in str(req.headers)


# ---- failures ----
@pytest.mark.parametrize("status,code,word", [(403, "InvalidClientTokenId", "refused"), (403, "SignatureDoesNotMatch", "refused"),
                                              (400, "Throttling", "throttling"), (503, "ServiceUnavailable", "unavailable")])
def test_outages_and_refusals_are_readable_and_not_query_errors(status, code, word):
    c, s = make()
    s.error = (status, code, "nope")
    with pytest.raises(CloudWatchError, match=word):
        run(c.instant('AWS/RDS CPUUtilization{DBInstanceIdentifier="db-a"}'))
    assert word in c.last_error


def test_a_rejected_query_is_a_query_error():
    c, s = make()
    s.error = (400, "InvalidParameterValue", "bad stat")
    with pytest.raises(PromQueryError, match="bad stat"):
        run(c.instant('AWS/RDS CPUUtilization{DBInstanceIdentifier="db-a"}'))


def test_not_xml_means_a_wrong_endpoint():
    cfg = CloudWatchCfg(region="ap-south-1", endpoint_url="http://cw.test", **CREDS)
    c = CloudWatch(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html"))))
    with pytest.raises(CloudWatchError, match="endpoint_url"):
        run(c.instant('AWS/RDS CPUUtilization{DBInstanceIdentifier="db-a"}'))


# ---- credentials ----
def imds(role_exp="2099-01-01T00:00:00Z"):
    seen = []

    def h(req: httpx.Request):
        seen.append((req.method, req.url.path, req.headers.get("x-aws-ec2-metadata-token")))
        if req.url.path == "/latest/api/token":
            return httpx.Response(200, text="IMDSTOKEN")
        if req.url.path.endswith("security-credentials/"):
            return httpx.Response(200, text="my-role\n")
        return httpx.Response(200, json={"AccessKeyId": "ASIAROLE", "SecretAccessKey": "rolesecret", "Token": "roletok", "Expiration": role_exp})
    return h, seen


def test_credentials_prefer_config_then_environment_then_the_instance_role(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert run(make()[0].credentials()).access_key_id == "AKIATEST"
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "envsecret")
    cfg = CloudWatchCfg(region="ap-south-1")
    env_client = CloudWatch(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert run(env_client.credentials()).access_key_id == "AKIAENV"
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    h, seen = imds()
    role = CloudWatch(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    got = run(role.credentials())
    assert (got.access_key_id, got.session_token) == ("ASIAROLE", "roletok")
    assert seen[0][0] == "PUT" and seen[1][2] == "IMDSTOKEN"                       # IMDSv2: token first, then the role
    n = len(seen)
    run(role.credentials())
    assert len(seen) == n                                                          # cached until shortly before expiry


def test_an_expiring_role_credential_is_fetched_again(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    soon = datetime.fromtimestamp(time.time() + 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    h, seen = imds(soon)
    role = CloudWatch(CloudWatchCfg(region="ap-south-1"), client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    run(role.credentials())
    n = len(seen)
    run(role.credentials())
    assert len(seen) > n


def test_without_any_credentials_the_error_says_what_to_set(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)

    def down(req):
        raise httpx.ConnectError("no route")
    c = CloudWatch(CloudWatchCfg(region="ap-south-1"), client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    with pytest.raises(CloudWatchError, match="AWS_ACCESS_KEY_ID"):
        run(c.instant('AWS/RDS CPUUtilization{DBInstanceIdentifier="db-a"}'))


# ---- config ----
def test_config_validation(tmp_path):
    assert not CloudWatchCfg().enabled and CloudWatchCfg(region="eu-west-1").enabled
    for bad in ({"region": "Mumbai"}, {"region": "ap-south-1", "endpoint_url": "ftp://x"}):
        with pytest.raises(ValueError):
            CloudWatchCfg(**bad)
    f = tmp_path / "n.yaml"
    f.write_text('cloudwatch: {region: ap-south-1, secret_access_key: "${CW_SECRET:-x}"}\nservers:\n  - {id: a}\n  - {id: b, cloudwatch: {region: us-east-1}}\n')
    views = {v.id: v for v in load_config(str(f)).server_views()}
    assert views["a"].cloudwatch.region == "ap-south-1" and views["b"].cloudwatch.region == "us-east-1"


# ---- through the API ----
class FakeCloud:
    def __init__(self):
        self.calls = []

    async def instant(self, expr, at=None):
        self.calls.append(expr)
        return [{"labels": {"DBInstanceIdentifier": "db-a"}, "value": 95.5}]

    async def range(self, expr, start, end, step):
        return [{"labels": {"DBInstanceIdentifier": "db-a"}, "points": [[start, 40.0], [end, 95.5]]}]

    async def close(self):
        pass


def test_api_rule_preview_listing_and_incident_chart(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    H = {"X-Requested-With": "nigrani"}
    with TestClient(create_app(cfg, prom=FakeProm(), cloudwatch={"default": FakeCloud()})) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert c.get("/api/me").json()["servers"][0]["datasources"] == ["prometheus", "cloudwatch"]
        body = {"name": "RDS CPU", "expr": 'AWS/RDS CPUUtilization{DBInstanceIdentifier="*"}', "warn": 80, "crit": 90, "datasource": "cloudwatch"}
        prev = c.post("/api/rules/test", json=body, headers=H).json()
        assert prev["series"][0] == {"series": "db-a", "value": 95.5, "severity": "critical"}
        made = c.post("/api/rules", json=body, headers=H).json()
        iid = c.app.state.store.create_incident(source_id=made["id"], series_key="DBInstanceIdentifier=db-a", rule_name="RDS CPU", series="db-a",
                                                labels={"DBInstanceIdentifier": "db-a"}, severity="critical", started_at=time.time() - 60, server="default")
        d = c.get(f"/api/incidents/{iid}").json()
        assert d["chart"]["series"][0]["name"] == "db-a" and d["problems"] == []


def test_api_without_cloudwatch_rejects_its_rules(tmp_path):
    cfg = load_config(None)
    cfg.data_dir, cfg.auth.password = str(tmp_path), "pw"
    with TestClient(create_app(cfg, prom=FakeProm())) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        r = c.post("/api/rules", json={"name": "S", "expr": "AWS/RDS CPU", "crit": 1, "datasource": "cloudwatch"}, headers={"X-Requested-With": "nigrani"})
        assert r.status_code == 422 and "cloudwatch" in r.json()["detail"]


def test_the_cloudwatch_preset_loads(tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text("presets: [cloudwatch]\n")
    rules = {r.id: r for r in load_config(str(f)).rules}
    assert set(rules) == {"aws-rds-cpu", "aws-rds-storage", "aws-ec2-cpu", "aws-ec2-status-check"}
    assert all(r.datasource == "cloudwatch" for r in rules.values())
    for r in rules.values():
        parse(r.expr)
