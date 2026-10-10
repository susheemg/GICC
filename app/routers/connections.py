"""Cloud folder connections - Google Drive, OneDrive, and the demo folder."""
from __future__ import annotations

import json

from fastapi import (APIRouter, BackgroundTasks, Depends, Form, HTTPException,
                     Request)
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from ..config import PUBLIC_BASE_URL
from ..db import SessionLocal, get_db
from ..models import (CloudConnection, DocumentVersion, SyncedFile,
                      SyncedFileRevision, SyncRun, Tenant, User, now)
from ..security import require_tenant, require_user
from ..services import audit, cloudsync, cloudsync_demo, roles
from .web_core import page

router = APIRouter()


def _connection(db: Session, tenant: Tenant, connection_id: str) -> CloudConnection:
    connection = db.get(CloudConnection, connection_id)
    if connection is None or connection.tenant_id != tenant.id:
        raise HTTPException(404, "Connection not found")
    return connection


def _redirect_uri(request: Request, provider: str) -> str:
    # Behind a proxy the request can look like plain http. Providers reject a
    # redirect URI that differs by even the scheme, so PUBLIC_BASE_URL wins.
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/connections/{provider}/callback"


def _sync_in_background(connection_id: str, trigger: str, user_id: str | None) -> None:
    with SessionLocal() as db:
        connection = db.get(CloudConnection, connection_id)
        actor = db.get(User, user_id) if user_id else None
        if connection is None:
            return
        try:
            cloudsync.run_sync(db, connection, trigger=trigger, actor=actor)
            db.commit()
        except Exception as exc:
            db.rollback()
            connection = db.get(CloudConnection, connection_id)
            connection.error = f"{type(exc).__name__}: {exc}"[:500]
            db.commit()


def _running(db: Session, connection: CloudConnection) -> bool:
    """One sync per folder at a time. A run older than half an hour is assumed
    dead, so a crash cannot lock the folder forever."""
    from datetime import timedelta
    return bool(db.scalar(select(func.count(SyncRun.id)).where(and_(
        SyncRun.connection_id == connection.id, SyncRun.status == "running",
        SyncRun.started_at > now() - timedelta(minutes=30)))))


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------
@router.get("/connections")
def overview(request: Request, db: Session = Depends(get_db),
             user: User = Depends(require_user),
             tenant: Tenant = Depends(require_tenant)):
    connections = db.scalars(select(CloudConnection).where(and_(
        CloudConnection.tenant_id == tenant.id,
        CloudConnection.status != "disconnected"))
        .order_by(CloudConnection.created_at)).all()
    rows = []
    for connection in connections:
        files = db.scalar(select(func.count(SyncedFile.id)).where(and_(
            SyncedFile.connection_id == connection.id,
            SyncedFile.state != "removed"))) or 0
        rows.append({"connection": connection, "files": files,
                     "label": cloudsync.PROVIDER_LABEL.get(connection.provider)})
    return page(request, "connections.html", user, tenant, db,
                page_title="Cloud folders", crumb="My Documentation",
                nav="connections", rows=rows, providers=cloudsync.providers_status(),
                schedules=list(cloudsync.SCHEDULES),
                can_connect=roles.can(user, roles.SETTINGS_WRITE))


# --------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------
@router.post("/connections/{provider}/connect")
def connect(provider: str, request: Request, background: BackgroundTasks,
            db: Session = Depends(get_db), user: User = Depends(require_user),
            tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.SETTINGS_WRITE)

    if provider == "demo":
        connection = CloudConnection(
            tenant_id=tenant.id, provider="demo", status="active", schedule="daily",
            folder_name="Compliance documents (demo)",
            account_email="demo@drive.example", connected_by=user.email)
        db.add(connection)
        db.flush()
        connection.folder_id = connection.id
        cloudsync_demo.seed(connection.id, tenant.name)
        audit.record(db, tenant_id=tenant.id, action="cloud.connected",
                     actor_id=user.id, actor_label=user.email,
                     object_type="cloud_connection", object_id=connection.id,
                     payload={"provider": "demo"})
        db.commit()
        background.add_task(_sync_in_background, connection.id, "first", user.id)
        flash(request, "Demo folder connected. The first sync is running.")
        return RedirectResponse(f"/connections/{connection.id}", status_code=303)

    try:
        handler = cloudsync.provider_for(provider)
    except cloudsync.ProviderError:
        raise HTTPException(404, "Unknown provider")
    if not handler.configured:
        flash(request, f"{cloudsync.PROVIDER_LABEL[provider]} is not configured on "
                       "this deployment yet. An administrator needs to register an "
                       "OAuth app and set its client id and secret.", "warn")
        return RedirectResponse("/connections", status_code=303)

    connection = CloudConnection(tenant_id=tenant.id, provider=provider,
                                 status="pending", connected_by=user.email)
    db.add(connection)
    db.commit()
    state = cloudsync.new_state()
    request.session["_oauth"] = {"state": state, "connection_id": connection.id,
                                 "provider": provider}
    return RedirectResponse(handler.authorise_url(_redirect_uri(request, provider),
                                                  state), status_code=303)


@router.get("/connections/{provider}/callback")
def callback(provider: str, request: Request, code: str = "", state: str = "",
             error: str = "", db: Session = Depends(get_db),
             user: User = Depends(require_user),
             tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    pending = request.session.pop("_oauth", None)
    # The state parameter is what stops another site completing an OAuth flow
    # into this account. A mismatch is refused, not logged and ignored.
    if not pending or pending.get("state") != state or pending.get("provider") != provider:
        flash(request, "That sign-in could not be matched to a request from this "
                       "session, so it was refused. Start the connection again.", "bad")
        return RedirectResponse("/connections", status_code=303)
    connection = _connection(db, tenant, pending["connection_id"])
    if error or not code:
        connection.status = "disconnected"
        connection.error = error or "No authorisation code returned"
        db.commit()
        flash(request, "The connection was not authorised.", "warn")
        return RedirectResponse("/connections", status_code=303)

    handler = cloudsync.provider_for(provider)
    try:
        tokens = handler.exchange(code, _redirect_uri(request, provider))
        cloudsync.store_tokens(connection, tokens)
        connection.account_email = handler.account(tokens["access_token"])
    except Exception as exc:
        connection.status = "disconnected"
        connection.error = f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
        flash(request, f"Connecting failed: {connection.error}", "bad")
        return RedirectResponse("/connections", status_code=303)

    connection.status = "choose_folder"
    audit.record(db, tenant_id=tenant.id, action="cloud.connected",
                 actor_id=user.id, actor_label=user.email,
                 object_type="cloud_connection", object_id=connection.id,
                 payload={"provider": provider, "account": connection.account_email})
    db.commit()
    return RedirectResponse(f"/connections/{connection.id}/folder", status_code=303)


@router.get("/connections/{connection_id}/folder")
def choose_folder(connection_id: str, request: Request, parent: str = "",
                  db: Session = Depends(get_db), user: User = Depends(require_user),
                  tenant: Tenant = Depends(require_tenant)):
    connection = _connection(db, tenant, connection_id)
    handler = cloudsync.provider_for(connection.provider)
    folders, error = [], None
    try:
        folders = handler.folders(cloudsync.access_token(db, connection), parent or None)
        db.commit()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
    return page(request, "connection_folder.html", user, tenant, db,
                page_title="Choose a folder", crumb="Cloud folders",
                nav="connections", connection=connection, folders=folders,
                parent=parent, error=error,
                label=cloudsync.PROVIDER_LABEL.get(connection.provider))


@router.post("/connections/{connection_id}/folder")
def set_folder(connection_id: str, request: Request, background: BackgroundTasks,
               folder_id: str = Form(...), folder_name: str = Form(...),
               schedule: str = Form("daily"), db: Session = Depends(get_db),
               user: User = Depends(require_user),
               tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.SETTINGS_WRITE)
    connection = _connection(db, tenant, connection_id)
    connection.folder_id = folder_id
    connection.folder_name = folder_name
    connection.schedule = schedule if schedule in cloudsync.SCHEDULES else "daily"
    connection.status = "active"
    db.commit()
    background.add_task(_sync_in_background, connection.id, "first", user.id)
    flash(request, f"Watching “{folder_name}”. The first sync is running.")
    return RedirectResponse(f"/connections/{connection.id}", status_code=303)


# --------------------------------------------------------------------------
# A connection
# --------------------------------------------------------------------------
@router.get("/connections/{connection_id}")
def detail(connection_id: str, request: Request, db: Session = Depends(get_db),
           user: User = Depends(require_user),
           tenant: Tenant = Depends(require_tenant)):
    connection = _connection(db, tenant, connection_id)
    runs = db.scalars(select(SyncRun).where(SyncRun.connection_id == connection.id)
                      .order_by(desc(SyncRun.started_at)).limit(12)).all()
    files = db.scalars(select(SyncedFile).where(SyncedFile.connection_id == connection.id)
                       .order_by(SyncedFile.path)).all()
    file_rows = []
    drafts = 0
    for record in files:
        version = (db.get(DocumentVersion, record.current_version_id)
                   if record.current_version_id else None)
        revisions = db.scalar(select(func.count(SyncedFileRevision.id)).where(
            SyncedFileRevision.synced_file_id == record.id)) or 0
        latest = db.scalar(select(SyncedFileRevision).where(
            SyncedFileRevision.synced_file_id == record.id)
            .order_by(desc(SyncedFileRevision.seen_at)).limit(1))
        findings = json.loads(latest.authority_findings_json) if latest and \
            latest.authority_findings_json else {}
        if version is not None and version.status == "draft" and record.state == "active":
            drafts += 1
        file_rows.append({"file": record, "version": version, "revisions": revisions,
                          "authority": (findings.get("best") or {}).get("authority"),
                          "lookalike": bool(findings.get("lookalike")),
                          "type_code": latest.type_code if latest else None})
    last = runs[0] if runs else None
    summary = json.loads(last.summary_json) if last and last.summary_json else {}
    return page(request, "connection_detail.html", user, tenant, db,
                page_title=connection.folder_name or "Cloud folder",
                crumb="Cloud folders", nav="connections", connection=connection,
                label=cloudsync.PROVIDER_LABEL.get(connection.provider),
                runs=runs, files=file_rows, drafts=drafts, last=last,
                summary=summary, running=_running(db, connection),
                schedules=list(cloudsync.SCHEDULES),
                can_write=roles.can(user, roles.DOCUMENTS_WRITE),
                can_connect=roles.can(user, roles.SETTINGS_WRITE))


@router.post("/connections/{connection_id}/sync")
def sync_now(connection_id: str, request: Request, background: BackgroundTasks,
             db: Session = Depends(get_db), user: User = Depends(require_user),
             tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    connection = _connection(db, tenant, connection_id)
    if connection.status not in {"active", "reauthorise"}:
        flash(request, "Finish connecting this folder before syncing it.", "warn")
    elif _running(db, connection):
        flash(request, "A sync of this folder is already running.", "info")
    else:
        background.add_task(_sync_in_background, connection.id, "manual", user.id)
        flash(request, "Sync started. Refresh in a moment to see what changed.")
    return RedirectResponse(f"/connections/{connection_id}", status_code=303)


@router.post("/connections/{connection_id}/settings")
def settings(connection_id: str, request: Request, schedule: str = Form("daily"),
             auto_publish_renewals: str = Form(""), db: Session = Depends(get_db),
             user: User = Depends(require_user),
             tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.SETTINGS_WRITE)
    connection = _connection(db, tenant, connection_id)
    connection.schedule = schedule if schedule in cloudsync.SCHEDULES else "daily"
    connection.auto_publish_renewals = auto_publish_renewals == "on"
    cloudsync.schedule_next(connection)
    db.commit()
    flash(request, "Schedule saved." + (
        f" Next check {connection.next_sync_at:%d %b %H:%M} UTC."
        if connection.next_sync_at else " Automatic checks are off."))
    return RedirectResponse(f"/connections/{connection_id}", status_code=303)


@router.post("/connections/{connection_id}/disconnect")
def disconnect(connection_id: str, request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_user),
               tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.SETTINGS_WRITE)
    connection = _connection(db, tenant, connection_id)
    cloudsync.disconnect(db, connection, user)
    db.commit()
    flash(request, "Disconnected. Access tokens are deleted; every credential and "
                   "version already filed stays in your register.", "warn")
    return RedirectResponse("/connections", status_code=303)


@router.get("/connections/{connection_id}/files/{file_id}")
def file_history(connection_id: str, file_id: str, request: Request,
                 db: Session = Depends(get_db), user: User = Depends(require_user),
                 tenant: Tenant = Depends(require_tenant)):
    connection = _connection(db, tenant, connection_id)
    record = db.get(SyncedFile, file_id)
    if record is None or record.connection_id != connection.id:
        raise HTTPException(404, "File not found")
    revisions = []
    for revision in db.scalars(select(SyncedFileRevision).where(
            SyncedFileRevision.synced_file_id == record.id)
            .order_by(desc(SyncedFileRevision.seen_at))).all():
        version = (db.get(DocumentVersion, revision.document_version_id)
                   if revision.document_version_id else None)
        revisions.append({"revision": revision, "version": version,
                          "findings": json.loads(revision.authority_findings_json)
                          if revision.authority_findings_json else {}})
    return page(request, "connection_file.html", user, tenant, db,
                page_title=record.name, crumb="Cloud folders", nav="connections",
                connection=connection, record=record, revisions=revisions)


@router.post("/connections/{connection_id}/demo/{action}")
def demo_action(connection_id: str, action: str, request: Request,
                db: Session = Depends(get_db), user: User = Depends(require_user),
                tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    connection = _connection(db, tenant, connection_id)
    if connection.provider != "demo":
        raise HTTPException(400, "Only the demo folder can be changed from here")
    message = cloudsync_demo.simulate(connection.id, tenant.name, action)
    flash(request, f"In the demo folder: {message}. Press Sync now, or wait for the "
                   "schedule, to see it arrive.")
    return RedirectResponse(f"/connections/{connection_id}", status_code=303)
