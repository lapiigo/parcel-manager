import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request, Depends
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeSerializer, BadSignature
from sqlalchemy.orm import Session

from app.database import get_db

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
SESSION_EXPIRE_DAYS = int(os.getenv("SESSION_EXPIRE_DAYS", "30"))
_signer = URLSafeSerializer(SECRET_KEY, salt="session")

COOKIE_NAME = "pm_session"


class NotAuthenticatedException(Exception):
    pass


class ForbiddenException(Exception):
    def __init__(self, message: str = "Доступ заборонено"):
        self.message = message


def sign_session(session_id: str) -> str:
    return _signer.dumps(session_id)


def unsign_session(token: str) -> Optional[str]:
    try:
        return _signer.loads(token)
    except BadSignature:
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)):
    from app.models.user import Session as DbSession, User

    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    session_id = unsign_session(token)
    if not session_id:
        return None
    db_session = (
        db.query(DbSession)
        .filter(
            DbSession.id == session_id,
            DbSession.expires_at > datetime.utcnow(),
        )
        .first()
    )
    if not db_session:
        return None
    user = db.query(User).filter(User.id == db_session.user_id, User.is_active == True).first()
    return user


def require_auth(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise NotAuthenticatedException()
    return user


def require_role(*roles: str):
    def dependency(request: Request, db: Session = Depends(get_db)):
        user = get_current_user(request, db)
        if not user:
            raise NotAuthenticatedException()
        if user.role not in roles:
            raise ForbiddenException()
        return user
    return dependency


def create_session(user_id: int, db: Session, request: Request) -> str:
    from app.models.user import Session as DbSession

    session_id = str(uuid.uuid4())
    expires = datetime.utcnow() + timedelta(days=SESSION_EXPIRE_DAYS)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent", "")[:255]
    db_session = DbSession(
        id=session_id,
        user_id=user_id,
        expires_at=expires,
        ip_address=ip,
        user_agent=ua,
    )
    db.add(db_session)
    db.commit()
    return sign_session(session_id)


def delete_session(token: str, db: Session):
    from app.models.user import Session as DbSession

    session_id = unsign_session(token)
    if session_id:
        db.query(DbSession).filter(DbSession.id == session_id).delete()
        db.commit()


# Role shortcuts
require_super_admin = require_role("super_admin")
# Any non-client logged-in user (permission checks happen inside routes via can())
require_manager_up = require_role("super_admin", "admin", "manager", "staff")

# Admin-level: super_admin, legacy admin, or staff with view_users permission
def require_admin_up(request: Request, db: Session = Depends(get_db)):
    from app.permissions import can
    user = get_current_user(request, db)
    if not user:
        raise NotAuthenticatedException()
    if user.role in ("super_admin", "admin"):
        return user
    if user.role in ("staff", "manager") and can(user, "view_users"):
        return user
    raise ForbiddenException()

require_any = require_role("super_admin", "admin", "manager", "staff", "client")
