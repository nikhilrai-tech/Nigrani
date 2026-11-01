"""AWS Signature Version 4 (just enough to call CloudWatch; no AWS SDK needed)."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit


@dataclass(frozen=True)
class Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str = ""
    expires_at: float | None = None  # epoch seconds; None = does not expire


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign(method: str, url: str, headers: dict[str, str], body: bytes, *, region: str, service: str,
         creds: Credentials, now: datetime | None = None) -> dict[str, str]:
    """Return `headers` plus Host, X-Amz-Date, X-Amz-Security-Token (if any) and Authorization."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    parts = urlsplit(url)
    out = {k: v for k, v in headers.items()}
    out["Host"] = parts.netloc
    out["X-Amz-Date"] = stamp
    if creds.session_token:
        out["X-Amz-Security-Token"] = creds.session_token

    signed = {k.lower(): " ".join(v.split()) for k, v in out.items()}
    names = ";".join(sorted(signed))
    canonical_headers = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
    query = "&".join(sorted(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, _, v in (p.partition("=") for p in parts.query.split("&") if p)))
    canonical = "\n".join([method.upper(), quote(parts.path or "/", safe="/-_.~"), query, canonical_headers, names,
                           hashlib.sha256(body).hexdigest()])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + creds.secret_access_key).encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={creds.access_key_id}/{scope}, "
                            f"SignedHeaders={names}, Signature={signature}")
    return out
