"""Azure Monitor metrics through the Azure Resource Manager REST API, read-only.

Sign-in is a Microsoft Entra ID service principal (client credentials). It plugs into the same interface as Prometheus for
alert rules. An `expr` names a resource type and a metric, and covers every resource of that type:

    Microsoft.DBforPostgreSQL/flexibleServers cpu_percent aggregation=Maximum interval=PT5M
    Microsoft.Compute/virtualMachines "Percentage CPU" resourcegroup=prod-rg resource=web-*
"""
from __future__ import annotations

import asyncio
import fnmatch
import re
import shlex
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import AzureCfg
from .prom import PromError, PromQueryError, PromUnavailable

MAX_RESOURCES = 50
AGGREGATIONS = {"average", "minimum", "maximum", "total", "count"}
INTERVALS = [("PT1M", 60), ("PT5M", 300), ("PT15M", 900), ("PT30M", 1800), ("PT1H", 3600), ("PT6H", 21600), ("PT12H", 43200), ("P1D", 86400)]
_TYPE = re.compile(r"^[A-Za-z0-9.]+(/[A-Za-z0-9.]+)+$")


class AzureError(PromUnavailable):
    """Azure could not be reached, refused the credentials, or answered something unexpected."""


class Query:
    def __init__(self, rtype: str, metric: str, aggregation: str, interval: str | None, group: str | None, resource: str | None):
        self.rtype, self.metric, self.aggregation, self.interval, self.group, self.resource = rtype, metric, aggregation, interval, group, resource


def parse(expr: str) -> Query:
    try:
        parts = shlex.split(expr or "")
    except ValueError as e:
        raise PromQueryError(f"unbalanced quotes in the expression: {e}") from e
    if len(parts) < 2 or not _TYPE.match(parts[0]):
        raise PromQueryError('write a resource type and a metric, for example: Microsoft.Compute/virtualMachines "Percentage CPU" aggregation=Maximum')
    opts = {"aggregation": "average", "interval": None, "resourcegroup": None, "resource": None}
    for o in parts[2:]:
        k, _, v = o.partition("=")
        if k not in opts or not v:
            raise PromQueryError(f"unknown option {o!r}: use aggregation=, interval=, resourcegroup= or resource= (a glob)")
        opts[k] = v.lower() if k == "aggregation" else v
    if opts["aggregation"] not in AGGREGATIONS:
        raise PromQueryError("aggregation must be Average, Minimum, Maximum, Total or Count")
    if opts["interval"] and opts["interval"] not in dict(INTERVALS):
        raise PromQueryError("interval must be one of " + ", ".join(i for i, _ in INTERVALS))
    return Query(parts[0], parts[1], opts["aggregation"], opts["interval"], opts["resourcegroup"], opts["resource"])


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _group(rid: str) -> str:
    m = re.search(r"/resourceGroups/([^/]+)/", rid, re.I)
    return m.group(1) if m else ""


def _interval_for(step: float, minimum: str | None) -> tuple[str, int]:
    floor = dict(INTERVALS).get(minimum or "PT1M", 60)
    return next(((n, s) for n, s in INTERVALS if s >= max(step, floor)), INTERVALS[-1])


class Azure:
    def __init__(self, cfg: AzureCfg, timeout: float = 10.0, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self.last_error: str | None = None
        self._token: tuple[str, float] | None = None
        self._resources: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._gate = asyncio.Semaphore(8)

    async def close(self) -> None:
        await self._client.aclose()

    async def _bearer(self) -> str:
        if self._token and self._token[1] - time.time() > 300:
            return self._token[0]
        c = self.cfg
        try:
            r = await self._client.post(f"{c.authority_url}/{c.tenant_id}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": c.client_id, "client_secret": c.client_secret,
                "scope": f"{c.management_url}/.default"})
        except httpx.HTTPError as e:
            self.last_error = f"Microsoft sign-in could not be reached ({type(e).__name__})"
            raise AzureError(self.last_error) from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or "access_token" not in body:
            code = body.get("error", r.status_code) if isinstance(body, dict) else r.status_code
            self.last_error = (f"Azure refused the service principal ({code}); check azure.tenant_id, client_id and "
                               "client_secret (the secret value, not its id, and not expired)")
            raise AzureError(self.last_error)
        self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return self._token[0]

    async def _get(self, url_or_path: str, params: dict[str, Any] | None = None, _retry: bool = True) -> Any:
        url = url_or_path if url_or_path.startswith("http") else self.cfg.management_url + url_or_path
        token = await self._bearer()
        try:
            r = await self._client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            self.last_error = f"Azure could not be reached ({type(e).__name__})"
            raise AzureError(self.last_error) from e
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code == 401 and _retry:
            self._token = None
            return await self._get(url_or_path, params, _retry=False)
        if r.status_code in (401, 403):
            self.last_error = (f"Azure refused access (HTTP {r.status_code}); give the service principal the Monitoring Reader "
                               "role on the subscription")
            raise AzureError(self.last_error)
        if r.status_code == 429:
            self.last_error = "Azure is rate limiting this principal; try again shortly"
            raise AzureError(self.last_error)
        if r.status_code >= 500:
            self.last_error = f"Azure returned HTTP {r.status_code}"
            raise AzureError(self.last_error)
        if body is None:
            self.last_error = "Azure returned something that is not JSON (wrong azure.management_url?)"
            raise AzureError(self.last_error)
        if r.status_code >= 400:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            msg = err.get("message") if isinstance(err, dict) else None
            raise PromQueryError(f"Azure rejected the query: {msg or r.status_code}")
        self.last_error = None
        return body

    async def _find(self, q: Query) -> list[dict[str, Any]]:
        cached = self._resources.get(q.rtype)
        if not cached or time.time() - cached[0] > 300:
            rows: list[dict[str, Any]] = []
            url: str | None = f"/subscriptions/{self.cfg.subscription_id}/resources"
            params: dict[str, Any] | None = {"api-version": "2021-04-01", "$filter": f"resourceType eq '{q.rtype}'"}
            for _ in range(20):
                body = await self._get(url, params)
                rows += [r for r in body.get("value", []) if isinstance(r, dict)]
                url, params = body.get("nextLink"), None
                if not url:
                    break
            self._resources[q.rtype] = cached = (time.time(), rows)
        groups = {g.lower() for g in ([q.group] if q.group else self.cfg.resource_groups)}
        found = [r for r in cached[1] if (not groups or _group(r.get("id", "")).lower() in groups)
                 and (not q.resource or fnmatch.fnmatchcase(r.get("name", ""), q.resource))]
        return sorted(found, key=lambda r: r.get("name", ""))[:MAX_RESOURCES]

    async def _series(self, rid: str, q: Query, start: float, end: float, interval: str) -> list[list[float | None]]:
        async with self._gate:
            body = await self._get(f"{rid}/providers/microsoft.insights/metrics", {
                "api-version": "2018-01-01", "metricnames": q.metric, "aggregation": q.aggregation.capitalize(),
                "interval": interval, "timespan": f"{_iso(start)}/{_iso(end)}"})
        pts: list[list[float | None]] = []
        for metric in body.get("value", []):
            for ts in metric.get("timeseries", [])[:1]:
                for d in ts.get("data", []):
                    t = datetime.fromisoformat(str(d.get("timeStamp", "")).replace("Z", "+00:00")).timestamp()
                    v = d.get(q.aggregation)
                    pts.append([t, float(v) if isinstance(v, (int, float)) else None])
        return pts

    async def _query(self, expr: str, start: float, end: float, step: float) -> list[dict[str, Any]]:
        q = parse(expr)
        interval, _ = _interval_for(step, q.interval)
        found = await self._find(q)
        results = await asyncio.gather(*(self._series(r["id"], q, start, end, interval) for r in found))
        return [{"labels": {"resource": r["name"], "resource_group": _group(r["id"])}, "points": pts} for r, pts in zip(found, results)]

    async def instant(self, expr: str, at: float | None = None) -> list[dict[str, Any]]:
        now = at or time.time()
        q = parse(expr)
        _, secs = _interval_for(0, q.interval)
        out = []
        for s in await self._query(expr, now - max(secs * 3, 900), now, 0):
            last = next((v for _, v in reversed(s["points"]) if v is not None), None)
            if last is not None:
                out.append({"labels": s["labels"], "value": last})
        return out

    async def range(self, expr: str, start: float, end: float, step: float) -> list[dict[str, Any]]:
        return await self._query(expr, start, end, step)

    async def healthy(self) -> bool:
        try:
            await self._get(f"/subscriptions/{self.cfg.subscription_id}", {"api-version": "2022-12-01"})
            return True
        except PromError:
            return False
