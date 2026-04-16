from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    contact_name = Column(String(255), nullable=True)
    email = Column(String(255), nullable=True)
    phone = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)
    platform = Column(String(50), nullable=True)  # alibaba, amazon, other
    website = Column(String(500), nullable=True)
    login_username = Column(String(255), nullable=True)
    login_password_encrypted = Column(Text, nullable=True)  # Fernet encrypted
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    parcels = relationship("Parcel", back_populates="supplier")
