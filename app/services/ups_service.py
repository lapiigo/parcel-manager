"""
UPS tracking via direct browser-emulation (no API key required).

Flow:
1. GET tracking page → picks up session cookies incl. XSRF token
2. POST to UPS internal API with the XSRF token
3. Parse packageStatus + deliveredDate/Time + gmtOffset from response
4. Convert local UPS time → UTC using the gmtOffset UPS provides

UPS always returns local time at the delivery location and typically
includes a gmtOffset field (e.g. "-04:00") in the response.
We use that offset for exact UTC conversion passed to Keepa.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _parse_gmt_offset(offset_str: str) -> Optional[timedelta]:
    """
    Parse UPS gmtOffset into a timedelta.
    Handles: "-04:00", "-4", "+5:30", "-0500", "4", "-04", etc.
    Returns None if offset_str is empty or unparseable.
    """
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
    """
    Parse UPS date+time strings and convert to naive UTC datetime.

    If gmt_offset is provided (from UPS response), uses it for exact conversion.
    If gmt_offset is None (UPS didn't include it), returns naive datetime as-is
    with no timezone assumption — caller should treat it as approximate.
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

    if gmt_offset is not None:
        # UPS gave us the local timezone offset — convert to exact UTC
        local_tz = timezone(gmt_offset)
        return parsed.replace(tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None)

    # No offset info — return as-is (naive, approximate local time)
    return parsed


def get_tracking_status(
    tracking_number: str,
    session: Optional[requests.Session] = None,
) -> dict:
    """
    Check UPS delivery status for a single tracking number.

    Returns:
        {
            "delivered": bool,
            "delivered_at": datetime | None,  # naive UTC if gmtOffset known, else approx local
            "status": str,
        }

    Raises UPSError on network or parse failure.
    """
    if session is None:
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
        raise UPSError(f"UPS API HTTP {resp.status_code}")

    try:
        data = resp.json()
    except Exception as exc:
        raise UPSError(f"Non-JSON UPS response: {exc}") from exc

    details = data.get("trackDetails") or []
    if not details:
        raise UPSError("No trackDetails in response")

    detail = details[0]
    status_str = (detail.get("packageStatus") or "").strip()
    is_delivered = "delivered" in status_str.lower()

    delivered_at: Optional[datetime] = None
    if is_delivered:
        # UPS typically provides gmtOffset in the top-level detail or in activity events
        raw_offset = (
            detail.get("gmtOffset")
            or detail.get("gmtOffsetHours")
            or detail.get("timeZoneOffset")
            or ""
        )
        # Also check first activity event for offset if top-level missing
        if not raw_offset:
            activities = (
                detail.get("shipmentProgressActivities")
                or detail.get("activities")
                or []
            )
            if activities:
                raw_offset = (
                    activities[0].get("gmtOffset")
                    or activities[0].get("timeZoneOffset")
                    or ""
                )

        gmt_offset = _parse_gmt_offset(str(raw_offset)) if raw_offset else None

        delivered_at = _to_utc(
            detail.get("deliveredDate", ""),
            detail.get("deliveredTime", ""),
            gmt_offset,
        )

    return {
        "delivered": is_delivered,
        "delivered_at": delivered_at,
        "status": status_str,
    }
