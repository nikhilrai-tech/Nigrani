"""Datadog (any site) through its REST API, read-only.

It plugs into the same interface as Prometheus for alert rules: an `expr` is a Datadog metric query
(`avg:system.cpu.user{env:prod} by {host}`) and every resulting series is one alert. It also lists the monitors that are
not OK.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx

from .config import DatadogCfg
from .prom import PromError, PromQueryError, PromUnavailable

WINDOW = 600  # an instant value is the latest point of the last ten minutes
STATE_ORDER = {"Alert": 0, "No Data": 1, "Warn": 2}


class DatadogError(PromUnavailable):
    """Datadog could not be reached, refused the keys, or answered something unexpected."""


def labels_of(series: dict[str, Any]) -> dict[str, str]:
    tags = series.get("tag_set") or [t for t in str(series.get("scope", "")).split(",") if t and t != "*"]
    out = {}
    for t in tags:
        k, sep, v = str(t).partition(":")
        out[k] = v if sep else "true"
    return out or {"metric": str(series.get("metric") or series.get("display_name") or "value")}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class Datadog:
    def __init__(self, cfg: DatadogCfg, timeout: float = 10.0, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self.last_error: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def api(self) -> str:
        return self.cfg.url or f"https://api.{self.cfg.site}"

    @property
    def app(self) -> str:
        return f"https://app.{self.cfg.site}"

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            r = await self._client.get(self.api + path, params=params,
                                       headers={"DD-API-KEY": self.cfg.api_key, "DD-APPLICATION-KEY": self.cfg.app_key})
        except httpx.HTTPError as e:
            self.last_error = f"Datadog could not be reached ({type(e).__name__})"
            raise DatadogError(self.last_error) from e
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code in (401, 403):
            self.last_error = (f"Datadog refused the keys (HTTP {r.status_code}); check datadog.site, and that the application "
                               "key has the timeseries_query and monitors_read scopes")
            raise DatadogError(self.last_error)
        if r.status_code == 429:
            self.last_error = "Datadog is rate limiting these keys; try again shortly"
            raise DatadogError(self.last_error)
        if r.status_code >= 500:
            self.last_error = f"Datadog returned HTTP {r.status_code}"
            raise DatadogError(self.last_error)
        if body is None:
            self.last_error = "Datadog returned something that is not JSON (wrong datadog.site?)"
            raise DatadogError(self.last_error)
        if r.status_code >= 400 or (isinstance(body, dict) and body.get("status") == "error"):
            msg = body.get("error") or "; ".join(body.get("errors", [])) if isinstance(body, dict) else ""
            raise PromQueryError(f"Datadog rejected the query: {msg or r.status_code}")
        self.last_error = None
        return body

    async def range(self, query: str, start: float, end: float, step: float = 0) -> list[dict[str, Any]]:
        body = await self._get("/api/v1/query", {"query": query, "from": int(start), "to": int(end)})
        if not isinstance(body, dict) or not isinstance(body.get("series", []), list):
            self.last_error = "Datadog returned an unexpected answer"
            raise DatadogError(self.last_error)
        out = []
        for s in body.get("series", []):
            pts = [[p[0] / 1000.0, _num(p[1])] for p in s.get("pointlist", []) if isinstance(p, list) and len(p) == 2 and _num(p[0]) is not None]
            out.append({"labels": labels_of(s), "points": pts})
        return out

    async def instant(self, expr: str, at: float | None = None) -> list[dict[str, Any]]:
        now = at or time.time()
        out = []
        for s in await self.range(expr, now - WINDOW, now):
            last = next((v for _, v in reversed(s["points"]) if v is not None), None)
            if last is not None:
                out.append({"labels": s["labels"], "value": last})
        return out

    async def monitors(self) -> dict[str, Any]:
        rows = await self._get("/api/v1/monitor", {"page": 0, "page_size": 1000})
        if not isinstance(rows, list):
            self.last_error = "Datadog returned an unexpected answer for the monitor list"
            raise DatadogError(self.last_error)
        counts: dict[str, int] = {}
        bad = []
        for m in rows:
            if not isinstance(m, dict):
                continue
            state = str(m.get("overall_state", "Unknown"))
            counts[state] = counts.get(state, 0) + 1
            if state in STATE_ORDER:
                try:
                    changed = datetime.fromisoformat(str(m.get("overall_state_modified", "")).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    changed = None
                bad.append({"id": m.get("id"), "name": m.get("name", ""), "type": m.get("type", ""), "state": state,
                            "tags": [t for t in m.get("tags", []) if isinstance(t, str)][:6], "since": changed,
                            "url": f"{self.app}/monitors/{m.get('id')}"})
        bad.sort(key=lambda m: (STATE_ORDER[m["state"]], -(m["since"] or 0)))
        return {"counts": counts, "monitors": bad[:200], "total": len(rows)}

    async def healthy(self) -> bool:
        try:
            await self._get("/api/v1/validate", {})
            return True
        except PromError:
            return False
