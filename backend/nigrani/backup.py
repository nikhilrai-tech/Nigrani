"""Export / import of what is configured through the UI: rules, notification channels and silences.

Channel credentials are the dangerous part of a backup. By default every *literal* secret (webhook URL, bot token...)
is exported as null; `${ENV_VAR}` references are kept because they are not secrets. Import never deletes anything,
validates the whole file before changing a single row, and creates channels whose secrets were not in the file
disabled instead of letting them fail silently.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import ValidationError

from . import templates as msg_templates
from .notifiers import CHANNEL_FIELDS
from .schemas import ChannelIn, RuleIn, rule_problem
from .store import Store

BACKUP_VERSION = 1
MAX_ITEMS = 1000
RULE_KEYS = ("id", "name", "expr", "direction", "warn", "crit", "for_seconds", "unit", "summary", "enabled",
             "servers", "datasource", "source")
CHANNEL_KEYS = ("id", "name", "type", "settings", "min_severity", "quiet_start", "quiet_end", "enabled", "servers",
                "title_template", "body_template")
Mode = Literal["merge", "overwrite"]


def _secret_keys(channel_type: str) -> set[str]:
    return {f["key"] for f in CHANNEL_FIELDS.get(channel_type, []) if f.get("secret")}


def _is_literal(value: Any) -> bool:
    return isinstance(value, str) and value != "" and "${" not in value


# ------------------------------------------------------------------ export
def export_backup(store: Store, *, include_secrets: bool, app_version: str) -> dict[str, Any]:
    channels = []
    for ch in store.list_channels():
        secret = _secret_keys(ch["type"])
        settings: dict[str, Any] = {}
        needs: list[str] = []
        for k, v in ch["settings"].items():
            if k in secret and _is_literal(v) and not include_secrets:
                settings[k] = None
                needs.append(k)
            else:
                settings[k] = v
        item = {k: ch.get(k) for k in CHANNEL_KEYS}
        item["settings"] = settings
        if needs:
            item["secrets_omitted"] = needs
        channels.append(item)
    return {
        "nigrani_backup": BACKUP_VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_version": app_version,
        "includes_secrets": include_secrets,
        "rules": [{k: r.get(k) for k in RULE_KEYS} for r in store.list_rules()],
        "channels": channels,
        "silences": [{k: s.get(k) for k in ("server", "source_id", "match", "until", "reason")}
                     for s in store.list_silences()],
    }


# ------------------------------------------------------------------ import
@dataclass
class Item:
    kind: str                 # rule | channel | silence
    id: str
    action: str               # create | update | skip
    detail: str = ""


@dataclass
class Plan:
    mode: str
    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rules: list[tuple[RuleIn, bool]] = field(default_factory=list)                # (rule, existed)
    channels: list[tuple[ChannelIn, bool, list[str]]] = field(default_factory=list)   # (channel, existed, missing secrets)
    silences: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for it in self.items:
            out.setdefault(it.kind + "s", {"create": 0, "update": 0, "skip": 0})[it.action] += 1
        return out

    def view(self) -> dict[str, Any]:
        return {
            "ok": not self.errors, "mode": self.mode, "summary": self.summary(), "errors": self.errors,
            "items": [vars(i) for i in self.items],
            "needs_secrets": [{"channel": c.id, "keys": miss} for c, _, miss in self.channels if miss],
        }


def _problem(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" if err["loc"] else err["msg"]
                     for err in e.errors())


def plan_import(store: Store, data: Any, *, mode: Mode, server_ids: set[str], datasources: set[str]) -> Plan:
    plan = Plan(mode=mode)
    if not isinstance(data, dict) or data.get("nigrani_backup") != BACKUP_VERSION:
        plan.errors.append("this is not a Nigrani backup file (or it was made by a newer version)")
        return plan
    rules, channels, silences = (data.get(k) or [] for k in ("rules", "channels", "silences"))
    for name, items in (("rules", rules), ("channels", channels), ("silences", silences)):
        if not isinstance(items, list):
            plan.errors.append(f"'{name}' must be a list")
            return plan
        if len(items) > MAX_ITEMS:
            plan.errors.append(f"too many {name} (limit {MAX_ITEMS})")
            return plan

    def unknown(servers: list[str]) -> str | None:
        bad = [s for s in servers if s not in server_ids]
        return f"unknown server(s): {', '.join(bad)}" if bad else None

    seen: set[tuple[str, str]] = set()
    for raw in rules:
        rid = raw.get("id") if isinstance(raw, dict) else None
        label = f"rule {rid or '?'}"
        try:
            rule = RuleIn(**{k: v for k, v in raw.items() if k in RULE_KEYS and k != "source"})
        except (ValidationError, TypeError, AttributeError) as e:
            plan.errors.append(f"{label}: {_problem(e) if isinstance(e, ValidationError) else 'not an object'}")
            continue
        if not rule.id:
            plan.errors.append("a rule has no id")
            continue
        err = rule_problem(rule) or unknown(rule.servers)
        if not err and rule.datasource not in datasources:
            err = f"uses {rule.datasource} but no server has it configured"
        if not err and ("rule", rule.id) in seen:
            err = "appears twice in the file"
        if err:
            plan.errors.append(f"{label}: {err}")
            continue
        seen.add(("rule", rule.id))
        existed = store.get_rule(rule.id) is not None
        action = ("skip" if mode == "merge" else "update") if existed else "create"
        plan.items.append(Item("rule", rule.id, action, "already exists" if action == "skip" else ""))
        if action != "skip":
            plan.rules.append((rule, existed))

    for raw in channels:
        cid = raw.get("id") if isinstance(raw, dict) else None
        label = f"channel {cid or '?'}"
        try:
            ch = ChannelIn(**{k: v for k, v in raw.items() if k in CHANNEL_KEYS})
        except (ValidationError, TypeError, AttributeError) as e:
            plan.errors.append(f"{label}: {_problem(e) if isinstance(e, ValidationError) else 'not an object'}")
            continue
        if not ch.id:
            plan.errors.append("a channel has no id")
            continue
        err = unknown(ch.servers) or msg_templates.validate(ch.title_template, ch.body_template)
        if not err and ("channel", ch.id) in seen:
            err = "appears twice in the file"
        if err:
            plan.errors.append(f"{label}: {err}")
            continue
        seen.add(("channel", ch.id))
        existing = store.get_channel(ch.id)
        secret = _secret_keys(ch.type)
        omitted = [k for k in secret if k in ch.settings and ch.settings[k] is None]
        missing = [k for k in omitted if not (existing and existing["settings"].get(k))]   # env refs count as present
        action = ("skip" if mode == "merge" else "update") if existing else "create"
        detail = "already exists" if action == "skip" else (
            f"created disabled until you add: {', '.join(missing)}" if missing and not existing else "")
        plan.items.append(Item("channel", ch.id, action, detail))
        if action != "skip":
            plan.channels.append((ch, existing is not None, missing))

    now = time.time()
    existing_sil = store.list_silences(now)
    for raw in silences:
        if not isinstance(raw, dict) or not isinstance(raw.get("until"), (int, float)):
            plan.errors.append("a silence has no valid 'until'")
            continue
        sid = f"{raw.get('source_id') or 'all rules'}{' / ' + raw['match'] if raw.get('match') else ''}"
        if raw.get("server") and raw["server"] not in server_ids:
            plan.errors.append(f"silence {sid}: unknown server(s): {raw['server']}")
            continue
        if raw["until"] <= now:
            plan.items.append(Item("silence", sid, "skip", "already expired"))
            continue
        dup = any(s["source_id"] == (raw.get("source_id") or None) and s["match"] == (raw.get("match") or None)
                  and s.get("server") == (raw.get("server") or None) and abs(s["until"] - raw["until"]) < 1
                  for s in existing_sil)
        plan.items.append(Item("silence", sid, "skip" if dup else "create", "already present" if dup else ""))
        if not dup:
            plan.silences.append(raw)
    return plan


def apply_import(store: Store, plan: Plan, *, preset_rule_ids: set[str], drop_rule) -> None:
    """Apply a plan that has no errors. Nothing is ever deleted."""
    assert not plan.errors
    for rule, _ in plan.rules:
        # a rule whose preset is not enabled here would be swept away at the next start if it kept source=preset
        source = "preset" if rule.id in preset_rule_ids else "custom"
        store.save_rule({**rule.model_dump(), "source": source})
        if not rule.enabled:
            drop_rule(rule.id)                      # close anything it still had open, quietly
    for ch, existed, missing in plan.channels:
        settings = dict(ch.settings)
        if existed:                                 # keep the stored credentials the backup left out
            current = (store.get_channel(ch.id) or {}).get("settings", {})
            for k, v in list(settings.items()):
                if v is None and k in current:
                    settings[k] = current[k]
        else:
            settings = {k: v for k, v in settings.items() if v is not None}
        store.save_channel({**ch.model_dump(), "settings": settings,
                            "enabled": ch.enabled and not (missing and not existed)})
    for s in plan.silences:
        store.add_silence(s.get("source_id"), s.get("match"), float(s["until"]), s.get("reason") or "", s.get("server"))
