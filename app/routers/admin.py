from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_admin_up
from app.models.user import User
from app.models.client import Client
from app.services.auth_service import create_user, hash_password
from app.permissions import can, ROLE_LABELS, ROLE_BADGE_COLORS

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")

ROLES = ["super_admin", "admin", "manager", "client"]


@router.get("/users", response_class=HTMLResponse)
def user_list(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        context={
            "current_user": current_user,
            "users": users,
            "ROLE_LABELS": ROLE_LABELS,
            "ROLE_BADGE_COLORS": ROLE_BADGE_COLORS,
            "can": can
        },
    )


@router.get("/users/new", response_class=HTMLResponse)
def user_new(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    clients = db.query(Client).order_by(Client.name).all()
    return templates.TemplateResponse(
        request,
        "admin/user_form.html",
        context={
            "current_user": current_user,
            "user": None,
            "clients": clients,
            "ROLES": ROLES,
            "ROLE_LABELS": ROLE_LABELS,
            "error": "",
            "can": can
        },
    )


@router.post("/users/new")
def user_create(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(""),
    email: str = Form(""),
    password: str = Form(...),
    role: str = Form("manager"),
    client_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    # Only super_admin can create super_admin users
    if role == "super_admin" and current_user.role != "super_admin":
        role = "admin"

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        clients = db.query(Client).order_by(Client.name).all()
        return templates.TemplateResponse(
            request,
            "admin/user_form.html",
            context={
                "current_user": current_user,
                "user": None,
                "clients": clients,
                "ROLES": ROLES,
                "ROLE_LABELS": ROLE_LABELS,
                "error": f"User '{username}' already exists",
                "can": can
            },
        )

    create_user(
        db,
        username=username.strip(),
        password=password,
        role=role,
        full_name=full_name.strip(),
        email=email.strip(),
        client_id=int(client_id) if client_id else None,
    )
    return RedirectResponse("/admin/users", status_code=302)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def user_edit_page(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/admin/users", status_code=302)
    clients = db.query(Client).order_by(Client.name).all()
    return templates.TemplateResponse(
        request,
        "admin/user_form.html",
        context={
            "current_user": current_user,
            "user": user,
            "clients": clients,
            "ROLES": ROLES,
            "ROLE_LABELS": ROLE_LABELS,
            "error": "",
            "can": can
        },
    )


@router.post("/users/{user_id}/edit")
def user_edit(
    request: Request,
    user_id: int,
    full_name: str = Form(""),
    email: str = Form(""),
    role: str = Form("manager"),
    client_id: str = Form(""),
    is_active: str = Form("on"),
    new_password: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/admin/users", status_code=302)

    # Prevent admin from editing super_admin (unless self is super_admin)
    if user.role == "super_admin" and current_user.role != "super_admin":
        return RedirectResponse("/admin/users", status_code=302)

    # Only super_admin can set super_admin role
    if role == "super_admin" and current_user.role != "super_admin":
        role = "admin"

    user.full_name = full_name.strip() or None
    user.email = email.strip() or None
    user.role = role
    user.client_id = int(client_id) if client_id else None
    user.is_active = is_active == "on"
    if new_password:
        user.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/delete")
def user_delete(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    if not can(current_user, "delete_user"):
        return RedirectResponse("/admin/users", status_code=302)
    if user_id == current_user.id:
        return RedirectResponse("/admin/users", status_code=302)
    user = db.query(User).filter(User.id == user_id).first()
    if user and not (user.role == "super_admin" and current_user.role != "super_admin"):
        db.delete(user)
        db.commit()
    return RedirectResponse("/admin/users", status_code=302)
