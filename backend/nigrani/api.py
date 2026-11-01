"""FastAPI app: REST API + static frontend."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .auth import COOKIE, Auth, make_dependency
from .checks import CheckRunner
from .config import Config, ConfigError, ServerView
from .containers import _fresh, collect_containers, collect_host, container_signals, forget_container, safe_name
from . import backup as backup_mod
from .digest import DigestScheduler
from .engine import Engine, classify
from .format import build_event
from .github import GithubDeploys
from .notifiers import CHANNEL_FIELDS, Notifier, mask_settings
from .prom import Prom, PromError, PromUnavailable, series_key, series_label
from .legacy_presets import SHIPPED_IN_0_1_0
from .schemas import ChannelIn, RuleIn, rule_problem
from .store import Store
from . import templates as msg_templates

log = logging.getLogger("nigrani.api")
RANGES = {"15m": 900, "1h": 3600, "3h": 10800, "6h": 21600, "24h": 86400, "7d": 604800}
MASK = "••••"


def _window(range_: str) -> tuple[float, float, float]:
    secs = RANGES.get(range_)
    if not secs:
        raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")
    end = time.time()
    return end - secs, end, max(15, int(secs / 180))


def _last(points: list[list]) -> float | None:
    for _, v in reversed(points):
        if v is not None:
            return v
    return None


def _prom_guard(e: PromError) -> HTTPException:
    code = 502 if isinstance(e, PromUnavailable) else 400
    return HTTPException(code, str(e))




class PreviewIn(BaseModel):
    title_template: str | None = None
    body_template: str | None = None
    kind: Literal["firing", "resolved", "escalated", "reminder"] = "firing"


class AckIn(BaseModel):
    hours: float | None = Field(None, gt=0, le=24 * 30)   # None = until the incident resolves
    note: str = Field("", max_length=300)


class SilenceIn(BaseModel):
    server: str | None = None
    source_id: str | None = None
    match: str | None = None
    minutes: int = Field(60, ge=1, le=60 * 24 * 30)
    reason: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


@dataclass
class Runtime:
    """Everything that belongs to one monitored server."""
    view: ServerView
    prom: Prom
    checks: CheckRunner
    github: GithubDeploys


def create_app(cfg: Config, prom: "Prom | dict[str, Prom] | None" = None,
               loki: "dict[str, Prom] | None" = None) -> FastAPI:
    allow_no_auth = os.environ.get("NIGRANI_ALLOW_NO_AUTH") == "1"
    if not cfg.auth.password and not allow_no_auth:
        raise ConfigError("auth.password is empty. Set NIGRANI_PASSWORD (NIGRANI_ALLOW_NO_AUTH=1 is for local dev only).")

    store = Store(str(Path(cfg.data_dir) / "nigrani.db"))
    views = cfg.server_views()
    if isinstance(prom, dict):
        proms = prom
    elif prom is not None:
        proms = {views[0].id: prom}
    else:
        proms = {v.id: Prom(v.datasources.prometheus, v.datasources.timeout) for v in views}
    notifier = Notifier(store, cfg.app.timezone)
    # Loki answers metric LogQL (rate / count_over_time) in the same shape as Prometheus, under /loki, so the same
    # client serves log-based alert rules.
    lokis = loki if loki is not None else {
        v.id: Prom(v.datasources.loki.rstrip("/") + "/loki", v.datasources.timeout) for v in views if v.datasources.loki}
    engine = Engine(store, proms, notifier, cfg, lokis=lokis)
    runtimes: dict[str, Runtime] = {
        v.id: Runtime(v, proms[v.id], CheckRunner(cfg, store, engine, v), GithubDeploys(v.github))
        for v in views if v.id in proms
    }
    default_id = next(iter(runtimes))
    digest = DigestScheduler(cfg, store, [(rt.view, rt.prom) for rt in runtimes.values()], notifier)
    auth = Auth(cfg.auth.username, cfg.auth.password, cfg.auth.secret or os.environ.get("NIGRANI_SECRET", ""),
                cfg.auth.session_hours, enabled=bool(cfg.auth.password))
    require = make_dependency(auth)
    avail_cache: dict[tuple[str, str], tuple[float, bool]] = {}

    def srv(server: str | None = Query(None)) -> Runtime:
        rt = runtimes.get(server or default_id)
        if rt is None:
            raise HTTPException(404, f"unknown server '{server}'")
        return rt

    def _check_servers(ids: list[str]) -> None:
        bad = [i for i in ids if i not in runtimes]
        if bad:
            raise HTTPException(422, f"unknown server(s): {', '.join(bad)}")

    def seed() -> None:
        ids = {r.id for r in cfg.rules}
        for r in store.list_rules():
            if r["source"] == "preset" and r["id"] not in ids:
                engine.drop_rule(r["id"])
                store.delete_rule(r["id"])
        for r in cfg.rules:
            if store.sync_preset_rule(r.model_dump(), SHIPPED_IN_0_1_0) == "updated":
                log.info("preset rule %s was updated to the version shipped with this release", r.id)
        for rid in cfg.disable.get("rules", []):
            r = store.get_rule(rid)
            if r and r["enabled"]:
                store.save_rule({**r, "enabled": False})
        for c in cfg.channels:
            store.seed_channel(c.model_dump())

    async def containers_loop(rt: Runtime) -> None:
        while True:
            try:
                cs = await collect_containers(rt.prom, cfg, store, rt.view.services, rt.view.id)
                await engine.sync_external("svc:", container_signals(cs), server=rt.view.id)
            except PromError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("container sync failed")
            await asyncio.sleep(30)

    async def housekeeping() -> None:
        while True:
            store.prune(cfg.retention_days)
            await asyncio.sleep(86400)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        seed()
        coros = [engine.run(), digest.run(), housekeeping()]
        for rt in runtimes.values():
            coros += [rt.checks.run(), containers_loop(rt)]
        tasks = [asyncio.create_task(t) for t in coros]
        yield
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for p in [*proms.values(), *lokis.values()]:
            await p.close()

    app = FastAPI(title="Nigrani", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.engine, app.state.cfg, app.state.runtimes = store, engine, cfg, runtimes

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    api = APIRouter(prefix="/api")
    secured = APIRouter(prefix="/api", dependencies=[Depends(require)])

    # ---------------- public ----------------
    @api.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @api.post("/login")
    async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
        ip = request.client.host if request.client else "?"
        if auth.throttled(ip):
            raise HTTPException(429, "too many failed attempts, try again in a few minutes")
        if not auth.check_login(ip, body.username, body.password):
            raise HTTPException(401, "wrong username or password")
        response.set_cookie(COOKIE, auth.make_token(body.username), max_age=auth.ttl, httponly=True,
                            samesite="lax", secure=request.url.scheme == "https")
        return {"user": body.username}

    @api.post("/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @api.get("/me")
    async def me(request: Request) -> dict[str, Any]:
        user = auth.user_from(request)
        if not user:
            raise HTTPException(401, "not authenticated")
        return {
            "user": user, "app": cfg.app.name, "environment": cfg.app.environment, "version": __version__,
            "default_server": default_id,
            "servers": [{"id": rt.view.id, "name": rt.view.name, "environment": rt.view.environment,
                         "logs": bool(rt.view.datasources.loki), "deploys": rt.github.enabled,
                         "checks": bool(rt.view.checks)} for rt in runtimes.values()],
            "features": {"logs": any(rt.view.datasources.loki for rt in runtimes.values()),
                         "deploys": any(rt.github.enabled for rt in runtimes.values()),
                         "checks": any(rt.view.checks for rt in runtimes.values()),
                         "digest": cfg.digest.enabled, "auth": auth.enabled, "multi": len(runtimes) > 1},
        }

    @api.get("/public/status")
    async def public_status() -> dict[str, Any]:
        if not cfg.status_page.enabled:
            raise HTTPException(404, "status page disabled")
        multi = len(runtimes) > 1
        check_rows = []
        for rt in runtimes.values():
            for c in rt.checks.summary():
                name = f"{rt.view.name} · {c['name']}" if multi else c["name"]
                check_rows.append({"name": name, "ok": c["ok"], "uptime_30d": c["uptime_30d"]})
        # Only uptime-check incidents are public: internal rule names (disks, containers...) stay private.
        public = [i for i in store.recent_incidents(100, time.time() - 7 * 86400) if i["source_id"].startswith("check:")]
        failing = [c for c in check_rows if c["ok"] is False]
        return {
            "title": cfg.status_page.title, "name": cfg.app.name,
            "status": "major" if failing else "operational",
            "checks": check_rows,
            "incidents": [{"rule": i["rule_name"].removeprefix("Check failing: "), "severity": i["severity"],
                           "started_at": i["started_at"], "resolved_at": i["resolved_at"]} for i in public[:20]],
        }

    # ---------------- overview ----------------
    async def _overview(rt: Runtime) -> dict[str, Any]:
        prom = rt.prom
        now = time.time()
        prom_ok = await prom.healthy()

        async def tile(t) -> dict[str, Any]:
            base = {"id": t.id, "title": t.title, "unit": t.unit, "warn": t.warn, "crit": t.crit,
                    "direction": t.direction, "description": t.description, "value": None, "spark": [],
                    "status": "nodata", "error": None}
            try:
                res = await prom.range(t.query, now - 3600, now, 60)
            except PromError as e:
                return {**base, "error": str(e)}
            if not res:
                return base
            pick = (max if t.direction == "above" else min)(res, key=lambda r: _last(r["points"]) or 0)
            v = _last(pick["points"])
            sev = classify(v, t.direction, t.warn, t.crit)[0] if v is not None else None
            return {**base, "value": v, "spark": pick["points"],
                    "status": "nodata" if v is None else {"critical": "crit", "warning": "warn", None: "ok"}[sev]}

        tiles = list(await asyncio.gather(*(tile(t) for t in cfg.tiles)))

        cont: dict[str, Any] | None = None
        try:
            cs = await collect_containers(prom, cfg, store, rt.view.services, rt.view.id)
            cont = {"up": sum(1 for c in cs if c["up"]), "total": len(cs),
                    "down": [c["service"] or c["name"] for c in cs if not c["up"]]}
        except PromError:
            pass

        open_inc = [i for i in store.open_incidents() if i["server"] == rt.view.id]
        crit = sum(1 for i in open_inc if i["severity"] == "critical")
        warn = len(open_inc) - crit
        if not prom_ok:
            banner = {"status": "unknown", "title": "Can't reach Prometheus",
                      "detail": prom.last_error or "Metrics are unavailable, so health can't be determined."}
        elif crit:
            banner = {"status": "major", "title": f"{crit} critical issue{'s' if crit != 1 else ''}",
                      "detail": ", ".join(sorted({i['rule_name'] for i in open_inc if i['severity'] == 'critical'}))}
        elif warn:
            banner = {"status": "degraded", "title": f"{warn} warning{'s' if warn != 1 else ''}",
                      "detail": ", ".join(sorted({i['rule_name'] for i in open_inc}))}
        else:
            banner = {"status": "operational", "title": "All systems operational",
                      "detail": "No active alerts."}
        chk = rt.checks.summary()
        ups = [c["uptime_30d"] for c in chk if c["uptime_30d"] is not None]
        return {
            "server": {"id": rt.view.id, "name": rt.view.name, "environment": rt.view.environment},
            "banner": banner, "tiles": tiles, "containers": cont, "incidents": open_inc[:8],
            "incident_counts": {"critical": crit, "warning": warn},
            "checks": chk, "uptime_30d": (sum(ups) / len(ups)) if ups else None,
            "prometheus": {"ok": prom_ok, "error": prom.last_error}, "generated_at": now,
        }

    @secured.get("/overview")
    async def overview(rt: Runtime = Depends(srv)) -> dict[str, Any]:
        return await _overview(rt)

    @secured.get("/fleet")
    async def fleet() -> list[dict[str, Any]]:
        """Every server's overview at once, for the all-servers screen."""
        return list(await asyncio.gather(*(_overview(rt) for rt in runtimes.values())))

    # ---------------- containers / host ----------------
    @secured.get("/containers")
    async def containers(rt: Runtime = Depends(srv)) -> list[dict[str, Any]]:
        try:
            return await collect_containers(rt.prom, cfg, store, rt.view.services, rt.view.id)
        except PromError as e:
            raise _prom_guard(e) from e

    @secured.get("/containers/{name:path}/metrics")
    async def container_metrics(name: str, range: str = "1h", rt: Runtime = Depends(srv)) -> dict[str, Any]:
        try:
            name = safe_name(name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        start, end, step = _window(range)
        sel = f'{cfg.containers.label}="{name}"'
        fresh = _fresh(sel, cfg.containers.stale_seconds)  # ignore stale series left by an earlier container of the same name
        qs = {
            "cpu": f"sum(rate(container_cpu_usage_seconds_total{{{sel}}}[2m]) and on (id) {fresh}) * 100",
            "mem": f"max(container_memory_working_set_bytes{{{sel}}} and on (id) {fresh})",
            "rx": f"sum(rate(container_network_receive_bytes_total{{{sel}}}[2m]) and on (id) {fresh})",
            "tx": f"sum(rate(container_network_transmit_bytes_total{{{sel}}}[2m]) and on (id) {fresh})",
        }
        try:
            res = await asyncio.gather(*(rt.prom.range(q, start, end, step) for q in qs.values()))
        except PromError as e:
            raise _prom_guard(e) from e
        return {k: (r[0]["points"] if r else []) for k, r in zip(qs, res)}

    @secured.delete("/containers/{name:path}")
    async def forget(name: str, rt: Runtime = Depends(srv)) -> dict[str, bool]:
        await forget_container(store, name, rt.view.id)
        return {"ok": True}

    @secured.get("/host")
    async def host(range: str = "1h", rt: Runtime = Depends(srv)) -> dict[str, Any]:
        start, end, step = _window(range)
        try:
            return await collect_host(rt.prom, cfg, start, end, step)
        except PromError as e:
            raise _prom_guard(e) from e

    # ---------------- dashboard pages (preset / user defined) ----------------
    async def _available(rt: Runtime, metric: str) -> bool:
        if not metric:
            return True
        hit = avail_cache.get((rt.view.id, metric))
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        ok = await rt.prom.has_metric(metric)
        avail_cache[(rt.view.id, metric)] = (time.time(), ok)
        return ok

    @secured.get("/pages")
    async def pages(rt: Runtime = Depends(srv)) -> list[dict[str, Any]]:
        oks = await asyncio.gather(*(_available(rt, p.requires) for p in cfg.pages))
        return [{"id": p.id, "title": p.title, "icon": p.icon, "available": ok, "hint": p.hint, "requires": p.requires}
                for p, ok in zip(cfg.pages, oks)]

    @secured.get("/pages/{page_id}")
    async def page(page_id: str, range: str = "1h", rt: Runtime = Depends(srv)) -> dict[str, Any]:
        p = next((x for x in cfg.pages if x.id == page_id), None)
        if not p:
            raise HTTPException(404, "unknown page")
        start, end, step = _window(range)
        available = await _available(rt, p.requires)

        async def panel(pn) -> dict[str, Any]:
            out: dict[str, Any] = {"title": pn.title, "unit": pn.unit, "kind": pn.kind, "wide": pn.wide,
                                   "description": pn.description, "series": [], "error": None}
            try:
                results = await asyncio.gather(*(rt.prom.range(q.expr, start, end, step) for q in pn.queries))
            except PromError as e:
                out["error"] = str(e)
                return out
            series = []
            for q, res in zip(pn.queries, results):
                for r in res:
                    name = q.legend
                    if name:
                        name = re.sub(r"\{(\w+)\}", lambda m: r["labels"].get(m.group(1), ""), name)
                    series.append({"name": name or series_label(r["labels"]) or pn.title, "points": r["points"]})
            series.sort(key=lambda s: -(_last(s["points"]) or 0))
            out["series"] = series[: pn.max_series]
            if pn.kind == "stat" and series:
                out["value"] = _last(series[0]["points"])
            return out

        panels = await asyncio.gather(*(panel(pn) for pn in p.panels)) if available else []
        return {"id": p.id, "title": p.title, "available": available, "hint": p.hint, "requires": p.requires,
                "panels": panels}

    # ---------------- alerts ----------------
    @secured.get("/rules")
    async def list_rules() -> list[dict[str, Any]]:
        return [{**r, "error": engine.rule_error(r["id"])} for r in store.list_rules()]

    def _validate_rule(r: RuleIn) -> None:
        _check_servers(r.servers)
        if r.datasource == "loki" and not lokis:
            raise HTTPException(422, "no server has Loki configured (datasources.loki), so a log rule can't be evaluated")
        problem = rule_problem(r)
        if problem:
            raise HTTPException(422, problem)

    @secured.post("/rules")
    async def create_rule(r: RuleIn) -> dict[str, Any]:
        _validate_rule(r)
        rid = r.id or re.sub(r"[^a-z0-9]+", "-", r.name.lower()).strip("-")[:60] or "rule"
        if store.get_rule(rid):
            rid = f"{rid}-{int(time.time()) % 100000}"
        return store.save_rule({**r.model_dump(), "id": rid, "source": "custom"})

    @secured.put("/rules/{rule_id}")
    async def update_rule(rule_id: str, r: RuleIn) -> dict[str, Any]:
        old = store.get_rule(rule_id)
        if not old:
            raise HTTPException(404, "unknown rule")
        _validate_rule(r)
        if not r.enabled:
            engine.drop_rule(rule_id)
        return store.save_rule({**r.model_dump(), "id": rule_id, "source": old["source"]})

    @secured.delete("/rules/{rule_id}")
    async def delete_rule(rule_id: str) -> dict[str, bool]:
        engine.drop_rule(rule_id)
        store.delete_rule(rule_id)
        return {"ok": True}

    @secured.post("/rules/test")
    async def test_rule(r: RuleIn, rt: Runtime = Depends(srv)) -> dict[str, Any]:
        client = rt.prom if r.datasource == "prometheus" else lokis.get(rt.view.id)
        if client is None:
            raise HTTPException(400, f"{rt.view.name} has no Loki configured (datasources.loki)")
        try:
            res = await client.instant(r.expr)
        except PromError as e:
            raise _prom_guard(e) from e
        rows = []
        for s in res[:50]:
            if s["value"] is None:
                continue
            sev, _ = classify(s["value"], r.direction, r.warn, r.crit)
            rows.append({"series": series_label(s["labels"]), "value": s["value"], "severity": sev})
        return {"count": len(res), "series": rows}

    @secured.get("/incidents")
    async def incidents(status: str = "all", limit: int = Query(100, le=500), days: float = Query(7, gt=0, le=60),
                        server: str | None = None) -> list[dict]:
        rows = store.recent_incidents(limit, time.time() - days * 86400, server=server)
        if status in ("firing", "resolved"):
            rows = [i for i in rows if i["status"] == status]
        return rows

    @secured.get("/incidents/{incident_id}")
    async def incident_detail(incident_id: int) -> dict[str, Any]:
        """Everything around one alert: lifecycle, the metric behind it, deploys, restarts, related alerts."""
        inc = store.get_incident(incident_id)
        if not inc:
            raise HTTPException(404, "unknown incident")
        rt = runtimes.get(inc["server"]) or next(iter(runtimes.values()))
        now = time.time()
        end = min(now, (inc["resolved_at"] or now) + 600)
        start = min(inc["started_at"] - 1200, end - 1800)
        step = max(15, int((end - start) / 160))
        problems: list[str] = []
        out: dict[str, Any] = {
            "incident": inc, "events": store.list_events(incident_id), "window": {"start": start, "end": end},
            "chart": None, "container": None, "restarts": [], "deploys": [], "related": [], "problems": problems,
        }

        rule = store.get_rule(inc["source_id"])
        if rule:
            client = rt.prom if rule["datasource"] == "prometheus" else lokis.get(inc["server"])
            if client is not None:
                try:
                    res = await client.range(rule["expr"], start, end, step)
                    pick = [r for r in res if series_key(r["labels"]) == inc["series_key"]] or res[:3]
                    out["chart"] = {
                        "name": rule["name"], "unit": rule["unit"], "direction": rule["direction"],
                        "warn": rule["warn"], "crit": rule["crit"],
                        "series": [{"name": series_label(r["labels"]) or rule["name"], "points": r["points"]} for r in pick]}
                except PromError as e:
                    problems.append(f"metric: {e}")

        label = cfg.containers.label
        cname = inc["labels"].get(label) or inc["labels"].get(rt.view.datasources.loki_container_label)
        if cname and re.fullmatch(r"[A-Za-z0-9_.\-/]+", cname):
            sel = f'{label}="{cname}"'
            fresh = _fresh(sel, cfg.containers.stale_seconds)
            try:
                cpu, mem = await asyncio.gather(
                    rt.prom.range(f"sum(rate(container_cpu_usage_seconds_total{{{sel}}}[2m]) and on (id) {fresh}) * 100", start, end, step),
                    rt.prom.range(f"max(container_memory_working_set_bytes{{{sel}}} and on (id) {fresh})", start, end, step))
                if cpu or mem:
                    out["container"] = {"name": cname, "cpu": cpu[0]["points"] if cpu else [],
                                        "mem": mem[0]["points"] if mem else []}
            except PromError as e:
                problems.append(f"container metrics: {e}")

        try:   # containers that (re)started shortly before or during the incident
            started = await rt.prom.instant(
                f'max by ({label}) (container_start_time_seconds{{{label}!=""}}) > {int(start - 3600)}', at=end)
            # other containers only if they started close to the incident, so a pile of long-running containers
            # does not drown the one that matters
            restarts = [{"container": r["labels"].get(label, ""), "ts": r["value"]} for r in started
                        if r["value"] and r["labels"].get(label) != cname and r["value"] >= inc["started_at"] - 600]
            if cname and re.fullmatch(r"[A-Za-z0-9_.\-/]+", cname):
                # The incident's own container: docker restarts it in place, which cAdvisor reports as a new series per
                # RestartCount. The first sample of each series is when that restart happened.
                runs = await rt.prom.range(f'container_start_time_seconds{{{label}="{cname}"}}', start, end, step)
                runs = sorted((r for r in runs if r["points"]), key=lambda r: r["points"][0][0])
                for n, r in enumerate(runs):
                    if n == 0 and r["points"][0][0] <= start + 2 * step:
                        continue                             # already running when the window began
                    restarts.append({"container": cname, "ts": r["points"][0][0],
                                     "count": r["labels"].get("container_label_restartcount")})
            out["restarts"] = sorted(restarts, key=lambda x: x["ts"])[-30:]
        except PromError as e:
            problems.append(f"restarts: {e}")

        if rt.github.enabled:
            out["deploys"] = [r for r in await rt.github.runs()
                              if r.get("finished_at") and start - 3600 <= r["finished_at"] <= end]

        out["related"] = [
            {k: i[k] for k in ("id", "rule_name", "series", "severity", "status", "started_at", "resolved_at", "server")}
            for i in store.recent_incidents(200, inc["started_at"] - 900)
            if i["id"] != incident_id and i["started_at"] <= inc["started_at"] + 900][:10]
        return out

    @secured.post("/incidents/{incident_id}/ack")
    async def ack_incident(incident_id: int, body: AckIn, user: str = Depends(require)) -> dict[str, Any]:
        """"I'm on it": pause this incident's reminders and tell the channels once."""
        inc = await engine.acknowledge(incident_id, user, body.note.strip(), body.hours)
        if inc is None:
            raise HTTPException(409, "only a firing incident can be acknowledged")
        return inc

    @secured.delete("/incidents/{incident_id}/ack")
    async def unack_incident(incident_id: int) -> dict[str, Any]:
        inc = engine.unacknowledge(incident_id)
        if inc is None:
            raise HTTPException(409, "only a firing incident can be un-acknowledged")
        return inc

    @secured.get("/silences")
    async def silences() -> list[dict[str, Any]]:
        return store.list_silences()

    @secured.post("/silences")
    async def add_silence(s: SilenceIn) -> dict[str, int]:
        _check_servers([s.server] if s.server else [])
        return {"id": store.add_silence(s.source_id, s.match, time.time() + s.minutes * 60, s.reason, s.server)}

    @secured.delete("/silences/{silence_id}")
    async def del_silence(silence_id: int) -> dict[str, bool]:
        store.delete_silence(silence_id)
        return {"ok": True}

    # ---------------- channels ----------------
    def _public_channel(c: dict[str, Any]) -> dict[str, Any]:
        return {**c, "settings": mask_settings(c["type"], c["settings"])}

    @secured.get("/channel-types")
    async def channel_types() -> dict[str, Any]:
        return CHANNEL_FIELDS

    @secured.get("/channels")
    async def channels() -> list[dict[str, Any]]:
        return [_public_channel(c) for c in store.list_channels()]

    @secured.post("/channels")
    async def save_channel(c: ChannelIn) -> dict[str, Any]:
        _check_servers(c.servers)
        bad = msg_templates.validate(c.title_template, c.body_template)
        if bad:
            raise HTTPException(422, bad)
        cid = c.id or re.sub(r"[^a-z0-9]+", "-", c.name.lower()).strip("-")[:60] or "channel"
        if not c.id and store.get_channel(cid):
            cid = f"{cid}-{int(time.time()) % 100000}"
        old = store.get_channel(cid)
        settings = dict(c.settings)
        if old:  # keep stored secrets when the UI sends back the masked placeholder
            for k, v in settings.items():
                if isinstance(v, str) and v.startswith(MASK):
                    settings[k] = old["settings"].get(k, "")
        saved = store.save_channel({**c.model_dump(), "id": cid, "settings": settings})
        return _public_channel(saved)

    @secured.get("/message-placeholders")
    async def message_placeholders() -> list[dict[str, str]]:
        return [{"name": k, "description": v} for k, v in msg_templates.PLACEHOLDERS.items()]

    @secured.post("/channels/preview")
    async def preview_message(p: PreviewIn) -> dict[str, Any]:
        """Render the templates against a sample alert, so they can be checked before saving."""
        bad = msg_templates.validate(p.title_template, p.body_template)
        if bad:
            raise HTTPException(422, bad)
        now = time.time()
        multi = len(runtimes) > 1
        ev = build_event(
            p.kind, severity="critical", rule_name="High CPU", series="web-1", value=93.4, threshold=90.0,
            unit="percent", summary="Check which container is busy on the Containers page.", started_at=now - 540,
            now=now, environment=cfg.app.environment, link=cfg.app.public_url or "https://nigrani.example.com",
            server=next(iter(runtimes.values())).view.name if multi else "")
        out = msg_templates.apply({"title_template": p.title_template, "body_template": p.body_template}, ev)
        return {"title": out["title"], "body": out["body"]}

    @secured.delete("/channels/{channel_id}")
    async def delete_channel(channel_id: str) -> dict[str, bool]:
        store.delete_channel(channel_id)
        return {"ok": True}

    @secured.post("/channels/{channel_id}/test")
    async def test_channel(channel_id: str) -> dict[str, Any]:
        ch = store.get_channel(channel_id)
        if not ch:
            raise HTTPException(404, "unknown channel")
        now = time.time()
        ev = build_event("test", severity="info", rule_name="Test", series="", value=None, threshold=None,
                         unit="short", summary="If you can read this, the channel works.", started_at=now, now=now,
                         environment=cfg.app.environment, link=cfg.app.public_url)
        ev["body"] = "If you can read this, the channel works."
        err = await notifier.send_to(ch, ev)
        return {"ok": err is None, "error": err}

    @secured.post("/digest/send")
    async def send_digest() -> dict[str, Any]:
        ev = await digest.send_now()
        return {"title": ev["title"], "body": ev["body"]}

    # ---------------- logs / deploys / checks ----------------
    @secured.get("/logs")
    async def logs(container: str = "", q: str = "", level: str = "", limit: int = Query(200, le=1000),
                   since: str = "1h", start: float | None = None, end: float | None = None,
                   rt: Runtime = Depends(srv)) -> dict[str, Any]:
        ds = rt.view.datasources
        if not ds.loki:
            raise HTTPException(404, "Loki is not configured (datasources.loki)")
        label = ds.loki_container_label
        if container:
            try:
                safe_name(container)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            selector = f'{{{label}="{container}"}}'
        else:
            selector = f'{{{label}=~".+"}}'
        query = selector
        if level == "error":
            query += ' |~ "(?i)(error|exception|traceback|critical|fatal)"'
        elif level == "warn":
            query += ' |~ "(?i)(warn|error|exception|traceback|critical|fatal)"'
        if q:
            query += " |= " + _logql_str(q)
        secs = RANGES.get(since, 3600)
        end_ts = end if (start is not None and end) else time.time()
        start_ts = start if start is not None else end_ts - secs
        try:
            async with httpx.AsyncClient(timeout=15) as cl:
                r = await cl.get(ds.loki.rstrip("/") + "/loki/api/v1/query_range", params={
                    "query": query, "limit": limit, "direction": "backward",
                    "start": int(start_ts * 1e9), "end": int(end_ts * 1e9)})
            r.raise_for_status()
            streams = r.json()["data"]["result"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise HTTPException(502, f"Loki error: {e}") from e
        rows = []
        for s in streams:
            for ts, line in s["values"]:
                rows.append({"ts": int(ts) / 1e9, "line": line, "container": s["stream"].get(label, "")})
        rows.sort(key=lambda x: -x["ts"])
        return {"query": query, "label": label, "rows": rows[:limit]}

    @secured.get("/deploys")
    async def deploys(rt: Runtime = Depends(srv)) -> dict[str, Any]:
        return {"enabled": rt.github.enabled, "error": rt.github.error, "runs": await rt.github.runs()}

    # ---------------- backup / restore ----------------
    @secured.get("/backup")
    async def backup(include_secrets: bool = False) -> JSONResponse:
        data = backup_mod.export_backup(store, include_secrets=include_secrets, app_version=__version__)
        name = f"nigrani-backup-{time.strftime('%Y%m%d')}.json"
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @secured.post("/backup/import")
    async def backup_import(request: Request, mode: Literal["merge", "overwrite"] = "merge", dry_run: bool = True):
        """Validate (dry_run=true, the default) or apply (dry_run=false) a backup. All-or-nothing: if anything in the
        file is invalid nothing is changed, and the problems are listed."""
        if int(request.headers.get("content-length") or 0) > 2_000_000:
            raise HTTPException(413, "backup file is too large")
        try:
            data = await request.json()
        except ValueError as e:
            raise HTTPException(400, "the file is not valid JSON") from e
        plan = backup_mod.plan_import(store, data, mode=mode, server_ids=set(runtimes), has_loki=bool(lokis))
        if dry_run:
            return plan.view()
        if plan.errors:
            return JSONResponse({**plan.view(), "applied": False}, status_code=422)
        backup_mod.apply_import(store, plan, preset_rule_ids={r.id for r in cfg.rules}, drop_rule=engine.drop_rule)
        return {**plan.view(), "applied": True}

    @secured.get("/checks")
    async def get_checks(rt: Runtime = Depends(srv)) -> list[dict[str, Any]]:
        return rt.checks.summary()

    app.include_router(api)
    app.include_router(secured)

    # ---------------- static frontend ----------------
    static = Path(os.environ.get("NIGRANI_STATIC_DIR", Path(__file__).resolve().parents[2] / "frontend" / "dist"))

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        if static.is_dir():
            f = (static / path).resolve()
            if path and f.is_file() and static.resolve() in f.parents:
                return FileResponse(f)
            index = static / "index.html"
            if index.is_file():
                return FileResponse(index)
        return JSONResponse({"detail": "frontend not built; see README"}, status_code=404)

    return app


def _logql_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
