import os
import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.auth import require_manager_up, get_current_user
from app.models.parcel import Parcel, ParcelPhoto, ParcelComment
from app.models.supplier import Supplier
from app.models.client import Client
from app.services.parcel_service import transition_parcel, STATUS_LABELS, STATUS_COLORS, VALID_TRANSITIONS
from app.permissions import can
from app.services import keepa_service

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")

router = APIRouter(prefix="/parcels")
templates = Jinja2Templates(directory="app/templates")


def _company_query(db, current_user):
    """Base Parcel query scoped to the user's company (or all for super_admin)."""
    q = db.query(Parcel)
    if current_user.role != "super_admin" and current_user.client_id:
        q = q.filter(Parcel.client_id == current_user.client_id)
    return q


def _check_parcel_access(parcel, current_user):
    """Return False if a non-super_admin user cannot access this parcel."""
    if current_user.role == "super_admin" or not current_user.client_id:
        return True
    return parcel.client_id == current_user.client_id


@router.get("", response_class=HTMLResponse)
def parcel_list(
    request: Request,
    status: str = Query("in_transit"),
    q: str = Query(""),
    unpaid: str = Query(""),
    report: str = Query(""),
    sync_flash: str = Query(""),
    client_filter: str = Query(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "view_parcels"):
        return RedirectResponse("/dashboard", status_code=302)
    query = _company_query(db, current_user)
    if status:
        query = query.filter(Parcel.status == status)
    if unpaid:
        query = query.filter(Parcel.payment_report_date.is_(None))
    if report:
        query = query.filter(Parcel.payment_report_date == report)
    if client_filter == "unassigned":
        query = query.filter(Parcel.client_id.is_(None))
    elif client_filter:
        query = query.filter(Parcel.client_id == int(client_filter))
    if q:
        q_stripped = q.strip()
        q_upper = q_stripped.upper()
        query = query.filter(
            Parcel.tracking_number.contains(q_stripped) |
            (Parcel.asin == q_upper) |
            Parcel.external_order_id.contains(q_stripped)
        )
    from sqlalchemy import case
    from collections import defaultdict
    parcels_flat = query.order_by(
        case((Parcel.external_order_id.is_(None), 1), else_=0),
        Parcel.external_order_id.asc(),
        Parcel.created_at.asc(),
    ).all()

    # Group by order_id
    order_groups: list[tuple[str | None, list]] = []
    _seen: dict = {}
    for p in parcels_flat:
        key = p.external_order_id or f"__solo_{p.id}"
        if key not in _seen:
            _seen[key] = []
            order_groups.append((p.external_order_id, _seen[key]))
        _seen[key].append(p)

    counts = {}
    base = _company_query(db, current_user)
    for s in ["unidentified", "in_transit", "delivered", "in_warehouse", "in_forwarding", "disposed", "sold"]:
        counts[s] = base.filter(Parcel.status == s).count()
    counts["unpaid"] = (
        _company_query(db, current_user)
        .filter(Parcel.status == "in_warehouse", Parcel.payment_report_date.is_(None))
        .count()
    )

    clients = db.query(Client).order_by(Client.name).all() if current_user.role == "super_admin" or not current_user.client_id else []

    return templates.TemplateResponse(
        request,
        "parcels/list.html",
        context={
            "current_user": current_user,
            "order_groups": order_groups,
            "parcels": parcels_flat,
            "active_status": status,
            "counts": counts,
            "q": q,
            "client_filter": client_filter,
            "sync_flash": sync_flash,
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_COLORS": STATUS_COLORS,
            "clients": clients,
            "can": can,
        },
    )


@router.post("/sync-transit")
def sync_transit(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "edit_parcel"):
        return RedirectResponse("/parcels?status=in_transit", status_code=302)

    from app.services.housecargo_service import sync_transit_updates as hc_sync_transit, HouseCargoError
    from app.services.shipx_service import sync_transit_updates as sx_sync_transit, ShipXError
    from app.services.crypto_service import decrypt

    suppliers = (
        db.query(Supplier)
        .filter(Supplier.platform.in_(["housecargo", "shipx"]))
        .all()
    )

    total_updated = 0
    errors: list[str] = []

    for supplier in suppliers:
        try:
            if supplier.platform == "housecargo":
                # Use per-client credentials when available
                from app.models.client import Client as ClientModel
                hc_clients = (
                    db.query(ClientModel)
                    .filter(
                        ClientModel.housecargo_supplier_id == supplier.id,
                        ClientModel.housecargo_username.isnot(None),
                        ClientModel.housecargo_password_encrypted.isnot(None),
                    )
                    .all()
                )
                if hc_clients:
                    for hc_client in hc_clients:
                        cli_pass = decrypt(hc_client.housecargo_password_encrypted)
                        result = hc_sync_transit(
                            supplier.id, hc_client.housecargo_username, cli_pass, db,
                            client_id=hc_client.id
                        )
                        total_updated += result["updated"]
                        errors.extend(result["errors"])
                elif supplier.login_username and supplier.login_password_encrypted:
                    password = decrypt(supplier.login_password_encrypted)
                    result = hc_sync_transit(supplier.id, supplier.login_username, password, db)
                    total_updated += result["updated"]
                    errors.extend(result["errors"])
            else:
                password = decrypt(supplier.login_password_encrypted)
                result = sx_sync_transit(supplier.id, supplier.login_username, password, db)
                total_updated += result["updated"]
                errors.extend(result["errors"])
        except (HouseCargoError, ShipXError) as exc:
            errors.append(f"{supplier.name}: {exc}")
        except Exception as exc:
            errors.append(f"{supplier.name}: unexpected error — {exc}")

    flash = f"Updated {total_updated} parcel(s) to Delivered."
    if errors:
        flash += f" Warnings: {'; '.join(errors[:3])}"

    import urllib.parse
    return RedirectResponse(
        f"/parcels?status=in_transit&sync_flash={urllib.parse.quote(flash)}",
        status_code=302,
    )


@router.post("/bulk")
def parcel_bulk(
    request: Request,
    action: str = Form(...),
    ids: list[int] = Form(default=[]),
    new_status: str = Form(""),
    new_client_id: str = Form(""),
    back_status: str = Form("in_transit"),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    import urllib.parse
    if not ids:
        return RedirectResponse(f"/parcels?status={back_status}", status_code=302)

    parcels = _company_query(db, current_user).filter(Parcel.id.in_(ids)).all()

    if action == "delete" and can(current_user, "delete_parcel"):
        for p in parcels:
            db.delete(p)
        db.commit()

    elif action == "set_status" and new_status and can(current_user, "edit_parcel"):
        for p in parcels:
            p.status = new_status
        db.commit()
        back_status = new_status

    elif action == "set_client" and can(current_user, "edit_parcel"):
        cid = int(new_client_id) if new_client_id else None
        for p in parcels:
            p.client_id = cid
            if cid and p.status == "unidentified":
                p.status = "in_transit"
        db.commit()

    return RedirectResponse(
        f"/parcels?status={urllib.parse.quote(back_status)}",
        status_code=302,
    )


@router.post("/{parcel_id}/assign-client")
def parcel_assign_client(
    request: Request,
    parcel_id: int,
    client_id: str = Form(""),
    back_status: str = Form("in_transit"),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if can(current_user, "edit_parcel"):
        parcel = _company_query(db, current_user).filter(Parcel.id == parcel_id).first()
        if parcel:
            parcel.client_id = int(client_id) if client_id else None
            if parcel.status == "unidentified" and client_id:
                parcel.status = "in_transit"
            db.commit()
    import urllib.parse
    return RedirectResponse(
        f"/parcels?status={urllib.parse.quote(back_status)}",
        status_code=302,
    )


@router.get("/report/new", response_class=HTMLResponse)
def report_new(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "edit_parcel"):
        return RedirectResponse("/parcels?status=in_warehouse", status_code=302)
    parcels = (
        _company_query(db, current_user)
        .filter(Parcel.status == "in_warehouse", Parcel.payment_report_date.is_(None))
        .order_by(Parcel.external_order_id.asc(), Parcel.created_at.asc())
        .all()
    )
    return templates.TemplateResponse(
        request,
        "parcels/report_new.html",
        context={
            "current_user": current_user,
            "parcels": parcels,
            "can": can,
            "today": datetime.utcnow().strftime("%Y-%m-%d"),
        },
    )


@router.post("/report/confirm")
def report_confirm(
    request: Request,
    report_date: str = Form(...),
    parcel_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "edit_parcel"):
        return RedirectResponse("/parcels?status=in_warehouse", status_code=302)
    if parcel_ids:
        parcels = (
            db.query(Parcel)
            .filter(Parcel.id.in_(parcel_ids))
            .all()
        )
        for p in parcels:
            p.payment_report_date = report_date
        db.commit()
    import urllib.parse
    return RedirectResponse(
        f"/parcels?status=in_warehouse&report={urllib.parse.quote(report_date)}",
        status_code=302,
    )


@router.get("/new", response_class=HTMLResponse)
def parcel_new(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "create_parcel"):
        return RedirectResponse("/parcels", status_code=302)
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    clients = db.query(Client).order_by(Client.name).all() if current_user.role == "super_admin" else []
    return templates.TemplateResponse(
        request,
        "parcels/form.html",
        context={
            "current_user": current_user,
            "parcel": None,
            "suppliers": suppliers,
            "clients": clients,
            "can": can,
            "error": ""
        },
    )


@router.post("/new")
async def parcel_create(
    request: Request,
    external_order_id: str = Form(""),
    tracking_number: str = Form(...),
    supplier_id: str = Form(""),
    client_id: str = Form(""),
    qty: str = Form("1"),
    asin: str = Form(""),
    purchase_price: str = Form(""),
    arrived_at: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    existing = db.query(Parcel).filter(Parcel.tracking_number == tracking_number).first()
    if existing:
        suppliers = db.query(Supplier).order_by(Supplier.name).all()
        clients = db.query(Client).order_by(Client.name).all() if current_user.role == "super_admin" else []
        return templates.TemplateResponse(
            request,
            "parcels/form.html",
            context={
                "current_user": current_user,
                "parcel": None,
                "suppliers": suppliers,
                "clients": clients,
                "can": can,
                "error": f"Tracking number '{tracking_number}' already exists"
            },
        )

    arrived_at_dt = None
    if arrived_at:
        try:
            arrived_at_dt = datetime.strptime(arrived_at, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass

    # Determine company: super_admin picks from form, others inherit their own
    if current_user.role == "super_admin":
        resolved_client_id = int(client_id) if client_id else None
    else:
        resolved_client_id = current_user.client_id

    parcel = Parcel(
        external_order_id=external_order_id.strip() or None,
        tracking_number=tracking_number.strip(),
        supplier_id=int(supplier_id) if supplier_id else None,
        client_id=resolved_client_id,
        qty=int(qty) if qty else 1,
        asin=asin.strip().upper() or None,
        purchase_price=float(purchase_price) if purchase_price else None,
        arrived_at=arrived_at_dt,
        notes=notes.strip() or None,
        status="in_transit",
    )
    db.add(parcel)
    db.commit()
    db.refresh(parcel)
    return RedirectResponse(f"/parcels/{parcel.id}", status_code=302)


@router.get("/{parcel_id}", response_class=HTMLResponse)
def parcel_detail(
    request: Request,
    parcel_id: int,
    cost_msg: str = Query(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel or not _check_parcel_access(parcel, current_user):
        return RedirectResponse("/parcels", status_code=302)
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    clients = db.query(Client).order_by(Client.name).all()

    # Other parcels belonging to the same order
    siblings = []
    if parcel.external_order_id:
        siblings = (
            db.query(Parcel)
            .filter(
                Parcel.external_order_id == parcel.external_order_id,
                Parcel.id != parcel.id,
            )
            .order_by(Parcel.created_at.asc())
            .all()
        )

    # Parse cost_msg query param: "ok:text" or "error:text"
    cost_flash = None
    if cost_msg:
        parts = cost_msg.split(":", 1)
        cost_flash = {"type": parts[0], "text": parts[1] if len(parts) > 1 else ""}

    return templates.TemplateResponse(
        request,
        "parcels/detail.html",
        context={
            "current_user": current_user,
            "parcel": parcel,
            "siblings": siblings,
            "suppliers": suppliers,
            "clients": clients,
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_COLORS": STATUS_COLORS,
            "VALID_TRANSITIONS": VALID_TRANSITIONS,
            "can": can,
            "cost_flash": cost_flash,
        },
    )


@router.get("/{parcel_id}/edit", response_class=HTMLResponse)
def parcel_edit_page(
    request: Request,
    parcel_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "edit_parcel"):
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel or not _check_parcel_access(parcel, current_user):
        return RedirectResponse("/parcels", status_code=302)
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    clients = db.query(Client).order_by(Client.name).all() if current_user.role == "super_admin" else []
    return templates.TemplateResponse(
        request,
        "parcels/form.html",
        context={
            "current_user": current_user,
            "parcel": parcel,
            "suppliers": suppliers,
            "clients": clients,
            "can": can,
            "error": "",
        },
    )


@router.post("/{parcel_id}/edit")
async def parcel_edit(
    request: Request,
    parcel_id: int,
    external_order_id: str = Form(""),
    tracking_number: str = Form(...),
    supplier_id: str = Form(""),
    client_id: str = Form(""),
    qty: str = Form("1"),
    asin: str = Form(""),
    purchase_price: str = Form(""),
    arrived_at: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel or not _check_parcel_access(parcel, current_user):
        return RedirectResponse("/parcels", status_code=302)
    if current_user.role == "super_admin":
        parcel.client_id = int(client_id) if client_id else None
    parcel.external_order_id = external_order_id.strip() or None
    parcel.tracking_number = tracking_number.strip()
    parcel.supplier_id = int(supplier_id) if supplier_id else None
    parcel.qty = int(qty) if qty else 1
    parcel.asin = asin.strip().upper() or None
    parcel.purchase_price = float(purchase_price) if purchase_price else None
    if arrived_at:
        try:
            parcel.arrived_at = datetime.strptime(arrived_at, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    else:
        parcel.arrived_at = None
    parcel.notes = notes.strip() or None
    parcel.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)


@router.post("/{parcel_id}/status")
def parcel_status_change(
    request: Request,
    parcel_id: int,
    new_status: str = Form(...),
    status_notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if parcel and _check_parcel_access(parcel, current_user):
        transition_parcel(
            parcel, new_status, db,
            changed_by=current_user.full_name or current_user.username,
            notes=status_notes,
        )
    return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)


@router.post("/{parcel_id}/comment")
def parcel_add_comment(
    request: Request,
    parcel_id: int,
    body: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    comment = ParcelComment(
        parcel_id=parcel_id,
        body=body.strip(),
        author=current_user.full_name or current_user.username,
    )
    db.add(comment)
    db.commit()
    return RedirectResponse(f"/parcels/{parcel_id}#comments", status_code=302)


@router.post("/{parcel_id}/photo")
async def parcel_upload_photo(
    request: Request,
    parcel_id: int,
    caption: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel:
        return RedirectResponse("/parcels", status_code=302)

    ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
    filename = f"{uuid.uuid4()}{ext}"
    save_dir = os.path.join(UPLOAD_DIR, "parcels", str(parcel_id))
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)
    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)

    photo = ParcelPhoto(
        parcel_id=parcel_id,
        file_path=f"/uploads/parcels/{parcel_id}/{filename}",
        caption=caption.strip() or None,
    )
    db.add(photo)
    db.commit()
    return RedirectResponse(f"/parcels/{parcel_id}#photos", status_code=302)


@router.post("/{parcel_id}/photo/{photo_id}/delete")
def parcel_delete_photo(
    request: Request,
    parcel_id: int,
    photo_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    photo = db.query(ParcelPhoto).filter(ParcelPhoto.id == photo_id, ParcelPhoto.parcel_id == parcel_id).first()
    if photo:
        try:
            os.remove(photo.file_path.lstrip("/"))
        except Exception:
            pass
        db.delete(photo)
        db.commit()
    return RedirectResponse(f"/parcels/{parcel_id}#photos", status_code=302)


@router.post("/{parcel_id}/calculate_cost")
def parcel_calculate_cost(
    request: Request,
    parcel_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    import urllib.parse
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel:
        return RedirectResponse("/parcels", status_code=302)

    errors = []
    info = []

    if not parcel.asin:
        errors.append("No ASIN — cannot look up Amazon price.")
    elif not parcel.arrived_at:
        errors.append("Delivery date not set. Run Sync or set it manually in Edit.")
    else:
        try:
            result = keepa_service.get_product_info(parcel.asin, parcel.arrived_at)
            if result.title and not parcel.title:
                parcel.title = result.title
            if result.cost is None:
                errors.append(f"Keepa: no NEW price for ASIN {parcel.asin} on {parcel.arrived_at.strftime('%d.%m.%Y')}.")
            else:
                parcel.amazon_price = result.amazon_price
                parcel.purchase_price = result.cost
                db.commit()
                info.append(f"Cost = ${result.amazon_price:.2f} × 0.45 = ${result.cost} (ASIN {parcel.asin}, {parcel.arrived_at.strftime('%d.%m.%Y')})")
        except keepa_service.KeepaError as exc:
            errors.append(f"Keepa error: {exc}")

    db.commit()
    msg_type = "error" if errors else "ok"
    encoded = urllib.parse.quote(" | ".join(errors) if errors else " | ".join(info))
    return RedirectResponse(f"/parcels/{parcel_id}?cost_msg={msg_type}:{encoded}", status_code=302)


@router.get("/{parcel_id}/accept", response_class=HTMLResponse)
def parcel_accept_page(
    request: Request,
    parcel_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    """Acceptance form: shown when moving delivered → in_warehouse."""
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel or parcel.status != "delivered":
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)
    return templates.TemplateResponse(
        request, "parcels/accept.html",
        context={"current_user": current_user, "parcel": parcel, "can": can},
    )


@router.post("/{parcel_id}/accept")
def parcel_accept(
    request: Request,
    parcel_id: int,
    condition: str = Form(...),        # "ok" | "problem"
    asin_override: str = Form(""),
    qty_override: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    import urllib.parse
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel:
        return RedirectResponse("/parcels", status_code=302)

    if condition == "problem":
        # Update ASIN/qty if provided
        if asin_override.strip():
            parcel.asin = asin_override.strip().upper()
            parcel.title = None  # will be fetched fresh below if ASIN changed
        if qty_override.strip():
            try:
                parcel.qty = int(qty_override.strip())
            except ValueError:
                pass

    # Transition to in_warehouse
    transition_parcel(parcel, "in_warehouse", db,
                      changed_by=current_user.full_name or current_user.username,
                      notes="problem" if condition == "problem" else "")

    if condition == "problem":
        parcel.purchase_price = 0
        parcel.amazon_price = None
        db.commit()
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)

    # condition == "ok" → auto-calculate cost from Keepa
    if not parcel.asin or not parcel.arrived_at:
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)

    try:
        result = keepa_service.get_product_info(parcel.asin, parcel.arrived_at)
        if result.title:
            parcel.title = result.title
        if result.cost is not None:
            parcel.amazon_price = result.amazon_price
            parcel.purchase_price = result.cost
    except keepa_service.KeepaError:
        pass  # cost stays None; user can recalculate manually

    db.commit()
    return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)


@router.post("/{parcel_id}/delete")
def parcel_delete(
    request: Request,
    parcel_id: int,
    back_status: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "delete_parcel"):
        return RedirectResponse("/parcels", status_code=302)
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    status = back_status or (parcel.status if parcel else "in_transit")
    if parcel:
        db.delete(parcel)
        db.commit()
    return RedirectResponse(f"/parcels?status={status}", status_code=302)
