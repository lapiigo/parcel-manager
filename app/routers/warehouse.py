from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.auth import require_manager_up
from app.models.warehouse import WarehouseItem, WAREHOUSE_STATUS_LABELS, WAREHOUSE_STATUS_COLORS
from app.models.client import Client
from app.permissions import can

router = APIRouter(prefix="/warehouse")
templates = Jinja2Templates(directory="app/templates")

ACTIVE_STATUSES = ["available", "reserved"]
ARCHIVE_STATUSES = ["sold", "fba_sent", "returned"]


@router.get("", response_class=HTMLResponse)
def warehouse_list(
    request: Request,
    status: str = Query(""),
    q: str = Query(""),
    client_filter: str = Query(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "view_parcels"):
        return RedirectResponse("/dashboard", status_code=302)

    query = db.query(WarehouseItem)

    if status == "archive":
        query = query.filter(WarehouseItem.status.in_(ARCHIVE_STATUSES))
    elif status:
        query = query.filter(WarehouseItem.status == status)
    else:
        query = query.filter(WarehouseItem.status.in_(ACTIVE_STATUSES))

    if client_filter:
        query = query.filter(WarehouseItem.client_id == int(client_filter))

    if q:
        from app.models.parcel import Parcel
        q_stripped = q.strip()
        matched_parcel_ids = (
            db.query(Parcel.id)
            .filter(
                Parcel.tracking_number.contains(q_stripped) |
                Parcel.asin.contains(q_stripped.upper()) |
                Parcel.title.contains(q_stripped)
            )
            .all()
        )
        pid_list = [r[0] for r in matched_parcel_ids]
        query = query.filter(WarehouseItem.parcel_id.in_(pid_list))

    items = query.order_by(WarehouseItem.created_at.desc()).all()

    counts = {}
    base = db.query(WarehouseItem)
    for s in ["available", "reserved", "sold", "fba_sent", "returned"]:
        counts[s] = base.filter(WarehouseItem.status == s).count()
    counts["archive"] = sum(counts.get(s, 0) for s in ARCHIVE_STATUSES)

    clients = db.query(Client).order_by(Client.name).all() if current_user.role == "super_admin" or not current_user.client_id else []

    return templates.TemplateResponse(
        request,
        "warehouse/list.html",
        {
            "current_user": current_user,
            "items": items,
            "active_status": status,
            "counts": counts,
            "q": q,
            "client_filter": client_filter,
            "clients": clients,
            "WAREHOUSE_STATUS_LABELS": WAREHOUSE_STATUS_LABELS,
            "WAREHOUSE_STATUS_COLORS": WAREHOUSE_STATUS_COLORS,
            "can": can,
        },
    )


@router.get("/{item_id}", response_class=HTMLResponse)
def warehouse_detail(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "view_parcels"):
        return RedirectResponse("/dashboard", status_code=302)

    item = db.query(WarehouseItem).filter(WarehouseItem.id == item_id).first()
    if not item:
        return RedirectResponse("/warehouse", status_code=302)

    return templates.TemplateResponse(
        request,
        "warehouse/detail.html",
        {
            "current_user": current_user,
            "item": item,
            "WAREHOUSE_STATUS_LABELS": WAREHOUSE_STATUS_LABELS,
            "WAREHOUSE_STATUS_COLORS": WAREHOUSE_STATUS_COLORS,
            "can": can,
        },
    )


@router.post("/{item_id}/update")
def warehouse_update(
    item_id: int,
    request: Request,
    status: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "edit_parcel"):
        return RedirectResponse(f"/warehouse/{item_id}", status_code=302)

    item = db.query(WarehouseItem).filter(WarehouseItem.id == item_id).first()
    if not item:
        return RedirectResponse("/warehouse", status_code=302)

    if status and status in WAREHOUSE_STATUS_LABELS:
        item.status = status
    if location is not None:
        item.location = location.strip() or None
    if notes is not None:
        item.notes = notes.strip() or None

    db.commit()
    return RedirectResponse(f"/warehouse/{item_id}", status_code=302)
