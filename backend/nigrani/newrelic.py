"""New Relic through NerdGraph (its GraphQL API), read-only.

It plugs into the same interface as Prometheus for alert rules: an `expr` is an NRQL query that returns numbers, and every
facet is one alert:

    SELECT average(cpuPercent) FROM SystemSample FACET hostname SINCE 5 minutes ago

Without a FACET the query gives one value. A query with several numeric columns gives one series per column. Only NRQL is
sent (NerdGraph's `nrql` field cannot change anything).
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .config import NewRelicCfg
from .prom import PromError, PromQueryError, PromUnavailable

NRQL = "query($account: Int!, $q: Nrql!) { actor { account(id: $account) { nrql(query: $q, timeout: 30) { results } } } }"
PING = "query($account: Int!) { actor { account(id: $account) { id } } }"
TIME_KEYS = {"beginTimeSeconds", "endTimeSeconds", "timestamp", "facet"}
AUTH_CLASSES = {"UNAUTHORIZED", "FORBIDDEN", "ACCESS_DENIED", "UNAUTHENTICATED"}
CLAUSES = re.compile(r"\s+(SINCE|UNTIL|TIMESERIES)\b.*$", re.I | re.S)
FACET = re.compile(r"\bFACET\s+(.+?)(?:\s+(?:LIMIT|SINCE|UNTIL|TIMESERIES|COMPARE|WITH|ORDER)\b|$)", re.I | re.S)


class NewRelicError(PromUnavailable):
    """New Relic could not be reached, refused the key, or answered something unexpected."""


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def facet_names(query: str) -> list[str]:
    """The attribute names in `FACET a, b`, so labels read hostname=web-1 and not facet=web-1. Empty when they cannot be told
    apart (a FACET on an expression such as cases(...))."""
    m = FACET.search(query or "")
    if not m or "(" in m.group(1):
        return []
    return [n.strip().strip("`") for n in m.group(1).split(",") if n.strip()]


def _labels(row: dict[str, Any], metric: str, columns: int, names: list[str]) -> dict[str, str]:
    facet = row.get("facet")
    out: dict[str, str] = {}
    if isinstance(facet, list):
        named = len(names) == len(facet)
        out = {(names[i] if named else f"facet{i + 1}"): str(v) for i, v in enumerate(facet)}
    elif facet is not None:
        out = {(names[0] if len(names) == 1 else "facet"): str(facet)}
    if columns > 1 or not out:
        out["metric"] = metric
    return out


def split_rows(rows: list[Any], names: list[str] | None = None) -> list[tuple[dict[str, str], dict[str, Any]]]:
    """-> [(labels, row)] with one entry per (facet, numeric column)."""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cols = [k for k, v in row.items() if k not in TIME_KEYS and _num(v) is not None]
        out += [(_labels(row, c, len(cols), names or []), {"column": c, "value": float(row[c]), "row": row}) for c in cols]
    return out


class NewRelic:
    def __init__(self, cfg: NewRelicCfg, timeout: float = 10.0, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=max(timeout, 35.0))   # NerdGraph itself may take up to its 30 s query timeout
        self.last_error: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def endpoint(self) -> str:
        return self.cfg.url or f"https://api.{'eu.' if self.cfg.region == 'eu' else ''}newrelic.com/graphql"

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._client.post(self.endpoint, json={"query": query, "variables": variables},
                                        headers={"API-Key": self.cfg.api_key, "Content-Type": "application/json"})
        except httpx.HTTPError as e:
            self.last_error = f"New Relic could not be reached ({type(e).__name__})"
            raise NewRelicError(self.last_error) from e
        if r.status_code in (401, 403):
            self.last_error = f"New Relic refused the API key (HTTP {r.status_code}): use a User key (NRAK-...), and check newrelic.region"
            raise NewRelicError(self.last_error)
        if r.status_code == 429:
            self.last_error = "New Relic is rate limiting this key; try again shortly"
            raise NewRelicError(self.last_error)
        if r.status_code >= 500:
            self.last_error = f"New Relic returned HTTP {r.status_code}"
            raise NewRelicError(self.last_error)
        try:
            body = r.json()
        except ValueError:
            self.last_error = "New Relic returned something that is not JSON (wrong newrelic.url?)"
            raise NewRelicError(self.last_error) from None
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            klass = str((first.get("extensions") or {}).get("errorClass", "")).upper()
            msg = str(first.get("message", ""))[:300]
            if klass in AUTH_CLASSES or "api key" in msg.lower():
                self.last_error = "New Relic refused the API key or account: use a User key (NRAK-...) that may see newrelic.account_id"
                raise NewRelicError(self.last_error)
            if r.status_code >= 400 and not klass:
                self.last_error = f"New Relic returned HTTP {r.status_code}"
                raise NewRelicError(self.last_error)
            raise PromQueryError(f"New Relic rejected the query: {msg or klass or r.status_code}")
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            self.last_error = "New Relic returned an unexpected answer"
            raise NewRelicError(self.last_error)
        self.last_error = None
        return body["data"]

    async def _nrql(self, nrql: str) -> list[Any]:
        data = await self._graphql(NRQL, {"account": int(self.cfg.account_id), "q": nrql})
        try:
            rows = data["actor"]["account"]["nrql"]["results"]
        except (KeyError, TypeError):
            self.last_error = "New Relic returned an unexpected answer"
            raise NewRelicError(self.last_error) from None
        if not isinstance(rows, list):
            self.last_error = "New Relic returned an unexpected answer"
            raise NewRelicError(self.last_error)
        return rows

    async def instant(self, expr: str, at: float | None = None) -> list[dict[str, Any]]:
        return [{"labels": labels, "value": item["value"]} for labels, item in split_rows(await self._nrql(expr), facet_names(expr))]

    async def range(self, query: str, start: float, end: float, step: float = 0) -> list[dict[str, Any]]:
        """The same query over a window as a time series (its own SINCE/UNTIL/TIMESERIES are replaced)."""
        base = CLAUSES.sub("", query.strip())
        rows = await self._nrql(f"{base} SINCE {int(start)} UNTIL {int(end)} TIMESERIES")
        series: dict[tuple, dict[str, Any]] = {}
        for labels, item in split_rows(rows, facet_names(query)):
            ts = item["row"].get("beginTimeSeconds") or item["row"].get("endTimeSeconds")
            if _num(ts) is None:
                continue
            s = series.setdefault(tuple(sorted(labels.items())), {"labels": labels, "points": []})
            s["points"].append([float(ts), item["value"]])
        for s in series.values():
            s["points"].sort()
        return list(series.values())

    async def healthy(self) -> bool:
        try:
            await self._graphql(PING, {"account": int(self.cfg.account_id)})
            return True
        except PromError:
            return False
