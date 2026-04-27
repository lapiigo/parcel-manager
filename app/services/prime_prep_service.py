"""
Prime Prep service (prime-prep.com).

Credentials are read from .env — never hardcoded:
  PRIME_PREP_EMAIL=...
  PRIME_PREP_PASSWORD=...

Auth flow (Livewire 3):
  1. GET /login → extract XSRF-TOKEN cookie, CSRF _token, and auth.login
     component snapshot (wire:snapshot attribute).
  2. POST /livewire-<hash>/update with the snapshot + credentials + login call.
     A successful response has effects.redirect set.
  3. The requests.Session retains the new session cookie for subsequent calls.

Shipment flow (to be extended once API endpoints are confirmed):
  register_inbound(session, tracking, asin, qty) → prime_prep_shipment_id
  get_shipment_status(session, shipment_id)       → dict with status info
"""

from __future__ import annotations

import json
import os
import re
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


class PrimePrepError(Exception):
    pass


def _get_credentials() -> tuple[str, str]:
    email = os.getenv("PRIME_PREP_EMAIL", "")
    password = os.getenv("PRIME_PREP_PASSWORD", "")
    if not email or not password:
        raise PrimePrepError("PRIME_PREP_EMAIL / PRIME_PREP_PASSWORD not set in .env")
    return email, password


def _xsrf_header(session: requests.Session) -> str:
    """URL-decoded value of XSRF-TOKEN cookie, used as X-XSRF-TOKEN header."""
    raw = session.cookies.get("XSRF-TOKEN", "")
    return requests.utils.unquote(raw)


def login() -> requests.Session:
    """
    Authenticate with prime-prep.com and return an authenticated session.
    Raises PrimePrepError on failure.
    """
    email, password = _get_credentials()
    session = requests.Session()

    # ── Step 1: GET login page ────────────────────────────────────────────────
    r = session.get(
        BASE_URL + "/login",
        headers={**_HEADERS, "accept": "text/html,*/*"},
        timeout=15,
    )
    if r.status_code != 200:
        raise PrimePrepError(f"Login page HTTP {r.status_code}")

    html = r.text

    # Extract Livewire update URI (looks like /livewire-xxxxxxxx/update)
    uri_match = re.search(r'"uri"\s*:\s*"(/livewire[^"]+)"', html)
    if not uri_match:
        uri_match = re.search(r'data-update-uri="(/livewire[^"]+)"', html)
    update_uri = BASE_URL + (uri_match.group(1) if uri_match else "/livewire-5c96e4c8/update")

    # Extract Laravel CSRF _token
    csrf_match = re.search(r'"csrf"\s*:\s*"([^"]+)"', html)
    if not csrf_match:
        csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    csrf_token = csrf_match.group(1) if csrf_match else ""

    # Extract auth.login Livewire component snapshot
    # wire:snapshot attribute value is HTML-entity-encoded JSON
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

    # ── Step 2: POST Livewire login ───────────────────────────────────────────
    payload = {
        "_token": csrf_token,
        "components": [{
            "snapshot": json.dumps(snapshot),
            "updates": {
                "email": email,
                "password": password,
                "remember": True,
            },
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
        # Check for validation errors in snapshot
        try:
            snap_back = json.loads(components[0].get("snapshot", "{}"))
            errors = snap_back.get("memo", {}).get("errors", [])
            if errors:
                raise PrimePrepError(f"Login validation errors: {errors}")
        except (json.JSONDecodeError, IndexError):
            pass
        # No redirect but no errors — might still be ok (session cookie set)

    # Verify session cookie exists
    if not session.cookies.get("prime-prep-session"):
        raise PrimePrepError("No session cookie after login — credentials may be wrong")

    return session


# ── Inbound shipment API (stubs — endpoints to be confirmed) ──────────────────

def register_inbound(
    session: requests.Session,
    tracking_number: str,
    asin: Optional[str],
    qty: int,
) -> str:
    """
    Register an expected inbound shipment with prime-prep.
    Returns the shipment ID assigned by prime-prep.

    TODO: fill in correct endpoint + payload once confirmed.
    """
    raise NotImplementedError("register_inbound endpoint not yet confirmed")


def get_shipment_status(session: requests.Session, shipment_id: str) -> dict:
    """
    Fetch reception status for a specific shipment.
    Returns dict with keys: status, received_qty, issues (list), raw (full response).

    TODO: fill in correct endpoint once confirmed.
    """
    raise NotImplementedError("get_shipment_status endpoint not yet confirmed")
