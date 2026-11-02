"""Container + host views built from cAdvisor / node-exporter metrics."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from .config import Config, ServiceCfg
from .engine import Signal
from .prom import Prom
from .store import Store

NAME_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


def safe_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValueError("invalid container name")
    return name


def _fresh(sel: str, stale_seconds: int) -> str:
    """PromQL vector that only has series cAdvisor reported within the last `stale_seconds`."""
    return f"(time() - container_last_seen{{{sel}}} < {stale_seconds})"


def matches(container: str, wanted: str) -> bool:
    """Exact name, or docker-compose style <project>-<service>-<n> / <project>_<service>_<n>."""
    if container == wanted:
        return True
    return re.fullmatch(rf".*[-_]{re.escape(wanted)}[-_]\d+", container) is not None


def _by(res: list[dict[str, Any]], label: str) -> dict[str, float | None]:
    return {r["labels"].get(label, ""): r["value"] for r in res if r["labels"].get(label)}


def _known_key(server_id: str) -> str:
    return "known_containers" if server_id == "default" else f"known_containers:{server_id}"


async def collect_containers(prom: Prom, cfg: Config, store: Store, services: list[ServiceCfg] | None = None,
                             server_id: str = "default", now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    services = cfg.services if services is None else services
    label = cfg.containers.label
    sel = f'{label}!=""'
    fresh = _fresh(sel, cfg.containers.stale_seconds)
    # A recreated container (every deploy) leaves a stale series with the same name next to the new one, so
    # age/uptime take the freshest (min) and the rest only count series cAdvisor is still reporting.
    queries = {
        "age": f"min by ({label}) (time() - container_last_seen{{{sel}}})",
        "cpu": f"sum by ({label}) (rate(container_cpu_usage_seconds_total{{{sel}}}[2m]) and on (id) {fresh}) * 100",
        "mem": f"max by ({label}) (container_memory_working_set_bytes{{{sel}}} and on (id) {fresh})",
        "limit": f"max by ({label}) (container_spec_memory_limit_bytes{{{sel}}} and on (id) {fresh})",
        # cAdvisor labels every docker container with its RestartCount, so each restart or re-creation is a new series:
        # the distinct incarnations seen in the last hour, minus the first one, are the restarts.
        "restarts": f"count by ({label}) (count by ({label}, id, container_label_restartcount) "
                    f"(last_over_time(container_start_time_seconds{{{sel}}}[1h]))) - 1",
        "uptime": f"min by ({label}) ((time() - container_start_time_seconds{{{sel}}}) and on (id) {fresh})",
        "rx": f"sum by ({label}) (rate(container_network_receive_bytes_total{{{sel}}}[2m]) and on (id) {fresh})",
        "tx": f"sum by ({label}) (rate(container_network_transmit_bytes_total{{{sel}}}[2m]) and on (id) {fresh})",
    }
    keys = list(queries)
    results = await asyncio.gather(*(prom.instant(q) for q in queries.values()))
    data = {k: _by(r, label) for k, r in zip(keys, results)}

    stale = cfg.containers.stale_seconds
    skip = [re.compile(p) for p in cfg.containers.exclude]
    out: dict[str, dict[str, Any]] = {}
    for name, age in data["age"].items():
        if any(p.search(name) for p in skip):
            continue
        limit = data["limit"].get(name) or 0
        mem = data["mem"].get(name)
        out[name] = {
            "name": name, "up": age is not None and age < stale, "age": age, "cpu": data["cpu"].get(name),
            "mem": mem, "mem_limit": limit or None,
            "mem_pct": (mem / limit * 100) if (mem and limit) else None,
            "restarts_1h": data["restarts"].get(name), "uptime": data["uptime"].get(name),
            "rx": data["rx"].get(name), "tx": data["tx"].get(name),
            "service": None, "group": "", "critical": False, "missing": False, "auto": False,
        }
    # annotate with configured services
    for svc in services:
        wanted = svc.container or svc.name
        hit = [c for n, c in out.items() if matches(n, wanted)]
        if hit:
            for c in hit:
                c.update(service=svc.name, group=svc.group, critical=svc.critical)
        else:
            out[wanted] = {"name": wanted, "up": False, "age": None, "cpu": None, "mem": None, "mem_limit": None,
                           "mem_pct": None, "restarts_1h": None, "uptime": None, "rx": None, "tx": None,
                           "service": svc.name, "group": svc.group, "critical": svc.critical, "missing": True, "auto": False}

    # auto-learn long-running containers so a vanished one is noticed even when not configured
    if cfg.containers.auto_track:
        known: dict[str, float] = store.kv_get(_known_key(server_id), {})
        configured = {c["name"] for c in out.values() if c["service"]}
        changed = False
        for name, c in out.items():
            if (c["up"] and (c["uptime"] or 0) >= cfg.containers.learn_after_seconds
                    and name not in known and name not in configured):
                known[name] = now
                changed = True
        if changed:
            store.kv_set(_known_key(server_id), known)
        for name in known:
            if name in out:
                out[name]["auto"] = True
                out[name]["critical"] = out[name]["critical"] or True
            else:
                out[name] = {"name": name, "up": False, "age": None, "cpu": None, "mem": None, "mem_limit": None,
                             "mem_pct": None, "restarts_1h": None, "uptime": None, "rx": None, "tx": None,
                             "service": None, "group": "", "critical": True, "missing": True, "auto": True}
    return sorted(out.values(), key=lambda c: (c["up"], c["name"]))


def container_signals(containers: list[dict[str, Any]]) -> list[Signal]:
    sigs = []
    for c in containers:
        if not c["critical"]:
            continue
        down = not c["up"]
        sigs.append(Signal(
            source_id="svc:container-down", rule_name="Container down", series=c["service"] or c["name"],
            series_key=c["name"], severity="critical" if down else None,
            summary="Container is not running." if c["missing"] else "Container stopped reporting metrics.",
            for_seconds=60, labels={"name": c["name"]},
        ))
    return sigs


async def forget_container(store: Store, name: str, server_id: str = "default") -> None:
    known: dict[str, float] = store.kv_get(_known_key(server_id), {})
    known.pop(name, None)
    store.kv_set(_known_key(server_id), known)


async def collect_host(prom: Prom, cfg: Config, start: float, end: float, step: float) -> dict[str, Any]:
    fs = cfg.fs_selector()
    avail = f"max by (device, mountpoint) (node_filesystem_avail_bytes{{{fs}}})"
    size = f"max by (device, mountpoint) (node_filesystem_size_bytes{{{fs}}})"
    nic = 'device!~"lo|veth.*|docker.*|br-.*|cali.*|flannel.*"'
    inst = {
        "cores": "count(count by (cpu) (node_cpu_seconds_total))",
        "load1": "node_load1", "load5": "node_load5", "load15": "node_load15",
        "mem_total": "sum(node_memory_MemTotal_bytes)", "mem_avail": "sum(node_memory_MemAvailable_bytes)",
        "swap_total": "sum(node_memory_SwapTotal_bytes)", "swap_free": "sum(node_memory_SwapFree_bytes)",
        "uptime": "min(time() - node_boot_time_seconds)",
    }
    disk_q = {"avail": avail, "size": size, "forecast": f"({avail} / -deriv({avail}[6h:5m])) / 86400"}
    rng = {
        "cpu": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
        "iowait": 'avg(rate(node_cpu_seconds_total{mode="iowait"}[5m])) * 100',
        "mem": "(1 - sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)) * 100",
        "load": "avg(node_load1)",
        "rx": f"sum(rate(node_network_receive_bytes_total{{{nic}}}[5m]))",
        "tx": f"sum(rate(node_network_transmit_bytes_total{{{nic}}}[5m]))",
        "disk_r": "sum(rate(node_disk_read_bytes_total[5m]))",
        "disk_w": "sum(rate(node_disk_written_bytes_total[5m]))",
    }
    i_res, d_res, r_res = await asyncio.gather(
        asyncio.gather(*(prom.instant(q) for q in inst.values())),
        asyncio.gather(*(prom.instant(q) for q in disk_q.values())),
        asyncio.gather(*(prom.range(q, start, end, step) for q in rng.values())),
    )
    summary = {k: (r[0]["value"] if r else None) for k, r in zip(inst, i_res)}
    disks: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, res in zip(disk_q, d_res):
        for r in res:
            k = (r["labels"].get("device", ""), r["labels"].get("mountpoint", ""))
            d = disks.setdefault(k, {"device": k[0], "mountpoint": k[1]})
            d[kind] = r["value"]
    disk_list = []
    for d in disks.values():
        if d.get("size"):
            d["used_pct"] = (1 - (d.get("avail") or 0) / d["size"]) * 100
            d["forecast_days"] = d.get("forecast") if (d.get("forecast") or 0) > 0 else None
            disk_list.append(d)
    series = {k: (r[0]["points"] if r else []) for k, r in zip(rng, r_res)}
    return {"summary": summary, "disks": sorted(disk_list, key=lambda d: -d["used_pct"]), "series": series}
