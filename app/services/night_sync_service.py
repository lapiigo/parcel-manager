"""
Nightly UPS delivery sync + Keepa price update.

Scheduled at 23:00 UTC (= 02:00 UTC+3 Ukraine) via systemd timer.

Flow:
1. Query all in_transit parcels with UPS tracking (starts with 1Z).
2. For each, call UPS browser-emulation tracking (~2 tracks/sec).
3. Mark delivered parcels via transition_parcel().
4. Immediately try Keepa price update for delivered parcels.
5. If Keepa tokens exhausted, retry every 30 min up to 6-hour deadline.
6. Send Telegram report (incl. per-track errors) when done.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.parcel import Parcel
from app.services import keepa_service
from app.services.parcel_service import transition_parcel
from app.services.telegram_service import send_telegram_message
from app.services.ups_service import UPSError, get_tracking_status, make_session

logger = logging.getLogger("night_sync")

_SLEEP_BETWEEN_REQUESTS = 0.45  # ~2.2 tracks/sec
_KEEPA_RETRY_SLEEP = 1800       # 30 min between Keepa retry attempts
_KEEPA_DEADLINE_HOURS = 6       # give up on Keepa after 6 hours from start


def _apply_keepa(parcel: Parcel, db) -> bool:
    """Fetch Keepa price for parcel and persist. Returns True on success."""
    if not parcel.asin:
        return False
    try:
        coeff = (parcel.client.cost_coefficient or 0.45) if parcel.client else 0.45
        delivery_dt = parcel.arrived_at or datetime.utcnow()
        result = keepa_service.get_product_info(parcel.asin, delivery_dt, multiplier=coeff)
        if result.amazon_price is not None:
            parcel.amazon_price = result.amazon_price
        if result.cost is not None:
            parcel.purchase_price = float(result.cost)
        if result.title and not parcel.title:
            parcel.title = result.title
        db.commit()
        return True
    except Exception as exc:
        logger.warning("Keepa error for parcel %s ASIN %s: %s", parcel.id, parcel.asin, exc)
        return False


def _send_report(
    stats: dict,
    started_at: datetime,
    finished_at: datetime,
    errors: list[tuple[str, str]],
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
        f"  ⏳ Пропущено / немає токенів: {stats['keepa_skipped']}",
    ]

    if stats.get("keepa_deadline_hit"):
        lines.append("  ⚠️ Ліміт часу Keepa — частина цін не оновлена")

    if errors:
        lines.append("")
        lines.append(f"<b>Помилки треків ({len(errors)}):</b>")
        # Show up to 20 errors to avoid message being too long
        for tn, reason in errors[:20]:
            lines.append(f"  • <code>{tn}</code> — {reason}")
        if len(errors) > 20:
            lines.append(f"  … та ще {len(errors) - 20} помилок (дивись лог)")

    send_telegram_message("\n".join(lines))


def run_night_sync() -> dict:
    """
    Entry point called as FastAPI background task.
    Creates its own DB session (safe to call from background thread).
    """
    db = SessionLocal()
    started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    start_time = time.time()

    stats = {
        "ups_checked": 0,
        "ups_delivered": 0,
        "ups_errors": 0,
        "keepa_updated": 0,
        "keepa_skipped": 0,
        "keepa_deadline_hit": False,
    }
    error_list: list[tuple[str, str]] = []  # (tracking_number, reason)

    try:
        # ── Phase 1: UPS delivery check ──────────────────────────────────────
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
        need_keepa: list[int] = []

        for parcel in parcels:
            stats["ups_checked"] += 1
            try:
                result = get_tracking_status(parcel.tracking_number, session)
                if result["delivered"]:
                    ok, msg = transition_parcel(
                        parcel, "delivered", db, changed_by="night_sync",
                        notes=f"UPS: {result['status']}",
                    )
                    if ok:
                        stats["ups_delivered"] += 1
                        if result["delivered_at"]:
                            parcel.arrived_at = result["delivered_at"]
                            db.commit()
                        if parcel.asin:
                            need_keepa.append(parcel.id)
            except UPSError as exc:
                stats["ups_errors"] += 1
                reason = str(exc)
                error_list.append((parcel.tracking_number, reason))
                logger.warning("UPS error %s: %s", parcel.tracking_number, reason)

            time.sleep(_SLEEP_BETWEEN_REQUESTS)

        # ── Phase 2: Keepa price update with retry ──────────────────────────
        deadline = start_time + _KEEPA_DEADLINE_HOURS * 3600

        while need_keepa and time.time() < deadline:
            if not keepa_service.has_tokens():
                logger.info(
                    "Keepa tokens exhausted, %d parcels deferred. Sleeping 30 min.",
                    len(need_keepa),
                )
                time.sleep(_KEEPA_RETRY_SLEEP)
                continue

            parcel_id = need_keepa.pop(0)
            parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
            if parcel is None:
                continue

            if not keepa_service.has_tokens():
                need_keepa.insert(0, parcel_id)
                continue

            if _apply_keepa(parcel, db):
                stats["keepa_updated"] += 1
            else:
                stats["keepa_skipped"] += 1

        if need_keepa:
            stats["keepa_skipped"] += len(need_keepa)
            stats["keepa_deadline_hit"] = True
            logger.warning(
                "Night sync deadline reached, %d parcels still need Keepa.", len(need_keepa)
            )

    except Exception as exc:
        logger.exception("Night sync failed unexpectedly: %s", exc)
        stats["ups_errors"] += 1

    finally:
        db.close()

    finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    logger.info("Night sync complete: %s", stats)

    try:
        _send_report(stats, started_at, finished_at, error_list)
    except Exception as exc:
        logger.warning("Failed to send Telegram report: %s", exc)

    return stats
