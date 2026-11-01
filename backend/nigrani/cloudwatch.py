"""AWS CloudWatch metrics through its Query API, signed with SigV4 (no AWS SDK).

It plugs into the same interface as Prometheus for alert rules. An `expr` names a metric:

    AWS/RDS CPUUtilization{DBInstanceIdentifier="*"} stat=Maximum period=300

`*` as a dimension value means "every one that exists" (looked up with ListMetrics), so one rule covers every database.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import CloudWatchCfg
from .prom import PromError, PromQueryError, PromUnavailable
from .sigv4 import Credentials, sign

MAX_SERIES = 50  # one rule never fans out to more series than this
IMDS = "http://169.254.169.254"

_EXPR = re.compile(r"^\s*(?P<ns>[\w./#:-]+)\s+(?P<metric>[\w./#:-]+)\s*(?:\{(?P<dims>[^}]*)\})?\s*(?P<opts>.*?)\s*$")
_DIM = re.compile(r'\s*([\w./#:-]+)\s*=\s*"([^"]*)"\s*(?:,|$)')
_STAT = re.compile(r"^(Average|Sum|Minimum|Maximum|SampleCount|p\d{1,2}(\.\d+)?|tm\d{1,2}(\.\d+)?|IQM)$")


class CloudWatchError(PromUnavailable):
    """CloudWatch could not be reached, rejected the credentials, or answered something unexpected."""


class Query:
    def __init__(self, namespace: str, metric: str, dims: list[tuple[str, str]], stat: str, period: int):
        self.namespace, self.metric, self.dims, self.stat, self.period = namespace, metric, dims, stat, period


def parse(expr: str) -> Query:
    m = _EXPR.match(expr or "")
    if not m:
        raise PromQueryError('write a metric as: AWS/RDS CPUUtilization{DBInstanceIdentifier="*"} stat=Maximum period=300')
    dims: list[tuple[str, str]] = []
    raw = (m["dims"] or "").strip()
    pos = 0
    while pos < len(raw):
        d = _DIM.match(raw, pos)
        if not d:
            raise PromQueryError('dimensions look like {DBInstanceIdentifier="my-db"}; use "*" for every value')
        dims.append((d[1], d[2]))
        pos = d.end()
    stat, period = "Average", 300
    for opt in m["opts"].split():
        key, _, val = opt.partition("=")
        if key == "stat" and _STAT.match(val):
            stat = val
        elif key == "period" and val.isdigit() and int(val) >= 60 and int(val) % 60 == 0:
            period = int(val)
        else:
            raise PromQueryError(f"unknown option {opt!r}: use stat=Average|Sum|Minimum|Maximum|SampleCount|p99 and "
                                 "period=<seconds, a multiple of 60>")
    return Query(m["ns"], m["metric"], dims, stat, period)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(node: ET.Element | None, name: str) -> list[ET.Element]:
    return [c for c in (node if node is not None else []) if _local(c.tag) == name]


def _text(node: ET.Element | None, name: str) -> str:
    for c in _kids(node, name):
        return (c.text or "").strip()
    return ""


def _members(node: ET.Element | None, name: str) -> list[ET.Element]:
    for c in _kids(node, name):
        return _kids(c, "member")
    return []


class CloudWatch:
    def __init__(self, cfg: CloudWatchCfg, timeout: float = 10.0, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self.last_error: str | None = None
        self._creds: Credentials | None = None

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def endpoint(self) -> str:
        return self.cfg.endpoint_url or f"https://monitoring.{self.cfg.region}.amazonaws.com"

    # ---- credentials: config, then the AWS_* variables, then the EC2 instance role (IMDSv2) ----
    async def credentials(self) -> Credentials:
        c = self.cfg
        if c.access_key_id and c.secret_access_key:
            return Credentials(c.access_key_id, c.secret_access_key, c.session_token)
        env = os.environ
        if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
            return Credentials(env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"], env.get("AWS_SESSION_TOKEN", ""))
        if self._creds and (self._creds.expires_at or math.inf) - time.time() > 300:
            return self._creds
        base = env.get("AWS_EC2_METADATA_SERVICE_ENDPOINT", IMDS).rstrip("/")
        try:
            tok = await self._client.put(f"{base}/latest/api/token", timeout=2.0,
                                         headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
            tok.raise_for_status()
            h = {"X-aws-ec2-metadata-token": tok.text}
            role = (await self._client.get(f"{base}/latest/meta-data/iam/security-credentials/", headers=h, timeout=2.0))
            role.raise_for_status()
            name = role.text.split()[0]
            body = (await self._client.get(f"{base}/latest/meta-data/iam/security-credentials/{name}", headers=h,
                                           timeout=2.0)).json()
            exp = datetime.fromisoformat(body["Expiration"].replace("Z", "+00:00")).timestamp()
            self._creds = Credentials(body["AccessKeyId"], body["SecretAccessKey"], body.get("Token", ""), exp)
            return self._creds
        except (httpx.HTTPError, KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
            self.last_error = ("No AWS credentials: set cloudwatch.access_key_id and secret_access_key, the AWS_ACCESS_KEY_ID "
                               "and AWS_SECRET_ACCESS_KEY variables, or run on an EC2 instance with a role")
            raise CloudWatchError(self.last_error) from e

    async def _call(self, params: dict[str, str]) -> ET.Element:
        creds = await self.credentials()
        body = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted({**params, "Version": "2010-08-01"}.items())).encode()
        headers = sign("POST", self.endpoint + "/", {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
                       body, region=self.cfg.region, service="monitoring", creds=creds)
        try:
            r = await self._client.post(self.endpoint + "/", content=body, headers=headers)
        except httpx.HTTPError as e:
            self.last_error = f"CloudWatch could not be reached ({type(e).__name__})"
            raise CloudWatchError(self.last_error) from e
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as e:
            self.last_error = "CloudWatch returned something that is not XML (wrong cloudwatch.endpoint_url?)"
            raise CloudWatchError(self.last_error) from e
        if r.status_code >= 400:
            err = next((n for n in root.iter() if _local(n.tag) == "Error"), None)
            code, msg = _text(err, "Code"), _text(err, "Message")
            if code in ("InvalidClientTokenId", "SignatureDoesNotMatch", "UnrecognizedClientException",
                        "AccessDenied", "AccessDeniedException", "ExpiredToken", "ExpiredTokenException") \
                    or r.status_code in (401, 403):
                self.last_error = f"AWS refused the credentials ({code or r.status_code}); they need cloudwatch:GetMetricData and cloudwatch:ListMetrics"
                raise CloudWatchError(self.last_error)
            if code in ("Throttling", "ThrottlingException") or r.status_code >= 500:
                self.last_error = f"CloudWatch is unavailable or throttling ({code or r.status_code}); try again shortly"
                raise CloudWatchError(self.last_error)
            raise PromQueryError(f"CloudWatch rejected the query: {msg or code or r.status_code}")
        self.last_error = None
        return root

    async def _metrics(self, q: Query) -> list[dict[str, str]]:
        """The concrete dimension sets a query stands for (one per series)."""
        if q.dims and all(v != "*" for _, v in q.dims):
            return [dict(q.dims)]
        wanted = {k for k, _ in q.dims}
        params = {"Action": "ListMetrics", "Namespace": q.namespace, "MetricName": q.metric}
        for i, (k, v) in enumerate(q.dims, 1):
            params[f"Dimensions.member.{i}.Name"] = k
            if v != "*":
                params[f"Dimensions.member.{i}.Value"] = v
        found: list[dict[str, str]] = []
        for _ in range(10):
            root = await self._call(params)
            for m in _members(next((n for n in root.iter() if _local(n.tag) == "ListMetricsResult"), None), "Metrics"):
                d = {_text(x, "Name"): _text(x, "Value") for x in _members(m, "Dimensions")}
                if set(d) == wanted and d not in found:  # CloudWatch only matches a metric by its exact dimension set
                    found.append(d)
            token = next((n.text for n in root.iter() if _local(n.tag) == "NextToken" and n.text), None)
            if not token or len(found) >= MAX_SERIES:
                break
            params["NextToken"] = token
        return sorted(found, key=lambda d: sorted(d.items()))[:MAX_SERIES]

    async def _data(self, q: Query, start: float, end: float, period: int) -> list[dict[str, Any]]:
        sets = await self._metrics(q)
        if not sets:
            return []
        out: list[dict[str, Any]] = []
        for lo in range(0, len(sets), 20):  # keep each request small
            chunk = sets[lo:lo + 20]
            params = {"Action": "GetMetricData", "StartTime": _iso(start), "EndTime": _iso(end),
                      "ScanBy": "TimestampAscending"}
            for i, dims in enumerate(chunk, 1):
                p = f"MetricDataQueries.member.{i}"
                params[f"{p}.Id"] = f"m{i}"
                params[f"{p}.ReturnData"] = "true"
                params[f"{p}.MetricStat.Period"] = str(period)
                params[f"{p}.MetricStat.Stat"] = q.stat
                params[f"{p}.MetricStat.Metric.Namespace"] = q.namespace
                params[f"{p}.MetricStat.Metric.MetricName"] = q.metric
                for j, (k, v) in enumerate(sorted(dims.items()), 1):
                    params[f"{p}.MetricStat.Metric.Dimensions.member.{j}.Name"] = k
                    params[f"{p}.MetricStat.Metric.Dimensions.member.{j}.Value"] = v
            points: dict[str, list[list[float]]] = {f"m{i}": [] for i in range(1, len(chunk) + 1)}
            for _ in range(10):
                root = await self._call(params)
                for res in _members(next((n for n in root.iter() if _local(n.tag) == "GetMetricDataResult"), None),
                                    "MetricDataResults"):
                    ts = [datetime.fromisoformat(_t(x.text)).timestamp() for x in _members(res, "Timestamps")]
                    vals = [float(x.text) for x in _members(res, "Values")]
                    points.setdefault(_text(res, "Id"), []).extend([t, v] for t, v in zip(ts, vals))
                token = next((n.text for n in root.iter() if _local(n.tag) == "NextToken" and n.text), None)
                if not token:
                    break
                params["NextToken"] = token
            for i, dims in enumerate(chunk, 1):
                out.append({"labels": dims or {"metric": q.metric}, "points": sorted(points.get(f"m{i}", []))})
        return out

    async def instant(self, expr: str, at: float | None = None) -> list[dict[str, Any]]:
        """Latest datapoint per series. A series with no recent datapoint is left out, so the alert resolves."""
        q, now = parse(expr), at or time.time()
        rows = await self._data(q, now - max(q.period * 3, 600), now, q.period)
        return [{"labels": r["labels"], "value": r["points"][-1][1]} for r in rows if r["points"]]

    async def range(self, expr: str, start: float, end: float, step: float) -> list[dict[str, Any]]:
        q = parse(expr)
        period = max(60, int(math.ceil(max(step, q.period) / 60.0)) * 60)
        return await self._data(q, start, end, period)

    async def healthy(self) -> bool:
        try:
            await self._call({"Action": "ListMetrics", "Namespace": "AWS/EC2"})
            return True
        except PromError:
            return False


def _t(s: str | None) -> str:
    return (s or "").strip().replace("Z", "+00:00")


def _q(v: str) -> str:
    from urllib.parse import quote
    return quote(v, safe="-_.~")
