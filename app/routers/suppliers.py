from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_manager_up
from app.models.supplier import Supplier
from app.permissions import can
from app.services.crypto_service import encrypt, decrypt
from app.services import housecargo_service, shipx_service

router = APIRouter(prefix="/suppliers")
templates = Jinja2Templates(directory="app/templates")


def _supplier_context(supplier: Supplier | None) -> dict:
    """Add a non-sensitive helper flag to the template context."""
    if supplier is None:
        return {"supplier": None, "has_password": False}
    return {
        "supplier": supplier,
        "has_password": bool(supplier.login_password_encrypted),
    }


@router.get("", response_class=HTMLResponse)
def supplier_list(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return templates.TemplateResponse(
        request,
        "suppliers/list.html",
        context={"current_user": current_user, "suppliers": suppliers, "can": can},
    )


@router.get("/new", response_class=HTMLResponse)
def supplier_new(request: Request, current_user=Depends(require_manager_up)):
    return templates.TemplateResponse(
        request,
        "suppliers/form.html",
        context={"current_user": current_user, "error": "", "can": can, **_supplier_context(None)},
    )


@router.post("/new")
def supplier_create(
    request: Request,
    name: str = Form(...),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    country: str = Form(""),
    platform: str = Form(""),
    website: str = Form(""),
    login_username: str = Form(""),
    login_password: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    supplier = Supplier(
        name=name.strip(),
        contact_name=contact_name.strip() or None,
        email=email.strip() or None,
        phone=phone.strip() or None,
        country=country.strip() or None,
        platform=platform.strip() or None,
        website=website.strip() or None,
        login_username=login_username.strip() or None,
        login_password_encrypted=encrypt(login_password) if login_password else None,
        notes=notes.strip() or None,
    )
    db.add(supplier)
    db.commit()
    return RedirectResponse("/suppliers", status_code=302)


@router.get("/{supplier_id}/edit", response_class=HTMLResponse)
def supplier_edit_page(
    request: Request,
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not supplier:
        return RedirectResponse("/suppliers", status_code=302)
    return templates.TemplateResponse(
        request,
        "suppliers/form.html",
        context={"current_user": current_user, "error": "", "can": can, **_supplier_context(supplier)},
    )


@router.post("/{supplier_id}/edit")
def supplier_edit(
    request: Request,
    supplier_id: int,
    name: str = Form(...),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    country: str = Form(""),
    platform: str = Form(""),
    website: str = Form(""),
    login_username: str = Form(""),
    login_password: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not supplier:
        return RedirectResponse("/suppliers", status_code=302)
    supplier.name = name.strip()
    supplier.contact_name = contact_name.strip() or None
    supplier.email = email.strip() or None
    supplier.phone = phone.strip() or None
    supplier.country = country.strip() or None
    supplier.platform = platform.strip() or None
    supplier.website = website.strip() or None
    supplier.login_username = login_username.strip() or None
    if login_password:
        supplier.login_password_encrypted = encrypt(login_password)
    supplier.notes = notes.strip() or None
    db.commit()
    return RedirectResponse("/suppliers", status_code=302)


@router.post("/{supplier_id}/sync")
def supplier_sync(
    request: Request,
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not supplier:
        return RedirectResponse("/suppliers", status_code=302)

    if supplier.platform not in ("housecargo", "shipx"):
        return templates.TemplateResponse(
            request,
            "suppliers/sync_result.html",
            context={
                "current_user": current_user,
                "supplier": supplier,
                "can": can,
                "error": "Auto-sync is not supported for this platform.",
                "result": None,
            },
        )

    if not supplier.login_username or not supplier.login_password_encrypted:
        return templates.TemplateResponse(
            request,
            "suppliers/sync_result.html",
            context={
                "current_user": current_user,
                "supplier": supplier,
                "can": can,
                "error": "Credentials are not configured for this supplier.",
                "result": None,
            },
        )

    password = decrypt(supplier.login_password_encrypted)

    try:
        if supplier.platform == "housecargo":
            # Prefer per-client credentials; fall back to supplier-level credentials
            from app.models.client import Client as ClientModel
            hc_clients = (
                db.query(ClientModel)
                .filter(
                    ClientModel.housecargo_supplier_id == supplier.id,
                    ClientModel.housecargo_username.isnot(None),
                    ClientModel.housecargo_password_encrypted.isnot(None),
                )
                .all()
            )
            if hc_clients:
                result = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
                for hc_client in hc_clients:
                    from app.services.crypto_service import decrypt as _decrypt
                    cli_pass = _decrypt(hc_client.housecargo_password_encrypted)
                    r = housecargo_service.sync(
                        supplier.id, hc_client.housecargo_username, cli_pass, db,
                        client_id=hc_client.id
                    )
                    for k in ("created", "updated", "skipped"):
                        result[k] += r[k]
                    result["errors"].extend(r["errors"])
            else:
                result = housecargo_service.sync(supplier.id, supplier.login_username, password, db)
        elif supplier.platform == "shipx":
            result = shipx_service.sync(supplier.id, supplier.login_username, password, db)
        else:
            result = None
            error = "Auto-sync is not supported for this platform."
            return templates.TemplateResponse(
                request,
                "suppliers/sync_result.html",
                context={
                    "current_user": current_user,
                    "supplier": supplier,
                    "can": can,
                    "error": error,
                    "result": None,
                },
            )
        error = None
    except (housecargo_service.HouseCargoError, shipx_service.ShipXError) as exc:
        result = None
        error = str(exc)

    return templates.TemplateResponse(
        request,
        "suppliers/sync_result.html",
        context={
            "current_user": current_user,
            "supplier": supplier,
            "can": can,
            "error": error,
            "result": result,
        },
    )


@router.post("/{supplier_id}/delete")
def supplier_delete(
    request: Request,
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "delete_supplier"):
        return RedirectResponse("/suppliers", status_code=302)
    supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if supplier:
        db.delete(supplier)
        db.commit()
    return RedirectResponse("/suppliers", status_code=302)
