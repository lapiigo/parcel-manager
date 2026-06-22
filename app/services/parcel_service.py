"""
Parcel status state machine
===========================

  [Sync/Import] ──→ in_transit
                         │ (sync confirms delivery)
                         ↓
                     delivered   ← physically at warehouse, not yet processed
                    ↙     ↓     ↘
            forwarding  Accept  return_to_supplier
                │          ↓           │
         [new tracking]  ready_to_pay  [mark returned → is_returned=True]
                │          ↓
        new in_transit    paid
                           ↓
                          sold

  [Sync] ──→ unidentified ──→ in_transit / forwarding / ignored
  [Any]  ──→ ignored      ──→ in_transit (un-ignore)

Notes
-----
- forwarding and return_to_supplier have no further status transitions;
  special routes /forward and /mark-returned handle their lifecycle.
- payment_report_date is a metadata tag set when status → paid.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.parcel import Parcel, ParcelStatusLog

VALID_TRANSITIONS: dict[str, list[str]] = {
    "unidentified":       ["in_transit", "forwarding", "ignored"],
    "in_transit":         ["delivered", "ignored"],
    "delivered":          ["ready_to_pay", "forwarding", "return_to_supplier", "negotiating"],
    "negotiating":        ["ready_to_pay", "return_to_supplier"],
    "ready_to_pay":       ["paid", "return_to_supplier"],
    "paid":               ["sold"],
    "forwarding":         [],
    "return_to_supplier": [],
    "sold":               [],
    "ignored":            ["in_transit"],
    # legacy — kept for backward-compat display only
    "disposed":           [],
    "in_warehouse":       ["ready_to_pay"],
    "in_forwarding":      ["forwarding"],
}

STATUS_LABELS: dict[str, str] = {
    "unidentified":       "Unidentified",
    "in_transit":         "In Transit",
    "delivered":          "Delivered",
    "negotiating":        "In Negotiation",
    "ready_to_pay":       "Ready to Pay",
    "paid":               "Paid",
    "forwarding":         "Forwarding",
    "return_to_supplier": "Return to Supplier",
    "sold":               "Sold",
    "ignored":            "Ignored",
    # legacy
    "disposed":           "Disposed",
    "in_warehouse":       "In Warehouse (legacy)",
    "in_forwarding":      "In Forwarding (legacy)",
}

STATUS_COLORS: dict[str, str] = {
    "unidentified":       "bg-gray-100 text-gray-600",
    "in_transit":         "bg-yellow-100 text-yellow-800",
    "delivered":          "bg-sky-100 text-sky-800",
    "negotiating":        "bg-amber-100 text-amber-800",
    "ready_to_pay":       "bg-green-100 text-green-800",
    "paid":               "bg-emerald-100 text-emerald-800",
    "forwarding":         "bg-blue-100 text-blue-800",
    "return_to_supplier": "bg-orange-100 text-orange-800",
    "sold":               "bg-purple-100 text-purple-800",
    "ignored":            "bg-gray-50 text-gray-400",
    # legacy
    "disposed":           "bg-red-100 text-red-800",
    "in_warehouse":       "bg-green-100 text-green-700",
    "in_forwarding":      "bg-blue-100 text-blue-700",
}

ALL_STATUSES = [
    "unidentified", "in_transit", "delivered", "negotiating",
    "ready_to_pay", "paid", "forwarding",
    "return_to_supplier", "sold", "ignored",
]


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
            f"Transition from '{STATUS_LABELS.get(parcel.status, parcel.status)}' "
            f"to '{STATUS_LABELS.get(new_status, new_status)}' is not allowed"
        )

    db.add(ParcelStatusLog(
        parcel_id=parcel.id,
        old_status=parcel.status,
        new_status=new_status,
        changed_by=changed_by,
        notes=notes,
    ))

    parcel.status = new_status
    if new_status in ("delivered", "ready_to_pay") and not parcel.arrived_at:
        parcel.arrived_at = datetime.utcnow()
    parcel.updated_at = datetime.utcnow()

    if new_status == "ready_to_pay":
        _ensure_warehouse_item(db, parcel)

    db.commit()
    db.refresh(parcel)
    return True, "OK"


def _ensure_warehouse_item(db: Session, parcel: Parcel) -> None:
    from app.models.warehouse import WarehouseItem
    existing = db.query(WarehouseItem).filter(WarehouseItem.parcel_id == parcel.id).first()
    if not existing:
        db.add(WarehouseItem(
            parcel_id=parcel.id,
            client_id=parcel.client_id,
        ))
