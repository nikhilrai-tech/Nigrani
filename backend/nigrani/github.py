"""Recent GitHub Actions runs, used for deploy markers on charts."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx

from .config import GithubCfg


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class GithubDeploys:
    def __init__(self, cfg: GithubCfg, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(timeout=10)
        self._cache: tuple[float, list[dict[str, Any]]] | None = None
        self.error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.repo)

    async def runs(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        if self._cache and time.time() - self._cache[0] < 60:
            return self._cache[1]
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        params: dict[str, Any] = {"per_page": min(self.cfg.limit * 3, 100)}
        if self.cfg.branch:
            params["branch"] = self.cfg.branch
        try:
            r = await self.client.get(f"https://api.github.com/repos/{self.cfg.repo}/actions/runs",
                                      headers=headers, params=params)
            r.raise_for_status()
            runs = r.json().get("workflow_runs", [])
            self.error = None
        except (httpx.HTTPError, ValueError) as e:
            self.error = f"{type(e).__name__}: {e}"
            return self._cache[1] if self._cache else []
        out = []
        for w in runs:
            if self.cfg.workflow and self.cfg.workflow.lower() not in (w.get("name") or "").lower():
                continue
            start = _ts(w.get("run_started_at")) or _ts(w.get("created_at"))
            end = _ts(w.get("updated_at")) if w.get("status") == "completed" else None
            out.append({
                "id": w["id"], "name": w.get("name"), "title": (w.get("display_title") or "")[:120],
                "branch": w.get("head_branch"), "sha": (w.get("head_sha") or "")[:7], "status": w.get("status"),
                "conclusion": w.get("conclusion"), "started_at": start, "finished_at": end,
                "actor": (w.get("actor") or {}).get("login"), "url": w.get("html_url"),
            })
        out = out[: self.cfg.limit]
        self._cache = (time.time(), out)
        return out
