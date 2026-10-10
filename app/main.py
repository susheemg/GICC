from __future__ import annotations

from datetime import date, datetime

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import (APP_LONG_NAME, APP_NAME, APP_VERSION, BASE_DIR,
                     MCP_PROTOCOL_VERSION, SECRET_KEY, SESSION_COOKIE, SESSION_MAX_AGE)
from .db import SessionLocal
from .models import AccessRequest, ClientAccount, Document, Notification, Task
from .seed import init_db

app = FastAPI(title=f"{APP_NAME} — {APP_LONG_NAME}", version=APP_VERSION,
              docs_url="/docs", redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY,
                   session_cookie=SESSION_COOKIE, max_age=SESSION_MAX_AGE,
                   same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

ASSURANCE_NAMES = {
    "L0": "Self-declared", "L1": "Source-linked", "L2": "Human-verified",
    "L3": "Machine-verified", "L4": "Cryptographically verifiable",
}
SENSITIVITY_NAMES = {"P": "Public", "R": "Restricted", "C": "Confidential",
                     "S": "Special category"}


def _fmt_date(value, fmt="%d %b %Y"):
    if not value:
        return "—"
    if isinstance(value, (date, datetime)):
        return value.strftime(fmt)
    return str(value)


def _days_until(value) -> int | None:
    if not value:
        return None
    if isinstance(value, datetime):
        value = value.date()
    return (value - date.today()).days


templates.env.globals.update(
    assurance_name=lambda level: ASSURANCE_NAMES.get(level, "Unrated"),
    sensitivity_name=lambda code: SENSITIVITY_NAMES.get(code, code),
    app_name=APP_NAME, app_version=APP_VERSION, today=date.today,
    mcp_version=MCP_PROTOCOL_VERSION,
)
templates.env.filters["d"] = _fmt_date
templates.env.filters["days_until"] = _days_until
templates.env.filters["money"] = lambda v: f"{v:,.0f}" if v else "—"


def flash(request: Request, message: str, category: str = "ok") -> None:
    request.session.setdefault("_flashes", []).append((category, message))


def pop_flashes(request: Request) -> list:
    return request.session.pop("_flashes", [])


def sidebar_counts(db, tenant_id: str) -> dict:
    attention = db.scalar(select(func.count(Document.id)).where(and_(
        Document.tenant_id == tenant_id, Document.deleted_at.is_(None),
        Document.derived_status.in_(["expired", "suspended", "missing"])))) or 0
    tasks = db.scalar(select(func.count(Task.id)).where(and_(
        Task.tenant_id == tenant_id, Task.status == "open"))) or 0
    clients = db.scalar(select(func.count(ClientAccount.id)).where(and_(
        ClientAccount.tenant_id == tenant_id, ClientAccount.status == "active"))) or 0
    requests_open = db.scalar(select(func.count(AccessRequest.id)).where(and_(
        AccessRequest.tenant_id == tenant_id, AccessRequest.status == "open"))) or 0
    from .models import Asset
    from datetime import date
    calibration_due = db.scalar(select(func.count(Asset.id)).where(and_(
        Asset.tenant_id == tenant_id, Asset.status != "retired",
        Asset.next_due_on.is_not(None), Asset.next_due_on < date.today()))) or 0
    return {"attention": attention, "tasks": tasks, "clients": clients,
            "requests": requests_open, "recheck": 0, "unread": 0,
            "calibration_due": calibration_due}


def render(request: Request, template: str, context: dict) -> HTMLResponse:
    context.setdefault("flashes", pop_flashes(request))
    context["request"] = request
    return templates.TemplateResponse(request, template, context)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown.

    `@app.on_event` is deprecated and removed in newer Starlette; FastAPI pins
    only `starlette>=0.46.0`, so a fresh deploy can pick up a major version the
    build was never tested against. Lifespan is the supported form and works
    across both.
    """
    init_db()
    # Additive schema changes for an existing deployment, then any job left
    # running by a restart resumes from its pending items (GICC-07 §2.2).
    from .db import engine
    from . import migrations
    from .services import jobs

    migrations.run(engine)
    resumed = jobs.resume_incomplete()
    if resumed:
        print(f"[startup] resumed {resumed} incomplete job(s)", flush=True)

    sweeper = asyncio.create_task(_sweep_loop())
    cloud = asyncio.create_task(_cloud_sync_loop())
    try:
        yield
    finally:
        sweeper.cancel()
        cloud.cancel()


# The app is constructed above, so the handler is attached rather than passed
# to the constructor. Same effect, and it keeps the startup code beside the
# things it starts.
app.router.lifespan_context = lifespan


async def _cloud_sync_loop() -> None:
    """Scheduled folder checks. Wakes every few minutes and runs whichever
    connections are due; each runs in its own transaction, so one folder failing
    does not hold up the others."""
    from .config import CLOUD_SYNC_LOOP_MINUTES
    from .services import cloudsync, cloudsync_demo  # noqa: F401 - registers demo

    await asyncio.sleep(30)
    while True:
        try:
            with SessionLocal() as db:
                ran = await asyncio.to_thread(cloudsync.run_due, db)
            if ran:
                print(f"[cloudsync] scheduled sync ran for {ran} folder(s)", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[cloudsync] loop failed: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(max(60, CLOUD_SYNC_LOOP_MINUTES * 60))


async def _sweep_loop() -> None:
    """Background notification sweep - GICC-05 F20. One task, guarded so a
    failure logs and retries rather than killing the loop."""
    from .config import SWEEP_MINUTES
    from .services import notifications

    await asyncio.sleep(20)
    while True:
        try:
            with SessionLocal() as db:
                result = await asyncio.to_thread(notifications.sweep_all, db)
            if result["created"]:
                print(f"[sweep] {result}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[sweep] failed: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(max(60, SWEEP_MINUTES * 60))


@app.exception_handler(StarletteHTTPException)
async def _http_exception(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 303 and exc.headers and exc.headers.get("Location"):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith(("/api/", "/mcp")):
        from fastapi.responses import JSONResponse
        return JSONResponse({"type": "about:blank", "title": exc.detail,
                             "status": exc.status_code}, status_code=exc.status_code)
    return HTMLResponse(
        f"""<link rel="stylesheet" href="/static/css/brata.css">
        <div style="max-width:520px;margin:80px auto;padding:0 20px">
          <div class="card"><div class="body">
            <h1>{exc.status_code}</h1>
            <p class="muted">{exc.detail}</p>
            <a class="btn" href="/dashboard">Back to home</a>
          </div></div></div>""", status_code=exc.status_code)


@app.get("/healthz", include_in_schema=False)
def healthz():
    with SessionLocal() as db:
        db.execute(select(1))
    return {"status": "ok", "version": APP_VERSION}


from .routers import (admin, admin_import, api, assets_web, auth,  # noqa: E402
                      batch_web, buyer, certsearch_web, claim, connections, intelligence,
                      mcp, public,
                      onboarding_web, share, web_core, web_disclosure,
                      web_documents)

app.include_router(public.router)     # / and /search: the front door
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(admin_import.router)   # /admin/import, /admin/claims, /admin/proposals
app.include_router(claim.router)          # public profile search and claim
app.include_router(buyer.router)          # buyer portal
app.include_router(web_core.router)
app.include_router(onboarding_web.router)
app.include_router(intelligence.router)   # before web_documents: literal
app.include_router(batch_web.router)      # /documents/bulk must beat /documents/{id}
app.include_router(web_documents.router)  # paths must beat /documents/{id}
app.include_router(assets_web.router)
app.include_router(certsearch_web.router)
app.include_router(connections.router)
app.include_router(web_disclosure.router)
app.include_router(share.router)
app.include_router(api.router)
app.include_router(mcp.router)
