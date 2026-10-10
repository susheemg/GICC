"""Demo directory - real companies from the Indian MCA register.

These are **real companies**. That governs everything this module does.

They are loaded exactly as F26 loads a bulk import: as **unclaimed,
import-sourced organisations**, with public-register identity and nothing else.
No workspace, no user, no credentials, no Trust Index, no assurance level. That
is not a shortcut, it is the only defensible option — attaching an invented ISO
27001 certificate to a named, real business would put a fabricated compliance
claim against a company that has never heard of this platform, and a demo is
precisely where that would be seen.

The six fictional entities in `demo.py` keep their credential registers, their
calibration records and their Trust Index. They all use `.example` domains under
RFC 2606, and they are the only entities in the demo that hold documents.

What this gives the demo is the thing fictional data cannot: a directory with
real scale, real naming collisions, and real mess. Struck-off companies. Ragged
rows where the register itself has gaps. Addresses that are plainly residential.
Buyer search against 2,400 genuine records behaves like the product would;
buyer search against twelve invented ones does not.

It also demonstrates the rule that matters most here. Because every one of these
is unclaimed, the buyer directory withholds their registered addresses on its
own (K6) — a visitor sees the rule working rather than being told about it.

Source: Ministry of Corporate Affairs, Government of India, company master data,
published as open register data. Sampled from 1,473,784 records.
"""
from __future__ import annotations

import csv
import gzip
import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import ImportBatch, ImportRow, Organisation, now

DATA_FILE = Path(__file__).parent / "data" / "mca_companies.csv.gz"

SOURCE_NOTE = ("Ministry of Corporate Affairs (India) company master data — "
               "open register data, sampled from 1,473,784 records")

# Struck-off and dissolved companies are kept deliberately. A directory in which
# every company is in good standing teaches a buyer nothing; finding a supplier
# that was struck off in 2019 is the point.
STATUS_NOTE = {
    "Strike Off": "Struck off the register",
    "Under process of striking off": "Strike-off in progress",
    "Amalgamated": "Amalgamated into another entity",
    "Converted to LLP": "Converted to an LLP",
    "Dissolved (Liquidated)": "Dissolved following liquidation",
    "Inactive for e-filing": "Inactive for e-filing",
}


def rows() -> list[dict]:
    """From disk in the package build; from the embedded blob in the
    single-file build, where there is no data directory to read."""
    import io

    if DATA_FILE.exists():
        payload = DATA_FILE.read_bytes()
    else:
        payload = globals().get("DIRECTORY_CSV_GZ", b"")
    if not payload:
        return []
    with gzip.open(io.BytesIO(payload), "rt", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _date(value: str) -> date | None:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def build(db: Session, limit: int | None = None) -> int:
    """Idempotent: does nothing if the directory has already been loaded."""
    already = db.scalar(select(func.count(Organisation.id)).where(
        Organisation.source == "mca_register"))
    if already:
        return 0

    records = rows()
    if limit:
        records = records[:limit]
    if not records:
        return 0

    # A real ImportBatch, so the directory has a provenance an operator can open
    # at /admin/import rather than appearing from nowhere.
    batch = ImportBatch(
        filename="mca_company_master_sample.csv", row_count=len(records),
        status="committed", created_by="platform@gicc.example",
        committed_at=now(),
        mapping_json=json.dumps({"_header": ["CIN", "CompanyName", "CompanyROCcode",
                                             "CompanyRegistrationdate_date",
                                             "Registered_Office_Address"],
                                 "legal_name": 1, "country": -1,
                                 "registry_number": 0, "registry_authority": 2}),
        counts_json=json.dumps({"create": len(records), "update": 0, "skip": 0,
                                "reject": 0, "hold": 0,
                                "refused_columns": [
                                    {"column": "PaidupCapital",
                                     "reason": "financial figures"}],
                                "source": SOURCE_NOTE}))
    db.add(batch)
    db.flush()

    organisations, import_rows = [], []
    for position, record in enumerate(records, start=1):
        status = (record.get("status") or "").strip()
        industry = (record.get("industry") or "").strip()
        note = STATUS_NOTE.get(status)
        description = " · ".join(part for part in [
            industry or None,
            note,
            "Listed" if record.get("listing") == "Listed" else None,
        ] if part) or None

        organisation = Organisation(
            legal_name=record["name"][:255],
            country="IN",
            registry_number=record["cin"][:64] or None,
            registry_authority=(record.get("roc") or "")[:96] or None,
            entity_type=record.get("entity_type") or "private_limited",
            incorporated_on=_date(record.get("incorporated", "")),
            registered_address=(record.get("address") or "")[:1000] or None,
            sector_code=(record.get("nic") or "")[:32] or None,
            description=description,
            # The whole point: nobody here has agreed to anything.
            claim_status="unclaimed",
            verification_state="unverified",
            source="mca_register",
            import_batch_id=batch.id)
        organisations.append(organisation)
        import_rows.append({
            "batch_id": batch.id, "position": position,
            "classification": "create",
            "reason": "New profile from the public register",
            "normalised_json": json.dumps({
                "legal_name": record["name"], "country": "IN",
                "registry_number": record["cin"],
                "registry_authority": record.get("roc", "")}, default=str)})

    db.add_all(organisations)
    db.flush()

    for row, organisation in zip(import_rows, organisations):
        row["created_organisation_id"] = organisation.id
    db.bulk_insert_mappings(ImportRow, import_rows)
    db.flush()
    return len(organisations)


def summary(db: Session) -> dict:
    total = db.scalar(select(func.count(Organisation.id)).where(
        Organisation.source == "mca_register")) or 0
    claimed = db.scalar(select(func.count(Organisation.id)).where(
        Organisation.source == "mca_register",
        Organisation.claim_status == "claimed")) or 0
    return {"total": total, "claimed": claimed, "source": SOURCE_NOTE}
