"""Routes for the v2 feature set - GICC-05.

Smart upload, notification inbox, questionnaire automation, Trust Index and the
authenticity signal panel.
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from ..config import AI_EXCLUDED_SENSITIVITY
from ..db import get_db
from ..models import (AiInference, AnswerBank, AuthenticitySignal, ClientAccount,
                      ControlSummary, Document, DocumentType, DocumentVersion,
                      Notification, NotificationPreference, Organisation, Questionnaire,
                      QuestionnaireItem, Tenant, User, now)
from ..security import require_tenant, require_user
from ..services import (ai, audit, authenticity, documents as docsvc, notifications,
                        onboarding, questionnaire as qsvc, requirements as reqsvc,
                        roles, textextract, trust, verification)
from .web_core import page

router = APIRouter()


# --------------------------------------------------------------------------
# F16 - upload with automatic classification
# --------------------------------------------------------------------------
@router.get("/documents/upload")
def upload_form(request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    proposal = request.session.pop("_upload_proposal", None)
    return page(request, "upload.html", user, tenant, db, page_title="Upload a document",
                crumb="Credentials", nav="upload", proposal=proposal,
                ai_status=ai.status(db))


@router.post("/documents/upload")
async def upload_and_classify(request: Request, file: UploadFile = File(...),
                              db: Session = Depends(get_db),
                              user: User = Depends(require_user),
                              tenant: Tenant = Depends(require_tenant)):
    """Classify, extract, propose. Nothing is filed until the person confirms."""
    from ..main import flash
    roles.require(user, roles.DOCUMENTS_WRITE)

    data = await file.read()
    if not data:
        flash(request, "That file was empty.", "bad")
        return RedirectResponse("/documents/upload", status_code=303)

    file_path, digest = docsvc.store_upload(tenant.id, file.filename, data)
    text, method = textextract.extract(data, file.filename)
    catalogue = list(db.scalars(select(DocumentType).where(
        DocumentType.is_active.is_(True))).all())
    result = ai.classify_document(db, tenant.id, filename=file.filename,
                                  text=text, catalogue=catalogue)

    proposed = None
    if result.get("type_code"):
        proposed = db.scalar(select(DocumentType).where(
            DocumentType.code == result["type_code"]))

    existing = None
    if proposed:
        existing = db.scalar(select(Document).where(and_(
            Document.tenant_id == tenant.id,
            Document.document_type_id == proposed.id,
            Document.deleted_at.is_(None))))

    alternatives = []
    for entry in (result.get("alternatives") or [])[:3]:
        dtype = db.scalar(select(DocumentType).where(
            DocumentType.code == entry.get("type_code")))
        if dtype:
            alternatives.append({"type": dtype, "confidence": entry.get("confidence", 0)})

    audit.record(db, tenant_id=tenant.id, action="document.classified", actor_id=user.id,
                 actor_label=user.email, object_type="upload", object_id=digest[:32],
                 payload={"filename": file.filename, "proposed": result.get("type_code"),
                          "confidence": result.get("confidence"),
                          "engine": result.get("source"), "text_method": method})
    db.commit()

    request.session["_upload_proposal"] = {
        "filename": file.filename, "file_path": file_path, "digest": digest,
        "size": len(data), "mime": file.content_type,
        "type_code": proposed.code if proposed else None,
        "type_name": proposed.name if proposed else None,
        "existing_document_id": existing.id if existing else None,
        "confidence": round(float(result.get("confidence") or 0), 2),
        "alternatives": [{"code": a["type"].code, "name": a["type"].name,
                          "confidence": round(float(a["confidence"]), 2)}
                         for a in alternatives],
        "fields": result.get("fields", {}),
        "rationale": result.get("rationale", ""),
        "source": result.get("source", "heuristic"),
        "text_method": method,
        "text_chars": len(text),
    }
    return RedirectResponse("/documents/upload", status_code=303)


@router.post("/documents/upload/confirm")
def confirm_upload(request: Request, type_code: str = Form(...),
                   file_path: str = Form(...), filename: str = Form(""),
                   digest: str = Form(""), size: str = Form("0"), mime: str = Form(""),
                   issue_date: str = Form(""), expiry_date: str = Form(""),
                   certificate_number: str = Form(""), issuer_name: str = Form(""),
                   holder_name: str = Form(""), scope_statement: str = Form(""),
                   cover_amount: str = Form(""), verification_url: str = Form(""),
                   change_reason: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(require_user),
                   tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    roles.require(user, roles.DOCUMENTS_WRITE)

    dtype = db.scalar(select(DocumentType).where(DocumentType.code == type_code))
    if dtype is None:
        raise HTTPException(400, "Unknown document type")

    document = db.scalar(select(Document).where(and_(
        Document.tenant_id == tenant.id, Document.document_type_id == dtype.id,
        Document.deleted_at.is_(None))))
    if document is None:
        document = Document(tenant_id=tenant.id, document_type_id=dtype.id,
                            title=dtype.name, jurisdiction=tenant.organisation.country,
                            owner_user_id=user.id)
        db.add(document)
        db.flush()
        audit.record(db, tenant_id=tenant.id, action="document.created", actor_id=user.id,
                     actor_label=user.email, object_type="document", object_id=document.id,
                     payload={"type": dtype.code, "via": "smart upload"})

    previous = (db.get(DocumentVersion, document.active_version_id)
                if document.active_version_id else None)

    def parse(value: str):
        try:
            return date.fromisoformat(value) if value else None
        except ValueError:
            return None

    cover = None
    if cover_amount.strip():
        try:
            cover = float(cover_amount.replace(",", ""))
        except ValueError:
            cover = None

    version = docsvc.create_version(
        db, document=document, actor=user,
        issue_date=parse(issue_date), effective_from=parse(issue_date) or date.today(),
        expiry_date=parse(expiry_date),
        certificate_number=certificate_number.strip() or None,
        issuer_name=issuer_name.strip() or None,
        holder_name=holder_name.strip() or None,
        scope_statement=scope_statement.strip() or None,
        cover_amount=cover, currency="GBP" if cover else None,
        verification_url=verification_url.strip() or None,
        change_reason=change_reason.strip() or None,
        file_path=file_path, file_name=filename, mime_type=mime or None,
        file_size=int(size) if size.isdigit() else None, content_hash=digest or None)

    diff = ai.version_diff(previous, version)
    if previous and not version.change_reason:
        version.change_reason = diff["reason"]

    docsvc.publish_version(db, version, user)
    check = verification.run_check(db, version, actor=None)
    signals = authenticity.evaluate(db, version, actor=user)
    notifications.on_document_uploaded(db, tenant, document, version, user)
    if check.result in verification.NEGATIVE_RESULTS:
        notifications.on_adverse_verification(db, tenant, document, version, check.result)
    notifications.on_authenticity_signals(db, tenant, document, signals)
    docsvc.run_maintenance(db, tenant)
    trust.compute(db, tenant, actor=user)
    onboarding.sync_steps(db, tenant)
    onboarding.observe(db, tenant)
    db.commit()

    message = (f"{document.title} filed at assurance {version.assurance_level}.")
    if diff["material"]:
        message += " Material change: " + "; ".join(diff["material"]) + \
                   " — every client holding a grant can see it."
    if signals:
        message += f" {len(signals)} authenticity signal(s) raised for review."
    flash(request, message, "warn" if (diff["material"] or signals) else "ok")
    return RedirectResponse(f"/documents/{document.id}", status_code=303)


# --------------------------------------------------------------------------
# F19 - authenticity signals
# --------------------------------------------------------------------------
@router.post("/documents/{document_id}/versions/{version_id}/authenticity")
def rerun_signals(document_id: str, version_id: str, request: Request,
                  db: Session = Depends(get_db), user: User = Depends(require_user),
                  tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    roles.require(user, roles.DOCUMENTS_WRITE)
    version = db.get(DocumentVersion, version_id)
    if version is None or version.tenant_id != tenant.id:
        raise HTTPException(404, "Version not found")
    signals = authenticity.evaluate(db, version, actor=user)
    db.commit()
    flash(request, f"{len(signals)} signal(s) found. Signals never change an assurance "
                   "level — only a completed verification does.",
          "warn" if signals else "ok")
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@router.post("/authenticity/{signal_id}/resolve")
def resolve_signal(signal_id: str, request: Request, outcome: str = Form(...),
                   note: str = Form(""), db: Session = Depends(get_db),
                   user: User = Depends(require_user),
                   tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    roles.require(user, roles.DOCUMENTS_WRITE)
    signal = db.get(AuthenticitySignal, signal_id)
    if signal is None or signal.tenant_id != tenant.id:
        raise HTTPException(404, "Signal not found")
    authenticity.resolve(db, signal, user, outcome, note or "No note")
    trust.compute(db, tenant, actor=user)
    db.commit()
    flash(request, "Signal resolved.")
    version = db.get(DocumentVersion, signal.document_version_id)
    return RedirectResponse(f"/documents/{version.document_id}", status_code=303)


# --------------------------------------------------------------------------
# F20 - notifications
# --------------------------------------------------------------------------
@router.get("/notifications")
def notification_inbox(request: Request, unread: str = "", kind: str = "",
                       db: Session = Depends(get_db), user: User = Depends(require_user),
                       tenant: Tenant = Depends(require_tenant)):
    rows = notifications.inbox(db, tenant.id, user.id,
                               unread_only=unread == "1", kind=kind)
    preferences = {p.kind: p for p in db.scalars(select(NotificationPreference).where(
        NotificationPreference.user_id == user.id)).all()}
    return page(request, "notifications.html", user, tenant, db, page_title="Notifications",
                crumb="Operate", nav="notifications", rows=rows, unread=unread, kind=kind,
                kinds=notifications.KINDS, preferences=preferences)


@router.post("/notifications/read")
def mark_notifications_read(request: Request, notification_id: str = Form(""),
                            db: Session = Depends(get_db),
                            user: User = Depends(require_user),
                            tenant: Tenant = Depends(require_tenant)):
    notifications.mark_read(db, tenant.id, user.id, notification_id or None)
    db.commit()
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/preferences/{kind}")
def set_preference(kind: str, request: Request, channel: str = Form("inbox"),
                   db: Session = Depends(get_db), user: User = Depends(require_user),
                   tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    preference = db.scalar(select(NotificationPreference).where(and_(
        NotificationPreference.user_id == user.id, NotificationPreference.kind == kind)))
    if preference is None:
        preference = NotificationPreference(tenant_id=tenant.id, user_id=user.id, kind=kind)
        db.add(preference)
    preference.channel = channel
    db.commit()
    flash(request, f"{notifications.KINDS.get(kind, ('', set(), kind))[2]} set to {channel}.")
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/sweep")
def run_sweep(request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    result = notifications.sweep(db, tenant)
    flash(request, f"Sweep complete: {result['created']} new notification(s), "
                   f"{result.get('sent', 0)} delivered.")
    return RedirectResponse("/notifications", status_code=303)


# --------------------------------------------------------------------------
# F18 - control summary
# --------------------------------------------------------------------------
@router.get("/summary")
def control_summary_view(request: Request, db: Session = Depends(get_db),
                         user: User = Depends(require_user),
                         tenant: Tenant = Depends(require_tenant)):
    summary = db.scalar(select(ControlSummary).where(ControlSummary.tenant_id == tenant.id)
                        .order_by(desc(ControlSummary.generated_at)).limit(1))
    stale = False
    if summary:
        stale = summary.evidence_fingerprint != _evidence_fingerprint(db, tenant)
    return page(request, "summary.html", user, tenant, db, page_title="Control summary",
                crumb="Credentials", nav="summary", summary=summary, stale=stale,
                ai_status=ai.status(db))


def _evidence_fingerprint(db: Session, tenant: Tenant) -> str:
    facts, _ = _facts(db, tenant)
    import hashlib
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()


def _facts(db: Session, tenant: Tenant) -> tuple[list[dict], list[str]]:
    rows = db.execute(
        select(Document, DocumentType)
        .join(DocumentType, Document.document_type_id == DocumentType.id)
        .where(and_(Document.tenant_id == tenant.id,
                    Document.deleted_at.is_(None)))).all()
    facts = []
    for document, dtype in rows:
        version = (db.get(DocumentVersion, document.active_version_id)
                   if document.active_version_id else None)
        if version is None:
            continue
        facts.append({"code": dtype.code, "name": document.title,
                      "assurance": version.assurance_level,
                      "status": document.derived_status,
                      "expiry": version.expiry_date.isoformat() if version.expiry_date else None,
                      "scope": (version.scope_statement or "")[:200],
                      "cover": version.cover_amount})
    facts.sort(key=lambda f: f["code"])
    gaps = [g["name"] for g in reqsvc.gap_summary(db, tenant)[:10]]
    return facts, gaps


@router.post("/summary/generate")
def generate_summary(request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_user),
                     tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    roles.require(user, roles.PROFILE_WRITE)
    facts, gaps = _facts(db, tenant)
    if not facts:
        flash(request, "Nothing to summarise yet — add a credential first.", "info")
        return RedirectResponse("/summary", status_code=303)

    org = db.get(Organisation, tenant.organisation_id)
    result = ai.control_summary(db, tenant.id, org_name=org.legal_name,
                                facts=facts, gaps=gaps)
    previous = db.scalar(select(ControlSummary).where(
        ControlSummary.tenant_id == tenant.id)
        .order_by(desc(ControlSummary.generated_at)).limit(1))
    summary = ControlSummary(
        tenant_id=tenant.id, body=result["body"], source=result["source"],
        evidence_fingerprint=result["fingerprint"],
        published=previous.published if previous else False)
    db.add(summary)
    audit.record(db, tenant_id=tenant.id, action="control_summary.generated",
                 actor_id=user.id, actor_label=user.email,
                 object_type="control_summary", object_id=summary.id,
                 payload={"source": result["source"], "facts": len(facts)})
    db.commit()
    flash(request, f"Summary generated from {len(facts)} credentials "
                   f"({'model' if result['source'] == 'model' else 'templates, no model key set'}).")
    return RedirectResponse("/summary", status_code=303)


@router.post("/summary/publish")
def publish_summary(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_user),
                    tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    roles.require(user, roles.PROFILE_WRITE)
    summary = db.scalar(select(ControlSummary).where(ControlSummary.tenant_id == tenant.id)
                        .order_by(desc(ControlSummary.generated_at)).limit(1))
    if summary is None:
        return RedirectResponse("/summary", status_code=303)
    summary.published = not summary.published
    audit.record(db, tenant_id=tenant.id,
                 action="control_summary.published" if summary.published
                 else "control_summary.unpublished",
                 actor_id=user.id, actor_label=user.email,
                 object_type="control_summary", object_id=summary.id)
    db.commit()
    flash(request, "Summary is now on your public page." if summary.published
          else "Summary removed from your public page.")
    return RedirectResponse("/summary", status_code=303)


# --------------------------------------------------------------------------
# F22 - Trust Index
# --------------------------------------------------------------------------
@router.get("/trust")
def trust_view(request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    score = trust.latest(db, tenant.id) or trust.compute(db, tenant, actor=user)
    db.commit()
    domains = []
    for domain in sorted(score.domains, key=lambda d: -d.weight):
        try:
            detail = json.loads(domain.detail_json or "[]")
        except ValueError:
            detail = []
        domains.append({"row": domain, "detail": detail})
    modifiers = json.loads(score.modifiers_json or "[]")
    return page(request, "trust.html", user, tenant, db, page_title="Trust Index",
                crumb="Credentials", nav="trust", score=score, domains=domains,
                modifiers=modifiers, history=trust.history(db, tenant.id),
                action=trust.next_best_action(db, tenant, score),
                band_tone=trust.band_tone(score.band))


@router.post("/trust/recompute")
def recompute_trust(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_user),
                    tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    score = trust.compute(db, tenant, actor=user)
    db.commit()
    flash(request, f"Trust Index {score.composite:.0f} ({score.band}), "
                   f"algorithm v{score.algorithm_version}.")
    return RedirectResponse("/trust", status_code=303)


@router.post("/trust/publish")
def publish_trust(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_user),
                  tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    roles.require(user, roles.PROFILE_WRITE)
    score = trust.latest(db, tenant.id)
    if score is None:
        return RedirectResponse("/trust", status_code=303)
    score.published = not score.published
    audit.record(db, tenant_id=tenant.id,
                 action="trust.published" if score.published else "trust.unpublished",
                 actor_id=user.id, actor_label=user.email,
                 object_type="trust_score", object_id=score.id)
    db.commit()
    flash(request, "Band published on your public page. The number stays private."
          if score.published else "Trust Index removed from your public page.")
    return RedirectResponse("/trust", status_code=303)


# --------------------------------------------------------------------------
# F21 - questionnaires
# --------------------------------------------------------------------------
@router.get("/questionnaires")
def questionnaire_list(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_user),
                       tenant: Tenant = Depends(require_tenant)):
    rows = db.scalars(select(Questionnaire).where(Questionnaire.tenant_id == tenant.id)
                      .order_by(desc(Questionnaire.created_at))).all()
    clients = db.scalars(select(ClientAccount).where(and_(
        ClientAccount.tenant_id == tenant.id, ClientAccount.status == "active"))).all()
    banked = db.scalar(select(AnswerBank).where(AnswerBank.tenant_id == tenant.id))
    bank_size = len(db.scalars(select(AnswerBank.id).where(
        AnswerBank.tenant_id == tenant.id)).all())
    return page(request, "questionnaires.html", user, tenant, db,
                page_title="Questionnaires", crumb="Disclosure", nav="questionnaires",
                rows=rows, clients=clients, bank_size=bank_size, ai_status=ai.status(db))


@router.post("/questionnaires")
async def upload_questionnaire(request: Request, file: UploadFile = File(...),
                               name: str = Form(""), client_id: str = Form(""),
                               db: Session = Depends(get_db),
                               user: User = Depends(require_user),
                               tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    roles.require(user, roles.REQUIREMENTS_RUN)
    data = await file.read()
    rows = qsvc.parse(data, file.filename or "questions.txt")
    if not rows:
        flash(request, "No questions found. Use one question per line, or a CSV with a "
                       "question column.", "bad")
        return RedirectResponse("/questionnaires", status_code=303)

    record = Questionnaire(
        tenant_id=tenant.id, name=name.strip() or (file.filename or "Questionnaire"),
        client_id=client_id or None,
        source_format=(file.filename or "txt").rsplit(".", 1)[-1].lower(),
        source_filename=file.filename)
    db.add(record)
    db.flush()
    qsvc.build(db, tenant, record, rows, actor=user)
    db.commit()
    flash(request, f"{record.item_count} questions read. {record.answered_count} drafted "
                   f"from evidence you hold, {record.gap_count} could not be answered — "
                   "those are your gaps.", "ok" if record.answered_count else "warn")
    return RedirectResponse(f"/questionnaires/{record.id}", status_code=303)


@router.get("/questionnaires/{questionnaire_id}")
def questionnaire_detail(questionnaire_id: str, request: Request,
                         db: Session = Depends(get_db), user: User = Depends(require_user),
                         tenant: Tenant = Depends(require_tenant)):
    record = db.get(Questionnaire, questionnaire_id)
    if record is None or record.tenant_id != tenant.id:
        raise HTTPException(404, "Questionnaire not found")
    items = []
    for item in record.items:
        try:
            citations = json.loads(item.citations_json or "[]")
        except ValueError:
            citations = []
        items.append({"item": item, "citations": citations})
    client = db.get(ClientAccount, record.client_id) if record.client_id else None
    return page(request, "questionnaire_detail.html", user, tenant, db,
                page_title=record.name, crumb="Disclosure · Questionnaires",
                nav="questionnaires", record=record, items=items, client=client)


@router.post("/questionnaires/{questionnaire_id}/items/{item_id}")
def update_item(questionnaire_id: str, item_id: str, request: Request,
                answer: str = Form(""), action: str = Form("save"),
                db: Session = Depends(get_db), user: User = Depends(require_user),
                tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    roles.require(user, roles.REQUIREMENTS_RUN)
    record = db.get(Questionnaire, questionnaire_id)
    item = db.get(QuestionnaireItem, item_id)
    if record is None or record.tenant_id != tenant.id or item is None:
        raise HTTPException(404, "Not found")
    item.answer = answer.strip() or None
    if action == "approve" and item.answer:
        qsvc.approve(db, tenant, item, user)
        flash(request, "Approved and added to your answer bank — the next questionnaire "
                       "asking this will be pre-filled.")
    else:
        item.status = "drafted" if item.answer else "no_evidence"
    record.answered_count = sum(1 for i in record.items
                                if i.status in {"drafted", "approved"})
    record.gap_count = record.item_count - record.answered_count
    db.commit()
    return RedirectResponse(f"/questionnaires/{questionnaire_id}", status_code=303)


@router.get("/questionnaires/{questionnaire_id}/export")
def export_questionnaire(questionnaire_id: str, request: Request,
                         db: Session = Depends(get_db), user: User = Depends(require_user),
                         tenant: Tenant = Depends(require_tenant)):
    record = db.get(Questionnaire, questionnaire_id)
    if record is None or record.tenant_id != tenant.id:
        raise HTTPException(404, "Questionnaire not found")
    body = qsvc.export_csv(db, record)
    audit.record(db, tenant_id=tenant.id, action="questionnaire.exported",
                 actor_id=user.id, actor_label=user.email,
                 object_type="questionnaire", object_id=record.id,
                 payload={"items": record.item_count})
    db.commit()
    filename = f"{record.name.replace(' ', '-').lower()[:40]}-answers.csv"
    return PlainTextResponse(body, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})
