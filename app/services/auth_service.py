import bcrypt
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional

from app.models.user import User


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login = datetime.utcnow()
    db.commit()
    return user


def create_user(
    db: Session,
    username: str,
    password: str,
    role: str,
    full_name: str = "",
    email: str = "",
    client_id: int = None,
    permissions: str = None,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        full_name=full_name,
        email=email or None,
        client_id=client_id,
        permissions=permissions,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
