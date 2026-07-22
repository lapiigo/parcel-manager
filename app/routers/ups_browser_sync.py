"""
UPS Browser Sync — tracks UPS packages via the user's own browser.

Since UPS.com blocks datacenter IPs (Akamai), requests must come from a
residential IP. This router coordinates a Tampermonkey userscript that:
  1. Runs on www.ups.com in the user's browser
  2. Calls UPS internal tracking API (same-origin, no Akamai block)
  3. Posts results back to this server
  4. Closes the tab when done

Flow:
  Admin clicks "Sync UPS via Browser"
  → JS opens www.ups.com/track?__pm_token=TOKEN&__pm_server=http://ourserver
  → Tampermonkey script runs, reads TOKEN and server URL from URL params
  → Script fetches list of tracking numbers from /ups-sync/pending?token=TOKEN
  → Script calls UPS API one-by-one, posts progress
  → Script posts all results to /ups-sync/result?token=TOKEN
  → Script closes tab
  → Admin page polls /ups-sync/status?token=TOKEN and shows progress
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from app.auth import require_manager_up
from app.database import get_db
from app.models.parcel import Parcel
from app.services import keepa_service
from app.services.parcel_service import transition_parcel

router = APIRouter(prefix="/ups-sync")

# ── In-memory session store ────────────────────────────────────────────────────
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_SESSION_TTL = timedelta(hours=2)


def _clean_old_sessions() -> None:
    now = datetime.now(timezone.utc)
    with _lock:
        expired = [t for t, s in _sessions.items() if now - s["created_at"] > _SESSION_TTL]
        for t in expired:
            del _sessions[t]


def _get_session(token: str) -> Optional[dict]:
    with _lock:
        return _sessions.get(token)


# ── Tampermonkey userscript ────────────────────────────────────────────────────

_USERSCRIPT = """\
// ==UserScript==
// @name         Parcel Manager — UPS Sync
// @namespace    https://github.com/lapiigo/parcel-manager
// @version      1.2
// @description  Syncs UPS delivery status to Parcel Manager (opened automatically)
// @author       Parcel Manager
// @match        https://www.ups.com/track*
// @grant        GM_xmlhttpRequest
// @run-at       document-idle
// ==/UserScript==

(async function () {
    'use strict';

    const params = new URLSearchParams(window.location.search);
    const pmToken  = params.get('__pm_token');
    const pmServer = params.get('__pm_server');
    if (!pmToken || !pmServer) return;   // normal UPS page, not our sync

    console.log('[PM Sync] Starting UPS sync, token:', pmToken);

    // ── helpers ──────────────────────────────────────────────────────────────

    function gmGet(url) {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({ method: 'GET', url,
                onload:  r => resolve(JSON.parse(r.responseText)),
                onerror: e => reject(e) });
        });
    }

    function gmPost(url, body) {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method: 'POST', url,
                data: JSON.stringify(body),
                headers: { 'Content-Type': 'application/json' },
                onload:  r => resolve(r),
                onerror: e => reject(e),
            });
        });
    }

    function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

    function getXsrf() {
        for (const c of document.cookie.split(';')) {
            const [k, v] = c.trim().split('=');
            if (k === 'X-XSRF-TOKEN-ST' || k === 'X-XSRF-TOKEN') return decodeURIComponent(v || '');
        }
        return '';
    }

    // ── 1. fetch tracking numbers ─────────────────────────────────────────────
    let pending;
    try {
        pending = await gmGet(`${pmServer}/ups-sync/pending?token=${pmToken}`);
    } catch (e) {
        console.error('[PM Sync] Could not fetch pending list:', e);
        return;
    }

    const trackingNumbers = pending.tracking_numbers || [];
    console.log('[PM Sync]', trackingNumbers.length, 'parcels to check');

    // ── 2. wait for UPS cookies (page may still be loading) ──────────────────
    await sleep(2000);

    const results = [];
    const DELAY = 350;   // ms between requests (~2.8 req/sec)

    // ── 3. query UPS one-by-one ───────────────────────────────────────────────
    for (let i = 0; i < trackingNumbers.length; i++) {
        const tn = trackingNumbers[i];
        try {
            const xsrf = getXsrf();
            const resp = await fetch(
                'https://www.ups.com/track/api/Track/GetStatus?loc=en_US',
                {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-XSRF-TOKEN': xsrf,
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: JSON.stringify({ Locale: 'en_US', TrackingNumber: [tn] }),
                }
            );
            const data = await resp.json();
            const details = (data.trackDetails || [])[0] || {};
            const status  = (details.packageStatus || '').trim();
            const delivered = status.toLowerCase().includes('delivered');

            results.push({
                tracking_number: tn,
                delivered,
                delivered_at:  delivered ? (details.deliveredDate || '') : '',
                delivered_time: delivered ? (details.deliveredTime || '') : '',
                gmt_offset:    details.gmtOffset || '',
                status,
            });

            // send incremental progress
            gmPost(`${pmServer}/ups-sync/progress?token=${pmToken}`, { processed: i + 1 });
        } catch (err) {
            console.error('[PM Sync] Error for', tn, err);
            results.push({ tracking_number: tn, error: String(err) });
        }

        await sleep(DELAY);
    }

    // ── 4. send all results ───────────────────────────────────────────────────
    try {
        await gmPost(`${pmServer}/ups-sync/result?token=${pmToken}`, { results });
        console.log('[PM Sync] Done, closing tab');
    } catch (e) {
        console.error('[PM Sync] Failed to send results:', e);
    }

    window.close();
})();
"""


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/script.user.js", response_class=PlainTextResponse)
def get_userscript():
    """Serve Tampermonkey userscript. Opening this URL prompts Tampermonkey to install."""
    return Response(
        content=_USERSCRIPT,
        media_type="application/javascript",
        headers={"Content-Disposition": 'attachment; filename="parcel-manager-ups-sync.user.js"'},
    )


@router.post("/start")
def start_sync(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    """Create a new sync session. Returns token + count of parcels to check."""
    _clean_old_sessions()

    parcels = (
        db.query(Parcel)
        .filter(Parcel.status == "in_transit", Parcel.tracking_number.like("1Z%"))
        .all()
    )
    tracking_numbers = [p.tracking_number for p in parcels]

    token = str(uuid.uuid4())
    with _lock:
        _sessions[token] = {
            "created_at": datetime.now(timezone.utc),
            "tracking_numbers": tracking_numbers,
            "total": len(tracking_numbers),
            "processed": 0,
            "status": "running",
            "updated": 0,
            "errors": 0,
        }

    return {
        "token": token,
        "count": len(tracking_numbers),
        "ups_url": (
            f"https://www.ups.com/track?trackNums=1ZDUMMY"
            f"&__pm_token={token}"
            f"&__pm_server={request.base_url}".rstrip("/")
        ),
    }


@router.get("/pending")
def pending(token: str):
    """Return tracking numbers for this sync session (called by Tampermonkey)."""
    session = _get_session(token)
    if not session:
        return {"error": "session not found", "tracking_numbers": []}
    return {"tracking_numbers": session["tracking_numbers"]}


@router.post("/progress")
def update_progress(token: str, body: dict):
    """Incremental progress update from Tampermonkey."""
    session = _get_session(token)
    if session:
        with _lock:
            session["processed"] = body.get("processed", session["processed"])
    return {"ok": True}


@router.post("/result")
def receive_result(
    token: str,
    body: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Receive full tracking results from Tampermonkey.
    Updates parcel statuses synchronously, queues Keepa in background.
    """
    session = _get_session(token)
    if not session:
        return {"error": "session not found"}

    results: list[dict] = body.get("results") or []
    updated = 0
    errors = 0
    need_keepa: list[int] = []

    for item in results:
        tn = item.get("tracking_number", "")
        if item.get("error"):
            errors += 1
            continue

        parcel = db.query(Parcel).filter(Parcel.tracking_number == tn).first()
        if not parcel or parcel.status != "in_transit":
            continue

        if not item.get("delivered"):
            continue

        ok, _ = transition_parcel(
            parcel, "delivered", db,
            changed_by="ups_browser_sync",
            notes=f"UPS: {item.get('status', '')}",
        )
        if ok:
            updated += 1
            # Parse delivered_at with GMT offset
            delivered_at = _parse_delivered_at(
                item.get("delivered_at", ""),
                item.get("delivered_time", ""),
                item.get("gmt_offset", ""),
            )
            if delivered_at:
                parcel.arrived_at = delivered_at
                db.commit()
            if parcel.asin:
                need_keepa.append(parcel.id)

    with _lock:
        session["status"] = "done"
        session["processed"] = len(results)
        session["updated"] = updated
        session["errors"] = errors

    if need_keepa:
        background_tasks.add_task(_run_keepa, need_keepa)

    return {"ok": True, "updated": updated, "errors": errors}


@router.get("/status")
def sync_status(token: str):
    """Poll sync progress from admin page."""
    session = _get_session(token)
    if not session:
        return {"status": "not_found"}
    return {
        "status":    session["status"],
        "total":     session["total"],
        "processed": session["processed"],
        "updated":   session.get("updated", 0),
        "errors":    session.get("errors", 0),
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_delivered_at(date_str: str, time_str: str, gmt_offset: str) -> Optional[datetime]:
    from datetime import timedelta, timezone
    combined = f"{date_str} {time_str}".strip()
    parsed = None
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y",
                "%Y%m%d %H%M%S", "%Y%m%d %H%M", "%Y%m%d",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(combined, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    # Apply gmtOffset if provided
    offset = _parse_offset(gmt_offset)
    if offset is not None:
        return parsed.replace(tzinfo=timezone(offset)).astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_offset(s: str) -> Optional[timedelta]:
    s = (s or "").strip().replace(":", "")
    if not s:
        return None
    try:
        sign = -1 if s.startswith("-") else 1
        s = s.lstrip("+-")
        if len(s) <= 2:
            return timedelta(hours=sign * int(s))
        if len(s) == 4:
            return timedelta(hours=sign * int(s[:2]), minutes=sign * int(s[2:]))
    except (ValueError, IndexError):
        pass
    return None


def _run_keepa(parcel_ids: list[int]) -> None:
    """Background task: update Keepa prices for newly delivered parcels."""
    from app.database import SessionLocal
    import time
    db = SessionLocal()
    try:
        for pid in parcel_ids:
            if not keepa_service.has_tokens():
                time.sleep(300)
            parcel = db.query(Parcel).filter(Parcel.id == pid).first()
            if not parcel or not parcel.asin:
                continue
            try:
                coeff = (parcel.client.cost_coefficient or 0.45) if parcel.client else 0.45
                dt = parcel.arrived_at or datetime.utcnow()
                result = keepa_service.get_product_info(parcel.asin, dt, multiplier=coeff)
                if result.amazon_price is not None:
                    parcel.amazon_price = result.amazon_price
                if result.cost is not None:
                    parcel.purchase_price = float(result.cost)
                if result.title and not parcel.title:
                    parcel.title = result.title
                db.commit()
            except Exception as exc:
                import logging
                logging.getLogger("ups_browser_sync").warning("Keepa error %s: %s", parcel.asin, exc)
    finally:
        db.close()
