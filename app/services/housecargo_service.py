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


def _client_coeff(client_id, db) -> float:
    """Return cost coefficient for a client, defaulting to 0.45."""
    if not client_id:
        return 0.45
    from app.models.client import Client
    c = db.query(Client).filter(Client.id == client_id).first()
    return c.cost_coefficient if (c and c.cost_coefficient is not None) else 0.45


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
        # Also pick up unassigned parcels — manually created parcels may have no client_id yet
        q = q.filter((Parcel.client_id == client_id) | Parcel.client_id.is_(None))
    transit_parcels = q.all()
    # One tracking may map to multiple parcels (multi-item shipment)
    by_tracking: dict[str, list[Parcel]] = {}
    for p in transit_parcels:
        by_tracking.setdefault(p.tracking_number, []).append(p)

    if not by_tracking:
        return {"updated": 0, "skipped": 0, "errors": []}

    updated = skipped = 0
    errors: list[str] = []

    for d in deliveries:
        outbound_tracks = _outbound_tracks(d.get("tracks") or [])
        for track_obj in outbound_tracks:
            tracking = track_obj["number"]
            parcels_for_track = by_tracking.get(tracking, [])
            if not parcels_for_track:
                continue

            track_status = (track_obj.get("status") or "").strip()
            # deliveryDate carries estimated date in transit, actual date when delivered
            delivery_date = _parse_delivery_date(track_obj.get("deliveryDate"))
            is_delivered = "deliver" in track_status.lower()

            for parcel in parcels_for_track:
                if is_delivered and delivery_date:
                    parcel.arrived_at = delivery_date
                    parcel.estimated_delivery_at = None
                    parcel.status = "delivered"

                    if parcel.asin and parcel.purchase_price is None:
                        try:
                            from app.services import keepa_service
                            result = keepa_service.get_product_info(parcel.asin, delivery_date)
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
                    # Still in transit — delivery_date is the estimated arrival
                    if delivery_date and parcel.estimated_delivery_at != delivery_date:
                        parcel.estimated_delivery_at = delivery_date
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

        # ── 1 track + multiple items → one Parcel per item ──────────────────
        if n == 1 and len(items) > 1:
            track_obj = outbound_tracks[0]
            tracking = track_obj["number"]

            for item in items:
                asin = (item.get("asin") or "").strip().upper() or None
                try:
                    qty = max(1, int(item.get("quantity") or 1))
                except (ValueError, TypeError):
                    qty = 1

                # Upsert: match by (ext_id, tracking, asin) when asin is known
                parcel = None
                if ext_id and asin:
                    parcel = (
                        db.query(Parcel)
                        .filter(
                            Parcel.external_order_id == ext_id,
                            Parcel.supplier_id == supplier_id,
                            Parcel.tracking_number == tracking,
                            Parcel.asin == asin,
                        )
                        .first()
                    )
                elif ext_id:
                    parcel = (
                        db.query(Parcel)
                        .filter(
                            Parcel.external_order_id == ext_id,
                            Parcel.supplier_id == supplier_id,
                            Parcel.tracking_number == tracking,
                        )
                        .first()
                    )

                if parcel is not None and parcel.payment_report_date is not None:
                    skipped += 1
                    continue

                if parcel is None:
                    title = None
                    estimated_price = None
                    if asin:
                        try:
                            from app.services import keepa_service
                            coeff = _client_coeff(client_id, db)
                            result = keepa_service.get_estimated_cost(asin, coeff)
                            title = result.title or None
                            if result.cost is not None:
                                estimated_price = float(result.cost)
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
                        purchase_price=estimated_price,
                        arrived_at=None,
                        status="in_transit",
                        match_source="manual" if client_id else None,
                    )
                    db.add(parcel)
                    created += 1
                else:
                    changed = False
                    if ext_id and parcel.external_order_id != ext_id:
                        parcel.external_order_id = ext_id; changed = True
                    if parcel.qty != qty:
                        parcel.qty = qty; changed = True
                    if asin and parcel.asin != asin:
                        parcel.asin = asin; changed = True
                    if parcel.supplier_id != supplier_id:
                        parcel.supplier_id = supplier_id; changed = True
                    if client_id and parcel.client_id != client_id:
                        parcel.client_id = client_id; changed = True
                    if changed:
                        updated += 1
                    else:
                        skipped += 1
            continue  # delivery handled; skip per-track loop below

        # ── Multiple tracks (or 1 track + 1 item) → one Parcel per track ────
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
            qty = total_qty // n + (1 if i < total_qty % n else 0)
            if qty == 0:
                qty = 1

            asin = None
            src_item = items[i] if i < len(items) else (items[0] if items else None)
            if src_item:
                raw_asin = (src_item.get("asin") or "").strip().upper()
                asin = raw_asin or None

            parcel = None
            if ext_id:
                parcel = (
                    db.query(Parcel)
                    .filter(
                        Parcel.external_order_id == ext_id,
                        Parcel.supplier_id == supplier_id,
                        Parcel.tracking_number == tracking,
                    )
                    .first()
                )
            if parcel is None:
                parcel = db.query(Parcel).filter(
                    Parcel.tracking_number == tracking,
                    Parcel.supplier_id == supplier_id,
                ).first()

            if parcel is not None and parcel.payment_report_date is not None:
                skipped += 1
                continue

            if parcel is None:
                title = None
                estimated_price = None
                if asin:
                    try:
                        from app.services import keepa_service
                        coeff = _client_coeff(client_id, db)
                        result = keepa_service.get_estimated_cost(asin, coeff)
                        title = result.title or None
                        if result.cost is not None:
                            estimated_price = float(result.cost)
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
                    purchase_price=estimated_price,
                    arrived_at=None,
                    status="in_transit",
                    match_source="manual" if client_id else None,
                )
                db.add(parcel)
                created += 1
            else:
                changed = False
                if ext_id and parcel.external_order_id != ext_id:
                    parcel.external_order_id = ext_id; changed = True
                if parcel.qty != qty:
                    parcel.qty = qty; changed = True
                if asin and parcel.asin != asin:
                    parcel.asin = asin; changed = True
                if parcel.supplier_id != supplier_id:
                    parcel.supplier_id = supplier_id; changed = True
                if client_id and parcel.client_id != client_id:
                    parcel.client_id = client_id; changed = True
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
