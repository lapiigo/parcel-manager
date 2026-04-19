import csv
import io

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_manager_up
from app.models.client import Client
from app.models.supplier import Supplier
from app.models.wishlist import WishlistItem, ClientShipXAddress
from app.permissions import can
from app.services.crypto_service import encrypt, decrypt

router = APIRouter(prefix="/clients")
templates = Jinja2Templates(directory="app/templates")


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def client_list(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "view_clients"):
        return RedirectResponse("/dashboard", status_code=302)
    clients = db.query(Client).order_by(Client.name).all()
    return templates.TemplateResponse(
        request, "clients/list.html",
        context={"current_user": current_user, "clients": clients, "can": can},
    )


# ── New client ────────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
def client_new(request: Request, current_user=Depends(require_manager_up)):
    return templates.TemplateResponse(
        request, "clients/form.html",
        context={"current_user": current_user, "client": None, "error": "", "can": can},
    )


@router.post("/new")
def client_create(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    client_type: str = Form("direct"),
    balance: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    client = Client(
        name=name.strip(),
        email=email.strip() or None,
        phone=phone.strip() or None,
        type=client_type,
        balance=float(balance) if balance else 0.0,
        notes=notes.strip() or None,
    )
    db.add(client)
    db.commit()
    return RedirectResponse(f"/clients/{client.id}", status_code=302)


# ── Detail page ───────────────────────────────────────────────────────────────

@router.get("/{client_id}", response_class=HTMLResponse)
def client_detail(
    request: Request,
    client_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return RedirectResponse("/clients", status_code=302)

    hc_suppliers = db.query(Supplier).filter(Supplier.platform == "housecargo").all()
    sx_suppliers = db.query(Supplier).filter(Supplier.platform == "shipx").all()
    upload_msg = request.query_params.get("upload_msg", "")
    wl_added   = request.query_params.get("wl_added", "")
    wl_updated = request.query_params.get("wl_updated", "")
    wl_deleted = request.query_params.get("wl_deleted", "")

    return templates.TemplateResponse(
        request, "clients/detail.html",
        context={
            "current_user": current_user,
            "client": client,
            "hc_suppliers": hc_suppliers,
            "sx_suppliers": sx_suppliers,
            "upload_msg": upload_msg,
            "wl_added": wl_added,
            "wl_updated": wl_updated,
            "wl_deleted": wl_deleted,
            "can": can,
        },
    )


# ── Edit basic info ───────────────────────────────────────────────────────────

@router.get("/{client_id}/edit", response_class=HTMLResponse)
def client_edit_form(
    request: Request,
    client_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return RedirectResponse("/clients", status_code=302)
    return templates.TemplateResponse(
        request, "clients/form.html",
        context={"current_user": current_user, "client": client, "error": "", "can": can},
    )


@router.post("/{client_id}/edit")
def client_edit(
    request: Request,
    client_id: int,
    name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    client_type: str = Form("direct"),
    balance: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if client:
        client.name = name.strip()
        client.email = email.strip() or None
        client.phone = phone.strip() or None
        client.type = client_type
        client.balance = float(balance) if balance else 0.0
        client.notes = notes.strip() or None
        db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


# ── HouseCargo credentials ────────────────────────────────────────────────────

@router.post("/{client_id}/housecargo")
def client_housecargo_save(
    request: Request,
    client_id: int,
    housecargo_supplier_id: str = Form(""),
    housecargo_username: str = Form(""),
    housecargo_password: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if client and can(current_user, "edit_client"):
        client.housecargo_supplier_id = int(housecargo_supplier_id) if housecargo_supplier_id else None
        client.housecargo_username = housecargo_username.strip() or None
        if housecargo_password.strip():
            client.housecargo_password_encrypted = encrypt(housecargo_password.strip())
        elif not housecargo_username.strip():
            client.housecargo_password_encrypted = None
        db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


# ── Wishlist ──────────────────────────────────────────────────────────────────

@router.post("/{client_id}/wishlist/add")
def wishlist_add(
    request: Request,
    client_id: int,
    asin: str = Form(...),
    qty_per_month: str = Form("1"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client or not can(current_user, "edit_client"):
        return RedirectResponse(f"/clients/{client_id}", status_code=302)

    asin_clean = asin.strip().upper()
    if not asin_clean:
        return RedirectResponse(f"/clients/{client_id}", status_code=302)

    # Fetch title from Keepa
    title = None
    try:
        from app.services import keepa_service
        title = keepa_service.get_title_only(asin_clean)
    except Exception:
        pass

    item = WishlistItem(
        client_id=client_id,
        asin=asin_clean,
        title=title,
        qty_per_month=max(1, int(qty_per_month) if qty_per_month.isdigit() else 1),
        notes=notes.strip() or None,
    )
    db.add(item)
    db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


@router.post("/{client_id}/wishlist/upload")
async def wishlist_upload(
    request: Request,
    client_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    """Full wishlist sync from file: add new, update qty, delete removed. No Keepa calls."""
    import re as _re
    import urllib.parse
    _ASIN_RE = _re.compile(r'\bB[A-Z0-9]{9}\b', _re.IGNORECASE)

    client = db.query(Client).filter(Client.id == client_id).first()
    if not client or not can(current_user, "edit_client"):
        return RedirectResponse(f"/clients/{client_id}", status_code=302)

    if not file or not file.filename:
        return RedirectResponse(f"/clients/{client_id}", status_code=302)

    content = await file.read()
    if not content:
        return RedirectResponse(f"/clients/{client_id}", status_code=302)

    text = content.decode("utf-8-sig", errors="replace")
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return RedirectResponse(f"/clients/{client_id}", status_code=302)

    # Parse file → {asin: qty}
    file_qty_map: dict[str, int] = {}
    for line in lines:
        matches = list(_ASIN_RE.finditer(line.upper()))
        if not matches:
            continue
        for i, m in enumerate(matches):
            asin = m.group(0)
            seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            segment = line[m.end():seg_end]
            nums = _re.findall(r'(?<![.\d])\b([1-9][0-9]*)\b(?![.\d])', segment)
            qty = int(nums[0]) if nums else 1
            if asin not in file_qty_map:
                file_qty_map[asin] = qty

    if not file_qty_map:
        return RedirectResponse(
            f"/clients/{client_id}?upload_msg={urllib.parse.quote('No ASINs found in file.')}",
            status_code=302,
        )

    # Build existing lookup {asin: WishlistItem}
    existing: dict[str, WishlistItem] = {w.asin: w for w in client.wishlist_items}

    n_added = n_updated = n_deleted = 0

    # Add new / update qty for existing
    for asin, qty in file_qty_map.items():
        if asin in existing:
            if existing[asin].qty_per_month != qty:
                existing[asin].qty_per_month = qty
                n_updated += 1
        else:
            db.add(WishlistItem(client_id=client_id, asin=asin, title=None, qty_per_month=qty))
            n_added += 1

    # Delete items not present in file
    for asin, item in existing.items():
        if asin not in file_qty_map:
            db.delete(item)
            n_deleted += 1

    db.commit()

    params = urllib.parse.urlencode({
        "wl_added": n_added,
        "wl_updated": n_updated,
        "wl_deleted": n_deleted,
    })
    return RedirectResponse(f"/clients/{client_id}?{params}", status_code=302)


@router.get("/{client_id}/wishlist/fetch-titles")
async def wishlist_fetch_titles(
    client_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    """SSE stream: fetch Keepa titles for all wishlist items that have no title yet."""
    import json
    from fastapi.responses import StreamingResponse

    items_without_title = (
        db.query(WishlistItem)
        .filter(WishlistItem.client_id == client_id, WishlistItem.title.is_(None))
        .all()
    )
    total = len(items_without_title)

    async def event_stream():
        from app.services import keepa_service
        from app.database import SessionLocal
        # Use a fresh DB session for the streaming generator
        stream_db = SessionLocal()
        try:
            for i, item in enumerate(items_without_title, 1):
                title = None
                try:
                    title = keepa_service.get_title_only(item.asin)
                except Exception:
                    pass

                if title is not None:
                    # title="" means Keepa responded but ASIN has no title — persist so we don't retry
                    # title=None means API/network error — leave NULL so next SSE reconnect retries
                    db_item = stream_db.get(WishlistItem, item.id)
                    if db_item:
                        db_item.title = title
                        stream_db.commit()

                payload = json.dumps({
                    "current": i,
                    "total": total,
                    "asin": item.asin,
                    "title": title or "",
                })
                yield f"data: {payload}\n\n"

            yield "data: {\"done\": true}\n\n"
        finally:
            stream_db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{client_id}/wishlist/clear")
def wishlist_clear(
    request: Request,
    client_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if can(current_user, "edit_client"):
        db.query(WishlistItem).filter(WishlistItem.client_id == client_id).delete()
        db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


@router.post("/{client_id}/wishlist/{item_id}/delete")
def wishlist_delete(
    request: Request,
    client_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if can(current_user, "edit_client"):
        item = db.query(WishlistItem).filter(
            WishlistItem.id == item_id, WishlistItem.client_id == client_id
        ).first()
        if item:
            db.delete(item)
            db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


# ── ShipX Addresses ───────────────────────────────────────────────────────────

@router.post("/{client_id}/address/add")
def address_add(
    request: Request,
    client_id: int,
    supplier_id: str = Form(...),
    address_name: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client or not can(current_user, "edit_client"):
        return RedirectResponse(f"/clients/{client_id}", status_code=302)

    addr_name = address_name.strip()
    if addr_name:
        db.add(ClientShipXAddress(
            client_id=client_id,
            supplier_id=int(supplier_id),
            address_name=addr_name,
        ))
        db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


@router.post("/{client_id}/address/{addr_id}/delete")
def address_delete(
    request: Request,
    client_id: int,
    addr_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if can(current_user, "edit_client"):
        addr = db.query(ClientShipXAddress).filter(
            ClientShipXAddress.id == addr_id, ClientShipXAddress.client_id == client_id
        ).first()
        if addr:
            db.delete(addr)
            db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/{client_id}/delete")
def client_delete(
    request: Request,
    client_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_manager_up),
):
    if not can(current_user, "delete_client"):
        return RedirectResponse("/clients", status_code=302)
    client = db.query(Client).filter(Client.id == client_id).first()
    if client:
        db.delete(client)
        db.commit()
    return RedirectResponse("/clients", status_code=302)
