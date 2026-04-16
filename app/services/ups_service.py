"""
UPS delivery date lookup via 17track.net API.

Registration (free, 1 min): https://api.17track.net
Free tier: 100 trackings/month.

Set SEVENTEENTRACK_API_KEY in .env after registration.

Flow:
1. POST /track/v2/register  — register the tracking number (idempotent)
2. POST /track/v2/gettrackinfo  — get full event list
3. Find the first "delivered" event and return its datetime
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import requests

_BASE = "https://api.17track.net/track/v2"
_CARRIER_UPS = 100046  # 17track carrier code for UPS


class UPSError(Exception):
    pass


def _headers() -> dict:
    key = os.getenv("SEVENTEENTRACK_API_KEY", "")
    if not key:
        raise UPSError(
            "17TRACK_API_KEY is not set in .env. "
            "Register free at https://api.17track.net to get a key."
        )
    return {"17token": key, "Content-Type": "application/json"}


def _is_ups(tracking_number: str) -> bool:
    """UPS tracking numbers start with 1Z."""
    return tracking_number.strip().upper().startswith("1Z")


def _parse_dt(s: str) -> Optional[datetime]:
    """Parse 17track datetime string: '2024-10-15T14:30:00' or '2024-10-15 14:30:00'."""
    if not s:
        return None
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def get_delivery_datetime(tracking_number: str) -> Optional[datetime]:
    """
    Return the datetime the parcel was delivered, or None if not yet delivered.
    Raises UPSError on failure.
    """
    tn = tracking_number.strip()
    headers = _headers()

    # Register (idempotent — safe to call multiple times)
    payload = [{"number": tn}]
    if _is_ups(tn):
        payload = [{"number": tn, "carrier": _CARRIER_UPS}]

    try:
        requests.post(f"{_BASE}/register", json=payload, headers=headers, timeout=15)
    except requests.RequestException:
        pass  # registration failure is non-fatal; try gettrackinfo anyway

    # Fetch track info
    try:
        r = requests.post(
            f"{_BASE}/gettrackinfo",
            json=payload,
            headers=headers,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise UPSError(f"Network error: {exc}") from exc

    if r.status_code == 401:
        raise UPSError("Invalid 17track API key. Check SEVENTEENTRACK_API_KEY in .env.")
    if r.status_code != 200:
        raise UPSError(f"17track returned HTTP {r.status_code}: {r.text[:200]}")

    data = r.json()
    if data.get("code") != 0:
        raise UPSError(f"17track API error: {data.get('data', {})}")

    accepted = (data.get("data") or {}).get("accepted") or []
    if not accepted:
        errors = (data.get("data") or {}).get("rejected") or []
        msg = errors[0].get("error", {}).get("message", "unknown") if errors else "no data returned"
        raise UPSError(f"17track rejected tracking {tn}: {msg}")

    track_info = accepted[0].get("track") or {}
    events: list[dict] = track_info.get("z1") or []  # z1 = latest events

    for event in events:
        status_code = str(event.get("z") or event.get("status") or "")
        description = (event.get("z1") or event.get("d") or "").lower()

        # 40 = Delivered in 17track status codes; also check description
        if status_code == "40" or "deliver" in description:
            dt_str = event.get("a") or event.get("date") or ""
            dt = _parse_dt(dt_str)
            if dt:
                return dt

    return None  # Not yet delivered
