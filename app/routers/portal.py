"""Client portal — accessible only by users with role='client'."""
import os
import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.auth import require_role
from app.models.parcel import Parcel, ParcelComment, ParcelLog, ParcelNegotiation
from app.models.report import Report
from app.models.client import Client, ClientDeposit
from app.services.parcel_service import STATUS_LABELS, STATUS_COLORS, transition_parcel

require_client = require_role("client")

router = APIRouter(prefix="/portal")
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")

CLIENT_ACTION_LABELS = {
    "accepted":             "All OK",
    "overstock":            "Overstock",
    "damaged":              "Damaged",
    "very_damaged":         "Very Damaged",
    "wrong_item_accept":    "Wrong Item (Accept)",
    "wrong_item_discount":  "Wrong Item (Accept w/ Discount)",
    "wrong_item_return":    "Wrong Item (Return)",
}


def _get_client(current_user, db: Session) -> Client | None:
    if not current_user.client_id:
        return None
    return db.query(Client).filter(Client.id == current_user.client_id).first()


def _log(db, parcel_id: int, action: str, detail: str = None, user=None) -> None:
    db.add(ParcelLog(
        parcel_id=parcel_id,
        user_id=getattr(user, "id", None),
        user_name=(user.full_name or user.username) if user else "client",
        action=action,
        detail=detail,
    ))


def _confirmed_deposits_total(client: Client) -> float:
    return sum(d.amount for d in client.deposits if d.status == "confirmed")


@router.get("", response_class=HTMLResponse)
def portal_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    client = _get_client(current_user, db)
    if not client:
        return templates.TemplateResponse(request, "portal/no_client.html", {"current_user": current_user})

    base = db.query(Parcel).filter(Parcel.client_id == client.id)

    # Fetch parcels per status to get both counts and cost sums
    in_transit_parcels  = base.filter(Parcel.status == "in_transit").all()
    delivered_parcels   = base.filter(Parcel.status == "delivered").all()
    ready_parcels       = base.filter(Parcel.status == "ready_to_pay").all()
    negotiating_parcels = base.filter(Parcel.status == "negotiating").all()

    in_transit_count    = len(in_transit_parcels)
    delivered_count     = len(delivered_parcels)
    ready_to_pay_count  = len(ready_parcels)
    negotiating_count   = len(negotiating_parcels)

    in_transit_value    = sum(p.purchase_price or 0 for p in in_transit_parcels)
    delivered_value     = sum(p.purchase_price or 0 for p in delivered_parcels)
    ready_to_pay_value  = sum(p.purchase_price or 0 for p in ready_parcels)

    # Parcels needing client attention: delivered or negotiating where last round was from admin
    needs_action = list(delivered_parcels)
    for p in negotiating_parcels:
        if p.negotiations and p.negotiations[-1].actor == "admin":
            needs_action.append(p)

    # Recent reports
    reports = db.query(Report).filter(Report.client_id == client.id).order_by(Report.created_at.desc()).limit(5).all()

    balance = client.balance or 0
    # Projected balance = actual balance minus estimated cost of all active parcels
    balance_projected = balance - in_transit_value - delivered_value - ready_to_pay_value

    return templates.TemplateResponse(
        request,
        "portal/dashboard.html",
        {
            "current_user": current_user,
            "client": client,
            "in_transit_count": in_transit_count,
            "in_transit_value": in_transit_value,
            "delivered_count": delivered_count,
            "delivered_value": delivered_value,
            "ready_to_pay_count": ready_to_pay_count,
            "ready_to_pay_value": ready_to_pay_value,
            "negotiating_count": negotiating_count,
            "needs_action": needs_action,
            "reports": reports,
            "balance": balance,
            "balance_projected": balance_projected,
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_COLORS": STATUS_COLORS,
            "CLIENT_ACTION_LABELS": CLIENT_ACTION_LABELS,
        },
    )


@router.get("/parcels", response_class=HTMLResponse)
def portal_parcels(
    request: Request,
    status: str = "",
    q: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    client = _get_client(current_user, db)
    if not client:
        return RedirectResponse("/portal", status_code=302)

    base = db.query(Parcel).filter(Parcel.client_id == client.id)

    if status == "archive":
        query = base.filter(Parcel.payment_report_date.isnot(None))
        if q:
            q_upper = q.strip().upper()
            query = query.filter(
                Parcel.tracking_number.contains(q.strip()) |
                (Parcel.asin == q_upper)
            )
        parcels = query.order_by(Parcel.payment_report_date.desc(), Parcel.created_at.desc()).all()
    else:
        query = base.filter(
            Parcel.payment_report_date.is_(None),
            Parcel.status != "forwarding",
        )
        if status:
            query = query.filter(Parcel.status == status)
        if q:
            q_upper = q.strip().upper()
            query = query.filter(
                Parcel.tracking_number.contains(q.strip()) |
                (Parcel.asin == q_upper)
            )
        parcels = query.order_by(Parcel.created_at.desc()).all()

    counts = {}
    active = base.filter(Parcel.payment_report_date.is_(None), Parcel.status != "forwarding")
    for s in ["in_transit", "delivered", "negotiating", "ready_to_pay", "paid"]:
        counts[s] = active.filter(Parcel.status == s).count()
    counts["archive"] = base.filter(Parcel.payment_report_date.isnot(None)).count()

    return templates.TemplateResponse(
        request,
        "portal/parcels.html",
        {
            "current_user": current_user,
            "client": client,
            "parcels": parcels,
            "active_status": status,
            "counts": counts,
            "q": q,
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_COLORS": STATUS_COLORS,
        },
    )


@router.get("/parcels/{parcel_id}", response_class=HTMLResponse)
def portal_parcel_detail(
    request: Request,
    parcel_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    client = _get_client(current_user, db)
    if not client:
        return RedirectResponse("/portal", status_code=302)
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id, Parcel.client_id == client.id).first()
    if not parcel:
        return RedirectResponse("/portal/parcels", status_code=302)

    # Determine if client can add another negotiation round
    can_negotiate = False
    if parcel.status == "negotiating" and parcel.negotiations:
        can_negotiate = parcel.negotiations[-1].actor == "admin"

    return templates.TemplateResponse(
        request,
        "portal/parcel_detail.html",
        {
            "current_user": current_user,
            "client": client,
            "parcel": parcel,
            "can_negotiate": can_negotiate,
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_COLORS": STATUS_COLORS,
            "CLIENT_ACTION_LABELS": CLIENT_ACTION_LABELS,
        },
    )


@router.post("/parcels/{parcel_id}/process")
async def portal_process_parcel(
    request: Request,
    parcel_id: int,
    action: str = Form(...),
    discount: str = Form(""),
    notes: str = Form(""),
    photos: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    """Client processes a delivered parcel — first review round."""
    client = _get_client(current_user, db)
    if not client:
        return RedirectResponse("/portal", status_code=302)
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id, Parcel.client_id == client.id).first()
    if not parcel or parcel.status != "delivered":
        return RedirectResponse(f"/portal/parcels/{parcel_id}", status_code=302)

    discount_val: float | None = None
    if discount:
        try:
            discount_val = float(discount)
        except ValueError:
            pass

    # Save photos if provided
    for photo in photos:
        if photo and photo.filename:
            ext = os.path.splitext(photo.filename)[1].lower()
            filename = f"{uuid.uuid4().hex}{ext}"
            dest = os.path.join(UPLOAD_DIR, "parcels", filename)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(await photo.read())
            from app.models.parcel import ParcelPhoto
            db.add(ParcelPhoto(parcel_id=parcel_id, file_path=f"/uploads/parcels/{filename}", caption="Client photo"))

    # Store client review on parcel
    parcel.client_action = action
    parcel.client_discount_proposed = discount_val
    parcel.client_notes = notes.strip() or None
    parcel.client_reviewed_at = datetime.utcnow()

    actor_name = current_user.full_name or current_user.username
    round_num = 1

    if action == "accepted":
        # Direct acceptance — move to ready_to_pay, calculate actual cost from Keepa
        if parcel.asin and parcel.arrived_at:
            try:
                from app.services import keepa_service
                from app.models.client import Client as _Client
                _cli = db.query(_Client).filter(_Client.id == parcel.client_id).first()
                _coeff = (_cli.cost_coefficient if _cli and _cli.cost_coefficient else 0.45)
                result = keepa_service.get_product_info(parcel.asin, parcel.arrived_at, multiplier=_coeff)
                if result.amazon_price is not None:
                    parcel.amazon_price = result.amazon_price
                if result.cost is not None:
                    parcel.purchase_price = float(result.cost)
                if result.title and not parcel.title:
                    parcel.title = result.title
            except Exception:
                pass
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor_name, notes="Client accepted")
        _log(db, parcel_id, "client_accepted", "Client confirmed parcel is OK", user=current_user)
    else:
        # All other actions go to negotiating for admin review
        db.add(ParcelNegotiation(
            parcel_id=parcel_id,
            round_number=round_num,
            actor="client",
            action=action,
            discount_proposed=discount_val,
            notes=notes.strip() or None,
        ))
        transition_parcel(parcel, "negotiating", db, changed_by=actor_name,
                          notes=f"Client: {action}")
        _log(db, parcel_id, "client_counter_offer",
             f"Action: {action}" + (f", %: {discount_val}" if discount_val else ""),
             user=current_user)

    db.commit()
    return RedirectResponse(f"/portal/parcels/{parcel_id}", status_code=302)


@router.post("/parcels/{parcel_id}/negotiate")
def portal_negotiate(
    request: Request,
    parcel_id: int,
    action: str = Form(...),
    discount: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    """Client responds to admin's counter-offer (subsequent rounds)."""
    client = _get_client(current_user, db)
    if not client:
        return RedirectResponse("/portal", status_code=302)
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id, Parcel.client_id == client.id).first()
    if not parcel or parcel.status != "negotiating":
        return RedirectResponse(f"/portal/parcels/{parcel_id}", status_code=302)
    # Only allow if last round was from admin
    if not parcel.negotiations or parcel.negotiations[-1].actor != "admin":
        return RedirectResponse(f"/portal/parcels/{parcel_id}", status_code=302)

    discount_val: float | None = None
    if discount:
        try:
            discount_val = float(discount)
        except ValueError:
            pass

    round_num = len(parcel.negotiations) + 1
    actor_name = current_user.full_name or current_user.username

    if action == "accepted":
        db.add(ParcelNegotiation(
            parcel_id=parcel_id, round_number=round_num, actor="client",
            action="accepted", discount_proposed=None, notes="Client accepted admin offer",
        ))
        transition_parcel(parcel, "ready_to_pay", db, changed_by=actor_name,
                          notes="Client accepted admin counter-offer")
        _log(db, parcel_id, "client_accepted", "Client accepted admin counter-offer", user=current_user)
    else:
        # Another counter
        db.add(ParcelNegotiation(
            parcel_id=parcel_id, round_number=round_num, actor="client",
            action=action, discount_proposed=discount_val, notes=notes.strip() or None,
        ))
        parcel.client_discount_proposed = discount_val
        parcel.client_notes = notes.strip() or parcel.client_notes
        _log(db, parcel_id, "client_counter_offer",
             f"Round {round_num}: {action}" + (f", discount: {discount_val}%" if discount_val else ""),
             user=current_user)
        db.commit()

    db.commit()
    return RedirectResponse(f"/portal/parcels/{parcel_id}", status_code=302)


# ── Deposits ─────────────────────────────────────────────────────────────────

@router.get("/deposits", response_class=HTMLResponse)
def portal_deposits(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    client = _get_client(current_user, db)
    if not client:
        return RedirectResponse("/portal", status_code=302)
    deposits = db.query(ClientDeposit).filter(ClientDeposit.client_id == client.id)\
                 .order_by(ClientDeposit.created_at.desc()).all()
    confirmed_total = _confirmed_deposits_total(client)
    return templates.TemplateResponse(
        request,
        "portal/deposits.html",
        {
            "current_user": current_user,
            "client": client,
            "deposits": deposits,
            "confirmed_total": confirmed_total,
        },
    )


@router.post("/deposits")
async def portal_add_deposit(
    request: Request,
    amount: str = Form(...),
    transaction_ref: str = Form(""),
    notes: str = Form(""),
    screenshot: UploadFile = File(default=None),
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    client = _get_client(current_user, db)
    if not client:
        return RedirectResponse("/portal", status_code=302)

    try:
        amount_val = float(amount)
    except ValueError:
        return RedirectResponse("/portal/deposits", status_code=302)

    screenshot_path: str | None = None
    if screenshot and screenshot.filename:
        ext = os.path.splitext(screenshot.filename)[1].lower()
        filename = f"dep_{uuid.uuid4().hex}{ext}"
        dest = os.path.join(UPLOAD_DIR, "deposits", filename)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(await screenshot.read())
        screenshot_path = f"/uploads/deposits/{filename}"

    deposit = ClientDeposit(
        client_id=client.id,
        amount=amount_val,
        transaction_ref=transaction_ref.strip() or None,
        notes=notes.strip() or None,
        screenshot_path=screenshot_path,
        status="pending",
    )
    db.add(deposit)
    db.commit()
    return RedirectResponse("/portal/deposits", status_code=302)


# ── Reports ──────────────────────────────────────────────────────────────────

@router.get("/reports", response_class=HTMLResponse)
def portal_reports(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    client = _get_client(current_user, db)
    if not client:
        return RedirectResponse("/portal", status_code=302)
    reports = db.query(Report).filter(Report.client_id == client.id).order_by(Report.created_at.desc()).all()
    return templates.TemplateResponse(
        request,
        "portal/reports.html",
        {"current_user": current_user, "client": client, "reports": reports},
    )


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def portal_report_detail(
    request: Request,
    report_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    client = _get_client(current_user, db)
    if not client:
        return RedirectResponse("/portal", status_code=302)
    report = db.query(Report).filter(Report.id == report_id, Report.client_id == client.id).first()
    if not report:
        return RedirectResponse("/portal/reports", status_code=302)
    return templates.TemplateResponse(
        request,
        "portal/report_detail.html",
        {"current_user": current_user, "client": client, "report": report},
    )
