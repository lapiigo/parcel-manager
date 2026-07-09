"""
UPS tracking via direct browser-emulation (no API key required).

Flow:
1. GET tracking page → picks up session cookies incl. XSRF token
2. POST to UPS internal API with the XSRF token
3. Parse packageStatus / deliveredDate from response

Handles UPS tracking numbers starting with 1Z.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import requests

_PAGE_URL = "https://www.ups.com/track"
_API_URL = "https://www.ups.com/track/api/Track/GetStatus"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


class UPSError(Exception):
    pass


def make_session() -> requests.Session:
    """Create a new browser-like session. Reuse across multiple tracking calls."""
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _parse_dt(date_str: str, time_str: str = "") -> Optional[datetime]:
    """
    Parse UPS delivery date/time and return end-of-day UTC.

    UPS gives local recipient time, which we can't reliably convert to UTC
    without knowing the delivery timezone. To avoid underestimating the
    Keepa lookup time, we normalize to 23:59:59 of the delivery date —
    Keepa's _price_at finds the last known price *at or before* this
    timestamp, so end-of-day gives the correct price for the delivery day
    regardless of what local timezone UPS used.
    """
    combined = f"{date_str} {time_str}".strip()
    parsed: Optional[datetime] = None
    for fmt in (
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
    # Normalize to end-of-day so Keepa always finds the delivery-day price
    return parsed.replace(hour=23, minute=59, second=59, microsecond=0)


def get_tracking_status(
    tracking_number: str,
    session: Optional[requests.Session] = None,
) -> dict:
    """
    Check UPS delivery status for a single tracking number.

    Returns:
        {
            "delivered": bool,
            "delivered_at": datetime | None,  # naive, UTC-approximate
            "status": str,                    # raw status from UPS
        }

    Raises UPSError on network or parse failure.
    """
    own_session = session is None
    if own_session:
        session = make_session()

    tn = tracking_number.strip()

    # Step 1: load tracking page to acquire cookies / XSRF token
    try:
        session.get(
            _PAGE_URL,
            params={"track": "yes", "trackNums": tn, "requester": "MB/trackdetails"},
            timeout=15,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise UPSError(f"GET page failed: {exc}") from exc

    xsrf = (
        session.cookies.get("X-XSRF-TOKEN-ST")
        or session.cookies.get("X-XSRF-TOKEN")
        or ""
    )

    # Step 2: call internal tracking API
    try:
        resp = session.post(
            _API_URL,
            params={"loc": "en_US"},
            json={"Locale": "en_US", "TrackingNumber": [tn]},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json",
                "X-XSRF-TOKEN": xsrf,
                "Referer": f"{_PAGE_URL}?track=yes&trackNums={tn}",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://www.ups.com",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise UPSError(f"POST API failed: {exc}") from exc

    if resp.status_code != 200:
        raise UPSError(f"UPS API HTTP {resp.status_code} for {tn}")

    try:
        data = resp.json()
    except Exception as exc:
        raise UPSError(f"Non-JSON UPS response for {tn}: {exc}") from exc

    details = data.get("trackDetails") or []
    if not details:
        raise UPSError(f"No trackDetails in UPS response for {tn}")

    detail = details[0]
    status_str = (detail.get("packageStatus") or "").strip()
    is_delivered = "delivered" in status_str.lower()

    delivered_at: Optional[datetime] = None
    if is_delivered:
        delivered_at = _parse_dt(
            detail.get("deliveredDate", ""),
            detail.get("deliveredTime", ""),
        )

    return {
        "delivered": is_delivered,
        "delivered_at": delivered_at,
        "status": status_str,
    }
