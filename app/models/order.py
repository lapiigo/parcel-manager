from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    order_number = Column(String(255), unique=True, nullable=False, index=True)
    platform = Column(String(50), nullable=False)  # amazon | walmart | ebay | direct
    parcel_id = Column(Integer, ForeignKey("parcels.id"), nullable=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)
    sale_price = Column(Float, nullable=False, default=0.0)
    platform_commission = Column(Float, default=0.0)
    shipping_cost = Column(Float, default=0.0)
    other_costs = Column(Float, default=0.0)
    # profit = sale_price - platform_commission - shipping_cost - other_costs (computed as property)
    status = Column(String(50), default="pending")  # pending | shipped | delivered | returned
    order_date = Column(DateTime, nullable=True)
    shipped_date = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    parcel = relationship("Parcel", back_populates="orders")
    client = relationship("Client", back_populates="orders")

    @property
    def profit(self) -> float:
        return round(
            (self.sale_price or 0)
            - (self.platform_commission or 0)
            - (self.shipping_cost or 0)
            - (self.other_costs or 0),
            2,
        )
