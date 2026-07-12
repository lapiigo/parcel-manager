"""
UPS Tracking via official UPS Developer API (OAuth 2.0).

Registration (free): https://developer.ups.com
Create an app → get Client ID + Client Secret → set in .env:
    UPS_CLIENT_ID=...
    UPS_CLIENT_SECRET=...

The official API works from any server IP (no Akamai blocking).
Token is cached in-process and refreshed automatically when it expires.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

_TOKEN_URL = "https://onlinetools.ups.com/security/v1/oauth/token"
_TRACK_URL = "https://onlinetools.ups.com/api/track/v1/details/{tracking_number}"

# In-process token cache
_access_token: Optional[str] = None
_token_expires_at: float = 0.0


class UPSError(Exception):
    pass


def make_session() -> requests.Session:
    """Return a plain session — kept for interface compatibility with night_sync_service."""
    return requests.Session()


def _get_token() -> str:
    """Return a valid OAuth access token, refreshing if needed."""
    global _access_token, _token_expires_at

    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    client_id = os.getenv("UPS_CLIENT_ID", "")
    client_secret = os.getenv("UPS_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise UPSError(
            "UPS_CLIENT_ID and UPS_CLIENT_SECRET are not set in .env. "
            "Register free at https://developer.ups.com"
        )

    try:
        r = requests.post(
            _TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise UPSError(f"Token request failed: {exc}") from exc

    if r.status_code != 200:
        raise UPSError(f"Token endpoint HTTP {r.status_code}: {r.text[:200]}")

    data = r.json()
    _access_token = data.get("access_token")
    expires_in = int(data.get("expires_in", 3600))
    _token_expires_at = time.time() + expires_in
    return _access_token


def _parse_gmt_offset(offset_str: str) -> Optional[timedelta]:
    """Parse UPS gmtOffset string like '-04:00', '-4', '+5:30' → timedelta."""
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


def _to_utc(date_str: str, time_str: str, gmt_offset: Optional[timedelta]) -> Optional[datetime]:
    """Parse UPS date+time strings and return naive UTC datetime."""
    combined = f"{date_str} {time_str}".strip()
    parsed: Optional[datetime] = None
    for fmt in (
        "%Y%m%d %H%M%S",
        "%Y%m%d %H%M",
        "%Y%m%d",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
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


def get_tracking_status(
    tracking_number: str,
    session: Optional[requests.Session] = None,  # unused, kept for interface compat
) -> dict:
    """
    Check UPS delivery status via official API.

    Returns:
        {
            "delivered": bool,
            "delivered_at": datetime | None,  # naive UTC
            "status": str,
        }

    Raises UPSError on failure.
    """
    tn = tracking_number.strip()
    token = _get_token()

    try:
        resp = requests.get(
            _TRACK_URL.format(tracking_number=tn),
            headers={
                "Authorization": f"Bearer {token}",
                "transId": f"pm-{tn[-8:]}",
                "transactionSrc": "parcel-manager",
            },
            params={"locale": "en_US", "returnSignature": "false"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise UPSError(f"Tracking request failed: {exc}") from exc

    if resp.status_code == 404:
        raise UPSError("Tracking number not found")
    if resp.status_code != 200:
        raise UPSError(f"UPS API HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except Exception as exc:
        raise UPSError(f"Non-JSON response: {exc}") from exc

    # Navigate response: trackResponse.shipment[0].package[0]
    try:
        shipment = data["trackResponse"]["shipment"][0]
        package = shipment["package"][0]
    except (KeyError, IndexError) as exc:
        raise UPSError(f"Unexpected response structure: {exc}") from exc

    activity = package.get("activity") or []
    if not activity:
        raise UPSError("No activity in tracking response")

    # Latest activity is first in the list
    latest = activity[0]
    status_obj = latest.get("status") or {}
    status_str = status_obj.get("description", "").strip()
    status_type = status_obj.get("type", "").upper()
    is_delivered = status_type == "D" or "delivered" in status_str.lower()

    delivered_at: Optional[datetime] = None
    if is_delivered:
        raw_date = latest.get("date", "")  # e.g. "20241015"
        raw_time = latest.get("time", "")  # e.g. "143000"
        gmt_offset = _parse_gmt_offset(latest.get("gmtOffset", ""))
        delivered_at = _to_utc(raw_date, raw_time, gmt_offset)

    return {
        "delivered": is_delivered,
        "delivered_at": delivered_at,
        "status": status_str,
    }
