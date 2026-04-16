"""
Parcel status state machine:

  in_transit   → delivered    (automatic: housecargo sync marks as delivered)
  delivered    → in_warehouse (manual: staff checks physical parcel)
  in_warehouse → in_forwarding | disposed
  in_forwarding → in_warehouse | disposed
  in_warehouse / in_forwarding → sold (set by order_service)
  disposed → (terminal)
  sold     → (terminal)

Payment (separate from status):
  payment_report_date = None  → unpaid
  payment_report_date = "YYYY-MM-DD" → paid (tagged with report date)
"""
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.parcel import Parcel, ParcelStatusLog

VALID_TRANSITIONS = {
    "unidentified":  ["in_transit", "in_forwarding", "disposed"],
    "in_transit":    ["delivered", "in_warehouse", "disposed"],
    "delivered":     ["in_warehouse", "disposed"],
    "in_forwarding": ["in_warehouse", "disposed"],
    "in_warehouse":  ["in_forwarding", "disposed"],
    "disposed":      [],
    "sold":          [],
}

STATUS_LABELS = {
    "unidentified":  "Unidentified",
    "in_transit":    "In Transit",
    "delivered":     "Delivered to Warehouse",
    "in_warehouse":  "In Warehouse",
    "in_forwarding": "In Forwarding",
    "disposed":      "Disposed",
    "sold":          "Sold",
}

STATUS_COLORS = {
    "unidentified":  "bg-gray-100 text-gray-600",
    "in_transit":    "bg-yellow-100 text-yellow-800",
    "delivered":     "bg-sky-100 text-sky-800",
    "in_warehouse":  "bg-green-100 text-green-800",
    "in_forwarding": "bg-blue-100 text-blue-800",
    "disposed":      "bg-red-100 text-red-800",
    "sold":          "bg-purple-100 text-purple-800",
}


def transition_parcel(
    parcel: Parcel,
    new_status: str,
    db: Session,
    changed_by: str = "",
    notes: str = "",
) -> tuple[bool, str]:
    allowed = VALID_TRANSITIONS.get(parcel.status, [])
    if new_status not in allowed:
        return False, (
            f"Transition from '{STATUS_LABELS.get(parcel.status)}' "
            f"to '{STATUS_LABELS.get(new_status)}' is not allowed"
        )

    log = ParcelStatusLog(
        parcel_id=parcel.id,
        old_status=parcel.status,
        new_status=new_status,
        changed_by=changed_by,
        notes=notes,
    )
    db.add(log)

    parcel.status = new_status
    if new_status in ("delivered", "in_warehouse") and not parcel.arrived_at:
        parcel.arrived_at = datetime.utcnow()
    parcel.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(parcel)
    return True, "OK"
