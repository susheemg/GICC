"""The buyer portal.

Kept under its own `/buyer` prefix with its own base template. A buyer's mental
model is a supply base; a supplier's is a credential register. Sharing one
navigation would have made both worse.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (AccessRequest, Document, DocumentVersion, Organisation,
                      SupplierLink, Tenant, User)
from ..security import require_tenant, require_user
from ..services import buyers
from ..services import documents as docsvc

router = APIRouter()


def require_buyer(user: User = Depends(require_user),
                  db: Session = Depends(get_db)) -> Tenant:
    """A workspace reaches the buyer portal only if it was set up to buy. The
    check is on the tenant, not the user, because the capability belongs to the
    company."""
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(404, "Workspace not found")
    if tenant.kind not in {"buyer", "both"}:
        raise HTTPException(status_code=303, detail="not a buyer workspace",
                            headers={"Location": "/dashboard"})
    return tenant


def buyer_page(request: Request, template: str, user: User, buyer: Tenant, db: Session,
         **ctx):
    from ..main import render
    base = {"user": user, "buyer": buyer, "tenant": buyer,
            "counts": buyers.summary(db, buyer.id)}
    base.update(ctx)
    return render(request, template, base)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
@router.get("/register/buyer")
def register_form(request: Request):
    from ..main import render
    return render(request, "buyer_register.html", {})


@router.post("/register/buyer")
def register(request: Request, legal_name: str = Form(...), country: str = Form("GB"),
             registry_number: str = Form(""), full_name: str = Form(...),
             email: str = Form(...), password: str = Form(...),
             db: Session = Depends(get_db)):
    from ..main import flash
    from ..seed import provision_tenant

    email = email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        flash(request, "An account already exists for that email. Sign in instead.",
              "warn")
        return RedirectResponse("/login", status_code=303)
    if len(password) < 8:
        flash(request, "Choose a password of at least 8 characters.", "bad")
        return RedirectResponse("/register/buyer", status_code=303)

    tenant, user = provision_tenant(
        db, legal_name=legal_name.strip(), country=country,
        registry_number=registry_number.strip() or None, email=email,
        full_name=full_name.strip(), password=password)
    # A buyer is a company with an identity of its own. Suppliers are entitled
    # to know who is asking, so the buyer's own profile is a real one and it can
    # be turned into a selling workspace later without a second account.
    tenant.kind = "buyer"
    db.commit()
    request.session["user_id"] = user.id
    flash(request, f"Buyer workspace created for {tenant.name}. Search for a supplier "
                   "to get started.")
    return RedirectResponse("/buyer", status_code=303)


# --------------------------------------------------------------------------
# Home and search
# --------------------------------------------------------------------------
@router.get("/buyer")
def home(request: Request, db: Session = Depends(get_db),
         user: User = Depends(require_user), buyer: Tenant = Depends(require_buyer)):
    suppliers = buyers.supplier_list(db, buyer.id)
    requests_open = [r for r in buyers.requests_for(db, buyer.id)
                     if r.status == "open"]
    return buyer_page(request, "buyer_home.html", user, buyer, db,
                page_title="Supply base", nav="home",
                suppliers=suppliers[:8], open_requests=requests_open)


@router.get("/buyer/search")
def search(request: Request, q: str = "", country: str = "",
           db: Session = Depends(get_db), user: User = Depends(require_user),
           buyer: Tenant = Depends(require_buyer)):
    results, limited = [], None
    if q.strip():
        try:
            results = buyers.search(db, buyer=buyer, user=user, query=q,
                                    country=country)
            db.commit()
        except buyers.SearchLimited as exc:
            limited = str(exc)
    return buyer_page(request, "buyer_search.html", user, buyer, db,
                page_title="Find a supplier", nav="search", query=q,
                country=country, results=results, searched=bool(q.strip()),
                limited=limited, min_query=buyers.MIN_QUERY)


@router.get("/buyer/companies/{organisation_id}")
def company(organisation_id: str, request: Request, db: Session = Depends(get_db),
            user: User = Depends(require_user),
            buyer: Tenant = Depends(require_buyer)):
    organisation = db.get(Organisation, organisation_id)
    if organisation is None:
        raise HTTPException(404, "No company at that address")

    buyers.record_view(db, buyer=buyer, user=user, organisation=organisation)
    db.commit()

    entry = buyers.directory_entry(organisation,
                                   tenant=buyers.tenant_for(db, organisation.id))
    link = db.scalar(select(SupplierLink).where(and_(
        SupplierLink.buyer_tenant_id == buyer.id,
        SupplierLink.organisation_id == organisation.id,
        SupplierLink.removed_at.is_(None))))
    return buyer_page(request, "buyer_company.html", user, buyer, db,
                page_title=organisation.legal_name, nav="search", entry=entry,
                link=link)


# --------------------------------------------------------------------------
# The supply base
# --------------------------------------------------------------------------
@router.get("/buyer/suppliers")
def supplier_list(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_user),
                  buyer: Tenant = Depends(require_buyer)):
    return buyer_page(request, "buyer_suppliers.html", user, buyer, db,
                page_title="My suppliers", nav="suppliers",
                suppliers=buyers.supplier_list(db, buyer.id))


@router.post("/buyer/suppliers")
def supplier_add(request: Request, organisation_id: str = Form(...),
                 internal_reference: str = Form(""), category: str = Form(""),
                 criticality: int = Form(3), db: Session = Depends(get_db),
                 user: User = Depends(require_user),
                 buyer: Tenant = Depends(require_buyer)):
    from ..main import flash

    organisation = db.get(Organisation, organisation_id)
    if organisation is None:
        raise HTTPException(404, "No company at that address")
    link = buyers.add_supplier(db, buyer=buyer, user=user, organisation=organisation,
                               internal_reference=internal_reference,
                               category=category, criticality=int(criticality))
    db.commit()
    if link.state == "invited":
        flash(request, f"{organisation.legal_name} added. They have not claimed a "
                       "workspace yet, so ask them to claim their profile before you "
                       "request records.", "warn")
    else:
        flash(request, f"{organisation.legal_name} added to your supply base.")
    return RedirectResponse(f"/buyer/suppliers/{link.id}", status_code=303)


@router.get("/buyer/suppliers/{link_id}")
def supplier_detail(link_id: str, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_user),
                    buyer: Tenant = Depends(require_buyer)):
    link = db.get(SupplierLink, link_id)
    if link is None or link.buyer_tenant_id != buyer.id:
        raise HTTPException(404, "Not in your supply base")

    organisation = db.get(Organisation, link.organisation_id)
    supplier = buyers.tenant_for(db, organisation.id)
    state = buyers.connect_state(db, buyer.id, link)
    db.commit()

    documents = []
    for document in buyers.granted_documents(db, buyer.id, organisation.id):
        version = (db.get(DocumentVersion, document.active_version_id)
                   if document.active_version_id else None)
        documents.append({"document": document, "type": document.document_type,
                          "version": version})
    documents.sort(key=lambda row: row["type"].code)

    history = [r for r in buyers.requests_for(db, buyer.id)
               if supplier is not None and r.tenant_id == supplier.id]
    return buyer_page(request, "buyer_supplier_detail.html", user, buyer, db,
                page_title=organisation.legal_name, nav="suppliers", link=link,
                entry=buyers.directory_entry(organisation, tenant=supplier),
                state=state, documents=documents, history=history,
                public_slug=supplier.slug if supplier else None)


@router.post("/buyer/suppliers/{link_id}/request")
def supplier_request(link_id: str, request: Request, purpose: str = Form(...),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_user),
                     buyer: Tenant = Depends(require_buyer)):
    from ..main import flash

    link = db.get(SupplierLink, link_id)
    if link is None or link.buyer_tenant_id != buyer.id:
        raise HTTPException(404, "Not in your supply base")

    access_request = buyers.request_access(db, buyer=buyer, user=user, link=link,
                                           purpose=purpose)
    db.commit()
    if access_request is None:
        flash(request, "This company has not claimed a workspace yet, so there is "
                       "nobody to answer a request. Send them the claim link and they "
                       "can respond once they are set up.", "warn")
    else:
        flash(request, "Request sent. The supplier decides what to share and how long "
                       "for; you will see the records here once they do.")
    return RedirectResponse(f"/buyer/suppliers/{link_id}", status_code=303)


@router.post("/buyer/suppliers/{link_id}/remove")
def supplier_remove(link_id: str, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_user),
                    buyer: Tenant = Depends(require_buyer)):
    from ..main import flash

    link = db.get(SupplierLink, link_id)
    if link is None or link.buyer_tenant_id != buyer.id:
        raise HTTPException(404, "Not in your supply base")
    buyers.remove_supplier(db, buyer=buyer, user=user, link=link)
    db.commit()
    flash(request, "Removed from your supply base. Any access the supplier granted "
                   "you stays with them to revoke.", "warn")
    return RedirectResponse("/buyer/suppliers", status_code=303)


@router.get("/buyer/requests")
def request_list(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_user),
                 buyer: Tenant = Depends(require_buyer)):
    rows = []
    for access_request in buyers.requests_for(db, buyer.id):
        supplier = db.get(Tenant, access_request.tenant_id)
        rows.append({"request": access_request,
                     "supplier": supplier.name if supplier else "Unknown"})
    return buyer_page(request, "buyer_requests.html", user, buyer, db,
                page_title="Access requests", nav="requests", rows=rows)


@router.get("/buyer/documents/{document_id}")
def document_view(document_id: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_user),
                  buyer: Tenant = Depends(require_buyer)):
    """Readable only if a live grant resolves to it. The check is the shared
    resolver, so this view cannot drift from the share link or the API."""
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    supplier = db.get(Tenant, document.tenant_id)
    organisation_id = supplier.organisation_id if supplier else ""
    permitted = {d.id for d in buyers.granted_documents(db, buyer.id, organisation_id)}
    if document_id not in permitted:
        raise HTTPException(403, "No live grant covers that document")

    versions = db.scalars(select(DocumentVersion).where(
        DocumentVersion.document_id == document.id)
        .order_by(desc(DocumentVersion.version_no))).all()
    active = (db.get(DocumentVersion, document.active_version_id)
              if document.active_version_id else None)
    return buyer_page(request, "buyer_document.html", user, buyer, db,
                page_title=document.title, nav="suppliers", document=document,
                dtype=document.document_type, active=active,
                versions=[v for v in versions if v.status == "active"],
                supplier=supplier)
