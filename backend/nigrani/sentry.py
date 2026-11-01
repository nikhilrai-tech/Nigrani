"""Sentry (sentry.io or self-hosted) through its REST API.

It plugs into the same interface as Prometheus and Loki for alert rules: an `expr` is a Sentry search query
("is:unresolved firstSeen:-1h") and the value is how many issues match, per project.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from .config import SentryCfg
from .prom import PromError, PromQueryError, PromUnavailable

MAX_ISSUES = 100  # one page of Sentry's issue list; a count above this is reported as "at least"
SORTS = ("date", "new", "freq", "user", "priority")


class SentryError(PromUnavailable):
    """Sentry could not be reached, rejected the token, or answered something unexpected."""


def _epoch(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    project = raw.get("project") or {}
    return {
        "id": str(raw.get("id", "")), "short_id": raw.get("shortId", ""), "title": raw.get("title", ""),
        "culprit": raw.get("culprit", ""), "level": raw.get("level", ""), "status": raw.get("status", ""),
        "project": project.get("slug", ""), "project_name": project.get("name") or project.get("slug", ""),
        "count": _int(raw.get("count")), "users": _int(raw.get("userCount")),
        "first_seen": _epoch(raw.get("firstSeen")), "last_seen": _epoch(raw.get("lastSeen")),
        "url": raw.get("permalink", ""),
    }


class Sentry:
    def __init__(self, cfg: SentryCfg, timeout: float = 10.0, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self.last_error: str | None = None
        self._project_ids: tuple[float, dict[str, str]] | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: Any = None) -> Any:
        try:
            r = await self._client.get(f"{self.cfg.url}/api/0{path}", params=params,
                                       headers={"Authorization": f"Bearer {self.cfg.token}"})
        except httpx.HTTPError as e:
            self.last_error = f"Sentry could not be reached ({type(e).__name__})"
            raise SentryError(self.last_error) from e
        if r.status_code in (401, 403):
            self.last_error = f"Sentry refused the token (HTTP {r.status_code}); it needs project:read and event:read"
            raise SentryError(self.last_error)
        if r.status_code == 404:
            self.last_error = "Sentry says the organization or project was not found: check sentry.org and sentry.projects"
            raise SentryError(self.last_error)
        if r.status_code == 429:
            self.last_error = "Sentry is rate limiting this token; try again shortly"
            raise SentryError(self.last_error)
        if r.status_code >= 400:
            if r.status_code < 500:
                raise PromQueryError(f"Sentry rejected the query (HTTP {r.status_code})")
            self.last_error = f"Sentry returned HTTP {r.status_code}"
            raise SentryError(self.last_error)
        try:
            data = r.json()
        except ValueError as e:
            self.last_error = "Sentry returned something that is not JSON (wrong sentry.url?)"
            raise SentryError(self.last_error) from e
        self.last_error = None
        return data

    async def _ids(self) -> list[str]:
        """Project ids for the configured slugs (Sentry filters issues by id, not slug)."""
        if not self.cfg.projects:
            return []
        if not self._project_ids or time.time() - self._project_ids[0] > 3600:
            rows = await self._get(f"/organizations/{self.cfg.org}/projects/")
            self._project_ids = (time.time(), {p["slug"]: str(p["id"]) for p in rows if isinstance(p, dict)})
        known = self._project_ids[1]
        missing = [p for p in self.cfg.projects if p not in known]
        if missing:
            raise PromQueryError(f"unknown Sentry project(s): {', '.join(missing)}")
        return [known[p] for p in self.cfg.projects]

    async def issues(self, query: str = "is:unresolved", *, limit: int = 25, sort: str = "date") -> list[dict[str, Any]]:
        if sort not in SORTS:
            raise PromQueryError(f"sort must be one of {', '.join(SORTS)}")
        params: list[tuple[str, Any]] = [("query", query), ("limit", min(max(limit, 1), MAX_ISSUES)), ("sort", sort)]
        params += [("project", pid) for pid in await self._ids()]
        rows = await self._get(f"/organizations/{self.cfg.org}/issues/", params)
        if not isinstance(rows, list):
            self.last_error = "Sentry returned an unexpected answer for the issue list"
            raise SentryError(self.last_error)
        return [normalize(r) for r in rows if isinstance(r, dict)]

    async def instant(self, expr: str, at: float | None = None) -> list[dict[str, Any]]:
        """Alert-rule value: the number of issues matching the Sentry search, one series per project."""
        counts: dict[str, int] = {}
        for issue in await self.issues(expr, limit=MAX_ISSUES):
            counts[issue["project"]] = counts.get(issue["project"], 0) + 1
        return [{"labels": {"project": p}, "value": float(n)} for p, n in sorted(counts.items())]

    async def range(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        raise PromQueryError("Sentry has no time series")

    async def healthy(self) -> bool:
        try:
            await self._get(f"/organizations/{self.cfg.org}/")
            return True
        except PromError:
            return False

    def search_url(self, query: str) -> str:
        return f"{self.cfg.url}/organizations/{self.cfg.org}/issues/?query={quote(query, safe='')}"
