"""Daily/weekly digest message summarising incidents, uptime and resource peaks per server."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .checks import result_id
from .config import Config, ServerView
from .format import fmt_duration, fmt_value
from .notifiers import Notifier
from .prom import Prom, PromError
from .store import Store

log = logging.getLogger("nigrani.digest")


async def _server_lines(cfg: Config, store: Store, view: ServerView, prom: Prom, hours: int, now: float) -> list[str]:
    since = now - hours * 3600
    incidents = store.recent_incidents(500, since, server=view.id)
    crit = [i for i in incidents if i["severity"] == "critical"]
    lines = [f"Incidents: {len(incidents)} ({len(crit)} critical), "
             f"{sum(1 for i in incidents if i['status'] == 'firing')} still open"]
    if incidents:
        worst = max(incidents, key=lambda i: (i["resolved_at"] or now) - i["started_at"])
        lines.append(f"Longest: {worst['rule_name']} {worst['series']} "
                     f"({fmt_duration((worst['resolved_at'] or now) - worst['started_at'])})")
    peaks = []
    for t in cfg.tiles:
        if t.unit != "percent":
            continue
        try:
            r = await prom.instant(f"max_over_time(({t.query})[{hours}h:1m])")
        except PromError:
            continue
        if r and r[0]["value"] is not None:
            peaks.append(f"{t.title} peak {fmt_value(r[0]['value'], t.unit)}")
    if peaks:
        lines.append("; ".join(peaks))
    uptimes = []
    for c in view.checks:
        up, n = store.check_uptime(result_id(view.id, c.id), since)
        if n:
            uptimes.append(f"{c.name} {up / n * 100:.2f}%")
    if uptimes:
        lines.append("Uptime: " + ", ".join(uptimes))
    return lines


async def build_digest(cfg: Config, store: Store, servers: list[tuple[ServerView, Prom]], period_hours: int) -> dict:
    now = time.time()
    lines: list[str] = []
    for view, prom in servers:
        block = await _server_lines(cfg, store, view, prom, period_hours, now)
        if len(servers) > 1:
            lines.append(f"▸ {view.name}")
            lines += [f"  {x}" for x in block]
        else:
            lines += block
    label = "Daily" if period_hours <= 24 else "Weekly"
    return {
        "kind": "digest", "severity": "info", "orig_severity": "info",
        "title": f"📊 {label} summary — {cfg.app.name}" + (f" ({cfg.app.environment})" if len(servers) == 1 else ""),
        "body": "\n".join(lines), "rule": "digest", "series": "", "value": None, "threshold": None, "unit": "short",
        "environment": cfg.app.environment, "server": "", "started_at": now - period_hours * 3600,
        "incident_id": None, "link": cfg.app.public_url,
    }


class DigestScheduler:
    def __init__(self, cfg: Config, store: Store, servers: list[tuple[ServerView, Prom]], notifier: Notifier):
        self.cfg, self.store, self.servers, self.notifier = cfg, store, servers, notifier

    async def send_now(self) -> dict:
        hours = 24 if self.cfg.digest.weekday is None else 24 * 7
        ev = await build_digest(self.cfg, self.store, self.servers, hours)
        await self.notifier.send(ev, only=self.cfg.digest.channels or None)
        return ev

    async def run(self) -> None:
        tz = ZoneInfo(self.cfg.app.timezone)
        while True:
            await asyncio.sleep(60)
            d = self.cfg.digest
            if not d.enabled:
                continue
            now = datetime.now(tz)
            key = now.strftime("%Y-%m-%d")
            if now.hour != d.hour or (d.weekday is not None and now.weekday() != d.weekday):
                continue
            if self.store.kv_get("last_digest") == key:
                continue
            self.store.kv_set("last_digest", key)
            try:
                await self.send_now()
            except Exception:  # noqa: BLE001
                log.exception("digest failed")
