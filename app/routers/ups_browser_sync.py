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
  → Script calls UPS API in batches of 25 (UPS max per request), random 1-2 s delay
  → Script posts all results to /ups-sync/result?token=TOKEN
  → Script closes tab
  → Admin page polls /ups-sync/status?token=TOKEN (progress) then /ups-sync/report (per-parcel detail)
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
// @version      1.4
// @description  Syncs UPS delivery status to Parcel Manager (opened automatically)
// @author       Parcel Manager
// @match        https://www.ups.com/track*
// @grant        GM_xmlhttpRequest
// @run-at       document-idle
// ==/UserScript==

(async function () {
    'use strict';

    // Params are passed via URL hash (#pm_token=...&pm_server=...)
    // so UPS's SPA doesn't strip them when processing the query string.
    const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    const pmToken  = hashParams.get('pm_token');
    const pmServer = hashParams.get('pm_server');
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
    function randomDelay() { return Math.random() * 1000 + 1000; }  // 1-2 s

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
    const BATCH_SIZE = 25;   // UPS allows up to 25 tracking numbers per request

    // ── 3. query UPS in batches of 25 ────────────────────────────────────────
    for (let i = 0; i < trackingNumbers.length; i += BATCH_SIZE) {
        const batch = trackingNumbers.slice(i, i + BATCH_SIZE);
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
                    body: JSON.stringify({ Locale: 'en_US', TrackingNumber: batch }),
                }
            );
            const data = await resp.json();
            const details = data.trackDetails || [];

            for (const detail of details) {
                // Map by trackingNumber field in response (not by array index)
                const tn = (detail.trackingNumber || '').trim();
                if (!tn) continue;
                const status = (detail.packageStatus || '').trim();
                const delivered = status.toLowerCase().includes('delivered');

                results.push({
                    tracking_number: tn,
                    delivered,
                    delivered_at:   delivered ? (detail.deliveredDate  || '') : '',
                    delivered_time: delivered ? (detail.deliveredTime  || '') : '',
                    gmt_offset:     detail.gmtOffset || '',
                    status,
                });
            }

            // send incremental progress
            gmPost(`${pmServer}/ups-sync/progress?token=${pmToken}`, {
                processed: Math.min(i + BATCH_SIZE, trackingNumbers.length)
            });

        } catch (err) {
            console.error('[PM Sync] Batch error at index', i, err);
            // Mark each TN in the batch as an error so the server knows
            for (const tn of batch) {
                results.push({ tracking_number: tn, error: String(err) });
            }
        }

        // Random delay between batches to avoid rate limiting
        if (i + BATCH_SIZE < trackingNumbers.length) {
            await sleep(randomDelay());
        }
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
            "created_at":       datetime.now(timezone.utc),
            "tracking_numbers": tracking_numbers,
            "total":            len(tracking_numbers),
            "processed":        0,
            "status":           "running",
            "updated":          0,
            "errors":           0,
            # per-parcel detail (filled by /result + _run_keepa)
            "delivered_parcels": [],  # [{tn, parcel_id, asin, arrived_at}]
            "keepa_results":     {},  # {tn: {status, price, error}}
            "keepa_status":      "pending",  # pending | running | done
        }

    base = str(request.base_url).rstrip("/")
    return {
        "token":   token,
        "count":   len(tracking_numbers),
        # Pass params via URL hash (#) so UPS SPA doesn't strip them
        # when it reads and processes the ?trackNums query string.
        "ups_url": f"https://www.ups.com/track#pm_token={token}&pm_server={base}",
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
    Updates parcel statuses synchronously; only newly-delivered parcels with
    an ASIN are queued for Keepa price lookup.
    """
    session = _get_session(token)
    if not session:
        return {"error": "session not found"}

    results: list[dict] = body.get("results") or []
    updated = 0
    errors = 0
    need_keepa: list[dict] = []   # [{parcel_id, tn, asin}]
    delivered_parcels: list[dict] = []

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
            delivered_at = _parse_delivered_at(
                item.get("delivered_at", ""),
                item.get("delivered_time", ""),
                item.get("gmt_offset", ""),
            )
            if delivered_at:
                parcel.arrived_at = delivered_at
                db.commit()

            arrived_str = delivered_at.strftime("%d.%m %H:%M") if delivered_at else None
            delivered_parcels.append({
                "tn":        tn,
                "parcel_id": parcel.id,
                "asin":      parcel.asin or "",
                "arrived_at": arrived_str,
            })

            if parcel.asin:
                need_keepa.append({
                    "parcel_id": parcel.id,
                    "tn":        tn,
                    "asin":      parcel.asin,
                })

    with _lock:
        session["status"]           = "done"
        session["processed"]        = len(results)
        session["updated"]          = updated
        session["errors"]           = errors
        session["delivered_parcels"] = delivered_parcels
        if need_keepa:
            session["keepa_status"] = "running"
        else:
            session["keepa_status"] = "done"

    if need_keepa:
        background_tasks.add_task(_run_keepa, need_keepa, token)

    return {"ok": True, "updated": updated, "errors": errors}


@router.get("/status")
def sync_status(token: str):
    """Poll sync progress from admin page."""
    session = _get_session(token)
    if not session:
        return {"status": "not_found"}
    return {
        "status":       session["status"],
        "total":        session["total"],
        "processed":    session["processed"],
        "updated":      session.get("updated", 0),
        "errors":       session.get("errors", 0),
        "keepa_status": session.get("keepa_status", "pending"),
    }


@router.get("/report")
def sync_report(token: str):
    """
    Per-parcel report: which parcels were delivered and their Keepa price status.
    Polled by the frontend every 4 s until keepa_status == 'done'.
    """
    session = _get_session(token)
    if not session:
        return {"keepa_status": "not_found", "parcels": []}

    keepa_results: dict = session.get("keepa_results", {})
    keepa_status: str   = session.get("keepa_status", "pending")

    parcels_out = []
    for dp in session.get("delivered_parcels", []):
        tn = dp["tn"]
        kr = keepa_results.get(tn)
        if kr:
            keepa_cell = kr["status"]    # "ok" | "error" | "no_asin"
            price      = kr.get("price")
            err_msg    = kr.get("error")
        elif not dp["asin"]:
            keepa_cell = "no_asin"
            price      = None
            err_msg    = None
        else:
            keepa_cell = "pending"
            price      = None
            err_msg    = None

        parcels_out.append({
            "tracking_number": tn,
            "arrived_at":      dp.get("arrived_at"),
            "asin":            dp.get("asin") or "",
            "keepa":           keepa_cell,
            "price":           price,
            "error":           err_msg,
        })

    return {"keepa_status": keepa_status, "parcels": parcels_out}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_delivered_at(date_str: str, time_str: str, gmt_offset: str) -> Optional[datetime]:
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


def _run_keepa(items: list[dict], token: str) -> None:
    """
    Background task: update Keepa prices only for parcels newly marked delivered.
    `items` = [{parcel_id, tn, asin}]  — only parcels that transitioned this sync.
    Updates _sessions[token]['keepa_results'] as each parcel is processed.
    """
    import logging
    import time

    from app.database import SessionLocal
    from app.services.keepa_service import KeepaError

    log = logging.getLogger("ups_browser_sync.keepa")
    db = SessionLocal()

    try:
        for item in items:
            pid = item["parcel_id"]
            tn  = item["tn"]

            # Wait for tokens if exhausted
            while not keepa_service.has_tokens():
                log.info("Keepa tokens exhausted, sleeping 5 min before %s", tn)
                time.sleep(300)

            parcel = db.query(Parcel).filter(Parcel.id == pid).first()
            if not parcel or not parcel.asin:
                _set_keepa_result(token, tn, "no_asin", None, None)
                continue

            try:
                coeff  = (parcel.client.cost_coefficient or 0.45) if parcel.client else 0.45
                dt     = parcel.arrived_at or datetime.utcnow()
                result = keepa_service.get_product_info(parcel.asin, dt, multiplier=coeff)

                if result.amazon_price is not None:
                    parcel.amazon_price = result.amazon_price
                if result.cost is not None:
                    parcel.purchase_price = float(result.cost)
                if result.title and not parcel.title:
                    parcel.title = result.title
                db.commit()

                price = float(parcel.amazon_price) if parcel.amazon_price is not None else None
                _set_keepa_result(token, tn, "ok", price, None)
                log.info("Keepa OK %s → $%s", tn, price)

            except KeepaError as exc:
                _set_keepa_result(token, tn, "error", None, str(exc))
                log.warning("Keepa error %s: %s", tn, exc)
            except Exception as exc:
                _set_keepa_result(token, tn, "error", None, str(exc))
                log.warning("Keepa unexpected %s: %s", tn, exc)

    finally:
        db.close()
        session = _get_session(token)
        if session is not None:
            with _lock:
                session["keepa_status"] = "done"


def _set_keepa_result(token: str, tn: str, status: str, price: Optional[float], error: Optional[str]) -> None:
    session = _get_session(token)
    if session is not None:
        with _lock:
            session["keepa_results"][tn] = {"status": status, "price": price, "error": error}
