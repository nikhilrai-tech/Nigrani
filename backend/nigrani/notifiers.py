"""Notification channels. Settings may reference env vars as ${NAME}; they are expanded at send time
so secrets never have to be stored in the database."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import expand_env
from .format import SEV_COLOR
from .store import Store

log = logging.getLogger("nigrani.notify")
SEV_RANK = {"info": 0, "warning": 1, "critical": 2}
SECRET_KEYS = {"webhook_url", "bot_token", "password", "auth_token", "secret", "url", "to_number"}


def _s(settings: dict[str, Any]) -> dict[str, Any]:
    return {k: expand_env(v) if isinstance(v, str) else v for k, v in settings.items()}


def _need(s: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if not s.get(k)]
    if missing:
        raise ValueError(f"missing setting(s): {', '.join(missing)}")


def _check_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    return url


async def _post(client: httpx.AsyncClient, url: str, **kw: Any) -> None:
    r = await client.post(_check_url(url), timeout=15, **kw)
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")


async def send_slack(client: httpx.AsyncClient, s: dict[str, Any], ev: dict[str, Any]) -> None:
    _need(s, "webhook_url")
    ctx = f"{ev['environment']}" + (f" · <{ev['link']}|Open Nigrani>" if ev["link"] else "")
    body = {
        "text": ev["title"],
        "attachments": [{
            "color": "#" + SEV_COLOR.get(ev["severity"], "2e90fa"),
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{ev['title']}*\n{ev['body']}"}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]},
            ],
        }],
    }
    await _post(client, s["webhook_url"], json=body)


async def send_teams(client: httpx.AsyncClient, s: dict[str, Any], ev: dict[str, Any]) -> None:
    # Works with Teams "Workflows" webhooks (Adaptive Card envelope).
    _need(s, "webhook_url")
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": ev["title"], "weight": "Bolder", "size": "Medium", "wrap": True},
            {"type": "TextBlock", "text": ev["body"] or " ", "wrap": True},
            {"type": "TextBlock", "text": ev["environment"], "isSubtle": True, "spacing": "Small"},
        ],
        "actions": [{"type": "Action.OpenUrl", "title": "Open Nigrani", "url": ev["link"]}] if ev["link"] else [],
    }
    body = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}
    await _post(client, s["webhook_url"], json=body)


async def send_discord(client: httpx.AsyncClient, s: dict[str, Any], ev: dict[str, Any]) -> None:
    _need(s, "webhook_url")
    body = {"embeds": [{
        "title": ev["title"][:250], "description": ev["body"][:3500],
        "color": int(SEV_COLOR.get(ev["severity"], "2e90fa"), 16),
        "footer": {"text": ev["environment"]},
        **({"url": ev["link"]} if ev["link"] else {}),
    }]}
    await _post(client, s["webhook_url"], json=body)


async def send_telegram(client: httpx.AsyncClient, s: dict[str, Any], ev: dict[str, Any]) -> None:
    _need(s, "bot_token", "chat_id")
    text = f"<b>{html.escape(ev['title'])}</b>\n{html.escape(ev['body'])}\n<i>{html.escape(ev['environment'])}</i>"
    await _post(client, f"https://api.telegram.org/bot{s['bot_token']}/sendMessage",
                json={"chat_id": s["chat_id"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})


async def send_whatsapp(client: httpx.AsyncClient, s: dict[str, Any], ev: dict[str, Any]) -> None:
    """WhatsApp via the Twilio API (WhatsApp Business / sandbox numbers)."""
    _need(s, "account_sid", "auth_token", "from_number", "to_number")
    def wa(n: str) -> str:
        return n if n.startswith("whatsapp:") else f"whatsapp:{n}"
    await _post(
        client, f"https://api.twilio.com/2010-04-01/Accounts/{s['account_sid']}/Messages.json",
        auth=(s["account_sid"], s["auth_token"]),
        data={"From": wa(s["from_number"]), "To": wa(s["to_number"]), "Body": f"{ev['title']}\n{ev['body']}"[:1500]},
    )


async def send_webhook(client: httpx.AsyncClient, s: dict[str, Any], ev: dict[str, Any]) -> None:
    _need(s, "url")
    raw = json.dumps(ev, default=str).encode()
    headers = {"Content-Type": "application/json"}
    if s.get("secret"):
        headers["X-Nigrani-Signature"] = "sha256=" + hmac.new(s["secret"].encode(), raw, hashlib.sha256).hexdigest()
    await _post(client, s["url"], content=raw, headers=headers)


def _send_email_sync(s: dict[str, Any], ev: dict[str, Any]) -> None:
    _need(s, "host", "from", "to")
    msg = EmailMessage()
    msg["Subject"] = ev["title"]
    msg["From"] = s["from"]
    to = s["to"] if isinstance(s["to"], list) else [x.strip() for x in str(s["to"]).split(",") if x.strip()]
    msg["To"] = ", ".join(to)
    msg.set_content(f"{ev['title']}\n\n{ev['body']}\n\n{ev['environment']}" + (f"\n{ev['link']}" if ev["link"] else ""))
    port = int(s.get("port", 587))
    if s.get("ssl") or port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(s["host"], port, timeout=20)
    else:
        server = smtplib.SMTP(s["host"], port, timeout=20)
    with server:
        if not (s.get("ssl") or port == 465) and s.get("starttls", True):
            server.starttls()
        if s.get("username"):
            server.login(s["username"], s.get("password", ""))
        server.send_message(msg)


async def send_email(client: httpx.AsyncClient, s: dict[str, Any], ev: dict[str, Any]) -> None:
    await asyncio.to_thread(_send_email_sync, s, ev)


SENDERS = {
    "slack": send_slack, "teams": send_teams, "discord": send_discord, "telegram": send_telegram,
    "whatsapp": send_whatsapp, "webhook": send_webhook, "email": send_email,
}

# What each channel type needs, so the UI can render a form.
CHANNEL_FIELDS: dict[str, list[dict[str, Any]]] = {
    "slack": [{"key": "webhook_url", "label": "Incoming webhook URL", "secret": True}],
    "teams": [{"key": "webhook_url", "label": "Workflows webhook URL", "secret": True}],
    "discord": [{"key": "webhook_url", "label": "Webhook URL", "secret": True}],
    "telegram": [{"key": "bot_token", "label": "Bot token", "secret": True}, {"key": "chat_id", "label": "Chat ID"}],
    "whatsapp": [
        {"key": "account_sid", "label": "Twilio Account SID"}, {"key": "auth_token", "label": "Twilio auth token", "secret": True},
        {"key": "from_number", "label": "From (WhatsApp number)"}, {"key": "to_number", "label": "To (WhatsApp number)"}],
    "webhook": [{"key": "url", "label": "URL", "secret": True}, {"key": "secret", "label": "Signing secret (optional)", "secret": True}],
    "email": [
        {"key": "host", "label": "SMTP host"}, {"key": "port", "label": "Port (587)"}, {"key": "username", "label": "Username"},
        {"key": "password", "label": "Password", "secret": True}, {"key": "from", "label": "From address"},
        {"key": "to", "label": "To (comma separated)"}],
}


def mask_settings(ctype: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Hide secrets in API responses (env references like ${X} are safe to show)."""
    secret_keys = {f["key"] for f in CHANNEL_FIELDS.get(ctype, []) if f.get("secret")}
    out = {}
    for k, v in settings.items():
        if k in secret_keys and isinstance(v, str) and v and not v.startswith("${"):
            out[k] = "••••" + v[-4:] if len(v) > 8 else "••••"
        else:
            out[k] = v
    return out


class Notifier:
    def __init__(self, store: Store, timezone: str = "UTC", client: httpx.AsyncClient | None = None):
        self.store = store
        self.tz = ZoneInfo(timezone)
        self.client = client or httpx.AsyncClient()

    def _wanted(self, ch: dict[str, Any], ev: dict[str, Any], now: float) -> bool:
        if not ch["enabled"]:
            return False
        if ch.get("servers") and ev.get("server_id") and ev["server_id"] not in ch["servers"]:
            return False  # channel is scoped to other servers
        sev = ev["orig_severity"]
        direct = ev["kind"] in ("test", "digest")  # informational: not subject to severity / quiet hours
        if not direct and SEV_RANK.get(sev, 1) < SEV_RANK[ch["min_severity"]]:
            return False
        qs, qe = ch.get("quiet_start"), ch.get("quiet_end")
        if qs is not None and qe is not None and sev != "critical" and not direct:
            hour = datetime.fromtimestamp(now, self.tz).hour
            in_quiet = (qs <= hour < qe) if qs <= qe else (hour >= qs or hour < qe)
            if in_quiet:
                return False
        return True

    async def send_to(self, ch: dict[str, Any], ev: dict[str, Any]) -> str | None:
        """Deliver to one channel with retries. Returns an error string or None."""
        sender = SENDERS.get(ch["type"])
        if not sender:
            return f"unknown channel type {ch['type']}"
        settings = _s(ch["settings"])
        err: str | None = None
        for attempt in range(3):
            try:
                await sender(self.client, settings, ev)
                err = None
                break
            except Exception as e:  # noqa: BLE001 - surface any failure to the UI
                err = f"{type(e).__name__}: {e}"[:300]
                await asyncio.sleep(1.5 * (attempt + 1))
        self.store.channel_result(ch["id"], err)
        if err:
            log.warning("channel %s failed: %s", ch["id"], err)
        return err

    async def send(self, ev: dict[str, Any], *, only: list[str] | None = None, now: float | None = None) -> dict[str, int]:
        """Deliver to every matching channel. Returns {"wanted": n, "ok": m} so callers can retry total failures."""
        now = now or time.time()
        chans = [c for c in self.store.list_channels() if (not only or c["id"] in only) and self._wanted(c, ev, now)]
        errors = await asyncio.gather(*(self.send_to(c, ev) for c in chans)) if chans else []
        return {"wanted": len(chans), "ok": sum(1 for e in errors if e is None)}
