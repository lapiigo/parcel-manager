"""
ShipX API service (api.shipx.cash).

Auth:   POST /auth/token  (x-www-form-urlencoded: username, password)
Orders: GET  /orders      (Bearer token, paginated)

Field mapping:
  order.id                → parcel.external_order_id
  label_ext.track         → parcel.tracking_number  (client-facing outbound)
  label_ext.status        → delivery state ("Delivered" / "In Transit")
  label_ext.delivery_at   → parcel.arrived_at  (ISO-8601 with tz → naive UTC)
  products[].quantity sum → parcel.qty
  products[].description  → ASIN extraction + title fallback
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

BASE_URL = "https://api.shipx.cash"
LOGIN_PATH = "/auth/token"
ORDERS_PATH = "/orders"

_BROWSER_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "origin": "https://shipx.cash",
    "referer": "https://shipx.cash/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
}

# ASIN is exactly 10 chars: B + 9 uppercase alphanumeric
_ASIN_RE = re.compile(r'\bB[A-Z0-9]{9}\b')


class ShipXError(Exception):
    pass


def _login(username: str, password: str) -> str:
    url = BASE_URL + LOGIN_PATH
    try:
        r = requests.post(
            url,
            data={"username": username, "password": password},
            headers={**_BROWSER_HEADERS, "content-type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ShipXError(f"Login request failed: {exc}") from exc

    if r.status_code != 200:
        raise ShipXError(f"Login failed (HTTP {r.status_code}): {r.text[:300]}")

    data = r.json()
    token = data.get("token") or data.get("access_token")
    if not token:
        raise ShipXError(f"No token in login response: {str(data)[:200]}")
    return token


def _fetch_orders(token: str) -> list[dict]:
    """Fetch all orders, handling pagination."""
    url = BASE_URL + ORDERS_PATH
    headers = {**_BROWSER_HEADERS, "Authorization": f"Bearer {token}"}
    all_orders: list[dict] = []
    page = 1

    while True:
        params = {"page": page, "per_page": 100}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            raise ShipXError(f"Orders request failed: {exc}") from exc

        if r.status_code == 401:
            raise ShipXError("Token expired — sync will re-login automatically on next attempt.")
        if r.status_code != 200:
            raise ShipXError(f"Orders fetch failed (HTTP {r.status_code}): {r.text[:200]}")

        data = r.json()
        if isinstance(data, list):
            batch = data
        elif isinstance(data, dict):
            batch = data.get("data") or data.get("items") or data.get("orders") or []
        else:
            batch = []

        if not batch:
            break

        all_orders.extend(batch)

        # Stop if we got fewer than a full page (last page)
        if len(batch) < 100:
            break

        page += 1

    return all_orders


def _parse_delivery_date(dt_str: str | None) -> datetime | None:
    """Parse ISO-8601 datetime with optional timezone offset → naive UTC."""
    if not dt_str:
        return None

    s = dt_str.strip()
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
                dt = dt - tz_offset  # convert to UTC
            return dt
        except ValueError:
            continue
    return None


def _extract_asin(description: str) -> Optional[str]:
    """
    Extract ASIN from product description.
    Handles:
      "B0050DI9YQ ARB CKMTA12 ..."         → first word is ASIN
      "...ASIN: B004VTGRL2"                → explicit tag
      "...General Pump ... (B01M1DXN7X)"   → in parentheses
    """
    if not description:
        return None
    m = _ASIN_RE.search(description.upper())
    return m.group(0) if m else None


def _match_client_for_order(
    order: dict, supplier_id: int, db
) -> tuple[Optional[int], Optional[str], Optional[str], bool]:
    """
    Try to identify the client and ASIN for an order.

    Returns (client_id, asin, match_source, is_wrong_address)
      match_source: 'address' | 'wishlist' | None
      is_wrong_address: True when address client ≠ wishlist client
    """
    from app.models.wishlist import ClientShipXAddress, WishlistItem

    address_name = ((order.get("address") or {}).get("name") or "").strip()
    address_client_id: Optional[int] = None

    if address_name:
        addr_row = (
            db.query(ClientShipXAddress)
            .filter(
                ClientShipXAddress.supplier_id == supplier_id,
                ClientShipXAddress.address_name == address_name,
            )
            .first()
        )
        if addr_row:
            address_client_id = addr_row.client_id

    # ASIN + description from first product
    products = order.get("products") or []
    description = (products[0].get("description") or "") if products else ""
    asin_from_desc = _extract_asin(description)

    # Fuzzy title matching against wishlist
    wishlist_client_id: Optional[int] = None
    wishlist_asin: Optional[str] = None

    if description:
        desc_lower = description.lower()
        all_items = db.query(WishlistItem).filter(WishlistItem.title.isnot(None)).all()
        best_ratio = 0.0
        best_item = None
        for item in all_items:
            title_lower = item.title.lower()
            ratio = max(
                SequenceMatcher(None, desc_lower, title_lower).ratio(),
                SequenceMatcher(None, title_lower, desc_lower).ratio(),
            )
            if ratio > best_ratio:
                best_ratio = ratio
                best_item = item

        if best_item and best_ratio >= 0.80:
            wishlist_client_id = best_item.client_id
            wishlist_asin = best_item.asin

    # Determine final result
    asin = wishlist_asin or asin_from_desc

    if address_client_id and wishlist_client_id:
        is_wrong = address_client_id != wishlist_client_id
        # Product identified by wishlist → that's the true owner
        return wishlist_client_id, asin, "wishlist", is_wrong
    elif address_client_id:
        return address_client_id, asin, "address", False
    elif wishlist_client_id:
        return wishlist_client_id, asin, "wishlist", False
    else:
        return None, asin_from_desc, None, False


def sync(supplier_id: int, username: str, password: str, db) -> dict:
    """
    Sync orders from shipx.cash — creates new parcels as in_transit only.
    No status updates — use sync_transit_updates() for that.

    Returns {"created": int, "updated": int, "skipped": int, "errors": list[str]}
    """
    from app.models.parcel import Parcel

    token = _login(username, password)
    orders = _fetch_orders(token)

    created = updated = skipped = 0
    errors: list[str] = []

    for order in orders:
        ext_id = str(order.get("id") or "").strip()
        if not ext_id:
            skipped += 1
            continue

        label_ext = order.get("label_ext") or {}
        tracking = (label_ext.get("track") or "").strip()
        if not tracking:
            skipped += 1
            errors.append(f"Order {ext_id}: no label_ext track — skipped")
            continue

        products: list[dict] = order.get("products") or []

        # Qty: sum all product quantities
        qty = 0
        for p in products:
            try:
                qty += int(p.get("quantity") or 0)
            except (ValueError, TypeError):
                pass
        if qty == 0:
            qty = 1

        # ASIN from first product's description
        asin = None
        if products:
            asin = _extract_asin(products[0].get("description") or "")

        # Find existing parcel
        parcel = db.query(Parcel).filter(Parcel.tracking_number == tracking).first()
        if parcel is None and ext_id:
            siblings = (
                db.query(Parcel)
                .filter(Parcel.external_order_id == ext_id, Parcel.supplier_id == supplier_id)
                .all()
            )
            if len(siblings) == 1:
                parcel = siblings[0]

        # Skip paid parcels — already processed manually or by report
        if parcel is not None and parcel.payment_report_date is not None:
            skipped += 1
            continue

        # Skip orders that have been paid out on ShipX side (payout.buyer.amount != "0")
        payout_amount = str(
            (order.get("payout") or {}).get("buyer", {}).get("amount", "0")
        ).strip()
        if payout_amount != "0":
            skipped += 1
            continue

        # Match client by address name and/or wishlist
        matched_client_id, matched_asin, match_source, is_wrong_address = \
            _match_client_for_order(order, supplier_id, db)

        # Use matched ASIN over description-extracted ASIN
        final_asin = matched_asin or asin
        final_status = "in_transit" if matched_client_id else "unidentified"

        if parcel is None:
            # Fetch title from Keepa (best-effort)
            title = None
            if final_asin:
                try:
                    from app.services import keepa_service
                    title = keepa_service.get_title_only(final_asin)
                except Exception:
                    pass

            parcel = Parcel(
                external_order_id=ext_id or None,
                tracking_number=tracking,
                supplier_id=supplier_id,
                client_id=matched_client_id,
                qty=qty,
                asin=final_asin,
                title=title,
                arrived_at=None,
                status=final_status,
                match_source=match_source,
                is_wrong_address=is_wrong_address,
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
            if final_asin and parcel.asin != final_asin:
                parcel.asin = final_asin
                changed = True
            if parcel.supplier_id != supplier_id:
                parcel.supplier_id = supplier_id
                changed = True
            # Update client match if not yet set
            if matched_client_id and not parcel.client_id:
                parcel.client_id = matched_client_id
                parcel.match_source = match_source
                parcel.is_wrong_address = is_wrong_address
                if parcel.status == "unidentified":
                    parcel.status = "in_transit"
                changed = True
            if changed:
                updated += 1
            else:
                skipped += 1

    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


def sync_transit_updates(supplier_id: int, username: str, password: str, db) -> dict:
    """
    Fetch delivery updates from shipx for parcels currently in `in_transit` status only.
    No new parcels created — only updates existing in_transit parcels.

    Returns {"updated": int, "skipped": int, "errors": list[str]}
    """
    from app.models.parcel import Parcel

    token = _login(username, password)
    orders = _fetch_orders(token)

    transit_parcels = (
        db.query(Parcel)
        .filter(
            Parcel.supplier_id == supplier_id,
            Parcel.status == "in_transit",
            Parcel.payment_report_date.is_(None),
        )
        .all()
    )
    by_tracking: dict[str, Parcel] = {p.tracking_number: p for p in transit_parcels}

    if not by_tracking:
        return {"updated": 0, "skipped": 0, "errors": []}

    updated = skipped = 0
    errors: list[str] = []

    for order in orders:
        label_ext = order.get("label_ext") or {}
        tracking = (label_ext.get("track") or "").strip()
        parcel = by_tracking.get(tracking)
        if parcel is None:
            continue

        status_str = (label_ext.get("status") or "").strip().lower()
        is_delivered = status_str == "delivered"
        track_delivered_at = _parse_delivery_date(label_ext.get("delivery_at"))

        if is_delivered and track_delivered_at:
            parcel.arrived_at = track_delivered_at
            parcel.status = "delivered"

            # Auto-calculate cost from Keepa
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
