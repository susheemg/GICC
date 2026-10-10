"""MCP server - GICC-01 F13, targeting the 2026-07-28 specification.

Implemented here:
  * stateless core - no session state, any process can serve any request
  * Mcp-Method / Mcp-Name routing headers, rejected when they disagree with
    the body
  * ttlMs / cacheScope on list and resource results, never "shared" for
    tenant-scoped data
  * MRTR: mutating and NDA-gated calls return resultType "input_required"
    with an echoed requestState, resumed via inputResponses
  * OAuth-style bearer auth; the server behaves as a resource server and
    intersects token scopes with the grant behind the key

Deliberately not implemented: sampling, notifications, server-initiated
streams. They are not needed for a read-mostly credential service.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from ..config import APP_VERSION, MCP_PROTOCOL_VERSION
from ..db import get_db
from ..models import (AccessGrant, AgentCallLog, ApiClient, CertificateGroup, Document,
                      DocumentVersion, Organisation, RequirementProfile, Tenant,
                      VerificationCheck, now)
from ..security import api_client_from_request, has_scope
from ..services import access, audit, requirements as reqsvc

router = APIRouter(tags=["MCP"])

LIST_TTL_MS = 60_000
RESOURCE_TTL_MS = 30_000

TOOLS = [
    {"name": "get_supplier_profile",
     "title": "Get supplier profile",
     "description": "Legal identity, registration identifiers and readiness score for the supplier this key is bound to.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "list_documents",
     "title": "List credentials",
     "description": "Credentials visible to this key, with status, expiry and assurance level.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["valid", "expiring_soon", "expired", "suspended", "missing"]},
         "domain": {"type": "string", "description": "Three-letter catalogue domain, e.g. SEC"},
     }, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_document_metadata",
     "title": "Get credential detail",
     "description": "Full metadata and verification record for one credential, including version history.",
     "inputSchema": {"type": "object", "properties": {
         "document_id": {"type": "string"}}, "required": ["document_id"],
         "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_document_content",
     "title": "Get credential content",
     "description": "Extracted, watermarked summary of a credential. Requires confidentiality acceptance when the group is NDA-gated. Never returns raw special-category files.",
     "inputSchema": {"type": "object", "properties": {
         "document_id": {"type": "string"}}, "required": ["document_id"],
         "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "verify_credential",
     "title": "Verify a credential",
     "description": "Latest verification result for a credential: source, method, outcome and whether the connector was simulated.",
     "inputSchema": {"type": "object", "properties": {
         "document_id": {"type": "string"}}, "required": ["document_id"],
         "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "list_expiries",
     "title": "List upcoming expiries",
     "description": "Credentials expiring within a window, ranked by days remaining and criticality.",
     "inputSchema": {"type": "object", "properties": {
         "within_days": {"type": "integer", "minimum": 1, "maximum": 730, "default": 90}},
         "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "check_requirements",
     "title": "Check against a requirement profile",
     "description": "Evaluates the supplier against a requirement profile and returns per-item conformance citing the exact version read.",
     "inputSchema": {"type": "object", "properties": {
         "requirement_profile_id": {"type": "string"},
         "profile_name": {"type": "string", "description": "Alternative to the id"}},
         "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "request_access",
     "title": "Request access to a group",
     "description": "Raises an access request against a certificate group. Mutating: requires confirmation before it is created.",
     "inputSchema": {"type": "object", "properties": {
         "group_code": {"type": "string"}, "purpose": {"type": "string"}},
         "required": ["group_code", "purpose"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
]

RESOURCES = [
    {"uri": "gicc://supplier/profile", "name": "Supplier profile",
     "description": "Identity and readiness snapshot", "mimeType": "application/json"},
    {"uri": "gicc://supplier/documents", "name": "Credential register",
     "description": "All credentials visible to this key", "mimeType": "application/json"},
    {"uri": "gicc://catalogue/types", "name": "Document type catalogue",
     "description": "The 182-type catalogue", "mimeType": "application/json"},
]

PROMPTS = [
    {"name": "supplier_due_diligence_brief",
     "title": "Supplier due diligence brief",
     "description": "Summarise this supplier's credential position for a third-party risk review.",
     "arguments": [{"name": "focus", "description": "Optional area to emphasise, e.g. security",
                    "required": False}]},
    {"name": "expiry_watchlist_summary",
     "title": "Expiry watchlist",
     "description": "List what is about to lapse and what the buyer should chase.",
     "arguments": [{"name": "within_days", "description": "Window in days", "required": False}]},
]


def _err(request_id, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": code, "message": message}}, status_code=status)


def _content(payload: dict | list) -> dict:
    text = json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "structuredContent": payload}


def _state(data: dict) -> str:
    return base64.b64encode(json.dumps(data, default=str).encode()).decode()


def _unstate(token: str) -> dict:
    try:
        return json.loads(base64.b64decode(token.encode()).decode())
    except Exception:
        return {}


def _doc_row(db: Session, document: Document) -> dict:
    version = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
    return {
        "document_id": document.id, "type_code": document.document_type.code,
        "name": document.document_type.name, "domain": document.document_type.domain_name,
        "status": document.derived_status,
        "assurance_level": version.assurance_level if version else None,
        "assurance_meaning": {"L0": "self-declared", "L1": "source-linked",
                              "L2": "human-verified", "L3": "machine-verified",
                              "L4": "cryptographically verifiable"}.get(
            version.assurance_level if version else "L0"),
        "expiry_date": version.expiry_date if version else None,
        "certificate_number": version.certificate_number if version else None,
        "issuer": version.issuer_name if version else None,
    }


@router.get("/mcp")
def mcp_discovery():
    """Server metadata, so a client can inspect the endpoint before connecting."""
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "serverInfo": {"name": "gicc", "title": "Global Identity & Certificates Center",
                       "version": APP_VERSION},
        "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
        "authorization": {"type": "bearer",
                          "description": "Send the GICC API key as Authorization: Bearer or X-API-Key"},
        "transport": "streamable-http",
        "instructions": ("Read-mostly credential service. Assurance levels L0-L4 tell you how "
                         "strongly a credential is evidenced; treat L0 and L1 as unverified "
                         "claims. Results flagged simulated=true came from a stand-in connector."),
    }


@router.post("/mcp")
async def mcp_endpoint(request: Request, db: Session = Depends(get_db)):
    started = time.time()
    try:
        body = await request.json()
    except Exception:
        return _err(None, -32700, "Parse error: body is not valid JSON")

    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    header_method = request.headers.get("mcp-method")
    header_name = request.headers.get("mcp-name")
    if header_method and header_method != method:
        return _err(request_id, -32600,
                    "Mcp-Method header does not match the request body")
    if header_name and method in {"tools/call", "resources/read", "prompts/get"}:
        body_name = params.get("name") or params.get("uri")
        if body_name and header_name != body_name:
            return _err(request_id, -32600,
                        "Mcp-Name header does not match the request body")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False},
                             "resources": {"listChanged": False},
                             "prompts": {"listChanged": False}},
            "serverInfo": {"name": "gicc", "version": APP_VERSION},
            "instructions": mcp_discovery()["instructions"]}}

    if method in {"notifications/initialized", "ping"}:
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    client = api_client_from_request(request, db)
    if client is None:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id,
             "error": {"code": -32001, "message": "Unauthorized: supply a GICC API key"}},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="gicc"'})

    tenant = db.get(Tenant, client.tenant_id)
    result: dict | None = None
    status_label = "ok"

    try:
        if method == "tools/list":
            result = {"tools": TOOLS, "ttlMs": LIST_TTL_MS, "cacheScope": "session"}
        elif method == "resources/list":
            result = {"resources": RESOURCES, "ttlMs": LIST_TTL_MS, "cacheScope": "session"}
        elif method == "prompts/list":
            result = {"prompts": PROMPTS, "ttlMs": LIST_TTL_MS, "cacheScope": "session"}
        elif method == "resources/read":
            result = _read_resource(db, client, tenant, params.get("uri", ""))
        elif method == "prompts/get":
            result = _get_prompt(db, client, tenant, params)
        elif method == "tools/call":
            result = _call_tool(db, client, tenant, params, request)
            if result.get("resultType") == "input_required":
                status_label = "input_required"
        else:
            return _err(request_id, -32601, f"Method not found: {method}")
    except PermissionError as exc:
        status_label = "forbidden"
        return _err(request_id, -32003, str(exc))
    except LookupError as exc:
        status_label = "not_found"
        return _err(request_id, -32004, str(exc))

    payload = json.dumps(result, default=str)
    db.add(AgentCallLog(tenant_id=tenant.id, api_client_id=client.id, protocol="mcp",
                        mcp_method=method, mcp_name=params.get("name") or params.get("uri"),
                        scopes=client.scopes, result_status=status_label,
                        bytes_out=len(payload),
                        latency_ms=int((time.time() - started) * 1000)))
    client.last_used_at = now()
    db.commit()
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


# --------------------------------------------------------------------------
def _read_resource(db: Session, client: ApiClient, tenant: Tenant, uri: str) -> dict:
    if uri == "gicc://supplier/profile":
        if not has_scope(client, "profile:read"):
            raise PermissionError("This key is missing the profile:read scope")
        org = db.get(Organisation, tenant.organisation_id)
        data = {"legal_name": org.legal_name, "country": org.country,
                "registry_number": org.registry_number, "lei": org.lei,
                "entity_verification": org.verification_state,
                "readiness_score": tenant.readiness_score,
                "lifecycle_state": tenant.lifecycle_state}
    elif uri == "gicc://supplier/documents":
        if not has_scope(client, "documents:read"):
            raise PermissionError("This key is missing the documents:read scope")
        data = [_doc_row(db, d) for d in access.scoped_documents(db, client)]
    elif uri == "gicc://catalogue/types":
        from ..models import DocumentType
        data = [{"code": t.code, "name": t.name, "domain": t.domain_name,
                 "sensitivity": t.sensitivity, "criticality": t.criticality}
                for t in db.scalars(select(DocumentType).order_by(DocumentType.code)).all()]
    else:
        raise LookupError(f"Unknown resource: {uri}")

    return {"contents": [{"uri": uri, "mimeType": "application/json",
                          "text": json.dumps(data, indent=2, default=str)}],
            "ttlMs": RESOURCE_TTL_MS,
            "cacheScope": "session"}  # never "shared" - this is tenant data


def _get_prompt(db: Session, client: ApiClient, tenant: Tenant, params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    documents = [_doc_row(db, d) for d in access.scoped_documents(db, client)]
    if name == "supplier_due_diligence_brief":
        focus = args.get("focus", "overall credential position")
        text = (f"Write a third-party risk brief on {tenant.name}, focusing on {focus}. "
                "Use only the credential data below. State clearly where assurance is L0 or "
                "L1, because those are unverified claims rather than verified facts. "
                "Flag anything expired or expiring within 90 days.\n\n"
                + json.dumps(documents, indent=2, default=str))
    elif name == "expiry_watchlist_summary":
        window = int(args.get("within_days", 90))
        soon = [d for d in documents if d["expiry_date"] and
                (d["expiry_date"] - date.today()).days <= window]
        text = (f"Summarise what lapses at {tenant.name} within {window} days and what the "
                "buyer should chase, ordered by urgency.\n\n"
                + json.dumps(soon, indent=2, default=str))
    else:
        raise LookupError(f"Unknown prompt: {name}")
    return {"description": name,
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            "ttlMs": RESOURCE_TTL_MS, "cacheScope": "session"}


def _call_tool(db: Session, client: ApiClient, tenant: Tenant, params: dict,
               request: Request) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    responses = params.get("inputResponses") or {}
    state = _unstate(params.get("requestState", "")) if params.get("requestState") else {}

    def audit_call(action: str, payload: dict | None = None, object_id: str | None = None):
        audit.record(db, tenant_id=tenant.id, action=action, actor_kind="agent",
                     actor_id=client.id, actor_label=f"mcp:{client.name}",
                     object_id=object_id, grant_id=client.grant_id,
                     ip=request.client.host if request.client else None, payload=payload)

    if name == "get_supplier_profile":
        if not has_scope(client, "profile:read"):
            raise PermissionError("This key is missing the profile:read scope")
        org = db.get(Organisation, tenant.organisation_id)
        audit_call("mcp.profile_read")
        return _content({"legal_name": org.legal_name, "country": org.country,
                         "registry_number": org.registry_number, "lei": org.lei,
                         "entity_verification": org.verification_state,
                         "readiness_score": tenant.readiness_score,
                         "lifecycle_state": tenant.lifecycle_state,
                         "website": org.website, "employees": org.employees})

    if name == "list_documents":
        if not has_scope(client, "documents:read"):
            raise PermissionError("This key is missing the documents:read scope")
        rows = [_doc_row(db, d) for d in access.scoped_documents(db, client)]
        if args.get("status"):
            rows = [r for r in rows if r["status"] == args["status"]]
        if args.get("domain"):
            rows = [r for r in rows if r["type_code"].startswith(args["domain"])]
        audit_call("mcp.documents_listed", {"returned": len(rows)})
        return _content({"count": len(rows), "documents": rows})

    if name in {"get_document_metadata", "verify_credential", "get_document_content"}:
        if not has_scope(client, "documents:read"):
            raise PermissionError("This key is missing the documents:read scope")
        allowed = {d.id: d for d in access.scoped_documents(db, client)}
        document = allowed.get(args.get("document_id"))
        if document is None:
            raise LookupError("Document not found, or outside the scope of this key")

        version = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
        check = db.scalar(select(VerificationCheck).where(
            VerificationCheck.document_version_id == (version.id if version else "-"))
            .order_by(desc(VerificationCheck.checked_at)).limit(1))

        if name == "verify_credential":
            audit_call("mcp.verification_read", object_id=document.id)
            return _content({
                "document_id": document.id, "type_code": document.document_type.code,
                "assurance_level": version.assurance_level if version else "L0",
                "verification": {
                    "method": check.method, "source": check.source_code,
                    "result": check.result, "checked_at": check.checked_at,
                    "simulated": check.simulated, "detail": check.detail}
                if check else None,
                "authentication_link": version.verification_url if version else None})

        if name == "get_document_metadata":
            versions = db.scalars(select(DocumentVersion).where(
                DocumentVersion.document_id == document.id)
                .order_by(desc(DocumentVersion.version_no))).all()
            audit_call("mcp.document_read", object_id=document.id)
            return _content({
                **_doc_row(db, document),
                "sensitivity": document.document_type.sensitivity,
                "scope_statement": version.scope_statement if version else None,
                "cover_amount": version.cover_amount if version else None,
                "history": [{"version_no": v.version_no, "status": v.status,
                             "effective_from": v.effective_from, "expiry_date": v.expiry_date,
                             "assurance_level": v.assurance_level,
                             "change_reason": v.change_reason} for v in versions]})

        # get_document_content - NDA gate via MRTR, and never raw S-class files
        if document.document_type.sensitivity == "S":
            raise PermissionError(
                "Special-category documents are not available through the machine channel. "
                "Request a named-user grant in the web interface instead.")

        grant = db.get(AccessGrant, client.grant_id) if client.grant_id else None
        gated = bool(grant and grant.nda_required)
        if gated and not responses.get("confidentiality"):
            return {"resultType": "input_required",
                    "inputRequests": {"confidentiality": {
                        "type": "elicitation",
                        "message": (f"{tenant.name} shares this document in confidence. "
                                    "Confirm you accept the confidentiality terms and that "
                                    "your access will be logged and shared with the supplier."),
                        "schema": {"type": "boolean"}}},
                    "requestState": _state({"tool": name, "document_id": document.id})}

        watermark = f"Disclosed to {client.name} via GICC · {now():%d %b %Y %H:%M} UTC"
        audit_call("mcp.content_disclosed", {"document": document.title,
                                             "nda_accepted": bool(responses.get("confidentiality"))},
                   object_id=document.id)
        return _content({
            "document_id": document.id, "type_code": document.document_type.code,
            "title": document.title, "watermark": watermark,
            "extract": {
                "issuer": version.issuer_name if version else None,
                "certificate_number": version.certificate_number if version else None,
                "holder": version.holder_name if version else None,
                "scope": version.scope_statement if version else None,
                "issued": version.issue_date if version else None,
                "expires": version.expiry_date if version else None,
                "cover_amount": version.cover_amount if version else None,
                "assurance_level": version.assurance_level if version else "L0",
                "content_hash": version.content_hash if version else None},
            "note": ("Text extract only. The source file is not returned over the machine "
                     "channel; use the web share link for the original.")})

    if name == "list_expiries":
        if not has_scope(client, "documents:read"):
            raise PermissionError("This key is missing the documents:read scope")
        window = int(args.get("within_days", 90))
        rows = []
        for document in access.scoped_documents(db, client):
            version = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
            if version is None or version.expiry_date is None:
                continue
            days = (version.expiry_date - date.today()).days
            if days <= window:
                rows.append({**_doc_row(db, document), "days_remaining": days,
                             "criticality": document.document_type.criticality})
        rows.sort(key=lambda r: (r["days_remaining"], -r["criticality"]))
        audit_call("mcp.expiries_listed", {"within_days": window, "returned": len(rows)})
        return _content({"within_days": window, "count": len(rows), "items": rows})

    if name == "check_requirements":
        if not has_scope(client, "requirements:read"):
            raise PermissionError("This key is missing the requirements:read scope")
        profile = None
        if args.get("requirement_profile_id"):
            profile = db.get(RequirementProfile, args["requirement_profile_id"])
        elif args.get("profile_name"):
            profile = db.scalar(select(RequirementProfile).where(
                RequirementProfile.name.ilike(f"%{args['profile_name']}%")))
        else:
            profiles = db.scalars(select(RequirementProfile)).all()
            return _content({"available_profiles": [
                {"id": p.id, "name": p.name, "sector": p.sector} for p in profiles],
                "note": "Call again with requirement_profile_id or profile_name."})
        if profile is None:
            raise LookupError("Requirement profile not found")

        assessment = reqsvc.evaluate(db, tenant, profile, actor=None)
        audit_call("mcp.requirements_evaluated", {"profile": profile.name,
                                                  "score": assessment.score},
                   object_id=assessment.id)
        return _content({
            "profile": profile.name, "score": assessment.score,
            "met": assessment.met_count, "gaps": assessment.gap_count,
            "engine_version": assessment.engine_version,
            "results": [{"type_code": r.type_code, "name": r.type_name,
                         "outcome": r.outcome, "cited_version_id": r.cited_version_id,
                         "explanation": r.explanation} for r in assessment.results]})

    if name == "request_access":
        if not has_scope(client, "grants:request"):
            raise PermissionError("This key is missing the grants:request scope")
        group = db.scalar(select(CertificateGroup).where(and_(
            CertificateGroup.tenant_id == tenant.id,
            CertificateGroup.code == args.get("group_code"))))
        if group is None:
            raise LookupError("No certificate group with that code")

        if not responses.get("confirm"):
            return {"resultType": "input_required",
                    "inputRequests": {"confirm": {
                        "type": "elicitation",
                        "message": (f"Raise an access request to {tenant.name} for the "
                                    f"'{group.name}' group? The supplier will see who asked "
                                    "and why."),
                        "schema": {"type": "boolean"}}},
                    "requestState": _state({"tool": name, "group_id": group.id,
                                            "purpose": args.get("purpose")})}

        from ..models import AccessRequest
        grant = db.get(AccessGrant, client.grant_id) if client.grant_id else None
        client_id = grant.client_id if grant else None
        if client_id is None:
            raise PermissionError("This key is not bound to a client account, so it cannot "
                                  "raise an access request")
        access_request = AccessRequest(
            tenant_id=tenant.id, client_id=client_id, scope_kind="group",
            scope_id=group.id, purpose=args.get("purpose"), status="open",
            contact_email=f"agent:{client.name}")
        db.add(access_request)
        db.flush()
        audit_call("access_request.raised", {"group": group.name,
                                             "purpose": args.get("purpose")},
                   object_id=access_request.id)
        from ..models import ClientAccount
        from ..services import notifications
        buyer = db.get(ClientAccount, client_id)
        notifications.on_access_request(db, tenant, access_request, group.name,
                                        buyer.name if buyer else "A client")
        return _content({"access_request_id": access_request.id, "status": "open",
                         "group": group.name,
                         "note": "The supplier decides within two business days."})

    raise LookupError(f"Unknown tool: {name}")
