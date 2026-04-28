"""
Prime Prep service (prime-prep.com).

Credentials and config are read from .env — never hardcoded:
  PRIME_PREP_EMAIL=...
  PRIME_PREP_PASSWORD=...
  PRIME_PREP_WAREHOUSE_ID=...   (UUID of the warehouse in prime-prep's system)

Inbound registration — two-phase flow:

  Phase 1 — create the inbound on /warehouse/inbound/new:
    1. GET /warehouse/inbound/new → extract warehouse.inbound.form snapshot.
    2. POST client_id → draft record created on server, inbound UUID assigned.
    3. POST remaining fields (tracking, carrier, date, qty) + finalize call.
       Response effects.redirect contains /warehouse/inbound/{uuid}.

  Phase 2 — attach SKU on the edit page /warehouse/inbound/{uuid}:
    4. GET /warehouse/inbound/{uuid} → fresh snapshot.
    5. POST sku_search=ASIN → parse response HTML for existing SKU UUID.
       Found:     POST sku_id=<uuid> + $commit  (live model save).
       Not found: POST saveQuickSku with quickSkuAsin + quickSkuTitle.
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


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _attach_sku(
    session: requests.Session,
    shipment_uuid: str,
    prime_prep_client_id: str,
    asin: str,
    title: str,
) -> str:
    """
    Phase 2: open the edit page and attach a SKU to an existing inbound.
    Returns empty string on success, or an error/diagnostic string on failure.
    """
    referer = f"/warehouse/inbound/{shipment_uuid}"

    r = session.get(
        BASE_URL + referer,
        headers={**_HEADERS, "accept": "text/html,*/*"},
        timeout=15,
    )
    if r.status_code != 200:
        return f"edit page HTTP {r.status_code}"

    html = r.text
    update_uri = _extract_update_uri(html)
    csrf_token = _extract_csrf(html)
    snapshot = _extract_component_snapshot(html, "warehouse.inbound.form")
    if snapshot is None:
        # List all component names found to help diagnose
        names = []
        for m in re.finditer(r'"name"\s*:\s*"([^"]+)"', html):
            names.append(m.group(1))
        return f"warehouse.inbound.form not found on edit page; components: {names[:5]}"

    # ── Search for existing SKU by ASIN ──────────────────────────────────────
    snap_search, effects_search = _livewire_update(
        session, update_uri, csrf_token, snapshot,
        updates={"sku_search": asin},
        calls=[{"method": "$commit", "params": [], "metadata": {"type": "model.live"}}],
        referer=referer,
    )

    data_search = snap_search.get("data", {})

    # Auto-selected by server (single exact match)
    if data_search.get("sku_id"):
        return ""

    # Parse Livewire HTML patch for SKU UUIDs in the dropdown
    excluded = {
        prime_prep_client_id.lower(),
        shipment_uuid.lower(),
        os.getenv("PRIME_PREP_WAREHOUSE_ID", "").lower(),
    }
    html_patch = effects_search.get("html", "")
    found_uuids = [u for u in _UUID_RE.findall(html_patch) if u.lower() not in excluded]

    if found_uuids:
        _livewire_update(
            session, update_uri, csrf_token, snap_search,
            updates={"sku_id": found_uuids[0]},
            calls=[{"method": "$commit", "params": [], "metadata": {"type": "model.live"}}],
            referer=referer,
        )
        return ""

    # ── SKU not found — create a new one ─────────────────────────────────────
    snap_create, _ = _livewire_update(
        session, update_uri, csrf_token, snap_search,
        updates={
            "showQuickSkuModal": True,
            "quickSkuClientId": prime_prep_client_id,
            "quickSkuAsin": asin,
            "quickSkuTitle": title or asin,
        },
        calls=[{"method": "saveQuickSku", "params": [], "metadata": {}}],
        referer=referer,
    )

    sku_id_after = snap_create.get("data", {}).get("sku_id")
    if sku_id_after:
        return ""

    errors = snap_create.get("memo", {}).get("errors", [])
    html_patch_after = effects_search.get("html", "")
    return (
        f"saveQuickSku did not set sku_id. "
        f"errors={errors}, "
        f"html_patch_len={len(html_patch)}, "
        f"found_uuids_before_create={found_uuids}"
    )


# ── Public API ────────────────────────────────────────────────────────────────

def login() -> requests.Session:
    """Authenticate and return an authenticated session. Raises PrimePrepError on failure."""
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

    r2 = session.post(
        update_uri,
        json={
            "_token": csrf_token,
            "components": [{
                "snapshot": json.dumps(snapshot),
                "updates": {"email": email, "password": password, "remember": True},
                "calls": [{"method": "login", "params": [], "metadata": {}}],
            }],
        },
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

    Phase 1 creates the inbound record (no SKU yet).
    Phase 2 attaches the SKU on the edit page (errors silently skipped).
    """
    warehouse_id = os.getenv("PRIME_PREP_WAREHOUSE_ID", "")
    if not warehouse_id:
        raise PrimePrepError("PRIME_PREP_WAREHOUSE_ID not set in .env")
    if not prime_prep_client_id:
        raise PrimePrepError("prime_prep_client_id is required (set on client profile)")

    if arrival_date is None:
        arrival_date = date.today().isoformat()

    # ── Phase 1, step 1: GET new inbound form ─────────────────────────────────
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

    # ── Phase 1, step 2: Set client_id → server creates draft ────────────────
    snap1, _ = _livewire_update(
        session, update_uri, csrf_token, snapshot,
        updates={"client_id": prime_prep_client_id},
        calls=[],
    )

    # ── Phase 1, step 3: Fill remaining fields + finalize ─────────────────────
    snap_final, effects = _livewire_update(
        session, update_uri, csrf_token, snap1,
        updates={
            "warehouse_id": warehouse_id,
            "reference_number": tracking_number,
            "order_number": order_number,
            "carrier_type": _detect_carrier(tracking_number),
            "arrival_date": arrival_date,
            "expected_qty": qty,
        },
        calls=[{"method": "finalize", "params": [], "metadata": {}}],
    )

    # Extract shipment UUID — check every likely location in the response
    shipment_uuid: Optional[str] = None

    # 1. effects.redirect  (most common in Livewire 3)
    for key in ("redirect", "path", "url"):
        val = effects.get(key, "")
        if val:
            m = re.search(r"/inbound/([0-9a-f-]{36})", val)
            if m:
                shipment_uuid = m.group(1)
                break

    # 2. snap_final memo.path (component path updates after finalize)
    if not shipment_uuid:
        memo_path = snap_final.get("memo", {}).get("path", "")
        m = re.search(r"/inbound/([0-9a-f-]{36})", memo_path)
        if m:
            shipment_uuid = m.group(1)

    # 3. inbound model key in snap_final data
    if not shipment_uuid:
        for snap in (snap_final, snap1):
            inbound_field = snap.get("data", {}).get("inbound")
            if isinstance(inbound_field, list) and len(inbound_field) > 1:
                inbound_model = inbound_field[1]
                if isinstance(inbound_model, dict) and inbound_model.get("key"):
                    shipment_uuid = inbound_model["key"]
                    break

    if not shipment_uuid:
        raise PrimePrepError(
            f"Could not extract shipment UUID. "
            f"effects keys={list(effects.keys())}, "
            f"redirect={effects.get('redirect','')!r}, "
            f"memo_path={snap_final.get('memo',{}).get('path','')!r}"
        )

    # ── Phase 2: attach SKU on the edit page ─────────────────────────────────
    sku_error = ""
    if asin:
        try:
            sku_error = _attach_sku(session, shipment_uuid, prime_prep_client_id, asin, title)
        except Exception as exc:
            sku_error = str(exc)

    return shipment_uuid, sku_error


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
