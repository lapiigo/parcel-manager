"""
Auto-sync service for housecargo.space.

Flow:
1. POST /api/login with browser-like headers  →  JWT token
2. GET  /api/deliveries                        →  list of delivery objects
3. For each delivery, collect ONLY typeId=2 tracks (client / outbound tracks).
   Create one Parcel per typeId=2 track; all share the same external_order_id.
   typeId=0 (inbound warehouse tracks) are ignored entirely.

Qty distribution: total_qty split evenly across tracks; remainder assigned
to the first track.  ASIN: items[i].asin matched by index; falls back to
items[0].asin when there are fewer items than tracks.

Field mapping:
  delivery.externalId              → parcel.external_order_id   (primary key)
  track.number  (typeId=2)         → parcel.tracking_number
  items[i].quantity / n_tracks     → parcel.qty
  items[i].asin                    → parcel.asin
  delivery.generalPrice / n_tracks → parcel.purchase_price
"""

from __future__ import annotations

import requests
from datetime import datetime

BASE_URL = "https://housecargo.space"
LOGIN_PATH = "/api/login"
DELIVERIES_PATH = "/api/deliveries"

_BROWSER_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json; charset=UTF-8",
    "origin": "https://housecargo.space",
    "referer": "https://housecargo.space/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
}


class HouseCargoError(Exception):
    pass


def _login(username: str, password: str) -> str:
    url = BASE_URL + LOGIN_PATH
    try:
        r = requests.post(
            url,
            json={"username": username, "password": password},
            headers=_BROWSER_HEADERS,
            cookies={"token": ""},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise HouseCargoError(f"Login request failed: {exc}") from exc

    if r.status_code != 200:
        raise HouseCargoError(f"Login failed (HTTP {r.status_code}): {r.text[:300]}")

    data = r.json()
    token = data.get("token") or data.get("access_token")
    if not token:
        raise HouseCargoError(f"No token in login response: {str(data)[:200]}")
    return token


def _fetch_deliveries(token: str, page: int = 1, per: int = 100) -> list[dict]:
    url = BASE_URL + DELIVERIES_PATH
    params = {"page": page, "per": per, "desc": "true", "sort": "firstOutTrackDate"}
    headers = {**_BROWSER_HEADERS, "Authorization": f"Bearer {token}"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise HouseCargoError(f"Deliveries request failed: {exc}") from exc

    if r.status_code == 401:
        raise HouseCargoError("Token expired — sync will re-login automatically on next attempt.")
    if r.status_code != 200:
        raise HouseCargoError(f"Deliveries fetch failed (HTTP {r.status_code}): {r.text[:200]}")

    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("data") or data.get("items") or data.get("deliveries") or []
    return []


def _outbound_tracks(tracks: list[dict]) -> list[dict]:
    """Return track objects for typeId=2 tracks only (client-facing)."""
    return [t for t in tracks if t.get("typeId") == 2 and t.get("number")]


def _parse_delivery_date(dt_str: str | None) -> datetime | None:
    """
    Parse ISO-8601 datetime and convert to UTC naive datetime for storage.
    e.g. "2026-04-10T14:11:56-04:00" → 2026-04-10 18:11:56 (UTC)
         "2026-04-10T18:11:56+00:00" → 2026-04-10 18:11:56 (UTC)
    """
    if not dt_str:
        return None
    import re
    from datetime import timezone, timedelta

    s = dt_str.strip()
    # Extract timezone offset if present: ±HH:MM at end
    tz_match = re.search(r'([+-])(\d{2}):(\d{2})$', s)
    tz_offset = None
    if tz_match:
        sign = 1 if tz_match.group(1) == '+' else -1
        tz_offset = timedelta(
            hours=int(tz_match.group(2)),
            minutes=int(tz_match.group(3))
        ) * sign
        s = s[:tz_match.start()]

    s = s.replace('T', ' ').strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if tz_offset is not None:
                # Convert to UTC: subtract the offset
                dt = dt - tz_offset
            return dt  # stored as naive UTC
        except ValueError:
            continue
    return None


def sync_transit_updates(supplier_id: int, username: str, password: str, db,
                         client_id: int | None = None) -> dict:
    """
    Fetch delivery updates from housecargo for parcels currently in `in_transit` status only.
    If client_id is given, only update parcels for that client.
    No new parcels are created.

    Returns {"updated": int, "skipped": int, "errors": list[str]}
    """
    from app.models.parcel import Parcel

    token = _login(username, password)
    deliveries = _fetch_deliveries(token)

    # Build a lookup of all in_transit unpaid parcels for this supplier by tracking number
    q = db.query(Parcel).filter(
        Parcel.supplier_id == supplier_id,
        Parcel.status == "in_transit",
        Parcel.payment_report_date.is_(None),
    )
    if client_id is not None:
        q = q.filter(Parcel.client_id == client_id)
    transit_parcels = q.all()
    by_tracking: dict[str, Parcel] = {p.tracking_number: p for p in transit_parcels}

    if not by_tracking:
        return {"updated": 0, "skipped": 0, "errors": []}

    updated = skipped = 0
    errors: list[str] = []

    for d in deliveries:
        outbound_tracks = _outbound_tracks(d.get("tracks") or [])
        for track_obj in outbound_tracks:
            tracking = track_obj["number"]
            parcel = by_tracking.get(tracking)
            if parcel is None:
                continue

            track_status = (track_obj.get("status") or "").strip()
            track_delivered_at = _parse_delivery_date(track_obj.get("deliveryDate"))
            is_delivered = "deliver" in track_status.lower()

            if is_delivered and track_delivered_at:
                parcel.arrived_at = track_delivered_at
                parcel.status = "delivered"

                # Auto-calculate cost from Keepa if ASIN is set and cost not already present
                if parcel.asin and parcel.purchase_price is None:
                    try:
                        from app.services import keepa_service
                        result = keepa_service.get_product_info(parcel.asin, track_delivered_at)
                        if result.amazon_price is not None:
                            parcel.amazon_price = result.amazon_price
                        if result.cost is not None:
                            parcel.purchase_price = float(result.cost)
                        if result.title and not parcel.title:
                            parcel.title = result.title
                    except Exception as exc:
                        errors.append(f"Keepa error for {parcel.tracking_number} ({parcel.asin}): {exc}")

                updated += 1
            else:
                skipped += 1

    db.commit()
    return {"updated": updated, "skipped": skipped, "errors": errors}


def sync(supplier_id: int, username: str, password: str, db,
         client_id: int | None = None) -> dict:
    """
    Sync parcels from housecargo.space.
    If client_id is given, all created/matched parcels are assigned to that client.

    Returns {"created": int, "updated": int, "skipped": int, "errors": list[str]}
    """
    from app.models.parcel import Parcel

    token = _login(username, password)
    deliveries = _fetch_deliveries(token)

    created = updated = skipped = 0
    errors: list[str] = []

    for d in deliveries:
        ext_id = str(d.get("externalId") or d.get("deliveryId") or "").strip()
        items: list[dict] = d.get("items") or []
        outbound_tracks = _outbound_tracks(d.get("tracks") or [])

        # Skip deliveries that have been paid out on HouseCargo side
        if d.get("priceOtdFinal") is not None:
            skipped += len(outbound_tracks) or 1
            continue

        if not outbound_tracks:
            skipped += 1
            errors.append(f"Order {ext_id or '?'}: no typeId=2 track found — skipped")
            continue

        n = len(outbound_tracks)

        # Total qty (from all items combined)
        total_qty = 0
        for it in items:
            try:
                total_qty += int(it.get("quantity") or 0)
            except (ValueError, TypeError):
                pass
        if total_qty == 0:
            total_qty = n  # fallback: 1 per track

        # purchase_price is NOT synced from supplier —
        # it is calculated separately from the Amazon price at delivery date × 0.45

        for i, track_obj in enumerate(outbound_tracks):
            tracking = track_obj["number"]
            track_status = (track_obj.get("status") or "").strip()
            track_delivered_at = _parse_delivery_date(track_obj.get("deliveryDate"))
            # Qty: even split, remainder to first track
            qty = total_qty // n + (1 if i < total_qty % n else 0)
            if qty == 0:
                qty = 1

            # ASIN: match by index if possible, fall back to first item
            asin = None
            src_item = items[i] if i < len(items) else (items[0] if items else None)
            if src_item:
                raw_asin = (src_item.get("asin") or "").strip().upper()
                asin = raw_asin or None

            # Find existing parcel
            parcel = db.query(Parcel).filter(Parcel.tracking_number == tracking).first()
            if parcel is None and ext_id:
                # secondary match: same order_id + index position isn't reliable,
                # so only match by order_id when there's exactly 1 existing track for it
                siblings = (
                    db.query(Parcel)
                    .filter(Parcel.external_order_id == ext_id, Parcel.supplier_id == supplier_id)
                    .all()
                )
                if len(siblings) == 1 and n == 1:
                    parcel = siblings[0]

            # Skip paid parcels — already processed manually or by report
            if parcel is not None and parcel.payment_report_date is not None:
                skipped += 1
                continue

            # Map housecargo delivery status to our status
            is_delivered = "deliver" in track_status.lower()

            if parcel is None:
                # Fetch title from Keepa if ASIN available (best-effort)
                title = None
                if asin:
                    try:
                        from app.services import keepa_service
                        title = keepa_service.get_title_only(asin)
                    except Exception:
                        pass

                parcel = Parcel(
                    external_order_id=ext_id or None,
                    tracking_number=tracking,
                    supplier_id=supplier_id,
                    client_id=client_id,
                    qty=qty,
                    asin=asin,
                    title=title,
                    arrived_at=None,
                    status="in_transit",
                    match_source="manual" if client_id else None,
                )
                db.add(parcel)
                created += 1
            else:
                changed = False
                if ext_id and parcel.external_order_id != ext_id:
                    parcel.external_order_id = ext_id
                    changed = True
                if parcel.qty != qty:
                    parcel.qty = qty
                    changed = True
                if asin and parcel.asin != asin:
                    parcel.asin = asin
                    changed = True
                if parcel.supplier_id != supplier_id:
                    parcel.supplier_id = supplier_id
                    changed = True
                if client_id and parcel.client_id != client_id:
                    parcel.client_id = client_id
                    changed = True
                if changed:
                    updated += 1
                else:
                    skipped += 1

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        errors.append(f"DB commit error: {e}")
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}
