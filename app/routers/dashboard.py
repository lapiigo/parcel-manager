from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.auth import require_manager_up
from app.models.parcel import Parcel
from app.models.order import Order
from app.models.client import Client
from app.models.supplier import Supplier
from app.permissions import can

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    # Parcel counts by status
    parcel_counts = dict(
        db.query(Parcel.status, func.count(Parcel.id))
        .group_by(Parcel.status)
        .all()
    )

    # Recent parcels
    recent_parcels = (
        db.query(Parcel)
        .order_by(Parcel.created_at.desc())
        .limit(8)
        .all()
    )

    # Recent orders
    recent_orders = (
        db.query(Order)
        .order_by(Order.created_at.desc())
        .limit(8)
        .all()
    )

    # Financial summary (only for admin+)
    financials = None
    if can(current_user, "view_statistics"):
        orders_all = db.query(Order).all()
        total_sales = sum(o.sale_price or 0 for o in orders_all)
        total_profit = sum(o.profit for o in orders_all)
        orders_by_platform = {}
        for o in orders_all:
            orders_by_platform.setdefault(o.platform, {"count": 0, "profit": 0})
            orders_by_platform[o.platform]["count"] += 1
            orders_by_platform[o.platform]["profit"] += o.profit
        financials = {
            "total_sales": round(total_sales, 2),
            "total_profit": round(total_profit, 2),
            "orders_count": len(orders_all),
            "by_platform": orders_by_platform,
        }

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context={
            "current_user": current_user,
            "parcel_counts": parcel_counts,
            "recent_parcels": recent_parcels,
            "recent_orders": recent_orders,
            "financials": financials,
            "total_clients": db.query(Client).count(),
            "total_suppliers": db.query(Supplier).count(),
            "can": can
        },
    )
