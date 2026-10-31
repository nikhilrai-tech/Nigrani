"""Human formatting of values and notification events."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SEV_EMOJI = {"critical": "🔴", "warning": "🟠", "info": "ℹ️", "resolved": "✅"}
SEV_COLOR = {"critical": "d92d20", "warning": "f79009", "info": "2e90fa", "resolved": "12b76a"}


def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def fmt_value(v: float | None, unit: str = "short") -> str:
    if v is None:
        return "—"
    if unit == "percent":
        return f"{v:.1f}%"
    if unit == "bytes":
        return fmt_bytes(v)
    if unit == "bps":
        return fmt_bytes(v) + "/s"
    if unit == "seconds":
        return f"{v * 1000:.0f} ms" if abs(v) < 1 else f"{v:.2f} s"
    if unit == "ms":
        return f"{v:.0f} ms"
    if unit == "days":
        return f"{v:.1f} d"
    if unit == "rps":
        return f"{v:.2f}/s"
    return f"{v:.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:.2f}"


def build_event(
    kind: str,
    *,
    severity: str,
    rule_name: str,
    series: str,
    value: float | None,
    threshold: float | None,
    unit: str,
    summary: str,
    started_at: float,
    now: float,
    environment: str,
    link: str = "",
    incident_id: int | None = None,
    server: str = "",
) -> dict[str, Any]:
    what = f"{rule_name}" + (f" — {series}" if series else "")
    tag = f" [{server}]" if server else ""
    if kind == "resolved":
        title = f"✅ Resolved: {what}{tag}"
    elif kind == "test":
        title = f"🔔 Test notification from Nigrani ({environment})"
    else:
        level = "CRITICAL" if severity == "critical" else "WARNING"
        prefix = {"escalated": "⬆ ", "reminder": "⏰ "}.get(kind, "")
        title = f"{SEV_EMOJI.get(severity, '')} {prefix}{level}: {what}{tag}"
    lines = []
    if value is not None:
        if kind == "resolved":
            lines.append(f"Now: {fmt_value(value, unit)}")
        else:
            cur = f"Current: {fmt_value(value, unit)}"
            if threshold is not None and kind != "test":
                cur += f" (threshold {fmt_value(threshold, unit)})"
            lines.append(cur)
    if kind in ("resolved", "reminder", "escalated"):
        lines.append(f"Duration: {fmt_duration(now - started_at)}")
    if summary and kind not in ("resolved",):
        lines.append(summary)
    status = {"firing": "FIRING", "resolved": "RESOLVED", "escalated": "ESCALATED", "reminder": "REMINDER",
              "test": "TEST"}.get(kind, kind.upper())
    shown = "resolved" if kind == "resolved" else severity
    return {
        "status": status,
        "emoji": SEV_EMOJI.get(shown, ""),
        "value_text": fmt_value(value, unit) if value is not None else "",
        "threshold_text": fmt_value(threshold, unit) if threshold is not None else "",
        "duration": fmt_duration(now - started_at),
        "summary": summary,
        "started_text": datetime.fromtimestamp(started_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "kind": kind,
        "severity": "resolved" if kind == "resolved" else severity,
        "orig_severity": severity,
        "title": title,
        "body": "\n".join(lines),
        "rule": rule_name,
        "series": series,
        "value": value,
        "threshold": threshold,
        "unit": unit,
        "environment": environment,
        "server": server,
        "started_at": started_at,
        "incident_id": incident_id,
        "link": link,
    }
