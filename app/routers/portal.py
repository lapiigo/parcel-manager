"""Client portal — accessible only by users with role='client'."""
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_role
from app.models.parcel import Parcel, ParcelComment
from app.models.report import Report
from app.models.client import Client
from app.services.parcel_service import STATUS_LABELS, STATUS_COLORS

require_client = require_role("client")

router = APIRouter(prefix="/portal")
templates = Jinja2Templates(directory="app/templates")


def _get_client(current_user, db: Session) -> Client | None:
    if not current_user.client_id:
        return None
    return db.query(Client).filter(Client.id == current_user.client_id).first()


@router.get("", response_class=HTMLResponse)
def portal_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    client = _get_client(current_user, db)
    if not client:
        return templates.TemplateResponse(
            request,
            "portal/no_client.html",
            context={
"current_user": current_user
            },
        )
    parcels = db.query(Parcel).filter(Parcel.client_id == client.id).order_by(Parcel.created_at.desc()).limit(10).all()
    reports = db.query(Report).filter(Report.client_id == client.id).order_by(Report.created_at.desc()).limit(5).all()
    return templates.TemplateResponse(
        request,
        "portal/dashboard.html",
        context={
            "current_user": current_user,
            "client": client,
            "parcels": parcels,
            "reports": reports,
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_COLORS": STATUS_COLORS
        },
    )


@router.get("/parcels", response_class=HTMLResponse)
def portal_parcels(
    request: Request,
    status: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    client = _get_client(current_user, db)
    if not client:
        return RedirectResponse("/portal", status_code=302)
    query = db.query(Parcel).filter(Parcel.client_id == client.id)
    if status:
        query = query.filter(Parcel.status == status)
    parcels = query.order_by(Parcel.created_at.desc()).all()
    counts = {}
    for s in ["in_transit", "in_warehouse", "sold", "disposed"]:
        counts[s] = db.query(Parcel).filter(Parcel.client_id == client.id, Parcel.status == s).count()
    return templates.TemplateResponse(
        request,
        "portal/parcels.html",
        context={
            "current_user": current_user,
            "client": client,
            "parcels": parcels,
            "active_status": status,
            "counts": counts,
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_COLORS": STATUS_COLORS
        },
    )


@router.post("/parcels/{parcel_id}/comment")
def portal_add_comment(
    request: Request,
    parcel_id: int,
    body: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_client),
):
    client = _get_client(current_user, db)
    if not client:
        return RedirectResponse("/portal", status_code=302)
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id, Parcel.client_id == client.id).first()
    if parcel:
        comment = ParcelComment(
            parcel_id=parcel_id,
            body=body.strip(),
            author=f"[Client] {current_user.full_name or current_user.username}",
        )
        db.add(comment)
        db.commit()
    return RedirectResponse(f"/portal/parcels/{parcel_id}", status_code=302)


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
    return templates.TemplateResponse(
        request,
        "portal/parcel_detail.html",
        context={
            "current_user": current_user,
            "client": client,
            "parcel": parcel,
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_COLORS": STATUS_COLORS
        },
    )


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
        context={
            "current_user": current_user,
            "client": client,
            "reports": reports
        },
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
        context={
            "current_user": current_user,
            "client": client,
            "report": report
        },
    )
