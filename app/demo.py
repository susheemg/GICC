"""Synthetic demo data.

Six fictional entities across four sectors, three regions and four lifecycle
states, with roughly two months of backdated history so the dashboards, the
activity log and the read receipts show something real rather than an empty
shell.

Everything here is invented. Company names, people, certificate numbers and
domains are fictional; every domain ends in .example, which is reserved by
RFC 2606 and can never resolve. Set SEED_DEMO=0 for a clean instance.

Deterministic: the random seed is fixed, so two deployments of the same build
produce the same dataset and a demo script stays true.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .config import DEMO_PASSWORD
from .models import (AccessRequest, AgentCallLog, ApiClient, BreakGlassSession,
                     CertificateGroup, ClientAccount, ClientContact, Document,
                     DocumentType, DocumentVersion, Organisation, PlatformAdmin,
                     ShareLink, ShareLinkView, Tenant, User, now)
from .security import hash_password, hash_token, new_share_token
from .services import (access, audit, authenticity, documents as docsvc,
                       notifications, questionnaire as qsvc, requirements as reqsvc,
                       trust, verification)
from .services import ai

RNG = random.Random(20260811)

# --------------------------------------------------------------------------
# Document plans
#   (type_code, certificate_no, issuer, issued_months_ago, valid_months,
#    cover_amount, verification_url)
# A complete row - number, issuer and a resolving link - reaches L3 when the
# connector runs. Leave the number or issuer out and it stops at L1 or L0,
# which is how the ladder shows variety without anyone setting a level by hand.
# --------------------------------------------------------------------------
IAF = "https://www.iafcertsearch.org/"
CH = "https://find-and-update.company-information.service.gov.uk/"
GLEIF = "https://search.gleif.org/"
VIES = "https://ec.europa.eu/taxation_customs/vies/"

NORTHGATE_DOCS = [
    ("LEG-001", "09123456", "Companies House", 84, None, None, CH),
    ("LEG-011", "213800XYZ12345678901", "GLEIF / LOU", 10, 12, None, GLEIF),
    ("TAX-001", "GB312445566", "HM Revenue & Customs", 60, None, None, VIES),
    ("SEC-001", "IS 776541", "BSI Group", 14, 36, None, IAF),
    ("SEC-002", None, "Northgate Data Systems Ltd", 8, 12, None, None),
    ("SEC-008", "SOC2-2026-NG-114", "Fenwick Assurance LLP", 5, 12, None,
     "https://trust.northgate.example/soc2"),
    ("SEC-012", "CE-2025-88213", "IASME Consortium", 8, 12, None,
     "https://iasme.co.uk/verify"),
    ("SEC-017", "PT-2026-0417", "Trailhead Security Ltd", 4, 12, None, None),
    ("SEC-020", None, "Northgate Data Systems Ltd", 7, 12, None, None),
    ("SEC-026", None, "Northgate Data Systems Ltd", 2, 6, None, None),
    ("SEC-027", None, "Northgate Data Systems Ltd", 9, 24, None, None),
    ("QMS-001", "FS 601233", "BSI Group", 20, 36, None, IAF),
    ("QMS-005", "BCMS 774100", "BSI Group", 26, 36, None, IAF),
    ("INS-001", "PL/2026/77120", "Aviva via Marchant Brokers", 3, 12, 10_000_000, None),
    ("INS-002", "EL/2026/77121", "Aviva via Marchant Brokers", 3, 12, 10_000_000, None),
    ("INS-003", "PI/2026/44190", "Hiscox via Marchant Brokers", 3, 12, 5_000_000, None),
    ("INS-004", "CY/2026/10233", "Beazley via Marchant Brokers", 3, 12, 2_000_000, None),
    ("FIN-001", "FY2025-AUD", "Perrin & Hale LLP", 7, 12, None, None),
    ("OWN-001", None, "Northgate Data Systems Ltd", 11, 12, None, None),
    ("OWN-009", None, "Northgate Data Systems Ltd", 18, 24, None, None),
    ("COM-005", None, "Northgate Data Systems Ltd", 13, 12, None, None),
    ("ESG-008", None, "Northgate Data Systems Ltd", 14, 12, None, None),
    ("HSE-001", None, "Northgate Data Systems Ltd", 6, 12, None, None),
    ("TEC-005", "SBOM-NG-2026-04", "Northgate Data Systems Ltd", 1, 6, None,
     "https://trust.northgate.example/sbom/2026-04"),
]

CALDERWELL_DOCS = [
    ("LEG-001", "04887210", "Companies House", 132, None, None, CH),
    ("TAX-001", "GB447120933", "HM Revenue & Customs", 96, None, None, VIES),
    ("TAX-004", "TC-2025-88401", "HM Revenue & Customs", 14, 12, None, None),
    ("INS-001", "PL/2025/31882", "Zurich via Ridgeway Brokers", 14, 12, 10_000_000, None),
    ("INS-002", "EL/2025/31883", "Zurich via Ridgeway Brokers", 14, 12, 10_000_000, None),
    ("INS-003", "PI/2026/91002", "AXA via Ridgeway Brokers", 2, 12, 2_000_000, None),
    ("HSE-001", None, "Calderwell Facilities Group Ltd", 4, 12, None, None),
    ("HSE-002", None, "Calderwell Facilities Group Ltd", 5, 12, None, None),
    ("HSE-005", "CHAS-2025-40218", "CHAS", 13, 12, None, "https://www.chas.co.uk/verify"),
    ("HSE-011", "LOL-2026-1187", "Alecto Inspection Services", 5, 12, None, None),
    ("QMS-001", "FS 220118", "NQA", 30, 36, None, IAF),
    ("QMS-003", "OHS 220119", "NQA", 30, 36, None, IAF),
    ("ESG-008", None, "Calderwell Facilities Group Ltd", 15, 12, None, None),
    ("PPL-006", None, "Calderwell Facilities Group Ltd", 9, 12, None, None),
    ("LIC-012", "CC-2025-77410", "Construction Licensing Board", 18, 24, None, None),
    ("LIC-014", "WCB-2024-3391", "Environment Agency", 30, 36, None, None),
]

AVENTINE_DOCS = [
    ("LEG-001", "NL-58831207", "Kamer van Koophandel", 108, None, None, None),
    ("LEG-011", "724500AVN00PHARMA123", "GLEIF / LOU", 8, 12, None, GLEIF),
    ("TAX-001", "NL861223344B01", "Belastingdienst", 90, None, None, VIES),
    ("LIC-007", "WDA-NL-2024-0881", "Inspectie Gezondheidszorg en Jeugd", 24, 60, None, None),
    ("LIC-008", "GDP-NL-2025-1140", "Inspectie Gezondheidszorg en Jeugd", 15, 36, None, None),
    ("QMS-007", "MD 118422", "TÜV SÜD", 19, 36, None, IAF),
    ("QMS-001", "FS 118420", "TÜV SÜD", 19, 36, None, IAF),
    ("SEC-001", "IS 118421", "TÜV SÜD", 19, 36, None, IAF),
    ("SEC-005", "PIMS 118423", "TÜV SÜD", 11, 36, None, IAF),
    ("SEC-027", None, "Aventine Pharma Solutions BV", 12, 24, None, None),
    ("SEC-028", None, "Aventine Pharma Solutions BV", 16, 24, None, None),
    ("INS-003", "PI/2026/EU/7741", "Allianz", 4, 12, 5_000_000, None),
    ("INS-005", "PROD/2026/EU/7742", "Allianz", 4, 12, 10_000_000, None),
    ("PPL-003", None, "Aventine Pharma Solutions BV", 20, 36, None, None),
    ("ESG-001", "EV-2025-NL-4471", "EcoVadis", 11, 12, None, None),
]

KESTREL_DOCS = [
    ("LEG-004", "FZE-DXB-114872", "Dubai Free Zone Authority", 30, 12, None, None),
    ("LEG-010", None, "Dubai Chamber of Commerce", 10, 12, None, None),
    ("LIC-016", "TOL-AE-2025-3318", "RTA", 16, 60, None, None),
    ("LIC-022", None, "Dubai Customs", 20, 12, None, None),
    ("COM-009", None, "Kestrel Logistics FZE", 22, None, None, None),
    ("INS-007", None, "Oman Insurance", 8, 12, None, None),
    ("INS-001", None, "Oman Insurance", 8, 12, 5_000_000, None),
    ("QMS-001", None, "Unnamed certification body", 25, 36, None, None),
    ("HSE-001", None, "Kestrel Logistics FZE", 14, 12, None, None),
]

HARLOW_DOCS = [
    ("LEG-001", "11902334", "Companies House", 60, None, None, CH),
    ("TAX-001", "GB388110274", "HM Revenue & Customs", 48, None, None, VIES),
    ("LIC-002", "EMI-2024-00913", "Financial Conduct Authority", 27, None, None,
     "https://register.fca.org.uk/"),
    ("SEC-001", "IS 903112", "LRQA", 22, 36, None, IAF),
    ("SEC-011", "AOC-2025-HFT-77", "Sentinel QSA Ltd", 10, 12, None, None),
    ("SEC-008", "SOC2-2025-HFT-09", "Fenwick Assurance LLP", 13, 12, None, None),
    ("INS-003", "PI/2025/60110", "Chubb", 13, 12, 10_000_000, None),
    ("INS-004", "CY/2025/60111", "Chubb", 13, 12, 5_000_000, None),
    ("FIN-001", "FY2024-AUD", "Barrowmore LLP", 19, 12, None, None),
    ("COM-005", None, "Harlow Financial Technology Ltd", 20, 12, None, None),
]

PELLWORTH_DOCS = [
    ("LEG-001", "13448210", "Companies House", 26, None, None, CH),
    # Deliberately duplicates Northgate's certificate numbers: the cross-tenant
    # duplicate check is the single highest-value authenticity signal, and no
    # single-tenant system can see it.
    ("SEC-001", "IS 776541", "BSI Group", 6, 36, None, IAF),
    ("QMS-001", "FS 601233", "BSI Group", 6, 36, None, IAF),
    ("INS-003", "PI/2026/00001", "Unnamed broker", 5, 12, 1_000_000, None),
    ("TAX-001", "GB999888777", "HM Revenue & Customs", 24, None, None, None),
    ("INS-001", None, "Unnamed broker", 20, 12, 1_000_000, None),
    ("QMS-005", None, "Certification body (disputed)", 30, 36, None, None),
    ("SEC-008", None, None, 26, 12, None, None),
    ("HSE-001", None, "Pellworth Consulting Ltd", 22, 12, None, None),
    ("COM-005", None, None, 25, 12, None, None),
]

# --------------------------------------------------------------------------
# Entity specifications
# --------------------------------------------------------------------------
ENTITIES = [
    {
        "slug": "northgate", "legal_name": "Northgate Data Systems Ltd",
        "trading_name": "Northgate", "country": "GB", "region": "uk1",
        "registry": "09123456", "lei": "213800XYZ12345678901", "vat": "GB312445566",
        "duns": "221456789", "domain": "northgate.example", "employees": 48,
        "turnover": 6_400_000, "incorporated_months": 84,
        "sector": "62012 - Business and domestic software development",
        "address": "Unit 4, Kestrel Court\n18 Cranmore Way\nReading RG1 8QT\nUnited Kingdom",
        "description": ("Northgate builds data integration and reconciliation software for "
                        "mid-market financial services firms. 48 staff, UK-only delivery, "
                        "hosted on AWS eu-west-2."),
        "documents": NORTHGATE_DOCS, "lifecycle": "active",
        "users": [("demo@northgate.example", "Alex Whitfield", "owner", True),
                  ("compliance@northgate.example", "Priya Sandhu", "compliance", True),
                  ("bids@northgate.example", "Marcus Bell", "sales", False),
                  ("readonly@northgate.example", "Jo Ferris", "viewer", False)],
        "clients": [
            ("Meridian Building Society", "customer", "GB", "CL-1042",
             "procurement@meridian.example", "Priya Raman", "Third-party risk manager",
             ["legal-registration", "security-privacy", "insurance", "restricted-security"]),
            ("Halewood Asset Management", "customer", "GB", "CL-1088",
             "vendorrisk@halewood.example", "Tom Beckett", "Vendor risk analyst",
             ["legal-registration", "security-privacy", "insurance", "restricted-security"]),
            ("Castleforth Insurance plc", "prospect", "GB", "CL-1131",
             "supplier.review@castleforth.example", "Nadia Okafor", "Procurement lead",
             ["company-basics", "security-privacy", "insurance"]),
            ("Brookvale Wealth Partners", "prospect", "GB", "CL-1155",
             "onboarding@brookvale.example", "Daniel Ross", "Operations manager",
             ["company-basics", "quality"]),
        ],
        "requirement": "UK financial services buyer",
        "reads": 22, "agent_calls": 34, "open_requests": 1, "questionnaire": True,
    },
    {
        "slug": "calderwell", "legal_name": "Calderwell Facilities Group Ltd",
        "trading_name": "Calderwell FM", "country": "GB", "region": "uk1",
        "registry": "04887210", "lei": None, "vat": "GB447120933", "duns": "339210447",
        "domain": "calderwell.example", "employees": 310, "turnover": 24_800_000,
        "incorporated_months": 132,
        "sector": "81100 - Combined facilities support activities",
        "address": "Calderwell House\nBrindley Park\nWarrington WA2 8TX\nUnited Kingdom",
        "description": ("Hard and soft facilities management across 140 sites in the north "
                        "of England. Mechanical, electrical, cleaning and grounds."),
        "documents": CALDERWELL_DOCS, "lifecycle": "active",
        "users": [("owner@calderwell.example", "Sian Doherty", "owner", True),
                  ("hse@calderwell.example", "Ray Nkemelu", "compliance", False),
                  ("tenders@calderwell.example", "Louise Tran", "sales", False)],
        "clients": [
            ("Pentland Estates Ltd", "customer", "GB", "PE-77",
             "supplychain@pentland.example", "Grace Adeyemi", "Supply chain manager",
             ["legal-registration", "insurance", "hse", "quality"]),
            ("Northern Rail Infrastructure", "customer", "GB", "NRI-2201",
             "assurance@nri.example", "Ben Copeland", "Assurance lead",
             ["insurance", "hse"]),
        ],
        "requirement": "Construction main contractor",
        "reads": 14, "agent_calls": 6, "open_requests": 2, "questionnaire": True,
    },
    {
        "slug": "aventine", "legal_name": "Aventine Pharma Solutions BV",
        "trading_name": "Aventine", "country": "NL", "region": "eu1",
        "registry": "NL-58831207", "lei": "724500AVN00PHARMA123", "vat": "NL861223344B01",
        "duns": "492118330", "domain": "aventine.example", "employees": 126,
        "turnover": 41_200_000, "incorporated_months": 108,
        "sector": "21200 - Manufacture of pharmaceutical preparations",
        "address": "Molenweg 22\n5215 AB 's-Hertogenbosch\nNetherlands",
        "description": ("Contract development and manufacturing for sterile injectables, "
                        "serving EU and UK marketing authorisation holders."),
        "documents": AVENTINE_DOCS, "lifecycle": "active",
        "users": [("owner@aventine.example", "Willem de Vries", "owner", True),
                  ("qa@aventine.example", "Ingrid Bakker", "compliance", True)],
        "clients": [
            ("Thameside Biologics Ltd", "customer", "GB", "TB-4410",
             "qualityaudit@thameside.example", "Dr Helen Marsh", "Head of quality",
             ["legal-registration", "licences", "quality", "security-privacy"]),
        ],
        "requirement": "Healthcare & life sciences buyer",
        "reads": 9, "agent_calls": 12, "open_requests": 0,
    },
    {
        "slug": "kestrel", "legal_name": "Kestrel Logistics FZE",
        "trading_name": "Kestrel", "country": "AE", "region": "me1",
        "registry": "FZE-DXB-114872", "lei": None, "vat": None, "duns": None,
        "domain": "kestrel.example", "employees": 64, "turnover": 9_100_000,
        "incorporated_months": 30,
        "sector": "52291 - Freight forwarding",
        "address": "Warehouse 14, Jebel Ali Free Zone\nDubai\nUnited Arab Emirates",
        "description": ("Freight forwarding and bonded warehousing between the Gulf, East "
                        "Africa and South Asia."),
        "documents": KESTREL_DOCS, "lifecycle": "active",
        "users": [("owner@kestrel.example", "Faisal Al-Mansoori", "owner", True)],
        "clients": [],
        "requirement": None,
        "reads": 0, "agent_calls": 0, "open_requests": 0,
    },
    {
        "slug": "harlow", "legal_name": "Harlow Financial Technology Ltd",
        "trading_name": "Harlow", "country": "GB", "region": "uk1",
        "registry": "11902334", "lei": None, "vat": "GB388110274", "duns": "771029338",
        "domain": "harlow.example", "employees": 22, "turnover": 3_100_000,
        "incorporated_months": 60,
        "sector": "64191 - Payment institution",
        "address": "Second Floor, 8 Bellhouse Street\nLondon EC2A 4RN\nUnited Kingdom",
        "description": ("Electronic money institution providing white-label payment "
                        "accounts. Acquired in June 2026 and migrating to the acquirer's "
                        "own supplier portal."),
        "documents": HARLOW_DOCS, "lifecycle": "closing",
        "close_reason": "Acquired - migrating to the acquirer's supplier portal",
        "users": [("owner@harlow.example", "Ruth Ellery", "owner", True)],
        "clients": [
            ("Ambervale Retail Group", "customer", "GB", "AV-9001",
             "vendors@ambervale.example", "Chris Nunn", "Vendor manager",
             ["legal-registration", "security-privacy", "insurance"]),
        ],
        "requirement": "UK financial services buyer",
        "reads": 6, "agent_calls": 3, "open_requests": 0,
    },
    {
        "slug": "pellworth", "legal_name": "Pellworth Consulting Ltd",
        "trading_name": None, "country": "GB", "region": "uk1",
        "registry": "13448210", "lei": None, "vat": "GB999888777", "duns": None,
        "domain": "pellworth.example", "employees": 6, "turnover": 480_000,
        "incorporated_months": 26,
        "sector": "70229 - Management consultancy",
        "address": "Suite 3, 27 Wharf Road\nManchester M1 5EQ\nUnited Kingdom",
        "description": "Management consultancy. Certificate authenticity under review.",
        "documents": PELLWORTH_DOCS, "lifecycle": "suspended",
        "suspension": ("fraud", "Certificate numbers on two ISO certificates do not resolve "
                              "at the certification body and the named body denies issuing them"),
        "users": [("owner@pellworth.example", "Dean Pellworth", "owner", True)],
        "clients": [
            ("Silverdale Group", "prospect", "GB", "SG-31",
             "procurement@silverdale.example", "Anita Shah", "Category manager",
             ["company-basics"]),
        ],
        "requirement": None,
        "reads": 3, "agent_calls": 0, "open_requests": 0,
    },
]

DEMO_IPS = ["81.145.22.104", "212.58.244.19", "51.140.68.7", "185.86.151.30",
            "94.30.11.222", "77.68.40.15"]


def _ago(days: float) -> datetime:
    return now() - timedelta(days=days, hours=RNG.randint(0, 20),
                             minutes=RNG.randint(0, 59))


def build(db: Session) -> None:
    """Creates the full synthetic estate. Safe to call only on an empty
    database - seed.py guards this."""
    from .seed import provision_tenant

    for spec in ENTITIES:
        primary = spec["users"][0]
        tenant, owner = provision_tenant(
            db, legal_name=spec["legal_name"], country=spec["country"],
            email=primary[0], full_name=primary[1], password=DEMO_PASSWORD,
            registry_number=spec["registry"], slug=spec["slug"])
        tenant.data_region = spec["region"]
        tenant.primary_domain = spec["domain"]
        tenant.domain_verified = True
        owner.role = primary[2]
        owner.sensitive_access = primary[3]

        org = db.get(Organisation, tenant.organisation_id)
        org.trading_name = spec["trading_name"]
        org.lei = spec["lei"]
        org.vat_number = spec["vat"]
        org.duns = spec["duns"]
        org.website = f"https://{spec['domain']}"
        org.employees = spec["employees"]
        org.annual_turnover = spec["turnover"]
        org.currency = "EUR" if spec["country"] == "NL" else (
            "AED" if spec["country"] == "AE" else "GBP")
        org.sector_code = spec["sector"]
        org.registered_address = spec["address"]
        org.description = spec["description"]
        org.incorporated_on = date.today() - timedelta(days=spec["incorporated_months"] * 30)
        if spec["lei"]:
            org.verification_state = "lei_bound"

        for email, name, role, sensitive in spec["users"][1:]:
            db.add(User(tenant_id=tenant.id, email=email, full_name=name,
                        password_hash=hash_password(DEMO_PASSWORD), role=role,
                        sensitive_access=sensitive,
                        last_login_at=_ago(RNG.uniform(0.5, 9))))
        owner.last_login_at = _ago(RNG.uniform(0.2, 3))
        db.flush()

        _documents(db, tenant, owner, spec)
        _clients_and_grants(db, tenant, owner, spec)
        _history(db, tenant, spec)
        _requests(db, tenant, spec)

        if spec["requirement"]:
            from .models import RequirementProfile
            profile = db.scalar(select(RequirementProfile).where(
                RequirementProfile.name == spec["requirement"]))
            if profile:
                reqsvc.evaluate(db, tenant, profile, actor=owner)

        _lifecycle(db, tenant, spec)
        docsvc.run_maintenance(db, tenant)
        _signals(db, tenant)
        _summary(db, tenant, spec)
        trust.compute(db, tenant)
        notifications.sweep(db, tenant)
        if spec.get("questionnaire"):
            _questionnaire(db, tenant, spec)

    _cross_tenant_pass(db)
    _onboarding_history(db)
    _stalled_registrations(db)
    _platform_history(db)
    db.commit()


def _documents(db: Session, tenant: Tenant, owner: User, spec: dict) -> None:
    today = date.today()
    for code, number, issuer, issued_ago, valid_months, cover, url in spec["documents"]:
        dtype = db.scalar(select(DocumentType).where(DocumentType.code == code))
        if dtype is None:
            continue
        document = Document(tenant_id=tenant.id, document_type_id=dtype.id,
                            title=dtype.name, jurisdiction=spec["country"],
                            owner_user_id=owner.id)
        db.add(document)
        db.flush()

        issue_date = today - timedelta(days=int(issued_ago * 30.44))
        expiry = (issue_date + timedelta(days=int(valid_months * 30.44))
                  if valid_months else None)
        version = docsvc.create_version(
            db, document=document, actor=owner,
            issue_date=issue_date, effective_from=issue_date, expiry_date=expiry,
            certificate_number=number, issuer_name=issuer,
            holder_name=spec["legal_name"],
            scope_statement=f"{dtype.name} covering {spec['trading_name'] or spec['legal_name']} operations",
            cover_amount=cover,
            currency=(org_currency(spec) if cover else None),
            verification_url=url,
            file_name=f"{code.lower()}-{spec['slug']}.pdf", mime_type="application/pdf",
            file_size=RNG.randint(70_000, 1_400_000),
            content_hash=f"{abs(hash((spec['slug'], code))):064x}"[:64])
        docsvc.publish_version(db, version, owner)
        verification.run_check(db, version, actor=None)

    # A recertification, so version history is real rather than theoretical.
    iso = db.scalar(select(Document).join(DocumentType)
                    .where(Document.tenant_id == tenant.id, DocumentType.code == "SEC-001"))
    if iso and spec["slug"] in {"northgate", "aventine"}:
        replacement = docsvc.create_version(
            db, document=iso, actor=owner,
            issue_date=today - timedelta(days=20), effective_from=today - timedelta(days=20),
            expiry_date=today + timedelta(days=1000),
            certificate_number=("IS 776541" if spec["slug"] == "northgate" else "IS 118421"),
            issuer_name=("BSI Group" if spec["slug"] == "northgate" else "TÜV SÜD"),
            holder_name=spec["legal_name"],
            scope_statement="ISMS covering software development, hosting and support",
            verification_url=IAF,
            file_name=f"sec-001-{spec['slug']}-recert.pdf", mime_type="application/pdf",
            file_size=412_004, content_hash=f"{abs(hash(('recert', spec['slug']))):064x}"[:64],
            change_reason="Recertification audit completed - three-year cycle renewed")
        docsvc.publish_version(db, replacement, owner)
        verification.run_check(db, replacement, actor=None)

    reviewed = [("SEC-002", "Statement of Applicability checked line by line against the "
                            "certificate scope"),
                ("SEC-020", "Continuity plan and the last test report reviewed together"),
                ("HSE-002", "Method statements sampled against three live sites"),
                ("SEC-026", "Subprocessor list reconciled against the signed DPA annex"),
                ("OWN-001", "Beneficial ownership checked against the PSC register")]
    for code, note in reviewed:
        held = db.scalar(select(Document).join(DocumentType).where(
            Document.tenant_id == tenant.id, DocumentType.code == code))
        if held and held.active_version_id:
            current = db.get(DocumentVersion, held.active_version_id)
            if current and current.assurance_level in {"L0", "L1"}:
                verification.manual_decision(db, current, owner, "confirm", note)


def org_currency(spec: dict) -> str:
    return {"NL": "EUR", "AE": "AED"}.get(spec["country"], "GBP")


def _clients_and_grants(db: Session, tenant: Tenant, owner: User, spec: dict) -> None:
    groups = {g.code: g for g in db.scalars(select(CertificateGroup).where(
        CertificateGroup.tenant_id == tenant.id)).all()}
    for (name, relationship, country, reference, email, contact_name,
         title, group_codes) in spec["clients"]:
        client = ClientAccount(tenant_id=tenant.id, name=name,
                               relationship_type=relationship, country=country,
                               internal_reference=reference)
        db.add(client)
        db.flush()
        contact = ClientContact(tenant_id=tenant.id, client_id=client.id, email=email,
                                full_name=contact_name, job_title=title)
        db.add(contact)
        db.flush()
        audit.record(db, tenant_id=tenant.id, action="client.created", actor_id=owner.id,
                     actor_label=owner.email, object_type="client_account",
                     object_id=client.id, payload={"name": name},
                     at=_ago(RNG.uniform(40, 120)))
        for code in group_codes:
            group = groups.get(code)
            if group is None:
                continue
            level = "view" if group.sensitivity_ceiling in {"C", "S"} else "view_download"
            access.create_grant(db, tenant_id=tenant.id, client_id=client.id,
                                scope_kind="group", scope_id=group.id,
                                permission_level=level, days=None,
                                purpose=f"Annual third-party review - {name}",
                                actor=owner, contact_id=contact.id)


def _history(db: Session, tenant: Tenant, spec: dict) -> None:
    """Backdated buyer and agent activity, generated in chronological order so
    the log reads as a sequence rather than a single import."""
    if not spec["reads"] and not spec["agent_calls"]:
        return

    grants = db.scalars(select(access.AccessGrant).where(
        access.AccessGrant.tenant_id == tenant.id,
        access.AccessGrant.status == "active")).all()
    if not grants:
        return
    contacts = {c.client_id: c for c in db.scalars(select(ClientContact).where(
        ClientContact.tenant_id == tenant.id)).all()}
    clients = {c.id: c for c in db.scalars(select(ClientAccount).where(
        ClientAccount.tenant_id == tenant.id)).all()}

    events: list[tuple[datetime, str, dict]] = []

    links: dict[str, ShareLink] = {}
    for grant in grants[:4]:
        token, digest = new_share_token()
        link = ShareLink(tenant_id=tenant.id, grant_id=grant.id, token_hash=digest,
                         label=f"Review pack - {clients[grant.client_id].name}",
                         expires_at=now() + timedelta(days=RNG.randint(5, 40)))
        db.add(link)
        db.flush()
        links[grant.id] = link

    sessions = max(1, spec["reads"] // 3)
    for index in range(sessions):
        grant = RNG.choice(list(links))
        grant_obj = next(g for g in grants if g.id == grant)
        link = links[grant]
        contact = contacts.get(grant_obj.client_id)
        if contact is None:
            continue
        when = _ago(RNG.uniform(1, 58))
        ip = RNG.choice(DEMO_IPS)
        documents = access.resolve_documents(db, grant_obj)
        if not documents:
            continue

        link.view_count += 1
        db.add(ShareLinkView(tenant_id=tenant.id, share_link_id=link.id,
                             viewer_email=contact.email, ip=ip, viewed_at=when,
                             action="open_index"))
        events.append((when, "share_link.viewed",
                       {"actor": contact.email, "object_type": "share_link",
                        "object_id": link.id, "grant_id": grant_obj.id, "ip": ip,
                        "payload": {"documents_visible": len(documents)}}))

        for document in RNG.sample(documents, min(len(documents), RNG.randint(1, 4))):
            offset = when + timedelta(minutes=RNG.randint(1, 25))
            db.add(ShareLinkView(tenant_id=tenant.id, share_link_id=link.id,
                                 viewer_email=contact.email, ip=ip, viewed_at=offset,
                                 document_id=document.id, action="view"))
            events.append((offset, "document.viewed",
                           {"actor": contact.email, "object_type": "document_version",
                            "object_id": document.active_version_id,
                            "grant_id": grant_obj.id, "ip": ip,
                            "payload": {"document": document.title,
                                        "client": clients[grant_obj.client_id].name}}))
            if grant_obj.download_allowed and RNG.random() < 0.45:
                later = offset + timedelta(minutes=RNG.randint(1, 6))
                db.add(ShareLinkView(tenant_id=tenant.id, share_link_id=link.id,
                                     viewer_email=contact.email, ip=ip, viewed_at=later,
                                     document_id=document.id, action="download"))
                events.append((later, "document.downloaded",
                               {"actor": contact.email, "object_type": "document_version",
                                "object_id": document.active_version_id,
                                "grant_id": grant_obj.id, "ip": ip,
                                "payload": {"document": document.title}}))
        grant_obj.last_used_at = when
        contact.last_seen_at = when

    # Agent traffic on a grant-bound key
    if spec["agent_calls"]:
        first = grants[0]
        client_name = clients[first.client_id].name
        key_name = f"{client_name} TPRM agent"
        api_client = ApiClient(tenant_id=tenant.id, name=key_name,
                               key_prefix="gicc_demo00",
                               key_hash=hash_token(f"demo-key-{tenant.slug}"),
                               scopes="profile:read documents:read verification:read "
                                      "requirements:read",
                               grant_id=first.id)
        db.add(api_client)
        db.flush()
        tools = [("mcp.documents_listed", "list_documents", {"returned": 12}),
                 ("mcp.expiries_listed", "list_expiries", {"within_days": 90}),
                 ("mcp.verification_read", "verify_credential", {}),
                 ("mcp.profile_read", "get_supplier_profile", {}),
                 ("mcp.document_read", "get_document_metadata", {})]
        for _ in range(spec["agent_calls"]):
            when = _ago(RNG.uniform(0.2, 45))
            action, tool, payload = RNG.choice(tools)
            db.add(AgentCallLog(tenant_id=tenant.id, api_client_id=api_client.id,
                                protocol="mcp", mcp_method="tools/call", mcp_name=tool,
                                scopes=api_client.scopes, result_status="ok",
                                bytes_out=RNG.randint(400, 9000),
                                latency_ms=RNG.randint(18, 240), occurred_at=when))
            events.append((when, action,
                           {"actor": f"mcp:{key_name}", "actor_kind": "agent",
                            "object_type": None, "object_id": None,
                            "grant_id": first.id, "payload": payload}))
        api_client.last_used_at = max((e[0] for e in events), default=now())

    for when, action, meta in sorted(events, key=lambda e: e[0]):
        audit.record(db, tenant_id=tenant.id, action=action,
                     actor_kind=meta.get("actor_kind", "client_contact"),
                     actor_label=meta["actor"], object_type=meta.get("object_type"),
                     object_id=meta.get("object_id"), grant_id=meta.get("grant_id"),
                     ip=meta.get("ip"), payload=meta.get("payload"), at=when)


def _requests(db: Session, tenant: Tenant, spec: dict) -> None:
    if not spec["open_requests"]:
        return
    clients = db.scalars(select(ClientAccount).where(
        ClientAccount.tenant_id == tenant.id)).all()
    groups = db.scalars(select(CertificateGroup).where(
        CertificateGroup.tenant_id == tenant.id,
        CertificateGroup.sensitivity_ceiling.in_(["C", "S"]))).all()
    if not clients or not groups:
        return
    purposes = ["Escalated review after a client incident",
                "Annual refresh of our supplier file",
                "Pre-contract due diligence for a new statement of work"]
    for index in range(spec["open_requests"]):
        client = clients[index % len(clients)]
        contact = db.scalar(select(ClientContact).where(
            ClientContact.client_id == client.id))
        group = groups[index % len(groups)]
        raised = _ago(RNG.uniform(0.5, 4))
        request_row = AccessRequest(
            tenant_id=tenant.id, client_id=client.id,
            contact_email=contact.email if contact else None,
            scope_kind="group", scope_id=group.id,
            purpose=purposes[index % len(purposes)], status="open",
            sla_due_at=raised + timedelta(days=2), created_at=raised)
        db.add(request_row)
        db.flush()
        audit.record(db, tenant_id=tenant.id, action="access_request.raised",
                     actor_kind="client_contact",
                     actor_label=contact.email if contact else client.name,
                     object_type="access_request", object_id=request_row.id,
                     payload={"group": group.name, "purpose": request_row.purpose},
                     at=raised)


def _lifecycle(db: Session, tenant: Tenant, spec: dict) -> None:
    """Puts two entities into non-active states so the exit path and the
    enforcement path are both visible without anyone having to trigger them."""
    if spec["lifecycle"] == "closing":
        started = _ago(6)
        tenant.lifecycle_state = "closing"
        tenant.closing_started_at = started
        tenant.close_reason = spec["close_reason"]
        owner = db.scalar(select(User).where(User.tenant_id == tenant.id))
        audit.record(db, tenant_id=tenant.id, action="tenant.closing",
                     actor_id=owner.id, actor_label=owner.email, object_type="tenant",
                     object_id=tenant.id, payload={"reason": spec["close_reason"]},
                     at=started)
        for client in db.scalars(select(ClientAccount).where(
                ClientAccount.tenant_id == tenant.id)).all():
            audit.record(db, tenant_id=tenant.id, action="supplier.exiting",
                         actor_kind="system", actor_label="lifecycle",
                         object_type="client_account", object_id=client.id,
                         payload={"client": client.name,
                                  "access_ends_at": (started + timedelta(days=30)).isoformat(),
                                  "download_enabled_for_exit": True},
                         at=started + timedelta(minutes=1))

    elif spec["lifecycle"] == "suspended":
        admin = db.scalar(select(PlatformAdmin))
        ground, reason = spec["suspension"]
        when = _ago(2)
        tenant.lifecycle_state = "suspended"
        tenant.suspended_at = when
        tenant.suspension_reason = f"{ground}: {reason}"
        tenant.suspended_by = admin.id if admin else None
        tenant.admin_note = ("Certification body contacted 9 August. Awaiting written "
                             "confirmation before referring onward.")
        audit.record(db, tenant_id=tenant.id, action="tenant.suspended",
                     actor_kind="platform_admin", actor_id=admin.id if admin else None,
                     actor_label=admin.email if admin else "platform",
                     object_type="tenant", object_id=tenant.id,
                     payload={"ground": ground, "reason": reason,
                              "decided_by": admin.email if admin else "platform"},
                     at=when)
        reviewer = db.scalar(select(User).where(User.tenant_id == tenant.id))
        for document in db.scalars(select(Document).where(
                Document.tenant_id == tenant.id)).all():
            if document.document_type.code in {"SEC-001", "QMS-001"}:
                current = db.get(DocumentVersion, document.active_version_id) \
                    if document.active_version_id else None
                if current:
                    verification.manual_decision(
                        db, current, reviewer, "reject",
                        "Certification body confirms it did not issue this certificate "
                        "number. Referred for investigation.")
                document.legal_hold = True
                document.hold_placed_by = "platform"
                audit.record(db, tenant_id=tenant.id, action="legal_hold.placed",
                             actor_kind="platform_admin",
                             actor_id=admin.id if admin else None,
                             actor_label=admin.email if admin else "platform",
                             object_type="document", object_id=document.id,
                             payload={"reason": "Evidence preserved pending investigation",
                                      "placed_by": "platform"},
                             at=when + timedelta(minutes=3))


def _signals(db: Session, tenant: Tenant) -> None:
    """Authenticity signals across every live version. On the demo estate this
    surfaces a duplicate certificate number and several format anomalies."""
    for version in db.scalars(select(DocumentVersion).where(and_(
            DocumentVersion.tenant_id == tenant.id,
            DocumentVersion.status == "active"))).all():
        authenticity.evaluate(db, version)


def _summary(db: Session, tenant: Tenant, spec: dict) -> None:
    from .models import ControlSummary, Organisation
    from .routers.intelligence import _facts

    org = db.get(Organisation, tenant.organisation_id)
    facts, gaps = _facts(db, tenant)
    if not facts:
        return
    result = ai.control_summary(db, tenant.id, org_name=org.legal_name,
                                facts=facts, gaps=gaps)
    db.add(ControlSummary(tenant_id=tenant.id, body=result["body"],
                          source=result["source"],
                          evidence_fingerprint=result["fingerprint"],
                          published=spec["slug"] in {"northgate", "aventine"}))
    db.flush()


DEMO_QUESTIONS = """QREF,Question
SEC-1,Do you hold a current ISO 27001 certification covering the services provided?
SEC-2,Please provide your most recent SOC 2 Type II report.
SEC-3,When was your last penetration test and by whom was it carried out?
SEC-4,Do you maintain a documented information security policy?
SEC-5,Provide a list of subprocessors with access to our data.
SEC-6,Do you have a data processing agreement incorporating standard contractual clauses?
BCM-1,Do you maintain a business continuity plan and when was it last tested?
BCM-2,What are your documented RTO and RPO targets?
INS-1,Confirm your professional indemnity cover and its limit.
INS-2,Confirm you hold cyber liability insurance.
CORP-1,Provide your certificate of incorporation and registered number.
CORP-2,Confirm your VAT registration.
FIN-1,Provide your most recent audited financial statements.
GOV-1,Do you have an anti-bribery and corruption policy?
GOV-2,Do you publish a modern slavery statement?
ESG-1,What are your carbon reduction commitments?
AI-1,Do you maintain model cards for any AI systems used in the service?
EXIT-1,Describe your exit and transition assistance arrangements.
"""


def _questionnaire(db: Session, tenant: Tenant, spec: dict) -> None:
    from .models import ClientAccount, Questionnaire

    client = db.scalar(select(ClientAccount).where(
        ClientAccount.tenant_id == tenant.id))
    record = Questionnaire(
        tenant_id=tenant.id,
        name=f"{client.name if client else 'Buyer'} annual review 2026",
        client_id=client.id if client else None, source_format="csv",
        source_filename="annual-review-2026.csv", created_at=_ago(4))
    db.add(record)
    db.flush()
    rows = qsvc.parse(DEMO_QUESTIONS.encode(), "annual-review-2026.csv")
    qsvc.build(db, tenant, record, rows)
    # Approve a few, so the answer bank is not empty on first look.
    for item in record.items[:4]:
        if item.status == "drafted":
            qsvc.approve(db, tenant, item, None)
    db.flush()


def _cross_tenant_pass(db: Session) -> None:
    """Re-run the signal rules once the whole estate exists. The duplicate
    certificate number check compares across tenants, so the first supplier
    seeded cannot see a clash created by the sixth."""
    numbers: dict[str, int] = {}
    versions = db.scalars(select(DocumentVersion).where(
        DocumentVersion.status == "active")).all()
    for version in versions:
        if version.certificate_number:
            numbers[version.certificate_number] = numbers.get(
                version.certificate_number, 0) + 1
    for version in versions:
        if version.certificate_number and numbers[version.certificate_number] > 1:
            authenticity.evaluate(db, version)
    for tenant in db.scalars(select(Tenant)).all():
        trust.compute(db, tenant)


# Registration dates and how far each demo entity got, so the funnel shows a
# realistic shape rather than a straight line.
ONBOARDING_PROFILE = {
    "northgate":   (58, [1, 2, 3, 4, 5], "Software and IT services", "financial"),
    "aventine":    (44, [1, 2, 3, 4, 5], "Healthcare and life sciences", "healthcare"),
    "calderwell":  (37, [1, 2, 3, 4, 5], "Facilities management", "construction,public"),
    "harlow":      (95, [1, 2, 3, 4, 5], "Financial services", "financial"),
    "pellworth":   (26, [1, 2, 3], "Professional services", "general"),
    "kestrel":     (12, [1, 2], "Logistics and transport", "general"),
}


def _onboarding_history(db: Session) -> None:
    from .models import OnboardingProgress
    from .services import onboarding as onboarding_service

    for tenant in db.scalars(select(Tenant)).all():
        days, completed, sector, buyers = ONBOARDING_PROFILE.get(
            tenant.slug, (20, [1, 2], "Other", "general"))
        registered = _ago(days)
        progress = db.scalar(select(OnboardingProgress).where(
            OnboardingProgress.tenant_id == tenant.id))
        if progress is None:
            progress = OnboardingProgress(tenant_id=tenant.id, registered_at=registered)
            db.add(progress)
            db.flush()
        progress.registered_at = registered
        progress.sector = sector
        progress.buyer_types = buyers

        steps, cursor = {}, registered
        for number in range(1, onboarding_service.TOTAL_STEPS + 1):
            if number in completed:
                started = cursor + timedelta(minutes=RNG.randint(1, 9))
                finished = started + timedelta(minutes=RNG.randint(2, 26))
                steps[str(number)] = {"status": "complete",
                                      "started_at": started.isoformat(),
                                      "completed_at": finished.isoformat()}
                cursor = finished
        progress.steps_json = json.dumps(steps)
        progress.current_step = (max(completed) + 1) if completed else 1
        if len(completed) == onboarding_service.TOTAL_STEPS:
            progress.completed_at = cursor

        # Milestones are stamped from what is actually true in the estate, then
        # backdated onto the journey so time-to-value is measurable.
        onboarding_service.observe(db, tenant)
        for milestone, offset in (("m1", 1), ("m2", 3), ("m3", 5)):
            stamp = getattr(progress, f"{milestone}_at")
            if stamp:
                setattr(progress, f"{milestone}_at",
                        registered + timedelta(
                            minutes=RNG.randint(4, 40) * offset,
                            days=RNG.randint(0, max(1, offset - 1))))
    db.flush()


# Registrations that went nowhere. Without these the funnel is a straight line
# and the analytics console demonstrates nothing - the whole point of the funnel
# is to show where people stop.
STALLED = [
    ("Alderley Print Services Ltd", "GB", 3, [1], None),
    ("Brentmoor Cleaning Co", "GB", 5, [1, 2], "construction"),
    ("Caldicot Surveying LLP", "GB", 8, [1, 2], "public"),
    ("Dunmore Electrical Ltd", "GB", 11, [1], None),
    ("Eastgate Translations Ltd", "GB", 14, [1, 2, 3], "general"),
    ("Fenwick Catering Ltd", "GB", 17, [1], None),
    ("Garrowby Test Labs Ltd", "GB", 21, [1, 2, 3], "healthcare"),
    ("Hexham Security Services Ltd", "GB", 24, [1, 2], "public"),
    ("Innisfree Analytics BV", "NL", 29, [1, 2, 3, 4], "financial"),
    ("Jarrow Metalwork Ltd", "GB", 33, [1], None),
    ("Kingsmoor Advisory Ltd", "GB", 41, [1, 2], "financial"),
    ("Lyndhurst Waste Ltd", "GB", 48, [1, 2, 3], "construction"),
    ("Marchwood Interiors Ltd", "GB", 55, [1], None),
    ("Netherby Software Ltd", "GB", 62, [1, 2, 3, 4], "financial"),
]


def _stalled_registrations(db: Session) -> None:
    """Signed up, got partway, stopped. Roughly two-thirds of real signups never
    activate; a demo that hides that teaches the wrong lesson."""
    from .models import OnboardingProgress
    from .seed import provision_tenant
    from .services import onboarding as onboarding_service

    for name, country, days_ago, completed, buyers in STALLED:
        slug = "".join(c for c in name.lower().replace(" ", "-")
                       if c.isalnum() or c == "-")[:28]
        handle = slug.split("-")[0]
        tenant, owner = provision_tenant(
            db, legal_name=name, country=country,
            email=f"owner@{handle}.example", full_name="Prospect Owner",
            password=DEMO_PASSWORD, registry_number=None, slug=slug)
        registered = _ago(days_ago)
        tenant.created_at = registered
        tenant.last_activity_at = registered + timedelta(hours=RNG.randint(1, 30))
        owner.last_login_at = tenant.last_activity_at

        progress = OnboardingProgress(
            tenant_id=tenant.id, registered_at=registered,
            buyer_types=buyers, current_step=(max(completed) + 1))
        steps, cursor = {}, registered
        for number in completed:
            started = cursor + timedelta(minutes=RNG.randint(1, 6))
            finished = started + timedelta(minutes=RNG.randint(1, 18))
            steps[str(number)] = {"status": "complete",
                                  "started_at": started.isoformat(),
                                  "completed_at": finished.isoformat()}
            cursor = finished
        # A few explicitly gave up rather than drifting away.
        if days_ago > 30 and len(completed) <= 2:
            progress.dismissed = True
        progress.steps_json = json.dumps(steps)
        db.add(progress)

        # Those who reached step 3 have a document, but nothing verified and
        # nothing shared - the most common shape of a stalled account.
        if 3 in completed:
            dtype = db.scalar(select(DocumentType).where(
                DocumentType.code == "LEG-001"))
            if dtype:
                document = Document(tenant_id=tenant.id, document_type_id=dtype.id,
                                    title=dtype.name, jurisdiction=country,
                                    owner_user_id=owner.id)
                db.add(document)
                db.flush()
                version = docsvc.create_version(
                    db, document=document, actor=owner,
                    issue_date=(registered - timedelta(days=400)).date(),
                    effective_from=(registered - timedelta(days=400)).date(),
                    holder_name=name,
                    file_name="incorporation.pdf", mime_type="application/pdf",
                    file_size=180_000)
                docsvc.publish_version(db, version, owner)
        db.flush()
    db.flush()


def _platform_history(db: Session) -> None:
    """A closed break-glass session, so the operator console and the supplier's
    activity log both have something to show on first load."""
    admin = db.scalar(select(PlatformAdmin))
    tenant = db.scalar(select(Tenant).where(Tenant.slug == "calderwell"))
    if admin is None or tenant is None:
        return
    user = db.scalar(select(User).where(User.tenant_id == tenant.id))
    started = _ago(11)
    record = BreakGlassSession(
        admin_id=admin.id, tenant_id=tenant.id, acting_user_id=user.id,
        reason="Upload failing with a 500 on a 40 MB insurance certificate",
        ticket_reference="SUP-1042", started_at=started,
        expires_at=started + timedelta(minutes=240),
        ended_at=started + timedelta(minutes=38), actions_taken=14)
    db.add(record)
    db.flush()
    audit.record(db, tenant_id=tenant.id, action="support.break_glass",
                 actor_kind="platform_admin", actor_id=admin.id, actor_label=admin.email,
                 object_type="tenant", object_id=tenant.id,
                 payload={"admin": admin.email, "reason": record.reason,
                          "ticket": "SUP-1042", "acting_as": user.email,
                          "expires_at": record.expires_at.isoformat()},
                 at=started)
    audit.record(db, tenant_id=tenant.id, action="support.break_glass_ended",
                 actor_kind="platform_admin", actor_id=admin.id, actor_label=admin.email,
                 object_type="tenant", object_id=tenant.id,
                 payload={"admin": admin.email, "actions_taken": 14,
                          "duration_minutes": 38},
                 at=started + timedelta(minutes=38))
