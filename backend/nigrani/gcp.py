"""Google Cloud Monitoring through its REST API (`timeSeries.list`), read-only.

Sign-in is a service account key (the JWT bearer flow, scope `monitoring.read`), or, with no key configured, the
credentials of the machine Nigrani runs on (the GCE / GKE metadata server). It plugs into the same interface as Prometheus
for alert rules. An `expr` names a metric type, optionally narrowed and tuned; every time series is one alert:

    cloudsql.googleapis.com/database/cpu/utilization resource.label.database_id=prod-*
    compute.googleapis.com/instance/cpu/utilization resource.label.zone=europe-* aligner=max period=300
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import GcpCfg
from .prom import PromError, PromQueryError, PromUnavailable

SCOPE = "https://www.googleapis.com/auth/monitoring.read"
METADATA_TOKEN = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
ALIGNERS = {"mean", "max", "min", "sum", "rate", "delta", "count", "stddev"}
MAX_PAGES = 5
_METRIC = re.compile(r"^[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+$")
_KEY = re.compile(r"^(resource|metric)\.labels?\.([A-Za-z0-9_.-]+)$")


class GcpError(PromUnavailable):
    """Google Cloud could not be reached, refused the credentials, or answered something unexpected."""


class Query:
    def __init__(self, metric: str, filters: list[tuple[str, str, str]], aligner: str, period: int):
        self.metric, self.filters, self.aligner, self.period = metric, filters, aligner, period


def parse(expr: str) -> Query:
    try:
        parts = shlex.split(expr or "")
    except ValueError as e:
        raise PromQueryError(f"unbalanced quotes in the expression: {e}") from e
    if not parts or not _METRIC.match(parts[0]):
        raise PromQueryError("write a metric type first, for example: cloudsql.googleapis.com/database/cpu/utilization")
    filters: list[tuple[str, str, str]] = []
    aligner, period = "mean", 300
    for o in parts[1:]:
        k, _, v = o.partition("=")
        if not v:
            raise PromQueryError(f"cannot read {o!r}: use resource.label.<name>=<value>, metric.label.<name>=<value>, aligner= or period=")
        if k == "aligner":
            if v.lower() not in ALIGNERS:
                raise PromQueryError("aligner must be one of " + ", ".join(sorted(ALIGNERS)))
            aligner = v.lower()
        elif k == "period":
            if not v.isdigit() or not 60 <= int(v) <= 86400:
                raise PromQueryError("period is in seconds, between 60 and 86400")
            period = int(v)
        elif _KEY.match(k):
            m = _KEY.match(k)
            filters.append((m.group(1), m.group(2), v))  # type: ignore[union-attr]
        else:
            raise PromQueryError(f"unknown option {k!r}: use resource.label.<name>, metric.label.<name>, aligner or period")
    return Query(parts[0], filters, aligner, period)


def _quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_filter(q: Query) -> str:
    out = [f"metric.type = {_quote(q.metric)}"]
    for kind, name, value in q.filters:
        field = f"{kind}.labels.{name}"
        if value == "*":
            continue
        if "*" in value or "?" in value:
            rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in value)
            out.append(f"{field} = monitoring.regex.full_match({_quote(rx)})")
        else:
            out.append(f"{field} = {_quote(value)}")
    return " AND ".join(out)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(s: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _value(v: Any) -> float | None:
    if not isinstance(v, dict):
        return None
    for key in ("doubleValue", "int64Value"):
        if key in v:
            try:
                return float(v[key])
            except (TypeError, ValueError):
                return None
    if isinstance(v.get("boolValue"), bool):
        return 1.0 if v["boolValue"] else 0.0
    return None


def _labels(series: dict[str, Any]) -> dict[str, str]:
    labels = {**(series.get("metric") or {}).get("labels", {}), **(series.get("resource") or {}).get("labels", {})}
    return {str(k): str(v) for k, v in labels.items()} or {"metric": str((series.get("metric") or {}).get("type", "value"))}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_jwt(key: dict[str, Any], scope: str, now: float | None = None) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    iat = int(now or time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": key["client_email"], "scope": scope, "aud": key.get("token_uri") or "https://oauth2.googleapis.com/token",
                              "iat": iat, "exp": iat + 3600}).encode())
    private = serialization.load_pem_private_key(key["private_key"].encode(), password=None)
    sig = private.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr,call-arg]
    return f"{head}.{claims}.{_b64(sig)}"


class Gcp:
    def __init__(self, cfg: GcpCfg, timeout: float = 10.0, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self.last_error: str | None = None
        self._token: tuple[str, float] | None = None
        self._key: dict[str, Any] | None = None

    async def close(self) -> None:
        await self._client.aclose()

    def _service_account(self) -> dict[str, Any] | None:
        if self._key is None:
            raw = self.cfg.credentials_json
            path = self.cfg.credentials_file or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            if not raw and path:
                try:
                    raw = open(path, encoding="utf-8").read()
                except OSError:
                    self.last_error = f"cannot read the service account key file {path}"
                    raise GcpError(self.last_error) from None
            if not raw:
                return None
            try:
                key = json.loads(raw)
                if not isinstance(key, dict) or not key.get("client_email") or not key.get("private_key"):
                    raise ValueError
            except ValueError:
                self.last_error = "the service account key is not valid JSON with client_email and private_key"
                raise GcpError(self.last_error) from None
            self._key = key
        return self._key

    async def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        key = self._service_account()
        try:
            if key:
                url = self.cfg.token_url or key.get("token_uri") or "https://oauth2.googleapis.com/token"
                try:
                    assertion = sign_jwt(key, SCOPE)
                except (ValueError, TypeError, KeyError) as e:
                    self.last_error = "the service account private key could not be read"
                    raise GcpError(self.last_error) from e
                r = await self._client.post(url, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
            else:
                r = await self._client.get(self.cfg.token_url or METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
        except httpx.HTTPError as e:
            self.last_error = ("Google sign-in could not be reached" if key else
                               "no service account key is configured and the metadata server could not be reached: set gcp.credentials_file")
            raise GcpError(self.last_error) from e
        if r.status_code >= 400:
            self.last_error = f"Google refused the service account (HTTP {r.status_code}): check the key and that the account is not disabled"
            raise GcpError(self.last_error)
        try:
            body = r.json()
            token, ttl = str(body["access_token"]), float(body.get("expires_in", 3000))
        except (ValueError, KeyError, TypeError):
            self.last_error = "Google sign-in returned an unexpected answer"
            raise GcpError(self.last_error) from None
        self._token = (token, time.time() + ttl)
        return token

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        token = await self._access_token()
        try:
            r = await self._client.get(self.cfg.url + path, params=params, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            self.last_error = f"Cloud Monitoring could not be reached ({type(e).__name__})"
            raise GcpError(self.last_error) from e
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code in (401, 403):
            self._token = None
            self.last_error = (f"Cloud Monitoring refused this account (HTTP {r.status_code}): it needs the Monitoring Viewer role on project "
                               f"{self.cfg.project_id}, and the Cloud Monitoring API must be enabled")
            raise GcpError(self.last_error)
        if r.status_code == 429:
            self.last_error = "Cloud Monitoring is rate limiting this account; try again shortly"
            raise GcpError(self.last_error)
        if r.status_code >= 500:
            self.last_error = f"Cloud Monitoring returned HTTP {r.status_code}"
            raise GcpError(self.last_error)
        if body is None or not isinstance(body, dict):
            self.last_error = "Cloud Monitoring returned something unexpected (wrong gcp.url?)"
            raise GcpError(self.last_error)
        if r.status_code == 404:
            self.last_error = f"project {self.cfg.project_id} was not found (check gcp.project_id)"
            raise GcpError(self.last_error)
        if r.status_code >= 400:
            msg = str((body.get("error") or {}).get("message", r.status_code))[:300]
            raise PromQueryError(f"Cloud Monitoring rejected the query: {msg}")
        self.last_error = None
        return body

    async def _series(self, q: Query, start: float, end: float) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "filter": build_filter(q), "interval.startTime": _iso(start), "interval.endTime": _iso(end), "view": "FULL",
            "aggregation.alignmentPeriod": f"{q.period}s", "aggregation.perSeriesAligner": f"ALIGN_{q.aligner.upper()}", "pageSize": 500}
        out: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            body = await self._get(f"/v3/projects/{self.cfg.project_id}/timeSeries", params)
            out += [s for s in body.get("timeSeries", []) if isinstance(s, dict)]
            if not body.get("nextPageToken"):
                break
            params = {**params, "pageToken": body["nextPageToken"]}
        return out

    async def range(self, expr: str, start: float, end: float, step: float = 0) -> list[dict[str, Any]]:
        out = []
        for s in await self._series(parse(expr), start, end):
            pts = [[t, v] for p in s.get("points", []) if isinstance(p, dict)
                   for t, v in [(_epoch((p.get("interval") or {}).get("endTime")), _value(p.get("value")))] if t is not None]
            out.append({"labels": _labels(s), "points": sorted(pts)})
        return out

    async def instant(self, expr: str, at: float | None = None) -> list[dict[str, Any]]:
        q = parse(expr)
        now = at or time.time()
        out = []
        for s in await self.range(expr, now - 3 * q.period, now):
            last = next((v for _, v in reversed(s["points"]) if v is not None), None)
            if last is not None:
                out.append({"labels": s["labels"], "value": last})
        return out

    async def healthy(self) -> bool:
        try:
            await self._get(f"/v3/projects/{self.cfg.project_id}/metricDescriptors", {"pageSize": 1})
            return True
        except PromError:
            return False
