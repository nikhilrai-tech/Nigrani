"""Per-channel message templates: "{rule} on {server} is {value}" style placeholders."""
from __future__ import annotations

import re
from typing import Any

PLACEHOLDERS: dict[str, str] = {
    "emoji": "severity emoji (🔴 🟠 ✅)",
    "status": "FIRING, RESOLVED, ESCALATED, REMINDER or TEST",
    "severity": "critical, warning or resolved",
    "rule": "alert rule name",
    "series": "what it fired for, e.g. a container or mount point",
    "value": "current value, formatted (93.4%)",
    "threshold": "the threshold that was crossed, formatted",
    "server": "server name (empty with a single server)",
    "environment": "environment label",
    "duration": "how long it has been going on",
    "summary": "the rule's \"what to do\" text",
    "link": "link back to Nigrani (needs app.public_url)",
    "started_at": "when it started (UTC)",
    "kind": "firing, resolved, escalated, reminder or test",
    "title": "the default title",
    "body": "the default body",
}
MAX_LEN = 600
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# kinds a template is not applied to: informational messages built without an alert behind them
SKIP_KINDS = {"digest"}


def unknown_placeholders(template: str | None) -> list[str]:
    return sorted({m for m in _PLACEHOLDER.findall(template or "") if m not in PLACEHOLDERS})


def validate(title: str | None, body: str | None) -> str | None:
    """Return an error message, or None if both templates are acceptable."""
    for label, tpl in (("title", title), ("body", body)):
        if tpl and len(tpl) > MAX_LEN:
            return f"{label} template is longer than {MAX_LEN} characters"
        bad = unknown_placeholders(tpl)
        if bad:
            return f"unknown placeholder(s) in {label} template: " + ", ".join("{" + b + "}" for b in bad) + \
                   ". Available: " + ", ".join("{" + p + "}" for p in PLACEHOLDERS)
    return None


def render(template: str, ev: dict[str, Any]) -> str:
    values = {
        "emoji": ev.get("emoji", ""), "status": ev.get("status", ""), "severity": ev.get("severity", ""),
        "rule": ev.get("rule", ""), "series": ev.get("series", ""), "value": ev.get("value_text", ""),
        "threshold": ev.get("threshold_text", ""), "server": ev.get("server", ""),
        "environment": ev.get("environment", ""), "duration": ev.get("duration", ""),
        "summary": ev.get("summary", ""), "link": ev.get("link", ""), "started_at": ev.get("started_text", ""),
        "kind": ev.get("kind", ""), "title": ev.get("title", ""), "body": ev.get("body", ""),
    }
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def apply(ch: dict[str, Any], ev: dict[str, Any]) -> dict[str, Any]:
    """The event as this channel should send it. Empty template = the default text."""
    title_t, body_t = ch.get("title_template"), ch.get("body_template")
    if ev.get("kind") in SKIP_KINDS or not (title_t or body_t):
        return ev
    out = dict(ev)
    if title_t:
        out["title"] = render(title_t, ev).strip()
    if body_t:
        out["body"] = render(body_t, ev).strip()
    return out
