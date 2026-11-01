"""Fingerprint of the part of an alert rule that a preset ships (what a user edit would change)."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def fingerprint(rule: dict[str, Any]) -> str:
    def num(v: Any) -> float | None:
        return None if v is None else float(v)

    parts = [rule["expr"], rule["direction"], num(rule.get("warn")), num(rule.get("crit")), int(rule["for_seconds"]),
             rule["unit"], rule.get("datasource") or "prometheus"]
    return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:16]
