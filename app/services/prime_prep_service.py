"""
Prime Prep service (prime-prep.com).

Credentials and config are read from .env — never hardcoded:
  PRIME_PREP_EMAIL=...
  PRIME_PREP_PASSWORD=...
  PRIME_PREP_WAREHOUSE_ID=...   (UUID of the warehouse in prime-prep's system)

Auth flow (Livewire 3):
  1. GET /login → extract XSRF-TOKEN cookie, CSRF _token, and auth.login
     component snapshot (wire:snapshot attribute).
  2. POST /livewire-<hash>/update with the snapshot + credentials + login call.
     A successful response has effects.redirect set.
  3. The requests.Session retains the new session cookie for subsequent calls.

Inbound registration flow:
  1. GET /warehouse/inbound/new → extract warehouse.inbound.form snapshot.
  2. POST update with client_id → draft inbound record created, shipment UUID assigned.
  3. POST update with sku_search=ASIN → look for existing SKU in response.
       Found:    POST update sku_id=<uuid> to select it.
       Not found: POST saveQuickSku to create new SKU → get sku_id from response.
  4. POST update with remaining fields + sku_id + finalize → shipment confirmed.
  5. Return the shipment UUID (stored as prime_prep_shipment_id on Parcel).
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Optional

import requests

BASE_URL = "https://www.prime-prep.com"

_HEADERS = {
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
}

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class PrimePrepError(Exception):
    pass


def _get_credentials() -> tuple[str, str]:
    email = os.getenv("PRIME_PREP_EMAIL", "")
    password = os.getenv("PRIME_PREP_PASSWORD", "")
    if not email or not password:
        raise PrimePrepError("PRIME_PREP_EMAIL / PRIME_PREP_PASSWORD not set in .env")
    return email, password


def _xsrf_header(session: requests.Session) -> str:
    raw = session.cookies.get("XSRF-TOKEN", "")
    return requests.utils.unquote(raw)


def _extract_update_uri(html: str) -> str:
    m = re.search(r'"uri"\s*:\s*"(/livewire[^"]+)"', html)
    if not m:
        m = re.search(r'data-update-uri="(/livewire[^"]+)"', html)
    return BASE_URL + (m.group(1) if m else "/livewire-5c96e4c8/update")


def _extract_csrf(html: str) -> str:
    m = re.search(r'"csrf"\s*:\s*"([^"]+)"', html)
    if not m:
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else ""


def _extract_component_snapshot(html: str, component_name: str) -> Optional[dict]:
    for m in re.finditer(r'wire:snapshot="((?:[^"\\]|\\.)*)"', html):
        raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
        try:
            snap = json.loads(raw)
            if snap.get("memo", {}).get("name") == component_name:
                return snap
        except json.JSONDecodeError:
            continue
    return None


def _livewire_update(
    session: requests.Session,
    update_uri: str,
    csrf_token: str,
    snapshot: dict,
    updates: dict,
    calls: list,
    referer: str = "/warehouse/inbound/new",
) -> tuple[dict, dict]:
    """POST a Livewire update. Returns (new_snapshot_dict, effects_dict)."""
    payload = {
        "_token": csrf_token,
        "components": [{
            "snapshot": json.dumps(snapshot),
            "updates": updates,
            "calls": calls,
        }],
    }
    r = session.post(
        update_uri,
        json=payload,
        headers={
            **_HEADERS,
            "accept": "*/*",
            "content-type": "application/json",
            "origin": BASE_URL,
            "referer": BASE_URL + referer,
            "x-livewire": "1",
            "x-xsrf-token": _xsrf_header(session),
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise PrimePrepError(f"Livewire update HTTP {r.status_code}: {r.text[:200]}")

    data = r.json()
    components = data.get("components") or []
    if not components:
        raise PrimePrepError("Empty components in Livewire response")

    comp = components[0]
    effects = comp.get("effects", {})
    try:
        new_snapshot = json.loads(comp.get("snapshot", "{}"))
    except json.JSONDecodeError:
        new_snapshot = {}

    return new_snapshot, effects


def _detect_carrier(tracking: str) -> str:
    t = tracking.strip().upper()
    if t.startswith("1Z"):
        return "ups"
    digits = re.sub(r"\D", "", t)
    if len(digits) in (12, 15, 20):
        return "fedex"
    if t.startswith("9") and len(digits) >= 20:
        return "usps"
    if len(digits) == 10 and t[0].isdigit():
        return "dhl"
    return "other"


def _find_or_create_sku(
    session: requests.Session,
    update_uri: str,
    csrf_token: str,
    snapshot: dict,
    inbound_uuid: str,
    prime_prep_client_id: str,
    asin: str,
    title: str,
) -> tuple[Optional[str], dict]:
    """
    Find an existing SKU by ASIN or create a new one.
    Returns (sku_id, updated_snapshot).
    """
    referer = f"/warehouse/inbound/{inbound_uuid}"

    # ── Step A: search by ASIN ────────────────────────────────────────────────
    snap_a, effects_a = _livewire_update(
        session, update_uri, csrf_token, snapshot,
        updates={"sku_search": asin},
        calls=[{"method": "$commit", "params": [], "metadata": {"type": "model.live"}}],
        referer=referer,
    )

    data_a = snap_a.get("data", {})

    # Check if a single match was auto-selected
    if data_a.get("sku_id"):
        return data_a["sku_id"], snap_a

    # Parse response HTML from effects for SKU UUIDs in dropdown items.
    # Livewire renders the dropdown as HTML patch; UUIDs appear in wire:click or data attrs.
    html_patch = effects_a.get("html", "")
    excluded = {
        prime_prep_client_id,
        inbound_uuid,
        os.getenv("PRIME_PREP_WAREHOUSE_ID", ""),
    }
    found_uuids = [u for u in _UUID_RE.findall(html_patch) if u.lower() not in excluded]

    if found_uuids:
        # First UUID in the dropdown is the best match for the searched ASIN
        sku_id = found_uuids[0]
        snap_select, _ = _livewire_update(
            session, update_uri, csrf_token, snap_a,
            updates={"sku_id": sku_id},
            calls=[{"method": "$commit", "params": [], "metadata": {"type": "model.live"}}],
            referer=referer,
        )
        return sku_id, snap_select

    # ── Step B: SKU not found — create it ────────────────────────────────────
    # Open the quick-SKU modal, fill ASIN + title, then save.
    snap_b, _ = _livewire_update(
        session, update_uri, csrf_token, snap_a,
        updates={
            "showQuickSkuModal": True,
            "quickSkuClientId": prime_prep_client_id,
            "quickSkuAsin": asin,
            "quickSkuTitle": title or asin,
        },
        calls=[{"method": "saveQuickSku", "params": [], "metadata": {}}],
        referer=referer,
    )

    sku_id = snap_b.get("data", {}).get("sku_id")
    return sku_id, snap_b


def login() -> requests.Session:
    """
    Authenticate with prime-prep.com and return an authenticated session.
    Raises PrimePrepError on failure.
    """
    email, password = _get_credentials()
    session = requests.Session()

    r = session.get(
        BASE_URL + "/login",
        headers={**_HEADERS, "accept": "text/html,*/*"},
        timeout=15,
    )
    if r.status_code != 200:
        raise PrimePrepError(f"Login page HTTP {r.status_code}")

    html = r.text
    update_uri = _extract_update_uri(html)
    csrf_token = _extract_csrf(html)

    snap_match = re.search(r'wire:snapshot="((?:[^"\\]|\\.)*)"\s', html)
    if not snap_match:
        raise PrimePrepError("auth.login component snapshot not found on login page")

    snapshot_raw = snap_match.group(1).replace("&quot;", '"').replace("&amp;", "&")
    try:
        snapshot = json.loads(snapshot_raw)
    except json.JSONDecodeError as exc:
        raise PrimePrepError(f"Failed to parse snapshot JSON: {exc}") from exc

    if snapshot.get("memo", {}).get("name") != "auth.login":
        raise PrimePrepError("First wire:snapshot is not auth.login — page structure changed")

    payload = {
        "_token": csrf_token,
        "components": [{
            "snapshot": json.dumps(snapshot),
            "updates": {"email": email, "password": password, "remember": True},
            "calls": [{"method": "login", "params": [], "metadata": {}}],
        }],
    }

    r2 = session.post(
        update_uri,
        json=payload,
        headers={
            **_HEADERS,
            "accept": "*/*",
            "content-type": "application/json",
            "origin": BASE_URL,
            "referer": BASE_URL + "/login",
            "x-livewire": "1",
            "x-xsrf-token": _xsrf_header(session),
        },
        timeout=15,
    )

    if r2.status_code != 200:
        raise PrimePrepError(f"Livewire login POST HTTP {r2.status_code}: {r2.text[:200]}")

    data = r2.json()
    components = data.get("components") or []
    effects = components[0].get("effects", {}) if components else {}

    if not effects.get("redirect"):
        try:
            snap_back = json.loads(components[0].get("snapshot", "{}"))
            errors = snap_back.get("memo", {}).get("errors", [])
            if errors:
                raise PrimePrepError(f"Login validation errors: {errors}")
        except (json.JSONDecodeError, IndexError):
            pass

    if not session.cookies.get("prime-prep-session"):
        raise PrimePrepError("No session cookie after login — credentials may be wrong")

    return session


def register_inbound(
    session: requests.Session,
    tracking_number: str,
    asin: Optional[str],
    qty: int,
    prime_prep_client_id: str,
    order_number: str = "",
    arrival_date: Optional[str] = None,
    title: str = "",
) -> str:
    """
    Register an expected inbound shipment with prime-prep.
    Returns the shipment UUID assigned by prime-prep.

    SKU flow: search for existing SKU by ASIN; create a new one if not found.
    The title for new SKU creation is taken from the `title` argument
    (should be the Keepa-fetched product title stored on the parcel).
    """
    warehouse_id = os.getenv("PRIME_PREP_WAREHOUSE_ID", "")
    if not warehouse_id:
        raise PrimePrepError("PRIME_PREP_WAREHOUSE_ID not set in .env")
    if not prime_prep_client_id:
        raise PrimePrepError("prime_prep_client_id is required (set on client profile)")

    if arrival_date is None:
        arrival_date = date.today().isoformat()

    # ── Step 1: GET the new inbound form ─────────────────────────────────────
    r = session.get(
        BASE_URL + "/warehouse/inbound/new",
        headers={**_HEADERS, "accept": "text/html,*/*"},
        timeout=15,
    )
    if r.status_code != 200:
        raise PrimePrepError(f"Inbound form page HTTP {r.status_code}")

    html = r.text
    update_uri = _extract_update_uri(html)
    csrf_token = _extract_csrf(html)

    snapshot = _extract_component_snapshot(html, "warehouse.inbound.form")
    if snapshot is None:
        raise PrimePrepError("warehouse.inbound.form component not found — page structure changed")

    # ── Step 2: Set client_id → server creates the draft inbound ─────────────
    snap1, _ = _livewire_update(
        session, update_uri, csrf_token, snapshot,
        updates={"client_id": prime_prep_client_id},
        calls=[],
    )

    # Extract the inbound UUID from the draft model in snapshot
    inbound_field = snap1.get("data", {}).get("inbound")
    inbound_uuid: Optional[str] = None
    if isinstance(inbound_field, list) and len(inbound_field) > 1:
        inbound_model = inbound_field[1]
        if isinstance(inbound_model, dict):
            inbound_uuid = inbound_model.get("key")

    # ── Step 3: Resolve SKU (find existing or create new) ────────────────────
    sku_id: Optional[str] = None
    current_snap = snap1

    if asin:
        sku_id, current_snap = _find_or_create_sku(
            session, update_uri, csrf_token, snap1,
            inbound_uuid=inbound_uuid or "",
            prime_prep_client_id=prime_prep_client_id,
            asin=asin,
            title=title,
        )

    # ── Step 4: Fill remaining fields + finalize ──────────────────────────────
    updates_final: dict = {
        "warehouse_id": warehouse_id,
        "reference_number": tracking_number,
        "order_number": order_number,
        "carrier_type": _detect_carrier(tracking_number),
        "arrival_date": arrival_date,
        "expected_qty": qty,
    }
    if sku_id:
        updates_final["sku_id"] = sku_id

    snap_final, effects = _livewire_update(
        session, update_uri, csrf_token, current_snap,
        updates=updates_final,
        calls=[{"method": "finalize", "params": [], "metadata": {}}],
    )

    # Prefer UUID from redirect URL (most reliable)
    shipment_uuid: Optional[str] = inbound_uuid
    redirect = effects.get("redirect", "")
    if redirect:
        m = re.search(r"/inbound/([0-9a-f-]{36})", redirect)
        if m:
            shipment_uuid = m.group(1)

    if not shipment_uuid:
        raise PrimePrepError("Could not extract shipment UUID from prime-prep response")

    return shipment_uuid


def get_shipment_status(session: requests.Session, shipment_id: str) -> dict:
    """
    Fetch reception status for a specific inbound shipment.
    Returns dict with keys: status, received_qty, issues (list), raw (full data).
    """
    r = session.get(
        BASE_URL + f"/warehouse/inbound/{shipment_id}",
        headers={**_HEADERS, "accept": "text/html,*/*"},
        timeout=15,
    )
    if r.status_code != 200:
        raise PrimePrepError(f"Inbound detail HTTP {r.status_code}")

    html = r.text

    for name in ("warehouse.inbound.show", "warehouse.inbound.detail", "warehouse.inbound.view"):
        snapshot = _extract_component_snapshot(html, name)
        if snapshot:
            data = snapshot.get("data", {})
            inbound = data.get("inbound") or {}
            if isinstance(inbound, list):
                inbound = inbound[1] if len(inbound) > 1 else {}
            status = (
                inbound.get("status")
                or inbound.get("state")
                or data.get("status")
                or "unknown"
            )
            return {
                "status": status,
                "received_qty": inbound.get("received_qty") or inbound.get("quantity_received"),
                "issues": inbound.get("issues", []),
                "raw": data,
            }

    m = re.search(r'(?:status|state)["\s:>]+([a-z_]+)', html, re.IGNORECASE)
    return {
        "status": m.group(1) if m else "unknown",
        "received_qty": None,
        "issues": [],
        "raw": {},
    }
