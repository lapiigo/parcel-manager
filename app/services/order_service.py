from datetime import datetime
from sqlalchemy.orm import Session

from app.models.order import Order
from app.models.parcel import Parcel, ParcelStatusLog


def create_order(db: Session, data: dict, actor: str = "") -> tuple[Order | None, str]:
    parcel_id = data.get("parcel_id")
    if parcel_id:
        parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
        if not parcel:
            return None, "Parcel not found"
        if parcel.status != "in_warehouse":
            return None, "Parcel must be in warehouse to be linked to an order"

    order = Order(
        order_number=data["order_number"],
        platform=data["platform"],
        parcel_id=parcel_id or None,
        client_id=data.get("client_id") or None,
        sale_price=float(data.get("sale_price", 0)),
        platform_commission=float(data.get("platform_commission", 0)),
        shipping_cost=float(data.get("shipping_cost", 0)),
        other_costs=float(data.get("other_costs", 0)),
        status=data.get("status", "pending"),
        order_date=data.get("order_date") or datetime.utcnow(),
        notes=data.get("notes", ""),
    )
    db.add(order)

    # Mark parcel as sold
    if parcel_id and parcel:
        log = ParcelStatusLog(
            parcel_id=parcel_id,
            old_status=parcel.status,
            new_status="sold",
            changed_by=actor,
            notes=f"Linked to order {data['order_number']}",
        )
        db.add(log)
        parcel.status = "sold"
        parcel.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(order)
    return order, "OK"


def compute_totals(orders: list[Order]) -> dict:
    total_sales = sum(o.sale_price or 0 for o in orders)
    total_commission = sum(o.platform_commission or 0 for o in orders)
    total_shipping = sum(o.shipping_cost or 0 for o in orders)
    total_other = sum(o.other_costs or 0 for o in orders)
    total_profit = sum(o.profit for o in orders)
    return {
        "total_sales": round(total_sales, 2),
        "total_commission": round(total_commission, 2),
        "total_shipping": round(total_shipping, 2),
        "total_other": round(total_other, 2),
        "total_profit": round(total_profit, 2),
        "count": len(orders),
    }
