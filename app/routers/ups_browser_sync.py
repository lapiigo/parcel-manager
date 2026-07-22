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
// @version      3.4
// @description  Syncs UPS delivery status to Parcel Manager (opened automatically)
// @author       Parcel Manager
// @match        https://www.ups.com/track*
// @grant        GM_xmlhttpRequest
// @run-at       document-start
// ==/UserScript==

(function () {
'use strict';

// Capture hash BEFORE UPS SPA rewrites URL with history.replaceState
var HASH = window.location.hash;

// Intercept both fetch AND XMLHttpRequest — UPS's Angular app uses XHR, not fetch.
var captured = [];

var _fetch = window.fetch.bind(window);
window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    return _fetch(input, init).then(function (resp) {
        if (url.indexOf('GetStatus') >= 0) {
            resp.clone().json().then(function (d) {
                var details = d.trackDetails || [];
                console.log('[PM] fetch GetStatus:', details.length, 'items');
                details.forEach(function (x) { captured.push(x); });
            }).catch(function () {});
        }
        return resp;
    });
};

var _xhrOpen = XMLHttpRequest.prototype.open;
var _xhrSend = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open = function (method, url) {
    this._pmUrl = url || '';
    return _xhrOpen.apply(this, arguments);
};
XMLHttpRequest.prototype.send = function (body) {
    var xhr = this;
    var url = xhr._pmUrl || '';
    // Log all non-trivial XHR URLs to identify the tracking API endpoint
    if (url && url.indexOf('ups.com') >= 0 && url.indexOf('tealium') < 0 && url.indexOf('analytics') < 0) {
        console.log('[PM] XHR:', url.split('?')[0]);
    }
    xhr.addEventListener('load', function () {
        try {
            var d = JSON.parse(xhr.responseText);
            // Capture any response that looks like tracking data
            var details = d.trackDetails || [];
            if (details.length > 0) {
                console.log('[PM] XHR tracking data from', url.split('?')[0], ':', details.length, 'items');
                details.forEach(function (x) { captured.push(x); });
            }
        } catch (e) {}
    });
    return _xhrSend.apply(this, arguments);
};

var SS = sessionStorage;
var BATCH = 25;

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

function gmGet(url) {
    return new Promise(function (ok, fail) {
        GM_xmlhttpRequest({
            method: 'GET', url: url,
            onload: function (r) {
                try { ok(JSON.parse(r.responseText)); } catch (e) { fail(e); }
            },
            onerror: fail,
        });
    });
}

function gmPost(url, body) {
    return new Promise(function (ok, fail) {
        GM_xmlhttpRequest({
            method: 'POST', url: url,
            data: JSON.stringify(body),
            headers: { 'Content-Type': 'application/json' },
            onload: ok, onerror: fail,
        });
    });
}

function showBanner(text) {
    var el = document.getElementById('__pm');
    if (!el) {
        el = document.createElement('div');
        el.id = '__pm';
        el.style.position = 'fixed';
        el.style.top = '0';
        el.style.left = '0';
        el.style.right = '0';
        el.style.zIndex = '999999';
        el.style.background = '#f59e0b';
        el.style.color = '#1c1917';
        el.style.fontWeight = '600';
        el.style.fontSize = '14px';
        el.style.textAlign = 'center';
        el.style.padding = '10px';
        el.style.fontFamily = 'sans-serif';
        document.body.prepend(el);
    }
    el.textContent = text;
    return el;
}

function gotoTrackPage() {
    window.location.replace('https://www.ups.com/track');
}

async function waitCapture(ms) {
    var end = Date.now() + ms;
    while (captured.length === 0 && Date.now() < end) { await sleep(300); }
    return captured.length > 0;
}

// Fill UPS textarea using React's internal setter (bypasses React's value tracking)
async function fillAndTrack(tns) {
    // Wait for textarea to appear
    var ta = null;
    var end = Date.now() + 8000;
    while (!ta && Date.now() < end) {
        ta = document.querySelector('textarea');
        if (!ta) await sleep(300);
    }
    if (!ta) { console.warn('[PM] No textarea found'); return false; }

    // React tracks the "last known value" on the element's internal descriptor.
    // Setting .value directly is ignored by React's onChange. Use the native setter.
    var nativeSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
    if (nativeSetter && nativeSetter.set) {
        nativeSetter.set.call(ta, tns.join('\\n'));
    } else {
        ta.value = tns.join('\\n');
    }
    ta.dispatchEvent(new Event('input',  { bubbles: true }));
    ta.dispatchEvent(new Event('change', { bubbles: true }));
    console.log('[PM] Filled textarea with', tns.length, 'tracking numbers');

    // Find the Track submit button — look inside the textarea's form first,
    // then fall back to any button whose full text is exactly "Track" (not "Tracking")
    await sleep(300);
    var btn = null;
    var form = ta.closest('form');
    if (form) {
        btn = form.querySelector('button[type="submit"]') || form.querySelector('button');
    }
    if (!btn) {
        var btns = document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            var txt = (btns[i].textContent || '').trim().replace(/\s+/g, ' ');
            if (/^track(\s*[>›»])?$/i.test(txt)) { btn = btns[i]; break; }
        }
    }
    if (btn) {
        console.log('[PM] Clicking:', (btn.textContent || '').trim());
        btn.click();
        return true;
    }
    console.warn('[PM] Track submit button not found');
    return false;
}

async function main() {
    if (document.readyState === 'loading') {
        await new Promise(function (r) {
            document.addEventListener('DOMContentLoaded', r, { once: true });
        });
    }

    var params  = new URLSearchParams(HASH.replace(/^#/, ''));
    var hToken  = params.get('pm_token');
    var hServer = params.get('pm_server');

    // ── Phase A: First load — hash has pm_token ──────────────────────────────
    if (hToken && hServer) {
        console.log('[PM] Phase A, token:', hToken);
        showBanner('Parcel Manager: завантаження трек-номерів…');
        var data;
        try {
            data = await gmGet(hServer + '/ups-sync/pending?token=' + hToken);
        } catch (e) {
            showBanner('Parcel Manager: помилка завантаження — ' + String(e));
            return;
        }
        var tns = data.tracking_numbers || [];
        console.log('[PM] Got', tns.length, 'TNs');
        SS.setItem('__pm_tok', hToken);
        SS.setItem('__pm_srv', hServer);
        SS.setItem('__pm_tns', JSON.stringify(tns));
        SS.setItem('__pm_res', '[]');
        SS.setItem('__pm_idx', '0');
        gotoTrackPage();
        return;
    }

    // ── Phase B: Continuation — state in sessionStorage ──────────────────────
    var token  = SS.getItem('__pm_tok');
    var server = SS.getItem('__pm_srv');
    if (!token || !server) return;

    var allTNs = JSON.parse(SS.getItem('__pm_tns') || '[]');
    var results = JSON.parse(SS.getItem('__pm_res') || '[]');
    var idx    = parseInt(SS.getItem('__pm_idx') || '0', 10);
    var total  = Math.ceil(allTNs.length / BATCH);

    console.log('[PM] Phase B: batch', idx + 1, 'of', total);
    var bEl = showBanner('⏳ Parcel Manager: партія ' + (idx + 1) + '/' + total + '…');

    // Fill UPS textarea directly and click Track — more reliable than URL pre-fill
    var batch = allTNs.slice(idx * BATCH, (idx + 1) * BATCH);
    await fillAndTrack(batch);

    // Wait for UPS's own GetStatus call to be intercepted
    var got = await waitCapture(15000);
    if (!got) {
        bEl.textContent = '⚠️ UPS не відповів на GetStatus. Закриття через 20с…';
        await sleep(20000);
        window.close();
        return;
    }

    // Map captured details by tracking number
    var byTN = {};
    captured.forEach(function (d) {
        var tn = (d.trackingNumber || '').trim();
        if (tn) byTN[tn] = d;
    });
    console.log('[PM] Mapped', Object.keys(byTN).length, 'unique results');

    batch.forEach(function (tn) {
        var d         = byTN[tn] || {};
        var status    = (d.packageStatus || '').trim();
        var delivered = status.toLowerCase().indexOf('delivered') >= 0;
        var row = {
            tracking_number: tn,
            delivered:       delivered,
            delivered_at:    delivered ? (d.deliveredDate  || '') : '',
            delivered_time:  delivered ? (d.deliveredTime  || '') : '',
            gmt_offset:      d.gmtOffset || '',
            status:          status,
        };
        if (!byTN[tn]) row.error = 'no_response';
        results.push(row);
        if (delivered) console.log('[PM] DELIVERED:', tn, status);
    });

    SS.setItem('__pm_res', JSON.stringify(results));

    var next = idx + 1;
    if (next < total) {
        SS.setItem('__pm_idx', String(next));
        gotoTrackPage();
        return;
    }

    // ── All done ─────────────────────────────────────────────────────────────
    ['__pm_tok','__pm_srv','__pm_tns','__pm_res','__pm_idx'].forEach(function (k) { SS.removeItem(k); });
    console.log('[PM] Complete:', results.length, 'results');
    try {
        await gmPost(server + '/ups-sync/result?token=' + token, { results: results });
        bEl.textContent = '✅ Parcel Manager: синхронізацію завершено!';
        await sleep(1000);
    } catch (e) {
        bEl.textContent = '⚠️ Parcel Manager: помилка відправки — ' + String(e);
        await sleep(20000);
    }
    window.close();
}

main().catch(function (e) { console.error('[PM] Fatal error:', e); });

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
