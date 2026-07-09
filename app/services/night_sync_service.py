"""
Nightly UPS delivery sync + Keepa price update.

Scheduled at 23:00 UTC (= 02:00 UTC+3 Ukraine) via systemd timer.

Flow:
1. Query all in_transit parcels with UPS tracking (starts with 1Z).
2. For each, call UPS browser-emulation tracking (~2 tracks/sec).
3. Mark delivered parcels via transition_parcel().
4. Try Keepa price update for every delivered parcel:
   - tokens exhausted → sleep 5 min → retry same parcel (no limit on retries)
   - real error (ASIN not found, etc.) → log track + reason, move on
   - price not returned → log track + reason, move on
   - success → move on
5. Send Telegram report (UPS stats + Keepa errors list) when all done.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from app.database import SessionLocal
from app.models.parcel import Parcel
from app.services import keepa_service
from app.services.keepa_service import KeepaError
from app.services.parcel_service import transition_parcel
from app.services.telegram_service import send_telegram_message
from app.services.ups_service import UPSError, get_tracking_status, make_session

logger = logging.getLogger("night_sync")

_SLEEP_BETWEEN_UPS = 0.45       # ~2.2 tracks/sec
_KEEPA_TOKEN_WAIT = 300         # 5 min wait when tokens exhausted


# ── Keepa status constants ────────────────────────────────────────────────────
_K_OK = "ok"
_K_RATE = "rate_limited"    # 429 or no tokens — retry after sleep
_K_ERR = "error"            # permanent failure — log and skip


def _keepa_update(parcel: Parcel, db) -> tuple[str, Optional[str]]:
    """
    Try to fetch Keepa price and persist it.

    Returns:
        (_K_OK,   None)      — price updated
        (_K_RATE, None)      — tokens exhausted, retry after sleep
        (_K_ERR,  "reason")  — permanent failure, log and skip
    """
    if not parcel.asin:
        return _K_ERR, "no ASIN"

    try:
        coeff = (parcel.client.cost_coefficient or 0.45) if parcel.client else 0.45
        delivery_dt = parcel.arrived_at or datetime.utcnow()
        result = keepa_service.get_product_info(parcel.asin, delivery_dt, multiplier=coeff)

        if result.amazon_price is None:
            return _K_ERR, "ціна відсутня в Keepa (товар міг бути знятий з продажу)"

        if result.amazon_price is not None:
            parcel.amazon_price = result.amazon_price
        if result.cost is not None:
            parcel.purchase_price = float(result.cost)
        if result.title and not parcel.title:
            parcel.title = result.title
        db.commit()
        return _K_OK, None

    except KeepaError as exc:
        msg = str(exc)
        # 429 or explicit "rate limit" → tokens exhausted, retry
        if "429" in msg or not keepa_service.has_tokens():
            return _K_RATE, None
        return _K_ERR, msg

    except Exception as exc:
        return _K_ERR, str(exc)


def _send_report(
    stats: dict,
    started_at: datetime,
    finished_at: datetime,
    ups_errors: list[tuple[str, str]],
    keepa_errors: list[tuple[str, str]],
) -> None:
    start_str = started_at.strftime("%d.%m.%Y %H:%M UTC")
    end_str = finished_at.strftime("%H:%M UTC")
    duration_min = int((finished_at - started_at).total_seconds() / 60)
    ups_ok = stats["ups_checked"] - stats["ups_errors"]

    lines = [
        "🌙 <b>Night UPS Sync Report</b>",
        "",
        f"⏱ {start_str} → {end_str} ({duration_min} min)",
        "",
        "<b>UPS tracking</b>",
        f"  📦 Перевірено: {stats['ups_checked']}",
        f"  ✅ Успішно: {ups_ok}",
        f"  🚚 Позначено доставленими: {stats['ups_delivered']}",
        f"  ❌ Помилок: {stats['ups_errors']}",
        "",
        "<b>Keepa ціни</b>",
        f"  💰 Оновлено: {stats['keepa_updated']}",
        f"  ❌ Не вдалось: {stats['keepa_errors']}",
    ]

    if ups_errors:
        lines += ["", f"<b>Помилки UPS ({len(ups_errors)}):</b>"]
        for tn, reason in ups_errors[:20]:
            lines.append(f"  • <code>{tn}</code> — {reason}")
        if len(ups_errors) > 20:
            lines.append(f"  … та ще {len(ups_errors) - 20}")

    if keepa_errors:
        lines += ["", f"<b>Помилки Keepa ({len(keepa_errors)}):</b>"]
        for tn, reason in keepa_errors[:20]:
            lines.append(f"  • <code>{tn}</code> — {reason}")
        if len(keepa_errors) > 20:
            lines.append(f"  … та ще {len(keepa_errors) - 20}")

    send_telegram_message("\n".join(lines))


def run_night_sync() -> dict:
    """
    Entry point called as FastAPI background task.
    Creates its own DB session (safe to call from background thread).
    """
    db = SessionLocal()
    started_at = datetime.now(timezone.utc).replace(tzinfo=None)

    stats = {
        "ups_checked": 0,
        "ups_delivered": 0,
        "ups_errors": 0,
        "keepa_updated": 0,
        "keepa_errors": 0,
    }
    ups_error_list: list[tuple[str, str]] = []
    keepa_error_list: list[tuple[str, str]] = []

    try:
        # ── Phase 1: UPS delivery check ───────────────────────────────────────
        parcels = (
            db.query(Parcel)
            .filter(
                Parcel.status == "in_transit",
                Parcel.tracking_number.like("1Z%"),
            )
            .all()
        )

        logger.info("Night sync: %d UPS parcels to check", len(parcels))
        session = make_session()

        # Collect (parcel_id, tracking_number) pairs that need Keepa
        need_keepa: list[tuple[int, str]] = []

        for parcel in parcels:
            stats["ups_checked"] += 1
            try:
                result = get_tracking_status(parcel.tracking_number, session)
                if result["delivered"]:
                    ok, _ = transition_parcel(
                        parcel, "delivered", db, changed_by="night_sync",
                        notes=f"UPS: {result['status']}",
                    )
                    if ok:
                        stats["ups_delivered"] += 1
                        if result["delivered_at"]:
                            parcel.arrived_at = result["delivered_at"]
                            db.commit()
                        need_keepa.append((parcel.id, parcel.tracking_number))
            except UPSError as exc:
                stats["ups_errors"] += 1
                ups_error_list.append((parcel.tracking_number, str(exc)))
                logger.warning("UPS error %s: %s", parcel.tracking_number, exc)

            time.sleep(_SLEEP_BETWEEN_UPS)

        # ── Phase 2: Keepa price update — retry until all processed ──────────
        # Queue: list of (parcel_id, tracking_number) to process
        queue = list(need_keepa)
        i = 0

        while i < len(queue):
            parcel_id, tracking_number = queue[i]
            parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()

            if parcel is None:
                i += 1
                continue

            status, err = _keepa_update(parcel, db)

            if status == _K_OK:
                stats["keepa_updated"] += 1
                logger.info("Keepa OK: %s", tracking_number)
                i += 1

            elif status == _K_RATE:
                # Tokens exhausted — wait 5 min and retry same parcel
                tokens = keepa_service.tokens_left()
                logger.info(
                    "Keepa tokens exhausted (left=%s), sleeping 5 min before retry %s",
                    tokens, tracking_number,
                )
                time.sleep(_KEEPA_TOKEN_WAIT)
                # Don't advance i — retry same parcel

            else:
                # Permanent error — log and skip
                stats["keepa_errors"] += 1
                keepa_error_list.append((tracking_number, err or "unknown"))
                logger.warning("Keepa error %s: %s", tracking_number, err)
                i += 1

    except Exception as exc:
        logger.exception("Night sync failed unexpectedly: %s", exc)
        stats["ups_errors"] += 1

    finally:
        db.close()

    finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    logger.info("Night sync complete: %s", stats)

    try:
        _send_report(stats, started_at, finished_at, ups_error_list, keepa_error_list)
    except Exception as exc:
        logger.warning("Failed to send Telegram report: %s", exc)

    return stats
