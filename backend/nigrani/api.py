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
from .digest import DigestScheduler
from .engine import Engine, classify
from .format import build_event
from .github import GithubDeploys
from .notifiers import CHANNEL_FIELDS, Notifier, mask_settings
from .prom import Prom, PromError, PromUnavailable, series_key, series_label
from .store import Store

log = logging.getLogger("nigrani.api")
RANGES = {"15m": 900, "1h": 3600, "3h": 10800, "6h": 21600, "24h": 86400, "7d": 604800}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
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


class RuleIn(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    expr: str = Field(min_length=1, max_length=2000)
    direction: Literal["above", "below"] = "above"
    warn: float | None = None
    crit: float | None = None
    for_seconds: int = Field(300, ge=0, le=86400)
    unit: str = "short"
    summary: str = ""
    enabled: bool = True
    servers: list[str] = []

    @field_validator("id")
    @classmethod
    def _id(cls, v: str | None) -> str | None:
        if v is not None and not ID_RE.match(v):
            raise ValueError("id must be lowercase letters, digits, - or _")
        return v


class ChannelIn(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=80)
    type: Literal["slack", "teams", "discord", "telegram", "email", "webhook", "whatsapp"]
    settings: dict[str, Any] = {}
    min_severity: Literal["info", "warning", "critical"] = "warning"
    quiet_start: int | None = Field(None, ge=0, le=23)
    quiet_end: int | None = Field(None, ge=0, le=23)
    enabled: bool = True
    servers: list[str] = []


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


def create_app(cfg: Config, prom: "Prom | dict[str, Prom] | None" = None) -> FastAPI:
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
    engine = Engine(store, proms, notifier, cfg)
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
            store.seed_rule(r.model_dump())
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
        for p in proms.values():
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
        if r.warn is not None and r.crit is not None:
            if (r.direction == "above" and r.warn > r.crit) or (r.direction == "below" and r.warn < r.crit):
                raise HTTPException(422, "warning threshold must be less severe than critical")
        if r.warn is None and r.crit is None:
            raise HTTPException(422, "set at least one of warn / crit")

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
        try:
            res = await rt.prom.instant(r.expr)
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
                   since: str = "1h", rt: Runtime = Depends(srv)) -> dict[str, Any]:
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
        end = time.time()
        try:
            async with httpx.AsyncClient(timeout=15) as cl:
                r = await cl.get(ds.loki.rstrip("/") + "/loki/api/v1/query_range", params={
                    "query": query, "limit": limit, "direction": "backward",
                    "start": int((end - secs) * 1e9), "end": int(end * 1e9)})
            r.raise_for_status()
            streams = r.json()["data"]["result"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise HTTPException(502, f"Loki error: {e}") from e
        rows = []
        for s in streams:
            for ts, line in s["values"]:
                rows.append({"ts": int(ts) / 1e9, "line": line, "container": s["stream"].get(label, "")})
        rows.sort(key=lambda x: -x["ts"])
        return {"query": query, "rows": rows[:limit]}

    @secured.get("/deploys")
    async def deploys(rt: Runtime = Depends(srv)) -> dict[str, Any]:
        return {"enabled": rt.github.enabled, "error": rt.github.error, "runs": await rt.github.runs()}

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
