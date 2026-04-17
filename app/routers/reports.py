from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_manager_up
from app.models.report import Report
from app.models.client import Client
from app.models.order import Order
from app.services.order_service import compute_totals
from app.permissions import can

router = APIRouter(prefix="/reports")
templates = Jinja2Templates(directory="app/templates")


def _reports_query(db, current_user):
    q = db.query(Report)
    if current_user.role != "super_admin" and current_user.client_id:
        q = q.filter(Report.client_id == current_user.client_id)
    return q


def _clients_for_user(db, current_user):
    if current_user.role == "super_admin":
        return db.query(Client).order_by(Client.name).all()
    if current_user.client_id:
        c = db.query(Client).filter(Client.id == current_user.client_id).first()
        return [c] if c else []
    return []


@router.get("", response_class=HTMLResponse)
def report_list(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "view_reports"):
        return RedirectResponse("/dashboard", status_code=302)
    reports = _reports_query(db, current_user).order_by(Report.created_at.desc()).all()
    clients = _clients_for_user(db, current_user)
    return templates.TemplateResponse(
        request,
        "reports/list.html",
        context={
"current_user": current_user, "reports": reports, "clients": clients, "can": can
        },
    )


@router.get("/new", response_class=HTMLResponse)
def report_new(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "view_reports"):
        return RedirectResponse("/dashboard", status_code=302)
    clients = _clients_for_user(db, current_user)
    return templates.TemplateResponse(
        request,
        "reports/form.html",
        context={
"current_user": current_user, "report": None, "clients": clients, "error": "", "can": can
        },
    )


@router.post("/new")
def report_create(
    request: Request,
    title: str = Form(...),
    client_id: str = Form(""),
    period_start: str = Form(""),
    period_end: str = Form(""),
    content: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    start = datetime.strptime(period_start, "%Y-%m-%d") if period_start else None
    end = datetime.strptime(period_end, "%Y-%m-%d") if period_end else None

    # Non-super_admin always uses their own company
    if current_user.role != "super_admin" and current_user.client_id:
        cid = current_user.client_id
    else:
        cid = int(client_id) if client_id else None
    q = db.query(Order).filter(Order.client_id == cid) if cid else db.query(Order)
    if start:
        q = q.filter(Order.order_date >= start)
    if end:
        q = q.filter(Order.order_date <= end)
    orders = q.all()
    totals = compute_totals(orders)

    report = Report(
        title=title.strip(),
        client_id=cid,
        period_start=start,
        period_end=end,
        content=content.strip() or None,
        total_parcels=totals["count"],
        total_sales=totals["total_sales"],
        total_profit=totals["total_profit"],
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return RedirectResponse(f"/reports/{report.id}", status_code=302)


@router.get("/{report_id}", response_class=HTMLResponse)
def report_detail(
    request: Request,
    report_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    report = db.query(Report).filter(Report.id == report_id).first()
    if not report:
        return RedirectResponse("/reports", status_code=302)
    if current_user.role != "super_admin" and current_user.client_id and report.client_id != current_user.client_id:
        return RedirectResponse("/reports", status_code=302)
    clients = _clients_for_user(db, current_user)
    return templates.TemplateResponse(
        request,
        "reports/form.html",
        context={
"current_user": current_user, "report": report, "clients": clients, "error": "", "can": can
        },
    )


@router.post("/{report_id}/edit")
def report_edit(
    request: Request,
    report_id: int,
    title: str = Form(...),
    content: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    report = db.query(Report).filter(Report.id == report_id).first()
    if report:
        report.title = title.strip()
        report.content = content.strip() or None
        report.updated_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(f"/reports/{report_id}", status_code=302)


@router.post("/{report_id}/delete")
def report_delete(
    request: Request,
    report_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "delete_report"):
        return RedirectResponse("/reports", status_code=302)
    report = db.query(Report).filter(Report.id == report_id).first()
    if report:
        db.delete(report)
        db.commit()
    return RedirectResponse("/reports", status_code=302)
