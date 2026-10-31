"""Config loading: nigrani.yaml + presets, validated strictly so typos fail loudly."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PRESET_DIR = Path(__file__).parent / "presets"
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

Direction = Literal["above", "below"]


class ConfigError(Exception):
    pass


def expand_env(text: str) -> str:
    """Replace ${VAR} / ${VAR:-default} from the environment."""
    return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""), text)


def _expand(obj: Any) -> Any:
    if isinstance(obj, str):
        return expand_env(obj)
    if isinstance(obj, list):
        return [_expand(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    return obj


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppCfg(_M):
    name: str = "Nigrani"
    environment: str = "production"
    timezone: str = "UTC"
    public_url: str = ""  # used for links inside notifications


class AuthCfg(_M):
    username: str = "admin"
    password: str = ""
    secret: str = ""
    session_hours: int = 24


class DatasourcesCfg(_M):
    prometheus: str = "http://prometheus:9090"
    loki: str = ""
    loki_container_label: str = "container"
    timeout: float = 10.0


class HostCfg(_M):
    fstype_exclude: str = "tmpfs|overlay|squashfs|devtmpfs|devfs|iso9660|nsfs|fuse.*"
    mountpoint_exclude: str = (
        "^/(proc|sys|dev|run)($|/.*)|.*/docker/(overlay2|containers|plugins|volumes).*"
        "|.*/snapshots/.*|^/etc/.*"
    )


class ContainersCfg(_M):
    label: str = "name"  # cAdvisor label holding the container name
    stale_seconds: int = 90  # cAdvisor last_seen older than this = not running
    learn_after_seconds: int = 600  # auto-track containers that stayed up this long
    auto_track: bool = True
    exclude: list[str] = Field(default_factory=list)  # regexes of container names to ignore (other projects, noise)


class ServiceCfg(_M):
    name: str
    container: str = ""
    group: str = ""
    critical: bool = True


class TileCfg(_M):
    id: str
    title: str
    query: str
    unit: str = "short"
    direction: Direction = "above"
    warn: float | None = None
    crit: float | None = None
    description: str = ""


class QueryCfg(_M):
    expr: str
    legend: str = ""  # e.g. "{status}" -> label value


class PanelCfg(_M):
    title: str
    queries: list[QueryCfg]
    unit: str = "short"
    kind: Literal["line", "area", "stat"] = "line"
    wide: bool = False
    description: str = ""
    max_series: int = 12

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, v: Any) -> Any:
        if isinstance(v, dict) and "expr" in v and "queries" not in v:
            v = dict(v)
            v["queries"] = [{"expr": v.pop("expr"), "legend": v.pop("legend", "")}]
        return v


class PageCfg(_M):
    id: str
    title: str
    icon: str = "chart"
    requires: str = ""  # metric name that must exist for the page to be "available"
    hint: str = ""  # shown when the metric is missing (which exporter to run)
    panels: list[PanelCfg]


class RuleCfg(_M):
    id: str
    name: str
    expr: str
    direction: Direction = "above"
    warn: float | None = None
    crit: float | None = None
    for_seconds: int = 300
    unit: str = "short"
    summary: str = ""
    enabled: bool = True
    servers: list[str] = Field(default_factory=list)  # empty = evaluate on every server
    datasource: Literal["prometheus", "loki"] = "prometheus"  # loki = a LogQL metric query (rate/count_over_time)


ChannelType = Literal["slack", "teams", "discord", "telegram", "email", "webhook", "whatsapp"]


class ChannelCfg(_M):
    id: str
    name: str
    type: ChannelType
    settings: dict[str, Any] = Field(default_factory=dict)
    min_severity: Literal["info", "warning", "critical"] = "warning"
    quiet_hours: list[int] | None = None  # [start_hour, end_hour] local; critical still sent
    enabled: bool = True
    servers: list[str] = Field(default_factory=list)  # empty = alerts from every server


class CheckCfg(_M):
    id: str
    name: str
    type: Literal["http", "tcp", "ssl"]
    target: str
    interval: int = 60
    timeout: float = 10.0
    expect_status: int | None = None
    warn_days: int = 14
    critical: bool = True
    verify_tls: bool = True


class GithubCfg(_M):
    repo: str = ""
    token: str = ""
    workflow: str = ""
    branch: str = ""
    limit: int = 15


class DigestCfg(_M):
    enabled: bool = False
    hour: int = 9
    weekday: int | None = None  # 0=Mon; None = daily
    channels: list[str] = Field(default_factory=list)


class AlertsCfg(_M):
    eval_interval: int = 30
    repeat_interval: int = 4 * 3600
    absent_grace: int = 300


class StatusPageCfg(_M):
    enabled: bool = False
    title: str = "System status"


class ServerCfg(_M):
    """One monitored environment (e.g. production, staging). Unset fields fall back to the top-level values."""
    id: str
    name: str = ""
    environment: str = ""
    datasources: DatasourcesCfg | None = None
    services: list[ServiceCfg] | None = None
    checks: list[CheckCfg] | None = None
    github: GithubCfg | None = None

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", v):
            raise ValueError("server id must be lowercase letters, digits, - or _")
        return v


@dataclass
class ServerView:
    """A server with every inherited default resolved."""
    id: str
    name: str
    environment: str
    datasources: DatasourcesCfg
    services: list[ServiceCfg]
    checks: list[CheckCfg]
    github: GithubCfg


class Config(_M):
    app: AppCfg = AppCfg()
    auth: AuthCfg = AuthCfg()
    datasources: DatasourcesCfg = DatasourcesCfg()
    host: HostCfg = HostCfg()
    containers: ContainersCfg = ContainersCfg()
    presets: list[str] = Field(default_factory=lambda: ["core"])
    servers: list[ServerCfg] = Field(default_factory=list)
    services: list[ServiceCfg] = Field(default_factory=list)
    tiles: list[TileCfg] = Field(default_factory=list)
    pages: list[PageCfg] = Field(default_factory=list)
    rules: list[RuleCfg] = Field(default_factory=list)
    channels: list[ChannelCfg] = Field(default_factory=list)
    checks: list[CheckCfg] = Field(default_factory=list)
    github: GithubCfg = GithubCfg()
    digest: DigestCfg = DigestCfg()
    alerts: AlertsCfg = AlertsCfg()
    status_page: StatusPageCfg = StatusPageCfg()
    disable: dict[str, list[str]] = Field(default_factory=dict)  # {tiles|pages|rules: [ids]}
    data_dir: str = "/data"
    retention_days: int = 35

    def server_views(self) -> list[ServerView]:
        """Without `servers:` there is one implicit server built from the top-level settings."""
        if not self.servers:
            env = self.app.environment
            return [ServerView("default", env or self.app.name, env, self.datasources, self.services, self.checks, self.github)]
        return [
            ServerView(
                s.id, s.name or s.id, s.environment or s.name or s.id,
                s.datasources or self.datasources,
                s.services if s.services is not None else self.services,
                s.checks if s.checks is not None else self.checks,
                s.github or self.github,
            )
            for s in self.servers
        ]

    def fs_selector(self) -> str:
        return f'fstype!~"{self.host.fstype_exclude}",mountpoint!~"{self.host.mountpoint_exclude}"'


def _merge_by_id(preset_items: list, user_items: list) -> list:
    out: dict[str, Any] = {}
    for item in [*preset_items, *user_items]:
        out[item.id] = item  # user entries replace preset entries with the same id
    return list(out.values())


def _subst(cfg: Config) -> None:
    fs = cfg.fs_selector()

    def sub(s: str) -> str:
        return s.replace("{{fs}}", fs)

    for t in cfg.tiles:
        t.query = sub(t.query)
    for r in cfg.rules:
        r.expr = sub(r.expr)
    for p in cfg.pages:
        for panel in p.panels:
            for q in panel.queries:
                q.expr = sub(q.expr)


def load_config(path: str | None = None, *, explicit: bool = False) -> Config:
    raw: dict[str, Any] = {}
    if path and Path(path).is_file():
        raw = yaml.safe_load(Path(path).read_text()) or {}
    elif explicit:
        raise ConfigError(f"config file not found: {path}")
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    # channel settings keep their ${VAR} references literally; they are expanded at send time
    channels = raw.pop("channels", [])
    raw = _expand(raw)
    raw["channels"] = channels
    try:
        cfg = Config(**raw)
    except ValidationError as e:
        raise ConfigError(str(e)) from e

    preset_tiles: list[TileCfg] = []
    preset_pages: list[PageCfg] = []
    preset_rules: list[RuleCfg] = []
    for name in cfg.presets:
        f = PRESET_DIR / f"{name}.yaml"
        if not f.is_file():
            known = ", ".join(sorted(p.stem for p in PRESET_DIR.glob("*.yaml")))
            raise ConfigError(f"unknown preset '{name}' (available: {known})")
        data = yaml.safe_load(f.read_text()) or {}
        try:
            preset_tiles += [TileCfg(**x) for x in data.get("tiles", [])]
            preset_pages += [PageCfg(**x) for x in data.get("pages", [])]
            preset_rules += [RuleCfg(**x) for x in data.get("rules", [])]
        except ValidationError as e:
            raise ConfigError(f"preset {name}: {e}") from e

    ids = [x.id for x in cfg.servers]
    if len(ids) != len(set(ids)):
        raise ConfigError("duplicate server id in `servers:`")
    unknown = {sid for r in [*cfg.rules, *cfg.channels] for sid in r.servers} - set(ids) if ids else set()
    if unknown:
        raise ConfigError(f"rules/channels reference unknown server(s): {', '.join(sorted(unknown))}")

    cfg.tiles = _merge_by_id(preset_tiles, cfg.tiles)
    cfg.pages = _merge_by_id(preset_pages, cfg.pages)
    cfg.rules = _merge_by_id(preset_rules, cfg.rules)
    for kind in ("tiles", "pages", "rules"):
        off = set(cfg.disable.get(kind, []))
        if off:
            setattr(cfg, kind, [x for x in getattr(cfg, kind) if x.id not in off])
    _subst(cfg)
    return cfg
