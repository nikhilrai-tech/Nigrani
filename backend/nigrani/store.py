"""SQLite persistence: rules, channels, incidents, silences, uptime checks, kv."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, expr TEXT NOT NULL,
  direction TEXT NOT NULL DEFAULT 'above', warn REAL, crit REAL,
  for_seconds INTEGER NOT NULL DEFAULT 300, unit TEXT NOT NULL DEFAULT 'short',
  summary TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL DEFAULT 'custom', updated_at REAL, servers TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}',
  min_severity TEXT NOT NULL DEFAULT 'warning', quiet_start INTEGER, quiet_end INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1, last_error TEXT, last_sent_at REAL, servers TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, series_key TEXT NOT NULL,
  rule_name TEXT NOT NULL, series TEXT NOT NULL DEFAULT '', labels TEXT NOT NULL DEFAULT '{}',
  severity TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'firing',
  started_at REAL NOT NULL, resolved_at REAL, last_value REAL, peak_value REAL,
  threshold REAL, unit TEXT NOT NULL DEFAULT 'short', summary TEXT NOT NULL DEFAULT '',
  last_notified REAL, silenced INTEGER NOT NULL DEFAULT 0, server TEXT NOT NULL DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS ix_incidents_status ON incidents(status, started_at);
CREATE TABLE IF NOT EXISTS silences (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, match TEXT,
  until REAL NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, server TEXT
);
CREATE TABLE IF NOT EXISTS check_results (
  check_id TEXT NOT NULL, ts REAL NOT NULL, ok INTEGER NOT NULL, latency_ms REAL, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_checks ON check_results(check_id, ts);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release to databases created by older versions."""
        wanted = {
            "rules": [("servers", "TEXT NOT NULL DEFAULT '[]'")],
            "channels": [("servers", "TEXT NOT NULL DEFAULT '[]'")],
            "incidents": [("server", "TEXT NOT NULL DEFAULT 'default'")],
            "silences": [("server", "TEXT")],
        }
        for table, cols in wanted.items():
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid or 0

    # ---- kv ----
    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def kv_set(self, key: str, value: Any) -> None:
        self._x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    # ---- rules ----
    def seed_rule(self, r: dict[str, Any]) -> None:
        self._x(
            "INSERT OR IGNORE INTO rules(id,name,expr,direction,warn,crit,for_seconds,unit,summary,enabled,source,updated_at,servers)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r["name"], r["expr"], r["direction"], r.get("warn"), r.get("crit"), r["for_seconds"],
             r["unit"], r.get("summary", ""), int(r.get("enabled", True)), "preset", time.time(),
             json.dumps(r.get("servers") or [])),
        )

    def save_rule(self, r: dict[str, Any]) -> dict[str, Any]:
        self._x(
            "INSERT INTO rules(id,name,expr,direction,warn,crit,for_seconds,unit,summary,enabled,source,updated_at,servers)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, expr=excluded.expr,"
            " direction=excluded.direction, warn=excluded.warn, crit=excluded.crit, for_seconds=excluded.for_seconds,"
            " unit=excluded.unit, summary=excluded.summary, enabled=excluded.enabled, updated_at=excluded.updated_at,"
            " servers=excluded.servers",
            (r["id"], r["name"], r["expr"], r["direction"], r.get("warn"), r.get("crit"), r["for_seconds"],
             r["unit"], r.get("summary", ""), int(r.get("enabled", True)), r.get("source", "custom"), time.time(),
             json.dumps(r.get("servers") or [])),
        )
        return self.get_rule(r["id"])  # type: ignore[return-value]

    def list_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rules" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY source, name"
        return [self._rule(r) for r in self._q(sql)]

    def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM rules WHERE id=?", (rule_id,))
        return self._rule(rows[0]) if rows else None

    def delete_rule(self, rule_id: str) -> None:
        self._x("DELETE FROM rules WHERE id=?", (rule_id,))

    @staticmethod
    def _rule(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        d["servers"] = json.loads(d.get("servers") or "[]")
        return d

    # ---- channels ----
    def seed_channel(self, c: dict[str, Any]) -> None:
        qh = c.get("quiet_hours") or [None, None]
        self._x(
            "INSERT OR IGNORE INTO channels(id,name,type,settings,min_severity,quiet_start,quiet_end,enabled,servers)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (c["id"], c["name"], c["type"], json.dumps(c.get("settings", {})), c.get("min_severity", "warning"),
             qh[0], qh[1], int(c.get("enabled", True)), json.dumps(c.get("servers") or [])),
        )

    def save_channel(self, c: dict[str, Any]) -> dict[str, Any]:
        self._x(
            "INSERT INTO channels(id,name,type,settings,min_severity,quiet_start,quiet_end,enabled,servers)"
            " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type,"
            " settings=excluded.settings, min_severity=excluded.min_severity, quiet_start=excluded.quiet_start,"
            " quiet_end=excluded.quiet_end, enabled=excluded.enabled, servers=excluded.servers",
            (c["id"], c["name"], c["type"], json.dumps(c.get("settings", {})), c.get("min_severity", "warning"),
             c.get("quiet_start"), c.get("quiet_end"), int(c.get("enabled", True)), json.dumps(c.get("servers") or [])),
        )
        return self.get_channel(c["id"])  # type: ignore[return-value]

    def list_channels(self) -> list[dict[str, Any]]:
        return [self._channel(r) for r in self._q("SELECT * FROM channels ORDER BY name")]

    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM channels WHERE id=?", (channel_id,))
        return self._channel(rows[0]) if rows else None

    def delete_channel(self, channel_id: str) -> None:
        self._x("DELETE FROM channels WHERE id=?", (channel_id,))

    def channel_result(self, channel_id: str, error: str | None) -> None:
        self._x("UPDATE channels SET last_error=?, last_sent_at=? WHERE id=?", (error, time.time(), channel_id))

    @staticmethod
    def _channel(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["settings"] = json.loads(d["settings"] or "{}")
        d["enabled"] = bool(d["enabled"])
        d["servers"] = json.loads(d.get("servers") or "[]")
        return d

    # ---- incidents ----
    def create_incident(self, **f: Any) -> int:
        return self._x(
            "INSERT INTO incidents(source_id,series_key,rule_name,series,labels,severity,status,started_at,"
            "last_value,peak_value,threshold,unit,summary,last_notified,silenced,server) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["source_id"], f["series_key"], f["rule_name"], f.get("series", ""), json.dumps(f.get("labels", {})),
             f["severity"], "firing", f["started_at"], f.get("value"), f.get("value"), f.get("threshold"),
             f.get("unit", "short"), f.get("summary", ""), f.get("last_notified"), int(f.get("silenced", False)),
             f.get("server", "default")),
        )

    def update_incident(self, incident_id: int, **f: Any) -> None:
        if not f:
            return
        cols = ", ".join(f"{k}=?" for k in f)
        self._x(f"UPDATE incidents SET {cols} WHERE id=?", (*f.values(), incident_id))

    def resolve_incident(self, incident_id: int, now: float, last_value: float | None) -> None:
        self._x("UPDATE incidents SET status='resolved', resolved_at=?, last_value=COALESCE(?, last_value) WHERE id=?",
                (now, last_value, incident_id))

    def get_incident(self, incident_id: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM incidents WHERE id=?", (incident_id,))
        return self._incident(rows[0]) if rows else None

    def open_incidents(self) -> list[dict[str, Any]]:
        return [self._incident(r) for r in self._q(
            "SELECT * FROM incidents WHERE status='firing' ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END, started_at")]

    def recent_incidents(self, limit: int = 100, since: float | None = None,
                         server: str | None = None) -> list[dict[str, Any]]:
        if server:
            rows = self._q("SELECT * FROM incidents WHERE started_at >= ? AND server=? ORDER BY started_at DESC LIMIT ?",
                           (since or 0, server, limit))
        else:
            rows = self._q("SELECT * FROM incidents WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?",
                           (since or 0, limit))
        return [self._incident(r) for r in rows]

    @staticmethod
    def _incident(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["labels"] = json.loads(d["labels"] or "{}")
        d["silenced"] = bool(d["silenced"])
        return d

    # ---- silences ----
    def add_silence(self, source_id: str | None, match: str | None, until: float, reason: str,
                    server: str | None = None) -> int:
        return self._x("INSERT INTO silences(source_id,match,until,reason,created_at,server) VALUES(?,?,?,?,?,?)",
                       (source_id or None, match or None, until, reason, time.time(), server or None))

    def list_silences(self, now: float | None = None) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM silences WHERE until > ? ORDER BY until", (now or time.time(),))
        return [dict(r) for r in rows]

    def delete_silence(self, silence_id: int) -> None:
        self._x("DELETE FROM silences WHERE id=?", (silence_id,))

    def is_silenced(self, source_id: str, series: str, now: float, server: str = "default") -> bool:
        for s in self.list_silences(now):
            if s["source_id"] and s["source_id"] != source_id:
                continue
            if s.get("server") and s["server"] != server:
                continue
            if s["match"] and s["match"].lower() not in series.lower():
                continue
            return True
        return False

    # ---- uptime checks ----
    def add_check_result(self, check_id: str, ts: float, ok: bool, latency_ms: float | None, detail: str) -> None:
        self._x("INSERT INTO check_results(check_id,ts,ok,latency_ms,detail) VALUES(?,?,?,?,?)",
                (check_id, ts, int(ok), latency_ms, detail))

    def check_uptime(self, check_id: str, since: float) -> tuple[int, int]:
        r = self._q("SELECT COALESCE(SUM(ok),0) AS up, COUNT(*) AS n FROM check_results WHERE check_id=? AND ts>=?",
                    (check_id, since))[0]
        return int(r["up"]), int(r["n"])

    def last_check_result(self, check_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM check_results WHERE check_id=? ORDER BY ts DESC LIMIT 1", (check_id,))
        return _row(rows[0]) if rows else None

    def check_history(self, check_id: str, since: float, limit: int = 120) -> list[dict[str, Any]]:
        rows = self._q("SELECT ts, ok, latency_ms FROM check_results WHERE check_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                       (check_id, since, limit))
        return [dict(r) for r in reversed(rows)]

    def prune(self, retention_days: int) -> None:
        cutoff = time.time() - retention_days * 86400
        self._x("DELETE FROM check_results WHERE ts < ?", (cutoff,))
        self._x("DELETE FROM incidents WHERE status='resolved' AND resolved_at < ?", (cutoff,))
        self._x("DELETE FROM silences WHERE until < ?", (time.time() - 86400,))
