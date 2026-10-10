"""Questionnaire automation - GICC-05 F21.

The rule that shapes everything here: an answer must cite a document version or
it is not an answer. "No evidence" is a first-class outcome, shown prominently,
because a plausible unsupportable claim is the worst thing this feature could
produce.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import (AnswerBank, Document, DocumentType, DocumentVersion,
                      Organisation, Questionnaire, QuestionnaireItem, Tenant, now)
from . import ai, audit

QUESTION_HINT = re.compile(r"question|requirement|control|query|ask|item|description",
                           re.I)
REFERENCE_HINT = re.compile(r"^(ref|id|no|number|item|q)\.?$", re.I)

# Keyword vocabulary mapping question language onto catalogue codes. The model
# refines this when enabled; without it this is the whole mapping, and it is
# deliberately conservative - a wrong citation is worse than no citation.
KEYWORDS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("iso 27001", "isms", "information security management system"), ("SEC-001",)),
    (("statement of applicability", "soa"), ("SEC-002",)),
    (("soc 2", "soc2", "service organisation control"), ("SEC-008", "SEC-007")),
    (("penetration test", "pen test", "pentest"), ("SEC-017", "SEC-018")),
    (("vulnerability", "patching"), ("SEC-019",)),
    (("cyber essentials",), ("SEC-012",)),
    (("pci dss", "cardholder"), ("SEC-011",)),
    (("iso 27701", "privacy information"), ("SEC-005",)),
    (("subprocessor", "sub-processor", "fourth party", "supply chain"), ("SEC-026",)),
    (("data processing agreement", "dpa", "standard contractual"), ("SEC-027",)),
    (("dpia", "data protection impact"), ("SEC-028",)),
    (("data flow", "data inventory", "records of processing"), ("SEC-025",)),
    (("incident response", "security incident"), ("SEC-022", "SEC-023")),
    (("business continuity", "bcp", "iso 22301"), ("SEC-020", "QMS-005")),
    (("disaster recovery", "rto", "rpo"), ("SEC-021",)),
    (("information security policy", "security policy"), ("SEC-030",)),
    (("iso 9001", "quality management"), ("QMS-001",)),
    (("iso 14001", "environmental management"), ("QMS-002",)),
    (("iso 45001", "occupational health"), ("QMS-003",)),
    (("iso 13485", "medical device"), ("QMS-007",)),
    (("professional indemnity", "errors and omissions"), ("INS-003",)),
    (("public liability", "general liability"), ("INS-001",)),
    (("employers liability", "employer's liability"), ("INS-002",)),
    (("cyber insurance", "cyber liability"), ("INS-004",)),
    (("product liability",), ("INS-005",)),
    (("workers compensation",), ("INS-008",)),
    (("certificate of incorporation", "registered", "companies house", "legal entity"),
     ("LEG-001", "LEG-002")),
    (("lei", "legal entity identifier"), ("LEG-011",)),
    (("vat", "tax registration"), ("TAX-001",)),
    (("tax clearance", "no dues"), ("TAX-004",)),
    (("beneficial owner", "ubo", "ownership"), ("OWN-001", "OWN-003")),
    (("sanctions", "denied party"), ("OWN-005",)),
    (("anti-bribery", "bribery", "corruption", "abc policy"), ("OWN-009",)),
    (("code of conduct",), ("OWN-010",)),
    (("whistleblow",), ("OWN-011",)),
    (("litigation", "regulatory action"), ("OWN-012",)),
    (("audited accounts", "financial statements", "annual accounts"), ("FIN-001",)),
    (("credit rating", "credit report"), ("FIN-006",)),
    (("going concern", "solvency"), ("FIN-007",)),
    (("modern slavery",), ("ESG-008",)),
    (("ecovadis",), ("ESG-001",)),
    (("carbon", "ghg", "emissions", "net zero"), ("ESG-005", "ESG-006")),
    (("conflict minerals",), ("ESG-010",)),
    (("health and safety policy", "h&s policy"), ("HSE-001",)),
    (("risk assessment", "rams", "method statement"), ("HSE-002",)),
    (("accident", "riddor", "incident statistics"), ("HSE-003",)),
    (("ssip", "chas", "safecontractor"), ("HSE-005",)),
    (("right to work", "immigration"), ("PPL-001",)),
    (("background screening", "bs7858", "vetting"), ("PPL-002", "PPL-003")),
    (("training", "competency"), ("PPL-006",)),
    (("exit plan", "transition", "termination assistance"), ("COM-005",)),
    (("escrow",), ("COM-006",)),
    (("service level", "sla"), ("COM-003",)),
    (("sbom", "bill of materials"), ("TEC-005",)),
    (("open source", "licence compliance"), ("TEC-013",)),
    (("accessibility", "vpat", "wcag"), ("TEC-009",)),
    (("ai", "model card", "algorithmic"), ("TEC-010", "SEC-006", "TEC-012")),
    (("uptime", "availability"), ("TEC-014",)),
]


def question_hash(question: str) -> str:
    normalised = re.sub(r"[^a-z0-9 ]", " ", question.lower())
    normalised = " ".join(sorted(set(normalised.split())))
    return hashlib.sha256(normalised.encode()).hexdigest()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse(data: bytes, filename: str) -> list[dict]:
    """CSV, TSV, Markdown or plain text. One question per row or line."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")

    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else "txt"
    if suffix in {"csv", "tsv"}:
        return _parse_delimited(text, "\t" if suffix == "tsv" else ",")
    return _parse_lines(text)


def _parse_delimited(text: str, delimiter: str) -> list[dict]:
    reader = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not reader:
        return []
    header = reader[0]
    question_col, reference_col = 0, None
    if any(QUESTION_HINT.search(cell or "") for cell in header):
        for index, cell in enumerate(header):
            if QUESTION_HINT.search(cell or "") and question_col == 0:
                question_col = index
            if REFERENCE_HINT.match((cell or "").strip()):
                reference_col = index
        rows = reader[1:]
    else:
        rows = reader
        if len(header) > 1:
            reference_col, question_col = 0, 1

    items = []
    for row in rows:
        if not row or question_col >= len(row):
            continue
        question = (row[question_col] or "").strip()
        if len(question) < 8:
            continue
        reference = (row[reference_col].strip()
                     if reference_col is not None and reference_col < len(row) else None)
        items.append({"question": question, "reference": reference or None})
    return items


def _parse_lines(text: str) -> list[dict]:
    items = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if len(line) < 8 or line.startswith("#"):
            continue
        reference = None
        match = re.match(r"^([A-Z]{0,4}[-.]?\d+(?:\.\d+)*)[).:\s]\s*(.+)$", line)
        if match:
            reference, line = match.group(1), match.group(2).strip()
        items.append({"question": line, "reference": reference})
    return items


# --------------------------------------------------------------------------
# Mapping and answering
# --------------------------------------------------------------------------
def map_types(question: str) -> list[str]:
    lowered = question.lower()
    matched: list[str] = []
    for phrases, codes in KEYWORDS:
        if any(phrase in lowered for phrase in phrases):
            for code in codes:
                if code not in matched:
                    matched.append(code)
    return matched[:4]


def _evidence(db: Session, tenant: Tenant, codes: list[str]) -> list[dict]:
    evidence = []
    for code in codes:
        row = db.execute(
            select(Document, DocumentType)
            .join(DocumentType, Document.document_type_id == DocumentType.id)
            .where(and_(Document.tenant_id == tenant.id, DocumentType.code == code,
                        Document.deleted_at.is_(None)))).first()
        if row is None:
            continue
        document, dtype = row
        version = (db.get(DocumentVersion, document.active_version_id)
                   if document.active_version_id else None)
        if version is None:
            continue
        evidence.append({
            "type_code": dtype.code, "name": document.title,
            "version_id": version.id, "assurance": version.assurance_level,
            "status": document.derived_status,
            "certificate_number": version.certificate_number,
            "issuer": version.issuer_name,
            "expiry": version.expiry_date.isoformat() if version.expiry_date else None,
            "scope": (version.scope_statement or "")[:300],
            "cover_amount": version.cover_amount,
        })
    return evidence


def build(db: Session, tenant: Tenant, questionnaire: Questionnaire,
          rows: list[dict], actor=None) -> Questionnaire:
    org = db.get(Organisation, tenant.organisation_id)
    answered = gaps = 0

    for position, row in enumerate(rows, start=1):
        question = row["question"]
        codes = map_types(question)
        evidence = _evidence(db, tenant, codes)

        banked = db.scalar(select(AnswerBank).where(and_(
            AnswerBank.tenant_id == tenant.id,
            AnswerBank.question_hash == question_hash(question))))

        if banked and evidence:
            answer, confidence, source, status = banked.answer, 0.9, "answer_bank", "drafted"
            banked.times_used += 1
        elif evidence:
            drafted = ai.draft_answer(db, tenant.id, question=question,
                                      evidence=evidence, org_name=org.legal_name)
            answer = drafted["answer"]
            confidence = drafted["confidence"]
            source = drafted["source"]
            status = "drafted"
        else:
            answer, confidence, source, status = (
                None, 0.0, "rule",
                "no_evidence" if codes else "unmapped")

        if status == "drafted":
            answered += 1
        else:
            gaps += 1

        db.add(QuestionnaireItem(
            questionnaire_id=questionnaire.id, position=position,
            reference=row.get("reference"), question=question,
            mapped_types=",".join(codes) or None, answer=answer,
            citations_json=json.dumps([{"type_code": e["type_code"], "name": e["name"],
                                        "version_id": e["version_id"],
                                        "assurance": e["assurance"],
                                        "status": e["status"]} for e in evidence]),
            confidence=confidence, source=source, status=status))

    questionnaire.item_count = len(rows)
    questionnaire.answered_count = answered
    questionnaire.gap_count = gaps
    questionnaire.status = "drafted"
    db.flush()

    audit.record(db, tenant_id=tenant.id, action="questionnaire.drafted",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="questionnaire", object_id=questionnaire.id,
                 payload={"name": questionnaire.name, "items": len(rows),
                          "answered": answered, "gaps": gaps,
                          "engine": "model" if ai.is_enabled() else "heuristic"})
    return questionnaire


def approve(db: Session, tenant: Tenant, item: QuestionnaireItem, actor) -> None:
    """Approving banks the answer, so the next questionnaire asking the same
    thing is pre-filled - F21.7, the compounding part."""
    item.status = "approved"
    item.approved_at = now()
    if not item.answer:
        return
    key = question_hash(item.question)
    existing = db.scalar(select(AnswerBank).where(and_(
        AnswerBank.tenant_id == tenant.id, AnswerBank.question_hash == key)))
    if existing:
        existing.answer = item.answer
        existing.citation_types = item.mapped_types
        existing.times_used += 1
    else:
        db.add(AnswerBank(tenant_id=tenant.id, question_hash=key, question=item.question,
                          answer=item.answer, citation_types=item.mapped_types))
    db.flush()


def export_csv(db: Session, questionnaire: Questionnaire) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Reference", "Question", "Answer", "Status", "Confidence",
                     "Evidence cited", "Assurance", "Version IDs"])
    for item in questionnaire.items:
        citations = json.loads(item.citations_json or "[]")
        writer.writerow([
            item.reference or "", item.question, item.answer or "",
            item.status, f"{item.confidence:.2f}",
            "; ".join(c["name"] for c in citations),
            "; ".join(c["assurance"] for c in citations),
            "; ".join(c["version_id"] for c in citations)])
    return output.getvalue()
