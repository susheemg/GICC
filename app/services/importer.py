"""Bulk profile import - GICC-07 F26.

Two rules do most of the work here.

**Permitted fields.** Creating a record about a company that never asked is
defensible where the data is public-register material. It stops being
defensible the moment the record holds a named contact, a turnover figure or an
inferred risk rating. So the permitted list is enforced at validation rather
than left to policy and good intentions.

**Validate, then commit.** Nothing reaches a live table until an operator has
seen the outcome report. A batch is reversible until one of its profiles is
claimed, at which point the profile belongs to somebody.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import ImportBatch, ImportRow, Organisation, now
from . import audit

# F26.8 — public-register material only.
PERMITTED_FIELDS = {
    "legal_name": ("Legal name", True),
    "trading_name": ("Trading name", False),
    "country": ("Country", True),
    "registry_number": ("Registry number", False),
    "registry_authority": ("Registry authority", False),
    "lei": ("LEI", False),
    "vat_number": ("VAT number", False),
    "duns": ("DUNS", False),
    "incorporated_on": ("Incorporation date", False),
    "registered_address": ("Registered address", False),
    "sector_code": ("Sector code", False),
    "website": ("Website", False),
}

# Anything matching these is rejected at validation with a stated reason,
# rather than quietly dropped.
PROHIBITED_PATTERNS = [
    (re.compile(r"contact|first.?name|last.?name|surname|director|officer|owner|"
                r"person|individual|email|phone|mobile|telephone", re.I),
     "personal or contact data"),
    (re.compile(r"turnover|revenue|profit|ebitda|balance|assets|liabilit|credit|"
                r"score|rating|risk|assessment", re.I),
     "financial figures or risk assessments"),
]

HEADER_HINTS = {
    "legal_name": ["legal name", "company name", "registered name", "name", "supplier"],
    "trading_name": ["trading name", "trading as", "dba", "brand"],
    "country": ["country", "jurisdiction", "country code", "domicile"],
    "registry_number": ["registry number", "company number", "company no",
                        "registration number", "reg no", "crn"],
    "registry_authority": ["registry", "registry authority", "registrar"],
    "lei": ["lei", "legal entity identifier"],
    "vat_number": ["vat", "vat number", "gst", "tax number"],
    "duns": ["duns", "d-u-n-s"],
    "incorporated_on": ["incorporated", "incorporation date", "date of incorporation"],
    "registered_address": ["address", "registered address", "registered office"],
    "sector_code": ["sector", "sic", "naics", "industry"],
    "website": ["website", "url", "web", "domain"],
}

MAX_ROWS = 10_000

COUNTRY_PATTERN = re.compile(r"^[A-Za-z]{2}$")

# Truncating an alpha-3 code to its first two letters is silently wrong: AUT
# would become AU, which is Australia rather than Austria. Convert the codes we
# know and let the validator reject anything else by name.
ALPHA3 = {
    "GBR": "GB", "USA": "US", "IRL": "IE", "FRA": "FR", "DEU": "DE", "NLD": "NL",
    "BEL": "BE", "ESP": "ES", "ITA": "IT", "PRT": "PT", "CHE": "CH", "AUT": "AT",
    "SWE": "SE", "NOR": "NO", "DNK": "DK", "FIN": "FI", "POL": "PL", "CZE": "CZ",
    "IND": "IN", "CHN": "CN", "JPN": "JP", "SGP": "SG", "AUS": "AU", "NZL": "NZ",
    "CAN": "CA", "MEX": "MX", "BRA": "BR", "ZAF": "ZA", "ARE": "AE", "SAU": "SA",
    "HKG": "HK", "KOR": "KR", "ISR": "IL", "TUR": "TR", "LUX": "LU", "MLT": "MT",
}
COUNTRY_NAMES = {
    "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "united states": "US", "usa": "US", "germany": "DE", "france": "FR",
    "netherlands": "NL", "ireland": "IE", "spain": "ES", "italy": "IT",
    "india": "IN", "singapore": "SG", "australia": "AU", "canada": "CA",
}


def _country(value: str) -> str:
    text = value.strip()
    if len(text) == 2:
        return text.upper()
    if len(text) == 3 and text.upper() in ALPHA3:
        return ALPHA3[text.upper()]
    named = COUNTRY_NAMES.get(text.lower())
    if named:
        return named
    return text.upper()[:16]        # kept intact so the reason names it
# Deliberately loose: registry formats vary widely and rejecting a valid number
# is worse than accepting an odd one, which the claim flow will catch anyway.
REGISTRY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/. ]{2,31}$")


# --------------------------------------------------------------------------
# Parsing and mapping
# --------------------------------------------------------------------------
def parse(data: bytes, filename: str) -> tuple[list[str], list[list[str]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    delimiter = "\t" if filename.lower().endswith(".tsv") else ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def propose_mapping(header: list[str]) -> dict[str, int]:
    """Best-effort column mapping, always shown to the operator for confirmation."""
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        cleaned = (cell or "").strip().lower()
        if not cleaned:
            continue
        for field, hints in HEADER_HINTS.items():
            if field in mapping:
                continue
            if cleaned in hints or any(h == cleaned for h in hints):
                mapping[field] = index
                break
        else:
            for field, hints in HEADER_HINTS.items():
                if field in mapping:
                    continue
                if any(h in cleaned for h in hints):
                    mapping[field] = index
                    break
    return mapping


def prohibited_columns(header: list[str]) -> list[tuple[str, str]]:
    """Named so the operator sees which column was refused and why."""
    found = []
    for cell in header:
        label = (cell or "").strip()
        if not label:
            continue
        for pattern, reason in PROHIBITED_PATTERNS:
            if pattern.search(label):
                found.append((label, reason))
                break
    return found


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def _normalise_row(row: list[str], mapping: dict[str, int]) -> dict:
    out = {}
    for field, index in mapping.items():
        if field not in PERMITTED_FIELDS or index >= len(row):
            continue
        value = (row[index] or "").strip()
        if not value:
            continue
        if field == "country":
            value = _country(value)
        if field in {"lei", "registry_number", "vat_number", "duns"}:
            value = value.replace(" ", "").upper() if field == "lei" else value
        out[field] = value[:255]
    return out


def _parse_date(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y"):
        try:
            from datetime import datetime
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _name_key(name: str) -> str:
    text = (name or "").lower()
    for token in [" limited", " ltd", " plc", " llp", " inc", " gmbh", " bv",
                  " pte", " llc", " fze", " sa", " ag", ".", ",", "(", ")", "&"]:
        text = text.replace(token, " ")
    return re.sub(r"\s+", " ", text).strip()


def classify_row(db: Session, values: dict) -> tuple[str, str, str | None]:
    """Returns (classification, reason, matched_organisation_id)."""
    if not values.get("legal_name"):
        return "reject", "No legal name", None
    if not values.get("country"):
        return "reject", "No country", None
    if not COUNTRY_PATTERN.match(values["country"]):
        return ("reject",
                f"Country '{values['country']}' is not a recognised country code",
                None)
    number = values.get("registry_number")
    if number and not REGISTRY_PATTERN.match(number):
        return "reject", f"Registry number '{number}' fails format validation", None
    if values.get("incorporated_on") and _parse_date(values["incorporated_on"]) is None:
        return "reject", f"Cannot read the date '{values['incorporated_on']}'", None

    # Exact identity match on (country, registry number) is authoritative.
    if number:
        existing = db.scalar(select(Organisation).where(and_(
            func.upper(Organisation.country) == values["country"],
            Organisation.registry_number == number)))
        if existing is not None:
            if existing.claim_status == "claimed":
                return ("skip", f"Already claimed by an existing workspace",
                        existing.id)
            return "update", "Updates an existing unclaimed profile", existing.id

    # Fuzzy name match is a candidate, never a decision.
    key = _name_key(values["legal_name"])
    if len(key) > 4:
        for candidate in db.scalars(select(Organisation).where(
                func.upper(Organisation.country) == values["country"])).all():
            if _name_key(candidate.legal_name) == key:
                if candidate.claim_status == "claimed":
                    return ("skip", "Name matches a claimed profile", candidate.id)
                return ("hold", "Name matches an existing profile but the registry "
                                "number differs or is absent", candidate.id)
    return "create", "New profile", None


def stage(db: Session, *, filename: str, header: list[str], rows: list[list[str]],
          actor=None) -> ImportBatch:
    """Persists the parsed file before any mapping decision. Rows live in the
    database rather than a session cookie, which could not hold ten thousand of
    them and would fail silently at about fifty."""
    batch = ImportBatch(filename=filename, row_count=len(rows), status="staged",
                        mapping_json=json.dumps({"_header": header[:60],
                                                 **propose_mapping(header)}),
                        counts_json=json.dumps({
                            "refused_columns": [{"column": c, "reason": r}
                                                for c, r in prohibited_columns(header)]}),
                        created_by=getattr(actor, "email", "operator"))
    db.add(batch)
    db.flush()
    for position, row in enumerate(rows[:MAX_ROWS], start=1):
        db.add(ImportRow(batch_id=batch.id, position=position,
                         raw_json=json.dumps(row[:60], default=str)[:6000],
                         classification="staged", reason="Awaiting a column mapping"))
    db.flush()
    return batch


def header_of(batch: ImportBatch) -> list[str]:
    try:
        return json.loads(batch.mapping_json or "{}").get("_header", [])
    except ValueError:
        return []


def mapping_of(batch: ImportBatch) -> dict[str, int]:
    try:
        stored = json.loads(batch.mapping_json or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in stored.items() if not k.startswith("_")}


def validate(db: Session, batch: ImportBatch, *, mapping: dict[str, int],
             actor=None) -> ImportBatch:
    """Classifies every staged row against the confirmed mapping. Writes nothing
    to organisation - this is the dry run."""
    header = header_of(batch)
    filename = batch.filename or "import.csv"
    batch.mapping_json = json.dumps({"_header": header, **mapping})
    batch.status = "validated"

    refused = prohibited_columns(header)
    counts = {"create": 0, "update": 0, "skip": 0, "reject": 0, "hold": 0}
    seen_numbers: dict[str, int] = {}

    staged_rows = db.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id)
                             .order_by(ImportRow.position)).all()
    for staged_row in staged_rows:
        position = staged_row.position
        try:
            row = json.loads(staged_row.raw_json or "[]")
        except ValueError:
            row = []
        values = _normalise_row(row, mapping)
        classification, reason, matched = classify_row(db, values)

        # A duplicate inside the same file is a file problem, not a platform one.
        number = values.get("registry_number")
        if classification == "create" and number:
            key = f"{values.get('country')}:{number}"
            if key in seen_numbers:
                classification = "reject"
                reason = f"Duplicate of row {seen_numbers[key]} in this file"
            else:
                seen_numbers[key] = position

        staged_row.normalised_json = json.dumps(values, default=str)
        staged_row.classification = classification
        staged_row.reason = reason
        staged_row.matched_organisation_id = matched
        counts[classification] += 1

    counts["refused_columns"] = [{"column": c, "reason": r} for c, r in refused]
    batch.counts_json = json.dumps(counts, default=str)
    db.flush()

    audit.record(db, tenant_id="platform", action="import.validated",
                 actor_kind="platform_admin", actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "operator"),
                 object_type="import_batch", object_id=batch.id,
                 payload={"filename": filename, "rows": len(staged_rows),
                          "counts": {k: v for k, v in counts.items()
                                     if isinstance(v, int)},
                          "refused_columns": [c for c, _ in refused]})
    return batch


# --------------------------------------------------------------------------
# Commit and reversal
# --------------------------------------------------------------------------
def commit(db: Session, batch: ImportBatch, actor=None) -> dict:
    if batch.status != "validated":
        raise ValueError(f"Batch is {batch.status}, not validated")

    created = updated = 0
    rows = db.scalars(select(ImportRow).where(and_(
        ImportRow.batch_id == batch.id,
        ImportRow.classification.in_(["create", "update"])))).all()

    for row in rows:
        values = json.loads(row.normalised_json or "{}")
        if row.classification == "create":
            organisation = Organisation(
                legal_name=values.get("legal_name", ""),
                country=values.get("country", "GB"),
                claim_status="unclaimed", source="import",
                import_batch_id=batch.id,
                verification_state="unverified")
            db.add(organisation)
            db.flush()
            row.created_organisation_id = organisation.id
            created += 1
        else:
            organisation = db.get(Organisation, row.matched_organisation_id)
            if organisation is None or organisation.claim_status == "claimed":
                continue                    # F26.6 — never touch a claimed profile
            updated += 1

        for field, value in values.items():
            if field not in PERMITTED_FIELDS:
                continue
            if field == "incorporated_on":
                parsed = _parse_date(value)
                if parsed:
                    organisation.incorporated_on = parsed
                continue
            # Import never overwrites a value that is already present on an
            # update: the existing record may be better than the file.
            if row.classification == "update" and getattr(organisation, field, None):
                continue
            setattr(organisation, field, value)

    batch.status = "committed"
    batch.committed_at = now()
    db.flush()

    audit.record(db, tenant_id="platform", action="import.committed",
                 actor_kind="platform_admin", actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "operator"),
                 object_type="import_batch", object_id=batch.id,
                 payload={"created": created, "updated": updated})
    return {"created": created, "updated": updated}


def reversal_blocker(db: Session, batch: ImportBatch) -> str | None:
    """A batch is reversible until one of its profiles is claimed — at that
    point the profile belongs to somebody."""
    claimed = db.scalar(select(func.count(Organisation.id)).where(and_(
        Organisation.import_batch_id == batch.id,
        Organisation.claim_status != "unclaimed")))
    if claimed:
        return (f"{claimed} profile(s) from this batch have been claimed or have a "
                "claim in progress. Reversal would delete a workspace's identity.")
    return None


def reverse(db: Session, batch: ImportBatch, actor=None) -> dict:
    blocker = reversal_blocker(db, batch)
    if blocker:
        batch.reversal_blocked_reason = blocker
        db.flush()
        raise ValueError(blocker)

    organisations = db.scalars(select(Organisation).where(
        Organisation.import_batch_id == batch.id)).all()
    removed = len(organisations)
    for organisation in organisations:
        db.delete(organisation)

    batch.status = "reversed"
    batch.reversed_at = now()
    db.flush()
    audit.record(db, tenant_id="platform", action="import.reversed",
                 actor_kind="platform_admin", actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "operator"),
                 object_type="import_batch", object_id=batch.id,
                 payload={"profiles_removed": removed})
    return {"removed": removed}


def discard(db: Session, batch: ImportBatch, actor=None) -> None:
    for row in db.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id)).all():
        db.delete(row)
    batch.status = "discarded"
    db.flush()


def counts(batch: ImportBatch) -> dict:
    try:
        return json.loads(batch.counts_json or "{}")
    except ValueError:
        return {}
