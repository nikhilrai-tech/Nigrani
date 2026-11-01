"""Deep links into Grafana: "Explore" with the exact query and time range of an alert, and links to your own dashboards.

Everything is built from the `grafana:` config; Nigrani never talks to Grafana, it only produces URLs for the browser.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal
from urllib.parse import quote

from .config import GrafanaCfg

PLACEHOLDER = re.compile(r"\{(\w+)\}")
VARIABLES = ("container", "server", "rule", "series", "from", "to")


def _base(g: GrafanaCfg) -> str:
    return g.url.rstrip("/")


def explore_url(g: GrafanaCfg, kind: Literal["prometheus", "loki"], expr: str, start: float, end: float) -> str | None:
    """Grafana's Explore view (Grafana 10+) with `expr` against the configured datasource and the time range."""
    uid = g.prometheus_datasource if kind == "prometheus" else g.loki_datasource
    if not g.url or not uid or not expr:
        return None
    query: dict[str, Any] = {"refId": "A", "expr": expr, "datasource": {"type": kind, "uid": uid}}
    if kind == "loki":
        query["queryType"] = "range"
    panes = {"a": {"datasource": uid, "queries": [query],
                   "range": {"from": str(int(start * 1000)), "to": str(int(end * 1000))}}}
    return f"{_base(g)}/explore?schemaVersion=1&panes={quote(json.dumps(panes, separators=(',', ':')))}&orgId=1"


def dashboard_links(g: GrafanaCfg, context: str, **values: Any) -> list[dict[str, str]]:
    """Configured dashboard links for a place in the UI, with {placeholders} filled in (URL-encoded).
    A link that needs a value this place doesn't have (no container on a host page...) is left out."""
    if not g.url:
        return []
    filled = {k: ("" if v is None else str(v)) for k, v in values.items()}
    for ts in ("from", "to"):                               # milliseconds, as Grafana expects
        if filled.get(ts):
            filled[ts] = str(int(float(filled[ts]) * 1000))
    out = []
    for d in g.dashboards:
        if context not in d.where:
            continue
        needed = PLACEHOLDER.findall(d.url)
        if any(not filled.get(n) for n in needed):
            continue
        url = PLACEHOLDER.sub(lambda m: quote(filled[m.group(1)], safe=""), d.url)
        out.append({"title": d.title, "url": url if url.startswith(("http://", "https://")) else f"{_base(g)}/{url.lstrip('/')}"})
    return out
