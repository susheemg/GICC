"""IAF CertSearch connector - confirming a management-system certificate.

IAF CertSearch is the right source to ask. It is the International
Accreditation Forum's database, fed by accreditation bodies and certification
bodies, and it exists precisely so that a buyer can tell a real ISO 9001
certificate from a convincing PDF. Roughly a third of the certificates people
present in due diligence do not survive contact with it.

Three things shape this module.

**Transport is not the same as source.** The authority comes from IAF. How we
reach it — a licensed API, a person reading the page, or a scraper — changes
how much that authority is worth to us, because it changes what we can prove
about the answer we got. So transport carries an assurance *ceiling*, and the
ceiling is applied by rule:

    licensed_api  L3   an attributable response from the source
    assisted      L2   a named person read the source and attested to it
    scrape        L2   capped: an unattributable, unstable read

A scrape can never reach L3 here. L3 means "matched automatically against an
authoritative register", and an HTML page fetched by pretending to be a browser
is not an attributable response — nobody at IAF can confirm they sent it, the
markup can change without notice, and a Cloudflare interstitial parses to
"no results found" as easily as a real empty page. Treating that as machine
verification would put the platform's central claim on the weakest link in it.

**A lookup is not a verdict.** Searching returns candidates. Reconciliation
compares a chosen candidate field by field against what the supplier uploaded.
Only reconciliation has an assurance consequence, and a mismatch on certificate
number, standard or status is a finding, not a rounding error.

**Absence is inconclusive, not negative.** A certificate missing from
CertSearch may be genuine: not every certification body publishes, and coverage
varies by scheme and region. Recording "not found" as a failure would let a
gap in IAF's coverage look like a supplier's dishonesty. It returns
`not_found`, which is inconclusive, and by the standing rule an inconclusive
check never downgrades an existing level.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from ..models import (CertSearchLookup, DocumentVersion, Organisation, Tenant,
                      VerificationCheck, now)
from . import audit, verification

SEARCH_BASE = "https://www.iafcertsearch.org/search/certified-entities"

# Ceiling by transport. Applied by rule in `apply`, never chosen by a user.
TRANSPORT_CEILING = {"licensed_api": "L3", "assisted": "L2", "scrape": "L2"}

# Catalogue codes this connector can speak to, and the standard each means.
STANDARDS = {
    "QMS-001": "ISO 9001", "QMS-002": "ISO 14001", "QMS-003": "ISO 45001",
    "QMS-004": "ISO 50001", "QMS-005": "ISO 22301", "QMS-006": "ISO 22000",
    "QMS-007": "ISO 13485", "QMS-010": "ISO/IEC 20000-1", "QMS-011": "ISO 37001",
    "SEC-001": "ISO/IEC 27001", "SEC-005": "ISO/IEC 27701",
}

COMPARABLE = [
    ("certificate_number", "Certificate number", True),
    ("standard", "Standard", True),
    ("status", "Certificate status", True),
    ("holder_name", "Certified organisation", True),
    ("expiry_date", "Valid to", False),
    ("issue_date", "Valid from", False),
    ("certification_body", "Certification body", False),
    ("accreditation_body", "Accreditation body", False),
    ("scope_statement", "Scope", False),
]


def supports(type_code: str) -> bool:
    return type_code in STANDARDS


def standard_for(type_code: str) -> str | None:
    return STANDARDS.get(type_code)


def search_url(*, name: str, country: str = "", standard: str = "") -> str:
    """The URL a person is sent to, and the one a transport would fetch.

    Query parameter names are a guess at the site's own scheme and may be
    ignored by it. That is tolerable for the assisted path - the operator lands
    on the search page and can retype - but it is one of the reasons the scrape
    transport is not trusted for an automatic verdict.
    """
    parts = [f"q={quote_plus(name.strip())}"]
    if country:
        parts.append(f"country={quote_plus(country.strip().upper())}")
    if standard:
        parts.append(f"standard={quote_plus(standard)}")
    return f"{SEARCH_BASE}?{'&'.join(parts)}"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse_payload(payload: str) -> list[dict]:
    """Normalises a CertSearch response into candidate records.

    Accepts JSON (a licensed API response) or a pasted/scraped HTML fragment.
    Written to fail closed: anything it cannot confidently read becomes no
    candidate rather than a half-filled one, because a half-filled candidate
    compared against a real certificate produces confident nonsense.
    """
    text = (payload or "").strip()
    if not text:
        return []

    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError:
            return []
        rows = data if isinstance(data, list) else (
            data.get("results") or data.get("data") or data.get("certifications") or [])
        return [_normalise_candidate(r) for r in rows if isinstance(r, dict)][:20]

    return _parse_html(text)


def _first(row: dict, *keys):
    for key in keys:
        for candidate in (key, key.lower(), key.replace("_", "")):
            if candidate in row and row[candidate] not in (None, ""):
                return row[candidate]
    return None


def _normalise_candidate(row: dict) -> dict:
    return {
        "certificate_number": _clean(_first(row, "certificate_number", "certificateNumber",
                                            "certNumber", "number", "certificate")),
        "standard": _clean(_first(row, "standard", "scheme", "standardName")),
        "status": _clean(_first(row, "status", "certificateStatus", "state")),
        "holder_name": _clean(_first(row, "organisation", "organization", "entity",
                                     "entityName", "companyName", "holder")),
        "country": _clean(_first(row, "country", "countryCode")),
        "issue_date": _clean(_first(row, "issue_date", "validFrom", "issued",
                                    "effectiveDate")),
        "expiry_date": _clean(_first(row, "expiry_date", "validTo", "expires",
                                     "expiryDate")),
        "certification_body": _clean(_first(row, "certification_body", "certificationBody",
                                            "cab", "cb")),
        "accreditation_body": _clean(_first(row, "accreditation_body", "accreditationBody",
                                            "ab")),
        "scope_statement": _clean(_first(row, "scope", "scopeStatement")),
    }


def _clean(value) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


LABELS = {
    "certificate_number": ["certificate number", "certificate no", "cert number",
                           "certificate id"],
    "standard": ["standard", "scheme", "management system standard"],
    "status": ["status", "certificate status"],
    "holder_name": ["certified entity", "organisation", "organization", "entity name",
                    "company"],
    "issue_date": ["valid from", "issue date", "effective date", "issued"],
    "expiry_date": ["valid to", "expiry date", "expires", "valid until"],
    "certification_body": ["certification body", "certification body name", "cab"],
    "accreditation_body": ["accreditation body", "accreditation body name"],
    "scope_statement": ["scope", "scope of certification"],
}


def _parse_html(html: str) -> list[dict]:
    """Label-driven, not position-driven.

    Reading the nth cell of the nth table breaks the first time a column is
    added and does so silently, mapping an expiry date onto a certificate
    number. Matching on visible labels degrades to *no* result instead, which
    is the failure worth having.
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;", "&", text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    record: dict = {}
    for index, line in enumerate(lines):
        lowered = line.lower().rstrip(":").strip()
        for field, labels in LABELS.items():
            if field in record:
                continue
            if lowered in labels:
                for following in lines[index + 1:index + 3]:
                    if following.lower().rstrip(":").strip() in {
                            l for group in LABELS.values() for l in group}:
                        break
                    record[field] = following
                    break
            else:
                match = re.match(rf"^({'|'.join(map(re.escape, labels))})\s*[:\-]\s*(.+)$",
                                 line, re.I)
                if match:
                    record[field] = match.group(2).strip()

    if not record.get("certificate_number") and not record.get("standard"):
        return []
    return [_normalise_candidate(record)]


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------
def _norm(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _norm_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y",
                "%b %d, %Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


ACTIVE_WORDS = {"active", "valid", "certified", "current", "issued"}
DEAD_WORDS = {"withdrawn", "suspended", "expired", "cancelled", "revoked",
              "terminated"}


def compare(version: DocumentVersion, candidate: dict, *,
            organisation: Organisation | None = None,
            type_code: str = "") -> dict:
    """Field-by-field against what the supplier uploaded.

    Every row carries its own verdict so a reviewer sees *which* field
    disagreed. "Does not match" without saying where is not a finding anybody
    can act on.
    """
    expected_standard = standard_for(type_code)
    held = {
        "certificate_number": version.certificate_number,
        "standard": expected_standard,
        "status": "active" if version.status == "active" else version.status,
        "holder_name": version.holder_name or (organisation.legal_name
                                               if organisation else None),
        "issue_date": version.issue_date,
        "expiry_date": version.expiry_date,
        "certification_body": version.issuer_name,
        "accreditation_body": None,
        "scope_statement": version.scope_statement,
    }

    rows = []
    for field, label, decisive in COMPARABLE:
        ours, theirs = held.get(field), candidate.get(field)
        rows.append({**_compare_field(field, ours, theirs), "field": field,
                     "label": label, "decisive": decisive,
                     "held": _display(ours), "found": _display(theirs)})

    decisive_rows = [r for r in rows if r["decisive"]]
    if any(r["verdict"] == "mismatch" for r in decisive_rows):
        verdict = "mismatch"
    elif any(r["verdict"] == "mismatch" for r in rows):
        verdict = "partial_match"
    elif all(r["verdict"] in {"match", "not_comparable"} for r in decisive_rows) and \
            any(r["verdict"] == "match" for r in decisive_rows):
        verdict = ("match" if all(r["verdict"] != "not_comparable"
                                  for r in decisive_rows) else "partial_match")
    else:
        verdict = "inconclusive"

    return {"verdict": verdict, "rows": rows,
            "decisive_mismatches": [r["label"] for r in decisive_rows
                                    if r["verdict"] == "mismatch"]}


def _compare_field(field: str, ours, theirs) -> dict:
    if ours in (None, "") or theirs in (None, ""):
        return {"verdict": "not_comparable",
                "detail": "not recorded on both sides"}

    if field in {"issue_date", "expiry_date"}:
        left, right = _norm_date(ours), _norm_date(theirs)
        if left is None or right is None:
            return {"verdict": "not_comparable", "detail": "unreadable date"}
        gap = abs((left - right).days)
        if gap == 0:
            return {"verdict": "match", "detail": ""}
        # A few days apart is a transcription or timezone artefact. A month is
        # a different certificate cycle.
        if gap <= 3:
            return {"verdict": "match", "detail": f"{gap} day(s) apart"}
        return {"verdict": "mismatch", "detail": f"{gap} days apart"}

    if field == "status":
        theirs_words = set(re.findall(r"[a-z]+", str(theirs).lower()))
        if theirs_words & DEAD_WORDS:
            return {"verdict": "mismatch",
                    "detail": f"IAF records this certificate as {theirs}"}
        if theirs_words & ACTIVE_WORDS:
            return {"verdict": "match", "detail": ""}
        return {"verdict": "not_comparable", "detail": f"unrecognised status '{theirs}'"}

    if field == "standard":
        return ({"verdict": "match", "detail": ""}
                if _norm(ours) in _norm(theirs) or _norm(theirs) in _norm(ours)
                else {"verdict": "mismatch", "detail": f"expected {ours}"})

    if field in {"holder_name", "certification_body", "accreditation_body"}:
        outcome = verification.name_match(str(theirs), str(ours))
        return {"exact": {"verdict": "match", "detail": ""},
                "partial": {"verdict": "match", "detail": "close but not identical"},
                "mismatch": {"verdict": "mismatch", "detail": ""}}[outcome]

    if field == "scope_statement":
        ours_words = set(re.findall(r"[a-z]{4,}", str(ours).lower()))
        theirs_words = set(re.findall(r"[a-z]{4,}", str(theirs).lower()))
        if not ours_words or not theirs_words:
            return {"verdict": "not_comparable", "detail": ""}
        overlap = len(ours_words & theirs_words) / len(ours_words)
        return ({"verdict": "match", "detail": f"{overlap:.0%} of terms in common"}
                if overlap >= 0.4 else
                {"verdict": "mismatch", "detail": "scope wording differs materially"})

    return ({"verdict": "match", "detail": ""} if _norm(ours) == _norm(theirs)
            else {"verdict": "mismatch", "detail": ""})


def _display(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return str(value)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
def start(db: Session, *, tenant: Tenant, version: DocumentVersion, actor,
          type_code: str, transport: str = "assisted") -> CertSearchLookup:
    organisation = tenant.organisation
    name = version.holder_name or (organisation.legal_name if organisation else "")
    country = organisation.country if organisation else ""
    standard = standard_for(type_code) or ""

    lookup = CertSearchLookup(
        tenant_id=tenant.id, document_version_id=version.id, transport=transport,
        query_name=name, query_country=country, query_standard=standard,
        search_url=search_url(name=name, country=country, standard=standard),
        assurance_ceiling=TRANSPORT_CEILING.get(transport, "L2"), status="open")
    db.add(lookup)
    db.flush()
    audit.record(db, tenant_id=tenant.id, action="certsearch.opened",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="certsearch_lookup", object_id=lookup.id,
                 payload={"name": name, "standard": standard,
                          "transport": transport})
    return lookup


def record_payload(db: Session, lookup: CertSearchLookup, payload: str) -> list[dict]:
    candidates = parse_payload(payload)
    lookup.raw_payload = (payload or "")[:20000]
    lookup.candidates_json = json.dumps(candidates, default=str)
    lookup.status = "candidates" if candidates else "not_found"
    if not candidates:
        lookup.verdict = "not_found"
    db.flush()
    return candidates


def apply(db: Session, *, tenant: Tenant, lookup: CertSearchLookup, actor,
          candidate: dict, type_code: str, note: str = "") -> dict:
    """Reconciles a chosen candidate and writes the verification check.

    The assurance the check may reach is the lesser of what the comparison
    supports and what the transport permits. Neither the supplier nor the
    reviewer chooses it.
    """
    version = db.get(DocumentVersion, lookup.document_version_id)
    organisation = tenant.organisation
    result = compare(version, candidate, organisation=organisation,
                     type_code=type_code)

    ceiling = TRANSPORT_CEILING.get(lookup.transport, "L2")
    order = ["L0", "L1", "L2", "L3", "L4"]

    if result["verdict"] == "match":
        method = ("certsearch_api" if lookup.transport == "licensed_api"
                  else "manual_review")
        outcome = "match"
    elif result["verdict"] == "mismatch":
        method, outcome = "certsearch_api", "mismatch"
    elif result["verdict"] == "partial_match":
        method, outcome = "manual_review", "partial_match"
    else:
        method, outcome = "manual_review", "inconclusive"

    proposed = verification.compute_assurance(db, version, outcome, method)
    resulting = proposed if order.index(proposed) <= order.index(ceiling) else ceiling
    # A negative result must not be raised by a ceiling.
    if outcome == "mismatch":
        resulting = verification.compute_assurance(db, version, outcome, method)

    check = VerificationCheck(
        tenant_id=tenant.id, document_version_id=version.id, method=method,
        source_code="IAF CertSearch", result=outcome, resulting_assurance=resulting,
        matched_fields=json.dumps([r["label"] for r in result["rows"]
                                   if r["verdict"] == "match"]),
        detail=_detail(lookup, result, ceiling, note),
        # Only a licensed API response is a real machine check. The assisted and
        # scrape paths are not, and must not present themselves as one.
        simulated=lookup.transport != "licensed_api",
        checked_by=getattr(actor, "email", "system"),
        next_check_due=now() + timedelta(days=180))
    db.add(check)
    version.assurance_level = resulting
    if lookup.search_url and not version.verification_url:
        version.verification_url = lookup.search_url

    lookup.selected_json = json.dumps(candidate, default=str)
    lookup.comparison_json = json.dumps(result, default=str)
    lookup.verdict = result["verdict"]
    lookup.resulting_assurance = resulting
    lookup.status = "applied"
    lookup.attested_by = getattr(actor, "email", "system")
    lookup.attested_at = now()
    lookup.note = note.strip() or None
    db.flush()
    lookup.verification_check_id = check.id
    db.flush()

    audit.record(db, tenant_id=tenant.id, action="certsearch.applied",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="document_version", object_id=version.id,
                 payload={"verdict": result["verdict"], "assurance": resulting,
                          "ceiling": ceiling, "transport": lookup.transport,
                          "mismatches": result["decisive_mismatches"],
                          "certificate": candidate.get("certificate_number")})
    return {"comparison": result, "assurance": resulting, "ceiling": ceiling,
            "check": check}


def _detail(lookup: CertSearchLookup, result: dict, ceiling: str, note: str) -> str:
    lines = [f"IAF CertSearch via {lookup.transport}; verdict {result['verdict']}."]
    if result["decisive_mismatches"]:
        lines.append("Disagreed on: " + ", ".join(result["decisive_mismatches"]) + ".")
    if lookup.transport != "licensed_api":
        lines.append(f"Capped at {ceiling}: this route is not an attributable "
                     "response from IAF, so it cannot support machine verification.")
    if note.strip():
        lines.append(note.strip())
    return " ".join(lines)


def mark_not_found(db: Session, *, tenant: Tenant, lookup: CertSearchLookup, actor,
                   note: str = "") -> VerificationCheck:
    """Absence is inconclusive. Coverage varies by scheme and region, and a
    certification body that does not publish is not a supplier who lied."""
    version = db.get(DocumentVersion, lookup.document_version_id)
    resulting = verification.compute_assurance(db, version, "not_found",
                                               "manual_review")
    check = VerificationCheck(
        tenant_id=tenant.id, document_version_id=version.id, method="manual_review",
        source_code="IAF CertSearch", result="not_found",
        resulting_assurance=resulting,
        detail=("Not found in IAF CertSearch. Coverage varies by scheme and region, "
                "so this is inconclusive rather than negative and the existing "
                "assurance level is unchanged. " + note.strip()).strip(),
        simulated=lookup.transport != "licensed_api",
        checked_by=getattr(actor, "email", "system"),
        next_check_due=now() + timedelta(days=90))
    db.add(check)
    lookup.status = "applied"
    lookup.verdict = "not_found"
    lookup.resulting_assurance = resulting
    lookup.attested_by = getattr(actor, "email", "system")
    lookup.attested_at = now()
    db.flush()
    lookup.verification_check_id = check.id
    db.flush()
    audit.record(db, tenant_id=tenant.id, action="certsearch.not_found",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="document_version", object_id=version.id,
                 payload={"assurance_unchanged": resulting})
    return check


def history(db: Session, version_id: str) -> list[CertSearchLookup]:
    return list(db.scalars(select(CertSearchLookup)
                           .where(CertSearchLookup.document_version_id == version_id)
                           .order_by(desc(CertSearchLookup.created_at))).all())


# --------------------------------------------------------------------------
# Automated transport
# --------------------------------------------------------------------------
def fetch(url: str) -> tuple[str, str | None]:
    """The scrape transport. Off unless CERTSEARCH_ALLOW_SCRAPE is set.

    Returns (payload, error). It is disabled by default for two reasons that
    have nothing to do with whether the code works: the site's terms govern
    automated collection, and IAF licenses this data commercially, so a
    deployment should make that call deliberately rather than inherit it. A
    licensed API key turns the same reconciliation into a real L3 check.
    """
    from ..config import CERTSEARCH_ALLOW_SCRAPE, CERTSEARCH_API_KEY

    if CERTSEARCH_API_KEY:
        return "", ("A licensed API key is configured but the client is not "
                    "implemented. Point `fetch` at the licensed endpoint.")
    if not CERTSEARCH_ALLOW_SCRAPE:
        return "", ("Automated collection is disabled. Use the assisted route, or "
                    "set CERTSEARCH_ALLOW_SCRAPE=1 having checked the site's terms.")
    try:
        import httpx

        response = httpx.get(url, timeout=20, follow_redirects=True,
                             headers={"Accept": "text/html"})
        if response.status_code != 200:
            return "", f"The source returned HTTP {response.status_code}."
        # A challenge page parses as "no certificates" unless caught here.
        if re.search(r"cf-browser-verification|captcha|just a moment",
                     response.text, re.I):
            return "", ("The source returned a bot-challenge page, not results. "
                        "Treat this as no answer, not as an absent certificate.")
        return response.text, None
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"
