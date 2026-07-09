import os
import uuid
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Form, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional, List

from app.database import get_db
from app.auth import require_manager_up, get_current_user
from app.models.parcel import Parcel, ParcelPhoto, ParcelComment, ParcelLog, ParcelNegotiation
from app.models.client import ClientDeposit
from app.models.supplier import Supplier
from app.models.client import Client
from app.services.parcel_service import transition_parcel, STATUS_LABELS, STATUS_COLORS, VALID_TRANSITIONS, ALL_STATUSES
from app.permissions import can
from app.services import keepa_service

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")


def _log(db, parcel_id: int, action: str, detail: str = None, user=None) -> None:
    """Append one entry to the parcel activity log."""
    db.add(ParcelLog(
        parcel_id=parcel_id,
        user_id=getattr(user, "id", None),
        user_name=(user.full_name or user.username) if user else "system",
        action=action,
        detail=detail,
    ))

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


_ACTIVE_STATUSES = [
    "unidentified", "in_transit", "delivered", "negotiating",
    "ready_to_pay", "forwarding", "return_to_supplier",
]
_ARCHIVE_STATUSES = ["paid", "sold", "ignored"]


@router.get("", response_class=HTMLResponse)
def parcel_list(
    request: Request,
    status: str = Query(""),
    q: str = Query(""),
    unpaid: str = Query(""),
    report: str = Query(""),
    sync_flash: str = Query(""),
    client_filter: str = Query(""),
    supplier_filter: str = Query(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "view_parcels"):
        return RedirectResponse("/dashboard", status_code=302)
    query = _company_query(db, current_user)
    if status == "archive":
        query = query.filter(Parcel.status.in_(_ARCHIVE_STATUSES))
    elif status:
        query = query.filter(Parcel.status == status)
    else:
        query = query.filter(Parcel.status.in_(_ACTIVE_STATUSES))
    if unpaid:
        query = query.filter(Parcel.payment_report_date.is_(None))
    if report:
        query = query.filter(Parcel.payment_report_date == report)
    if client_filter == "unassigned":
        query = query.filter(Parcel.client_id.is_(None))
    elif client_filter:
        query = query.filter(Parcel.client_id == int(client_filter))
    if supplier_filter:
        query = query.filter(Parcel.supplier_id == int(supplier_filter))
    if q:
        q_stripped = q.strip()
        q_upper = q_stripped.upper()
        query = query.filter(
            Parcel.tracking_number.contains(q_stripped) |
            (Parcel.asin == q_upper) |
            Parcel.external_order_id.contains(q_stripped)
        )
    from sqlalchemy import case
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
    for s in ALL_STATUSES:
        counts[s] = base.filter(Parcel.status == s).count()

    clients = db.query(Client).order_by(Client.name).all() if current_user.role == "super_admin" or not current_user.client_id else []
    suppliers = db.query(Supplier).order_by(Supplier.name).all()

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
            "supplier_filter": supplier_filter,
            "sync_flash": sync_flash,
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_COLORS": STATUS_COLORS,
            "clients": clients,
            "suppliers": suppliers,
            "report": report,
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
        return RedirectResponse("/parcels?status=in_transit", status_code=302)  # keep status filter on access denied

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
            else:  # shipx
                from app.models.client import Client as ClientModel
                sx_clients = (
                    db.query(ClientModel)
                    .filter(
                        ClientModel.shipx_supplier_id == supplier.id,
                        ClientModel.shipx_username.isnot(None),
                        ClientModel.shipx_password_encrypted.isnot(None),
                    )
                    .all()
                )
                if sx_clients:
                    for sx_client in sx_clients:
                        cli_pass = decrypt(sx_client.shipx_password_encrypted)
                        result = sx_sync_transit(
                            supplier.id, sx_client.shipx_username, cli_pass, db,
                            client_id=sx_client.id
                        )
                        total_updated += result["updated"]
                        errors.extend(result["errors"])
                elif supplier.login_username and supplier.login_password_encrypted:
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
        f"/parcels?sync_flash={urllib.parse.quote(flash)}",
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
        return RedirectResponse("/parcels?status=ready_to_pay", status_code=302)
    parcels = (
        _company_query(db, current_user)
        .filter(Parcel.status == "ready_to_pay")
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
        return RedirectResponse("/parcels?status=ready_to_pay", status_code=302)
    if parcel_ids:
        parcels = db.query(Parcel).filter(Parcel.id.in_(parcel_ids)).all()
        for p in parcels:
            p.status = "paid"
            p.payment_report_date = report_date
        db.commit()
    import urllib.parse
    return RedirectResponse(
        f"/parcels?status=paid&report={urllib.parse.quote(report_date)}",
        status_code=302,
    )


@router.get("/new", response_class=HTMLResponse)
def parcel_new(
    request: Request,
    order_id: str = Query(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "create_parcel"):
        return RedirectResponse("/parcels", status_code=302)
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    clients = db.query(Client).order_by(Client.name).all() if current_user.role == "super_admin" else []
    return templates.TemplateResponse(
        request,
        "parcels/form_new.html",
        context={
            "current_user": current_user,
            "suppliers": suppliers,
            "clients": clients,
            "can": can,
            "errors": [],
            "prefill_order_id": order_id,
        },
    )


@router.post("/new")
async def parcel_create(
    request: Request,
    external_order_id: str = Form(""),
    tracking_number: List[str] = Form(default=[]),
    supplier_id: str = Form(""),
    client_id: str = Form(""),
    qty: List[str] = Form(default=[]),
    asin: List[str] = Form(default=[]),
    purchase_price: List[str] = Form(default=[]),
    arrived_at: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    clients = db.query(Client).order_by(Client.name).all() if current_user.role == "super_admin" else []

    arrived_at_dt = None
    if arrived_at:
        try:
            arrived_at_dt = datetime.strptime(arrived_at, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass

    if current_user.role == "super_admin":
        resolved_client_id = int(client_id) if client_id else None
    else:
        resolved_client_id = current_user.client_id

    # Pad shorter lists so all have same length
    n = max(len(tracking_number), 1)
    def _pad(lst, length, default=""):
        return list(lst) + [default] * (length - len(lst))

    tracking_number = _pad(tracking_number, n)
    asin            = _pad(asin, n)
    qty             = _pad(qty, n, "1")
    purchase_price  = _pad(purchase_price, n)

    errors = []
    created_ids = []
    seen_combos: set[tuple] = set()  # (tracking, asin) pairs submitted in this form

    for i in range(n):
        track = tracking_number[i].strip()
        if not track:
            errors.append(f"Item {i+1}: tracking number is required")
            continue

        asin_val = asin[i].strip().upper() or None

        # Deduplicate within this form submission
        combo = (track, asin_val)
        if combo in seen_combos:
            errors.append(f"Item {i+1}: duplicate tracking+ASIN — skipped")
            continue
        seen_combos.add(combo)

        # Check DB: allow same tracking if different ASIN (multi-item shipment)
        if asin_val:
            existing = db.query(Parcel).filter(
                Parcel.tracking_number == track,
                Parcel.asin == asin_val,
            ).first()
        else:
            existing = db.query(Parcel).filter(Parcel.tracking_number == track).first()
        if existing:
            errors.append(f"Item {i+1}: tracking '{track}'" + (f" / ASIN {asin_val}" if asin_val else "") + f" already exists (parcel #{existing.id})")
            continue

        qty_val  = int(qty[i]) if qty[i].strip().isdigit() else 1
        price_val = None
        try:
            if purchase_price[i].strip():
                price_val = float(purchase_price[i])
        except ValueError:
            pass

        # Estimated cost if ASIN known and not manually provided
        title_val = None
        if asin_val and price_val is None:
            try:
                from app.services import keepa_service
                client_obj = db.query(Client).filter(Client.id == resolved_client_id).first() if resolved_client_id else None
                coeff = (client_obj.cost_coefficient if client_obj and client_obj.cost_coefficient is not None else 0.45)
                result = keepa_service.get_estimated_cost(asin_val, coeff)
                if result.cost is not None:
                    price_val = float(result.cost)
                if result.title:
                    title_val = result.title
            except Exception:
                pass

        parcel = Parcel(
            external_order_id=external_order_id.strip() or None,
            tracking_number=track,
            supplier_id=int(supplier_id) if supplier_id else None,
            client_id=resolved_client_id,
            qty=qty_val,
            asin=asin_val,
            title=title_val,
            purchase_price=price_val,
            arrived_at=arrived_at_dt,
            notes=notes.strip() or None,
            status="in_transit",
        )
        db.add(parcel)
        db.flush()
        _log(db, parcel.id, "created",
             f"Tracking: {parcel.tracking_number}" + (f" | Order: {parcel.external_order_id}" if parcel.external_order_id else ""),
             user=current_user)
        created_ids.append(parcel.id)

    db.commit()

    if errors and not created_ids:
        # All failed — show form with errors
        return templates.TemplateResponse(
            request,
            "parcels/form_new.html",
            context={
                "current_user": current_user,
                "suppliers": suppliers,
                "clients": clients,
                "can": can,
                "errors": errors,
                "prefill_order_id": external_order_id,
            },
        )

    # At least some created — redirect to first parcel (or list if order_id present)
    if external_order_id.strip() and len(created_ids) > 1:
        import urllib.parse
        return RedirectResponse(
            f"/parcels?status=in_transit&q={urllib.parse.quote(external_order_id.strip())}",
            status_code=302,
        )
    return RedirectResponse(f"/parcels/{created_ids[0]}", status_code=302)


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

    activity_logs = (
        db.query(ParcelLog)
        .filter(ParcelLog.parcel_id == parcel_id)
        .order_by(ParcelLog.created_at.asc())
        .all()
        if current_user.role in ("admin", "super_admin") else []
    )

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
            "activity_logs": activity_logs,
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

    # Capture diff before applying changes
    changes = []
    new_tracking = tracking_number.strip()
    new_ext = external_order_id.strip() or None
    new_sup = int(supplier_id) if supplier_id else None
    new_qty = int(qty) if qty else 1
    new_asin = asin.strip().upper() or None
    new_price = float(purchase_price) if purchase_price else None
    new_notes = notes.strip() or None
    if parcel.tracking_number != new_tracking:
        changes.append(f"tracking: {parcel.tracking_number} → {new_tracking}")
    if parcel.external_order_id != new_ext:
        changes.append(f"order_id: {parcel.external_order_id} → {new_ext}")
    if parcel.supplier_id != new_sup:
        changes.append(f"supplier_id: {parcel.supplier_id} → {new_sup}")
    if parcel.qty != new_qty:
        changes.append(f"qty: {parcel.qty} → {new_qty}")
    if parcel.asin != new_asin:
        changes.append(f"ASIN: {parcel.asin} → {new_asin}")
    if parcel.purchase_price != new_price:
        changes.append(f"price: {parcel.purchase_price} → {new_price}")
    if parcel.notes != new_notes:
        changes.append("notes updated")

    if current_user.role == "super_admin":
        parcel.client_id = int(client_id) if client_id else None
    parcel.external_order_id = new_ext
    parcel.tracking_number = new_tracking
    parcel.supplier_id = new_sup
    parcel.qty = new_qty
    parcel.asin = new_asin
    parcel.purchase_price = new_price
    if arrived_at:
        try:
            parcel.arrived_at = datetime.strptime(arrived_at, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    else:
        parcel.arrived_at = None
    parcel.notes = new_notes
    parcel.updated_at = datetime.utcnow()

    if changes:
        _log(db, parcel_id, "edited", " | ".join(changes), user=current_user)
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
        old_status = parcel.status
        transition_parcel(
            parcel, new_status, db,
            changed_by=current_user.full_name or current_user.username,
            notes=status_notes,
        )
        _log(db, parcel_id, "status_changed",
             f"{STATUS_LABELS.get(old_status, old_status)} → {STATUS_LABELS.get(new_status, new_status)}"
             + (f" | {status_notes}" if status_notes else ""),
             user=current_user)
        db.commit()
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
    _log(db, parcel_id, "comment_added", body.strip()[:120], user=current_user)
    db.commit()
    return RedirectResponse(f"/parcels/{parcel_id}#comments", status_code=302)


@router.post("/{parcel_id}/photo")
async def parcel_upload_photo(
    request: Request,
    parcel_id: int,
    caption: str = Form(""),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel:
        return RedirectResponse("/parcels", status_code=302)

    save_dir = os.path.join(UPLOAD_DIR, "parcels", str(parcel_id))
    os.makedirs(save_dir, exist_ok=True)

    for file in files:
        if not file.filename:
            continue
        ext = os.path.splitext(file.filename)[1] or ".jpg"
        filename = f"{uuid.uuid4()}{ext}"
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
        _log(db, parcel_id, "photo_added", file.filename, user=current_user)

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
        detail = photo.caption or photo.file_path.rsplit("/", 1)[-1]
        db.delete(photo)
        _log(db, parcel_id, "photo_deleted", detail, user=current_user)
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

    if not errors and info:
        _log(db, parcel_id, "cost_calculated", info[0], user=current_user)
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
    condition: str = Form(...),        # "ok" | "damaged" | "very_damaged" | "wrong_item"
    asin_override: str = Form(""),
    qty_override: str = Form(""),
    accept_notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel:
        return RedirectResponse("/parcels", status_code=302)

    notes_text = accept_notes.strip() or None

    if condition == "wrong_item":
        if asin_override.strip():
            parcel.asin = asin_override.strip().upper()
            parcel.title = None
        if qty_override.strip():
            try:
                parcel.qty = int(qty_override.strip())
            except ValueError:
                pass
        parcel.purchase_price = 0
        parcel.amazon_price = None
        if notes_text:
            parcel.notes = notes_text
        transition_parcel(parcel, "ready_to_pay", db,
                          changed_by=current_user.full_name or current_user.username,
                          notes="wrong item")
        _log(db, parcel_id, "accepted",
             "Condition: wrong item" + (f" | {notes_text}" if notes_text else ""),
             user=current_user)
        db.commit()
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)

    if condition in ("damaged", "very_damaged"):
        parcel.purchase_price = 0
        parcel.amazon_price = None
        if notes_text:
            parcel.notes = notes_text
        transition_parcel(parcel, "ready_to_pay", db,
                          changed_by=current_user.full_name or current_user.username,
                          notes=condition.replace("_", " "))
        _log(db, parcel_id, "accepted",
             f"Condition: {condition.replace('_', ' ')}" + (f" | {notes_text}" if notes_text else ""),
             user=current_user)
        db.commit()
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)

    # condition == "ok" → auto-calculate cost from Keepa
    transition_parcel(parcel, "ready_to_pay", db,
                      changed_by=current_user.full_name or current_user.username)
    if notes_text:
        parcel.notes = notes_text

    if parcel.asin and parcel.arrived_at:
        try:
            result = keepa_service.get_product_info(parcel.asin, parcel.arrived_at)
            if result.title:
                parcel.title = result.title
            if result.cost is not None:
                parcel.amazon_price = result.amazon_price
                parcel.purchase_price = result.cost
        except keepa_service.KeepaError:
            pass

    _log(db, parcel_id, "accepted",
         "Condition: ok" + (f" | {notes_text}" if notes_text else ""),
         user=current_user)
    db.commit()
    return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)


@router.post("/{parcel_id}/process")
def parcel_process(
    request: Request,
    parcel_id: int,
    action: str = Form(...),
    discount: str = Form(""),
    asin_override: str = Form(""),
    qty_override: str = Form(""),
    process_notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    """Handle all 7 acceptance conditions from the admin parcel detail modal."""
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel or parcel.status != "delivered":
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)

    notes_text = process_notes.strip() or None
    actor = current_user.full_name or current_user.username

    # Parse the submitted % of buybox
    try:
        pct = float(discount.strip()) if discount.strip() else None
    except ValueError:
        pct = None

    def _apply_overrides() -> None:
        if asin_override.strip():
            parcel.asin = asin_override.strip().upper()
            parcel.title = None
        if qty_override.strip():
            try:
                parcel.qty = int(qty_override.strip())
            except ValueError:
                pass

    def _ensure_amazon_price() -> None:
        """Fetch amazon_price (and title) from Keepa if not yet set."""
        if parcel.asin and parcel.arrived_at and not parcel.amazon_price:
            try:
                result = keepa_service.get_product_info(parcel.asin, parcel.arrived_at, multiplier=1.0)
                if result.amazon_price is not None:
                    parcel.amazon_price = result.amazon_price
                if result.title and not parcel.title:
                    parcel.title = result.title
            except keepa_service.KeepaError:
                pass

    def _apply_pct() -> None:
        """Set purchase_price = amazon_price × pct/100."""
        _ensure_amazon_price()
        if pct is not None and parcel.amazon_price:
            parcel.purchase_price = round(parcel.amazon_price * pct / 100, 2)
        elif pct is not None:
            # amazon_price unavailable — store pct for reference, price stays as-is
            pass

    if action == "accepted":
        _apply_pct()
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor)
        _log(db, parcel_id, "accepted",
             f"Condition: ok, rate {pct}% of buybox = ${parcel.purchase_price}" + (f" | {notes_text}" if notes_text else ""),
             user=current_user)

    elif action == "overstock":
        _apply_pct()
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor)
        _log(db, parcel_id, "accepted",
             f"Condition: overstock, rate {pct}% of buybox = ${parcel.purchase_price}" + (f" | {notes_text}" if notes_text else ""),
             user=current_user)

    elif action == "damaged":
        _apply_pct()
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor)
        _log(db, parcel_id, "accepted",
             f"Condition: damaged, rate {pct}% of buybox = ${parcel.purchase_price}" + (f" | {notes_text}" if notes_text else ""),
             user=current_user)

    elif action == "very_damaged":
        parcel.purchase_price = 0
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor, notes="very damaged — $0")
        _log(db, parcel_id, "accepted", "Condition: very damaged → $0" + (f" | {notes_text}" if notes_text else ""), user=current_user)

    elif action == "wrong_item_accept":
        _apply_overrides()
        _apply_pct()
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor)
        _log(db, parcel_id, "accepted",
             f"Condition: wrong item (accept), rate {pct}% = ${parcel.purchase_price}" + (f" | {notes_text}" if notes_text else ""),
             user=current_user)

    elif action == "wrong_item_discount":
        _apply_overrides()
        _apply_pct()
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor)
        _log(db, parcel_id, "accepted",
             f"Condition: wrong item w/ rate {pct}% = ${parcel.purchase_price}" + (f" | {notes_text}" if notes_text else ""),
             user=current_user)

    elif action == "wrong_item_return":
        _apply_overrides()
        parcel.purchase_price = 0
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor, notes="wrong item — $0")
        _log(db, parcel_id, "accepted", "Condition: wrong item → $0" + (f" | {notes_text}" if notes_text else ""), user=current_user)

    if notes_text:
        parcel.notes = (parcel.notes + "\n" + notes_text).strip() if parcel.notes else notes_text

    db.commit()
    return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)


def _bg_register_prep(
    parcel_id: int,
    tracking_number: str,
    asin: Optional[str],
    qty: int,
    prime_prep_client_id: str,
    order_number: str,
    title: str,
) -> None:
    """Background task: register inbound + attach SKU; writes result to DB."""
    from app.database import SessionLocal
    from app.services import prime_prep_service

    db = SessionLocal()
    try:
        pp_session = prime_prep_service.login()
        shipment_id, sku_diag = prime_prep_service.register_inbound(
            pp_session,
            tracking_number=tracking_number,
            asin=asin,
            qty=qty,
            prime_prep_client_id=prime_prep_client_id,
            order_number=order_number,
            title=title,
        )
        parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
        if parcel:
            parcel.prime_prep_shipment_id = shipment_id
            parcel.prime_prep_status = f"registered | SKU: {sku_diag}" if sku_diag else "registered"
            db.add(ParcelLog(
                parcel_id=parcel_id,
                user_name="system",
                action="prime_prep_registered",
                detail=f"Shipment: {shipment_id}" + (f" | SKU: {sku_diag}" if sku_diag else ""),
            ))
            db.commit()
    except Exception as exc:
        parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
        if parcel:
            parcel.prime_prep_status = f"error: {str(exc)[:200]}"
            db.add(ParcelLog(
                parcel_id=parcel_id,
                user_name="system",
                action="prime_prep_error",
                detail=str(exc)[:200],
            ))
            db.commit()
    finally:
        db.close()


@router.post("/{parcel_id}/register-prep")
def parcel_register_prep(
    request: Request,
    parcel_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "edit_parcel"):
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel or not parcel.client_id:
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)

    from app.models.client import Client
    client = db.query(Client).filter(Client.id == parcel.client_id).first()
    prime_prep_client_id = (client.prime_prep_client_id if client else None) or ""

    # Fetch title from Keepa now (fast, 1 token) so the background task has it
    title = parcel.title or ""
    if not title and parcel.asin:
        try:
            from app.services import keepa_service
            fetched = keepa_service.get_title_only(parcel.asin)
            if fetched:
                parcel.title = fetched
                title = fetched
                db.commit()
        except Exception:
            pass

    # Mark as pending immediately so the UI shows progress
    parcel.prime_prep_status = "registering…"
    _log(db, parcel_id, "prime_prep_initiated", f"ASIN: {parcel.asin} | qty: {parcel.qty or 1}", user=current_user)
    db.commit()

    background_tasks.add_task(
        _bg_register_prep,
        parcel_id=parcel_id,
        tracking_number=parcel.tracking_number,
        asin=parcel.asin,
        qty=parcel.qty or 1,
        prime_prep_client_id=prime_prep_client_id,
        order_number=parcel.external_order_id or "",
        title=title,
    )
    return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)


def _bg_fetch_prep_status(parcel_id: int, shipment_id: str) -> None:
    from app.database import SessionLocal
    from app.services import prime_prep_service

    db = SessionLocal()
    try:
        pp_session = prime_prep_service.login()
        info = prime_prep_service.get_shipment_status(pp_session, shipment_id)
        parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
        if parcel:
            parcel.prime_prep_status = info.get("status") or parcel.prime_prep_status
            db.commit()
    except Exception:
        pass
    finally:
        db.close()


@router.post("/{parcel_id}/fetch-prep-status")
def parcel_fetch_prep_status(
    request: Request,
    parcel_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "edit_parcel"):
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel or not parcel.prime_prep_shipment_id:
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)

    background_tasks.add_task(_bg_fetch_prep_status, parcel_id, parcel.prime_prep_shipment_id)
    return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)


@router.post("/{parcel_id}/forward")
def parcel_create_forward(
    request: Request,
    parcel_id: int,
    new_tracking: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    """Create a new in_transit parcel for the correct warehouse, linked to this forwarding parcel."""
    if not can(current_user, "edit_parcel"):
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)
    parent = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parent or parent.status != "forwarding":
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)

    child = Parcel(
        tracking_number=new_tracking.strip(),
        external_order_id=parent.external_order_id,
        supplier_id=parent.supplier_id,
        client_id=parent.client_id,
        qty=parent.qty,
        asin=parent.asin,
        title=parent.title,
        status="in_transit",
        forwarded_from_id=parent.id,
        notes=f"Forwarded from {parent.tracking_number}",
    )
    db.add(child)
    db.flush()
    _log(db, parent.id, "forward_created", f"New tracking: {new_tracking.strip()}", user=current_user)
    _log(db, child.id, "created", f"Forwarded from {parent.tracking_number}", user=current_user)
    db.commit()
    return RedirectResponse(f"/parcels/{child.id}", status_code=302)


@router.post("/{parcel_id}/mark-returned")
def parcel_mark_returned(
    request: Request,
    parcel_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    """Mark a return_to_supplier parcel as physically sent back."""
    if not can(current_user, "edit_parcel"):
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if parcel and parcel.status == "return_to_supplier" and not parcel.is_returned:
        parcel.is_returned = True
        parcel.returned_at = datetime.utcnow()
        _log(db, parcel_id, "returned_to_supplier", None, user=current_user)
        db.commit()
    return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)


@router.post("/{parcel_id}/admin-respond")
def parcel_admin_respond(
    request: Request,
    parcel_id: int,
    action: str = Form(...),   # approved | counter_offer | approve_return
    discount: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    """Admin responds to a client negotiation round."""
    if not can(current_user, "edit_parcel"):
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel or parcel.status != "negotiating":
        return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)

    discount_val: float | None = None
    if discount:
        try:
            discount_val = float(discount)
        except ValueError:
            pass

    round_num = len(parcel.negotiations) + 1
    actor_name = current_user.full_name or current_user.username

    if action == "approved":
        # discount_val = % of amazon_price (buybox at delivery), not % off
        if discount_val is not None and parcel.amazon_price:
            parcel.purchase_price = round(parcel.amazon_price * discount_val / 100, 2)
        elif discount_val is None and parcel.amazon_price and not parcel.purchase_price:
            # No discount specified but no price yet — keep existing or leave as-is
            pass
        db.add(ParcelNegotiation(
            parcel_id=parcel_id, round_number=round_num, actor="admin",
            action="approved", discount_proposed=discount_val,
            notes=notes.strip() or None,
        ))
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor_name,
                          notes="Admin approved negotiation")
        _log(db, parcel_id, "admin_approved_negotiation",
             f"Approved" + (f" at {discount_val}% of buybox = ${parcel.purchase_price}" if discount_val else ""),
             user=current_user)

    elif action == "approve_return":
        # Return: goes to ready_to_pay at $0 so it appears in the payment report
        parcel.purchase_price = 0
        parcel.amazon_price = parcel.amazon_price  # keep for reference
        db.add(ParcelNegotiation(
            parcel_id=parcel_id, round_number=round_num, actor="admin",
            action="approve_return", discount_proposed=None,
            notes=notes.strip() or None,
        ))
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor_name,
                          notes="Return approved — $0 for report")
        _log(db, parcel_id, "admin_approved_return", "Return at $0 → ready to pay", user=current_user)

    else:  # counter_offer
        # discount_val here is the % of amazon_price the admin offers
        db.add(ParcelNegotiation(
            parcel_id=parcel_id, round_number=round_num, actor="admin",
            action="counter_offer", discount_proposed=discount_val,
            notes=notes.strip() or None,
        ))
        _log(db, parcel_id, "admin_counter_offer",
             f"Round {round_num}" + (f", {discount_val}% of buybox" if discount_val else ""),
             user=current_user)
        db.commit()

    db.commit()
    return RedirectResponse(f"/parcels/{parcel_id}", status_code=302)


# ── Deposit management (admin side) ──────────────────────────────────────────

@router.post("/deposits/{deposit_id}/confirm")
def admin_confirm_deposit(
    request: Request,
    deposit_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    """Admin confirms a pending client deposit, updating client balance."""
    if not can(current_user, "edit_parcel"):
        return RedirectResponse("/parcels", status_code=302)
    deposit = db.query(ClientDeposit).filter(ClientDeposit.id == deposit_id).first()
    if deposit and deposit.status == "pending":
        deposit.status = "confirmed"
        deposit.confirmed_at = datetime.utcnow()
        deposit.confirmed_by = current_user.full_name or current_user.username
        # Update client balance
        client = db.query(Client).filter(Client.id == deposit.client_id).first()
        if client:
            client.balance = (client.balance or 0.0) + deposit.amount
        db.commit()
    return RedirectResponse(f"/clients/{deposit.client_id if deposit else ''}", status_code=302)


@router.post("/deposits/{deposit_id}/reject")
def admin_reject_deposit(
    request: Request,
    deposit_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    """Admin rejects a pending deposit."""
    if not can(current_user, "edit_parcel"):
        return RedirectResponse("/parcels", status_code=302)
    deposit = db.query(ClientDeposit).filter(ClientDeposit.id == deposit_id).first()
    client_id = deposit.client_id if deposit else None
    if deposit and deposit.status == "pending":
        deposit.status = "rejected"
        db.commit()
    return RedirectResponse(f"/clients/{client_id}" if client_id else "/parcels", status_code=302)


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
