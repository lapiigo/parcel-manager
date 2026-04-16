"""
Run this script once to create the initial super_admin account.
Usage:
    python create_admin.py
"""
import os
import sys

os.makedirs("data", exist_ok=True)
os.makedirs("uploads/parcels", exist_ok=True)

from dotenv import load_dotenv
load_dotenv()

from app.database import engine, SessionLocal, Base
import app.models  # register all models

# Create tables
Base.metadata.create_all(bind=engine)

db = SessionLocal()

from app.models.user import User
from app.services.auth_service import create_user, hash_password

existing = db.query(User).filter(User.role == "super_admin").first()
if existing:
    print(f"Super admin already exists: '{existing.username}'")
    db.close()
    sys.exit(0)

print("=== Parcel Manager — Створення адміністратора ===")
username = input("Логін (super_admin): ").strip() or "admin"
full_name = input("Повне ім'я: ").strip()
password = input("Пароль: ").strip()

if not password:
    print("Пароль не може бути порожнім!")
    db.close()
    sys.exit(1)

user = create_user(
    db,
    username=username,
    password=password,
    role="super_admin",
    full_name=full_name,
    email="",
)

print(f"\nАдміністратор '{user.username}' створений успішно!")
print(f"   Роль: super_admin")
print(f"\nЗапустіть сервер: run.bat")
print(f"   Відкрийте: http://localhost:8000/login")
db.close()
