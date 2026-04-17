import os
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import engine
import app.models  # register all models with SQLAlchemy

from app.auth import NotAuthenticatedException, ForbiddenException
from app.routers import auth, dashboard, parcels, suppliers, clients, orders, admin, reports, portal, todo

app = FastAPI(title="Parcel Manager", docs_url=None, redoc_url=None)

# ── Static files ──────────────────────────────────────────────────────────────
os.makedirs("uploads/parcels", exist_ok=True)
os.makedirs("static", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="app/templates")

# ── Exception handlers ────────────────────────────────────────────────────────
@app.exception_handler(NotAuthenticatedException)
async def not_authenticated_handler(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse("/login", status_code=302)


@app.exception_handler(ForbiddenException)
async def forbidden_handler(request: Request, exc: ForbiddenException):
    return HTMLResponse(
        content=f"""
        <html><head><title>403</title>
        <script src="https://cdn.tailwindcss.com"></script></head>
        <body class="min-h-screen flex items-center justify-center bg-gray-50">
        <div class="text-center">
          <p class="text-5xl mb-4">🚫</p>
          <h1 class="text-2xl font-bold text-gray-800 mb-2">Access Denied</h1>
          <p class="text-gray-500">{exc.message}</p>
          <a href="javascript:history.back()" class="mt-4 inline-block text-indigo-600 hover:underline text-sm">← Back</a>
        </div></body></html>
        """,
        status_code=403,
    )


# ── Root redirect ─────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return RedirectResponse("/dashboard", status_code=302)


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(parcels.router)
app.include_router(suppliers.router)
app.include_router(clients.router)
app.include_router(orders.router)
app.include_router(admin.router)
app.include_router(reports.router)
app.include_router(portal.router)
app.include_router(todo.router)


# ── DB init (create tables if they don't exist) ───────────────────────────────
@app.on_event("startup")
def startup():
    from app.database import Base
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)

    # Start background scheduler for Telegram reminders
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from app.services.telegram_service import check_reminders, poll_telegram_updates
        scheduler = BackgroundScheduler()
        scheduler.add_job(poll_telegram_updates, "interval", seconds=10, id="tg_poll")
        scheduler.add_job(check_reminders, "interval", minutes=1, id="reminders")
        scheduler.start()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Scheduler not started: {e}")
    # Add new columns to existing tables if they don't exist (SQLite migration)
    new_columns = [
        ("parcels", "external_order_id", "VARCHAR(255)"),
        ("parcels", "qty", "INTEGER DEFAULT 1"),
        ("parcels", "asin", "VARCHAR(20)"),
        ("parcels", "title", "VARCHAR(500)"),
        ("parcels", "amazon_price", "REAL"),
        ("parcels", "payment_report_date", "VARCHAR(10)"),
        ("parcels", "is_wrong_address", "INTEGER DEFAULT 0"),
        ("parcels", "match_source", "VARCHAR(20)"),
        ("suppliers", "website", "VARCHAR(500)"),
        ("suppliers", "login_username", "VARCHAR(255)"),
        ("suppliers", "login_password_encrypted", "TEXT"),
        ("clients", "housecargo_supplier_id", "INTEGER"),
        ("clients", "housecargo_username", "VARCHAR(255)"),
        ("clients", "housecargo_password_encrypted", "TEXT"),
        ("users", "telegram_chat_id", "VARCHAR(50)"),
        ("users", "telegram_token", "VARCHAR(64)"),
        ("users", "telegram_token_expires", "DATETIME"),
        ("users", "timezone", "VARCHAR(50) DEFAULT 'UTC'"),
    ]
    with engine.connect() as conn:
        for table, column, col_type in new_columns:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                conn.commit()
            except Exception:
                pass  # Column already exists
