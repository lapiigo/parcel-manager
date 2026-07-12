"""
UPS tracking via 17track.net API.

Register free (2 min, no business info): https://api.17track.net
Set SEVENTEENTRACK_API_KEY in .env after registration.

Free tier: 100 trackings/month.
Paid plans available if needed.

Flow:
1. POST /track/v2/register  — register tracking number (idempotent)
2. POST /track/v2/gettrackinfo  — get full event list
3. Find first delivered event → return UTC datetime

When UPS_CLIENT_ID + UPS_CLIENT_SECRET are set, uses official UPS API instead
(more reliable, unlimited). 17track is the fallback / quick-start option.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# ── 17track ──────────────────────────────────────────────────────────────────
_17T_BASE = "https://api.17track.net/track/v2"
_17T_CARRIER_UPS = 100046

# ── UPS official API ──────────────────────────────────────────────────────────
_UPS_TOKEN_URL = "https://onlinetools.ups.com/security/v1/oauth/token"
_UPS_TRACK_URL = "https://onlinetools.ups.com/api/track/v1/details/{tn}"

_ups_access_token: Optional[str] = None
_ups_token_expires_at: float = 0.0


class UPSError(Exception):
    pass


def make_session() -> requests.Session:
    return requests.Session()


# ── shared helpers ────────────────────────────────────────────────────────────

def _parse_gmt_offset(offset_str: str) -> Optional[timedelta]:
    s = (offset_str or "").strip().replace(":", "")
    if not s:
        return None
    try:
        sign = -1 if s.startswith("-") else 1
        s = s.lstrip("+-")
        if len(s) <= 2:
            return timedelta(hours=sign * int(s))
        elif len(s) == 3:
            return timedelta(hours=sign * int(s[:1]), minutes=sign * int(s[1:]))
        elif len(s) == 4:
            return timedelta(hours=sign * int(s[:2]), minutes=sign * int(s[2:]))
        elif len(s) == 5:
            return timedelta(hours=sign * int(s[:2]), minutes=sign * int(s[3:]))
    except (ValueError, IndexError):
        pass
    return None


def _parse_dt_to_utc(date_str: str, time_str: str = "", gmt_offset: Optional[timedelta] = None) -> Optional[datetime]:
    """Parse date/time strings and return naive UTC datetime."""
    combined = f"{date_str} {time_str}".strip()
    parsed: Optional[datetime] = None
    for fmt in (
        "%Y%m%d %H%M%S",
        "%Y%m%d %H%M",
        "%Y%m%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
    ):
        try:
            parsed = datetime.strptime(combined, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    if gmt_offset is not None:
        return parsed.replace(tzinfo=timezone(gmt_offset)).astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


# ── 17track implementation ────────────────────────────────────────────────────

def _17track_headers() -> dict:
    key = os.getenv("SEVENTEENTRACK_API_KEY", "")
    if not key:
        raise UPSError(
            "SEVENTEENTRACK_API_KEY is not set. "
            "Register free at https://api.17track.net"
        )
    return {"17token": key, "Content-Type": "application/json"}


def _get_status_via_17track(tn: str) -> dict:
    headers = _17track_headers()
    payload = [{"number": tn, "carrier": _17T_CARRIER_UPS}]

    # Register (idempotent)
    try:
        requests.post(f"{_17T_BASE}/register", json=payload, headers=headers, timeout=15)
    except requests.RequestException:
        pass  # non-fatal

    # Fetch
    try:
        r = requests.post(f"{_17T_BASE}/gettrackinfo", json=payload, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise UPSError(f"17track network error: {exc}") from exc

    if r.status_code == 401:
        raise UPSError("Invalid SEVENTEENTRACK_API_KEY")
    if r.status_code != 200:
        raise UPSError(f"17track HTTP {r.status_code}: {r.text[:200]}")

    data = r.json()
    if data.get("code") != 0:
        raise UPSError(f"17track API error: {data}")

    accepted = (data.get("data") or {}).get("accepted") or []
    if not accepted:
        rejected = (data.get("data") or {}).get("rejected") or []
        reason = rejected[0].get("error", {}).get("message", "rejected") if rejected else "no data"
        raise UPSError(f"17track rejected {tn}: {reason}")

    track_info = accepted[0].get("track") or {}
    events: list[dict] = track_info.get("z1") or []

    for event in events:
        status_code = str(event.get("z") or event.get("status") or "")
        description = (event.get("z1") or event.get("d") or "").lower()

        # 40 = Delivered in 17track status codes
        if status_code == "40" or "deliver" in description:
            dt_str = event.get("a") or event.get("date") or ""
            delivered_at = _parse_dt_to_utc(dt_str)
            return {
                "delivered": True,
                "delivered_at": delivered_at,
                "status": event.get("z1") or event.get("d") or "Delivered",
            }

    # Not yet delivered — return current status
    latest_status = ""
    if events:
        latest_status = events[0].get("z1") or events[0].get("d") or ""

    return {"delivered": False, "delivered_at": None, "status": latest_status}


# ── UPS official API implementation ──────────────────────────────────────────

def _get_ups_token() -> str:
    global _ups_access_token, _ups_token_expires_at
    if _ups_access_token and time.time() < _ups_token_expires_at - 60:
        return _ups_access_token
    client_id = os.getenv("UPS_CLIENT_ID", "")
    client_secret = os.getenv("UPS_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise UPSError("UPS_CLIENT_ID / UPS_CLIENT_SECRET not set")
    r = requests.post(
        _UPS_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if r.status_code != 200:
        raise UPSError(f"UPS token HTTP {r.status_code}")
    data = r.json()
    _ups_access_token = data["access_token"]
    _ups_token_expires_at = time.time() + int(data.get("expires_in", 3600))
    return _ups_access_token


def _get_status_via_ups_api(tn: str) -> dict:
    token = _get_ups_token()
    r = requests.get(
        _UPS_TRACK_URL.format(tn=tn),
        headers={"Authorization": f"Bearer {token}", "transId": f"pm-{tn[-8:]}", "transactionSrc": "parcel-manager"},
        params={"locale": "en_US", "returnSignature": "false"},
        timeout=15,
    )
    if r.status_code == 404:
        raise UPSError("Tracking number not found")
    if r.status_code != 200:
        raise UPSError(f"UPS API HTTP {r.status_code}: {r.text[:200]}")

    package = r.json()["trackResponse"]["shipment"][0]["package"][0]
    activity = package.get("activity") or []
    if not activity:
        raise UPSError("No activity in response")

    latest = activity[0]
    status_obj = latest.get("status") or {}
    status_str = status_obj.get("description", "").strip()
    is_delivered = status_obj.get("type", "").upper() == "D" or "delivered" in status_str.lower()

    delivered_at: Optional[datetime] = None
    if is_delivered:
        gmt_offset = _parse_gmt_offset(latest.get("gmtOffset", ""))
        delivered_at = _parse_dt_to_utc(latest.get("date", ""), latest.get("time", ""), gmt_offset)

    return {"delivered": is_delivered, "delivered_at": delivered_at, "status": status_str}


# ── public interface ──────────────────────────────────────────────────────────

def get_tracking_status(
    tracking_number: str,
    session: Optional[requests.Session] = None,
) -> dict:
    """
    Check UPS delivery status.

    Uses UPS official API if UPS_CLIENT_ID is set, otherwise 17track.net.

    Returns:
        {"delivered": bool, "delivered_at": datetime|None, "status": str}

    Raises UPSError on failure.
    """
    tn = tracking_number.strip()

    if os.getenv("UPS_CLIENT_ID", ""):
        return _get_status_via_ups_api(tn)
    else:
        return _get_status_via_17track(tn)
