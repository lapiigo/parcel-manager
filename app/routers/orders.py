from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.auth import require_manager_up
from app.models.order import Order
from app.models.parcel import Parcel
from app.models.client import Client
from app.services.order_service import create_order, compute_totals
from app.permissions import can

router = APIRouter(prefix="/orders")
templates = Jinja2Templates(directory="app/templates")

PLATFORM_LABELS = {
    "amazon": "Amazon",
    "walmart": "Walmart",
    "ebay": "eBay",
    "direct": "Direct",
}

ORDER_STATUS_LABELS = {
    "pending": "Pending",
    "shipped": "Shipped",
    "delivered": "Delivered",
    "returned": "Returned",
}

ORDER_STATUS_COLORS = {
    "pending": "bg-yellow-100 text-yellow-800",
    "shipped": "bg-blue-100 text-blue-800",
    "delivered": "bg-green-100 text-green-800",
    "returned": "bg-red-100 text-red-800",
}


def _order_company_query(db, current_user):
    q = db.query(Order)
    if current_user.role != "super_admin" and current_user.client_id:
        q = q.filter(Order.client_id == current_user.client_id)
    return q


@router.get("", response_class=HTMLResponse)
def order_list(
    request: Request,
    platform: str = Query(""),
    status: str = Query(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "view_orders"):
        return RedirectResponse("/dashboard", status_code=302)
    query = _order_company_query(db, current_user)
    if platform:
        query = query.filter(Order.platform == platform)
    if status:
        query = query.filter(Order.status == status)
    orders = query.order_by(Order.created_at.desc()).all()
    totals = compute_totals(orders) if can(current_user, "view_financials") else None

    return templates.TemplateResponse(
        request,
        "orders/list.html",
        context={
            "current_user": current_user,
            "orders": orders,
            "totals": totals,
            "active_platform": platform,
            "active_status": status,
            "PLATFORM_LABELS": PLATFORM_LABELS,
            "ORDER_STATUS_LABELS": ORDER_STATUS_LABELS,
            "ORDER_STATUS_COLORS": ORDER_STATUS_COLORS,
            "can": can
        },
    )


def _warehouse_parcels(db, current_user):
    q = db.query(Parcel).filter(Parcel.status == "in_warehouse")
    if current_user.role != "super_admin" and current_user.client_id:
        q = q.filter(Parcel.client_id == current_user.client_id)
    return q.all()


def _clients_for_user(db, current_user):
    if current_user.role == "super_admin":
        return db.query(Client).order_by(Client.name).all()
    if current_user.client_id:
        c = db.query(Client).filter(Client.id == current_user.client_id).first()
        return [c] if c else []
    return []


@router.get("/new", response_class=HTMLResponse)
def order_new(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    warehouse_parcels = _warehouse_parcels(db, current_user)
    clients = _clients_for_user(db, current_user)
    return templates.TemplateResponse(
        request,
        "orders/form.html",
        context={
            "current_user": current_user,
            "order": None,
            "warehouse_parcels": warehouse_parcels,
            "clients": clients,
            "PLATFORM_LABELS": PLATFORM_LABELS,
            "ORDER_STATUS_LABELS": ORDER_STATUS_LABELS,
            "error": "",
            "can": can
        },
    )


@router.post("/new")
def order_create(
    request: Request,
    order_number: str = Form(...),
    platform: str = Form(...),
    parcel_id: str = Form(""),
    client_id: str = Form(""),
    sale_price: str = Form("0"),
    platform_commission: str = Form("0"),
    shipping_cost: str = Form("0"),
    other_costs: str = Form("0"),
    status: str = Form("pending"),
    order_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    # Non-super_admin always uses their own company
    if current_user.role != "super_admin" and current_user.client_id:
        resolved_client_id = current_user.client_id
    else:
        resolved_client_id = int(client_id) if client_id else None

    data = {
        "order_number": order_number.strip(),
        "platform": platform,
        "parcel_id": int(parcel_id) if parcel_id else None,
        "client_id": resolved_client_id,
        "sale_price": sale_price,
        "platform_commission": platform_commission,
        "shipping_cost": shipping_cost,
        "other_costs": other_costs,
        "status": status,
        "order_date": datetime.strptime(order_date, "%Y-%m-%d") if order_date else None,
        "notes": notes.strip(),
    }
    order, msg = create_order(db, data, actor=current_user.full_name or current_user.username)
    if not order:
        warehouse_parcels = _warehouse_parcels(db, current_user)
        clients = _clients_for_user(db, current_user)
        return templates.TemplateResponse(
            request,
            "orders/form.html",
            context={
                "current_user": current_user,
                "order": None,
                "warehouse_parcels": warehouse_parcels,
                "clients": clients,
                "PLATFORM_LABELS": PLATFORM_LABELS,
                "ORDER_STATUS_LABELS": ORDER_STATUS_LABELS,
                "error": msg,
                "can": can
            },
        )
    return RedirectResponse(f"/orders/{order.id}", status_code=302)


@router.get("/{order_id}", response_class=HTMLResponse)
def order_detail(
    request: Request,
    order_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse("/orders", status_code=302)
    if current_user.role != "super_admin" and current_user.client_id and order.client_id != current_user.client_id:
        return RedirectResponse("/orders", status_code=302)
    warehouse_parcels = _warehouse_parcels(db, current_user)
    clients = _clients_for_user(db, current_user)
    return templates.TemplateResponse(
        request,
        "orders/form.html",
        context={
            "current_user": current_user,
            "order": order,
            "warehouse_parcels": warehouse_parcels,
            "clients": clients,
            "PLATFORM_LABELS": PLATFORM_LABELS,
            "ORDER_STATUS_LABELS": ORDER_STATUS_LABELS,
            "error": "",
            "can": can
        },
    )


@router.post("/{order_id}/edit")
def order_edit(
    request: Request,
    order_id: int,
    order_number: str = Form(...),
    platform: str = Form(...),
    sale_price: str = Form("0"),
    platform_commission: str = Form("0"),
    shipping_cost: str = Form("0"),
    other_costs: str = Form("0"),
    status: str = Form("pending"),
    order_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        order.order_number = order_number.strip()
        order.platform = platform
        order.sale_price = float(sale_price)
        order.platform_commission = float(platform_commission)
        order.shipping_cost = float(shipping_cost)
        order.other_costs = float(other_costs)
        order.status = status
        order.order_date = datetime.strptime(order_date, "%Y-%m-%d") if order_date else order.order_date
        order.notes = notes.strip() or None
        order.updated_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(f"/orders/{order_id}", status_code=302)


@router.post("/{order_id}/delete")
def order_delete(
    request: Request,
    order_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "delete_order"):
        return RedirectResponse("/orders", status_code=302)
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        db.delete(order)
        db.commit()
    return RedirectResponse("/orders", status_code=302)
