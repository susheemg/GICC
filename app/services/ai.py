"""The AI layer - GICC-05 §2.

One interface, two implementations. When ANTHROPIC_API_KEY is set the model
path runs; otherwise a deterministic heuristic produces the same shape of
output. Both are recorded in ai_inference with their source, so nothing in the
product can present a proposal as a verified fact.

Three rules hold everywhere in this module:
  * output is a *proposal*, never a fact - a human or a rule converts it
  * nothing here writes to assurance_level (AD10)
  * special-category documents are never sent to a model provider
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import (AI_MAX_INPUT_CHARS, AI_MODEL, AI_PROVIDER, ANTHROPIC_API_KEY,
                      MIN_FIELD_CONFIDENCE)
from ..models import AiInference, DocumentType, now
from . import providers

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# The active provider is resolved per call from the database, so an operator can
# change key or model without a redeploy. A module-level cache would serve a
# revoked key until the next restart.
_SESSION_FACTORY = None

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "certificate", "certification",
    "of", "to", "in", "on", "a", "an", "is", "are", "our", "your", "we", "you", "it",
    "be", "has", "have", "will", "any", "all", "or", "by", "as", "at", "if", "do",
    "does", "please", "provide", "confirm", "describe", "detail", "company", "ltd",
}


def _configured(db: Session | None = None):
    """Database configuration first, environment second. The environment path
    stays so a deployment can be brought up with a key and no console visit."""
    if db is not None:
        config = providers.active(db)
        if config is not None:
            key, error = providers.credentials(config)
            if key is not None:
                meta = providers.provider_meta(config.provider)
                if key or meta["auth"] == "bearer":
                    return config
    return None


def is_enabled(db: Session | None = None) -> bool:
    if _configured(db) is not None:
        return True
    return bool(ANTHROPIC_API_KEY) and AI_PROVIDER == "anthropic"


def status(db: Session | None = None) -> dict:
    if db is not None:
        summary = providers.status_summary(db)
        if summary["enabled"]:
            return summary
        if ANTHROPIC_API_KEY:
            return {"enabled": True, "provider": "anthropic (from environment)",
                    "label": "Anthropic", "model": AI_MODEL,
                    "fallback": "deterministic heuristics", "reason": None}
        return summary
    return {"enabled": bool(ANTHROPIC_API_KEY), "provider": AI_PROVIDER if ANTHROPIC_API_KEY else "none",
            "model": AI_MODEL if ANTHROPIC_API_KEY else None,
            "fallback": "deterministic heuristics"}


def active_model(db: Session | None = None) -> str | None:
    config = _configured(db)
    if config is not None:
        return config.model
    return AI_MODEL if ANTHROPIC_API_KEY else None


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(p or "" for p in parts).encode()).hexdigest()


# --------------------------------------------------------------------------
# Provider call
# --------------------------------------------------------------------------
def _call_model(system: str, prompt: str,
                db: Session | None = None) -> tuple[dict | None, str | None]:
    """Returns (parsed_json, error). Any failure returns an error rather than
    raising: an AI outage must never block an upload."""
    config = _configured(db)
    try:
        import httpx
        if config is not None:
            url, headers, body = providers.build_request(config, system, prompt)
            if url is None:
                return None, "stored key could not be decrypted"
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, headers=headers, json=body)
            if response.status_code != 200:
                return None, f"{config.provider} returned {response.status_code}"
            payload = response.json()
            text = providers.extract_text(config, payload)
            tokens_in, tokens_out = providers.token_usage(config, payload)
            config.calls_made += 1
            config.tokens_in += tokens_in
            config.tokens_out += tokens_out
            config.last_used_at = now()
        else:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    API_URL,
                    headers={"x-api-key": ANTHROPIC_API_KEY,
                             "anthropic-version": API_VERSION,
                             "content-type": "application/json"},
                    json={"model": AI_MODEL, "max_tokens": 1500, "temperature": 0,
                          "system": system,
                          "messages": [{"role": "user", "content": prompt}]})
            if response.status_code != 200:
                return None, f"provider returned {response.status_code}"
            blocks = response.json().get("content", [])
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        text = (text or "").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```")
        return json.loads(text), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _record(db: Session, *, tenant_id: str, kind: str, source: str, confidence: float,
            input_hash: str, output: dict, input_summary: str = "",
            object_type: str | None = None, object_id: str | None = None,
            latency_ms: int = 0, error: str | None = None) -> AiInference:
    inference = AiInference(
        tenant_id=tenant_id, kind=kind, source=source,
        model=active_model(db) if source == "model" else None, confidence=confidence,
        input_hash=input_hash, input_summary=input_summary[:400] or None,
        output_json=json.dumps(output, default=str)[:8000],
        object_type=object_type, object_id=object_id, latency_ms=latency_ms, error=error)
    db.add(inference)
    db.flush()
    return inference


def _cached(db: Session, tenant_id: str, kind: str, input_hash: str) -> dict | None:
    """Identical input, identical answer - and no second call to pay for."""
    row = db.scalar(select(AiInference).where(
        AiInference.tenant_id == tenant_id, AiInference.kind == kind,
        AiInference.input_hash == input_hash, AiInference.error.is_(None))
        .order_by(AiInference.created_at.desc()).limit(1))
    if row is None or not row.output_json:
        return None
    try:
        payload = json.loads(row.output_json)
        payload["_cached"] = True
        payload["source"] = row.source
        return payload
    except ValueError:
        return None


# --------------------------------------------------------------------------
# F16 - classification
# --------------------------------------------------------------------------
def classify_document(db: Session, tenant_id: str, *, filename: str, text: str,
                      catalogue: list[DocumentType]) -> dict:
    excerpt = (text or "")[:AI_MAX_INPUT_CHARS]
    input_hash = _hash("classify", filename, excerpt[:2000])
    cached = _cached(db, tenant_id, "classify", input_hash)
    if cached:
        return cached

    started = time.time()
    result, error, source = None, None, "heuristic"

    if is_enabled(db) and (excerpt.strip() or filename):
        shortlist = _shortlist(filename, excerpt, catalogue, limit=40)
        listing = "\n".join(f"{t.code}: {t.name} (issued by {t.typical_issuer})"
                            for t in shortlist)
        system = ("You classify supplier compliance documents against a fixed catalogue. "
                  "Reply with JSON only, no prose. Never invent a code that is not in the "
                  "list. If the document does not clearly match any entry, return "
                  'type_code null and confidence 0. Schema: {"type_code": str|null, '
                  '"confidence": 0-1, "alternatives": [{"type_code": str, "confidence": '
                  'number}], "fields": {"issuer_name": str|null, "certificate_number": '
                  'str|null, "issue_date": "YYYY-MM-DD"|null, "expiry_date": '
                  '"YYYY-MM-DD"|null, "holder_name": str|null, "scope_statement": str|null, '
                  '"cover_amount": number|null}, "field_confidence": {field: 0-1}, '
                  '"rationale": str}')
        prompt = (f"Catalogue:\n{listing}\n\nFilename: {filename}\n\n"
                  f"Document text (may be partial):\n{excerpt[:6000]}")
        result, error = _call_model(system, prompt, db)
        if result:
            source = "model"

    if result is None:
        result = _classify_heuristic(filename, excerpt, catalogue)

    result.setdefault("fields", {})
    result.setdefault("field_confidence", {})
    result["source"] = source
    result["fields"] = _clean_fields(result["fields"], result.get("field_confidence", {}))

    _record(db, tenant_id=tenant_id, kind="classify", source=source,
            confidence=float(result.get("confidence") or 0), input_hash=input_hash,
            output=result, input_summary=f"{filename} :: {excerpt[:200]}",
            latency_ms=int((time.time() - started) * 1000), error=error)
    return result


def _shortlist(filename: str, text: str, catalogue: list[DocumentType],
               limit: int = 40) -> list[DocumentType]:
    scored = sorted(catalogue, key=lambda t: -_type_score(filename, text, t))
    return scored[:limit]


GENERIC_WORDS = {"certificate", "certification", "statement", "record", "report",
                 "policy", "of", "the", "and", "a", "an", "document"}
# Single words too common on certificates of every kind to identify one. Every
# ISO certificate is headed "Certificate of Registration", so "registration"
# alone would pull all of them towards company registration.
BROAD_WORDS = {"registration", "accreditation", "information", "management",
               "assessment", "insurance", "statement", "compliance", "security"}


def _flat(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower().replace("'", "")).strip() + " "


def _name_phrases(name: str) -> list[str]:
    """Distinctive phrases a catalogue name is written as in real documents.

    Catalogue names are written for a picker - "Public / general liability
    certificate" - but documents say "public liability". The slash is expanded
    both ways and generic words are dropped, so the phrase that actually appears
    on the certificate can be matched whole. Whole-phrase matching is far
    stronger evidence than the token overlap that follows it.
    """
    variants = [name]
    if " / " in name:
        left, right = name.split(" / ", 1)
        variants += [left, right]
        right_words, left_words = right.split(), left.split()
        if len(left_words) == 1 and len(right_words) > 1:
            variants.append(" ".join([left_words[0]] + right_words[1:]))
        if len(right_words) == 1 and len(left_words) > 1:
            variants.append(" ".join(left_words[:-1] + right_words))
    phrases = []
    for variant in variants:
        words = [w for w in _flat(variant).split() if w not in GENERIC_WORDS]
        phrase = " ".join(words)
        single_ok = (len(words) == 1 and len(words[0]) >= 8
                     and words[0] not in BROAD_WORDS)
        if (len(words) >= 2 or single_ok) and phrase not in phrases:
            phrases.append(phrase)
    return phrases


def _type_score(filename: str, text: str, dtype: DocumentType) -> float:
    haystack = _tokens(f"{filename} {text[:4000]}")
    if not haystack:
        return 0.0
    name_tokens = _tokens(dtype.name)
    issuer_tokens = _tokens(dtype.typical_issuer)
    score = 0.0
    if name_tokens:
        score += 3.0 * len(name_tokens & haystack) / len(name_tokens)
    if issuer_tokens:
        score += 1.0 * len(issuer_tokens & haystack) / len(issuer_tokens)
    code_base = dtype.code.split("-")[0].lower()
    blob = f"{filename} {text[:2000]}".lower()
    if dtype.code.lower() in blob:
        score += 6.0
    # Standard numbers are the strongest signal available without a model.
    for marker, bonus in [("27001", 5), ("9001", 5), ("14001", 5), ("45001", 5),
                          ("22301", 5), ("13485", 5), ("27701", 5), ("42001", 5),
                          ("soc 2", 5), ("soc2", 5), ("pci", 4), ("cyber essentials", 5),
                          ("employers liability", 4), ("public liability", 4),
                          ("professional indemnity", 4), ("cyber liability", 4)]:
        if marker in blob and marker.replace(" ", "") in dtype.name.lower().replace(" ", ""):
            score += bonus
    if code_base in blob:
        score += 1.0
    # A filename and a document's opening lines are where its type is stated.
    title = _flat(f"{filename} {(text or '')[:400]}")
    if any(f" {phrase} " in title for phrase in _name_phrases(dtype.name)):
        score += 6.0
    return score


def _classify_heuristic(filename: str, text: str, catalogue: list[DocumentType]) -> dict:
    ranked = sorted(((_type_score(filename, text, t), t) for t in catalogue),
                    key=lambda pair: -pair[0])
    best_score, best = ranked[0] if ranked else (0.0, None)
    if best is None or best_score <= 0.5:
        return {"type_code": None, "confidence": 0.0, "alternatives": [],
                "fields": _extract_fields_heuristic(text),
                "field_confidence": {},
                "rationale": "No catalogue entry matched the filename or the text."}
    confidence = min(0.85, round(best_score / 12.0, 2))
    alternatives = [{"type_code": t.code, "confidence": min(0.8, round(s / 12.0, 2))}
                    for s, t in ranked[1:4] if s > 0.5]
    fields = _extract_fields_heuristic(text)
    return {"type_code": best.code, "confidence": confidence,
            "alternatives": alternatives, "fields": fields,
            "field_confidence": {k: 0.65 for k, v in fields.items() if v},
            "rationale": (f"Matched '{best.name}' on filename and document text. "
                          "Produced without a model - confirm before saving.")}


DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "%Y-%m-%d"),
    (re.compile(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})",
                re.I), "dmy"),
    (re.compile(r"(\d{2})/(\d{2})/(\d{4})"), "%d/%m/%Y"),
]
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _extract_fields_heuristic(text: str) -> dict:
    """Cheap, honest extraction: certificate numbers and dates have recognisable
    shapes. Everything else is left to a human or a model."""
    if not text:
        return {}
    fields: dict[str, object] = {}
    window = text[:AI_MAX_INPUT_CHARS]

    number = re.search(r"\b(?:certificate|cert|registration|policy|licence|license)"
                       r"\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/ ]{3,24})",
                       window, re.I)
    if number:
        fields["certificate_number"] = number.group(1).strip()
    else:
        iso_style = re.search(r"\b((?:IS|FS|EMS|OHS|BCMS|MD|PIMS)\s?\d{5,7})\b", window)
        if iso_style:
            fields["certificate_number"] = iso_style.group(1).strip()

    dates = _find_dates(window)
    if dates:
        fields["issue_date"] = dates[0].isoformat()
        if len(dates) > 1:
            fields["expiry_date"] = max(dates).isoformat()

    cover = re.search(r"(?:limit of indemnity|sum insured|cover(?:age)?)\D{0,20}"
                      r"([\d,]{4,15})", window, re.I)
    if cover:
        try:
            fields["cover_amount"] = float(cover.group(1).replace(",", ""))
        except ValueError:
            pass
    return fields


def _find_dates(text: str) -> list[date]:
    found: list[date] = []
    for pattern, fmt in DATE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                if fmt == "dmy":
                    day, month, year = match.groups()
                    found.append(date(int(year), MONTHS[month.lower()[:3]], int(day)))
                elif fmt == "%Y-%m-%d":
                    found.append(date(*(int(g) for g in match.groups())))
                else:
                    day, month, year = match.groups()
                    found.append(date(int(year), int(month), int(day)))
            except (ValueError, KeyError):
                continue
    return sorted({d for d in found if 1990 <= d.year <= 2100})


def _clean_fields(fields: dict, confidence: dict) -> dict:
    """Below the confidence floor a field is left blank. A wrong pre-fill that a
    human waves through is worse than an empty box (GICC-05 F16.6)."""
    cleaned = {}
    for key, value in (fields or {}).items():
        if value in (None, "", "null"):
            continue
        if float(confidence.get(key, 0.65)) < MIN_FIELD_CONFIDENCE:
            continue
        cleaned[key] = value
    return cleaned


# --------------------------------------------------------------------------
# F17 - version change summary
# --------------------------------------------------------------------------
def version_diff(previous, incoming) -> dict:
    """Deterministic. The model may phrase it; the rules decide what is
    material, because material changes notify grantees."""
    changes, material = [], []
    if previous is None:
        return {"changes": ["First version of this document"], "material": [],
                "reason": "Initial upload"}

    if incoming.expiry_date != previous.expiry_date:
        changes.append(f"Expiry moves from {previous.expiry_date or 'none'} "
                       f"to {incoming.expiry_date or 'none'}")
        if (previous.expiry_date and incoming.expiry_date
                and incoming.expiry_date < previous.expiry_date):
            material.append("Validity is shorter than the version it replaces")
    if (previous.cover_amount or 0) != (incoming.cover_amount or 0):
        changes.append(f"Cover moves from {previous.cover_amount or 0:,.0f} "
                       f"to {incoming.cover_amount or 0:,.0f}")
        if (incoming.cover_amount or 0) < (previous.cover_amount or 0):
            material.append("Cover has been reduced")
    if (incoming.issuer_name or "") != (previous.issuer_name or ""):
        changes.append(f"Issuer changes from {previous.issuer_name or 'none'} "
                       f"to {incoming.issuer_name or 'none'}")
        material.append("The issuing body has changed")
    if (incoming.certificate_number or "") != (previous.certificate_number or ""):
        changes.append("Certificate number differs from the previous version")
    old_scope, new_scope = previous.scope_statement or "", incoming.scope_statement or ""
    if old_scope and new_scope and len(new_scope) < len(old_scope) * 0.8:
        changes.append("Scope statement is materially shorter")
        material.append("Scope appears to have narrowed")

    reason = "; ".join(changes[:2]) if changes else "Replacement issued, no field changes"
    return {"changes": changes, "material": material, "reason": reason}


# --------------------------------------------------------------------------
# F18 - control summary
# --------------------------------------------------------------------------
def control_summary(db: Session, tenant_id: str, *, org_name: str,
                    facts: list[dict], gaps: list[str]) -> dict:
    fingerprint = _hash("summary", json.dumps(facts, sort_keys=True, default=str))
    started = time.time()
    source, error, body = "heuristic", None, None

    if is_enabled(db) and facts:
        system = ("You write control summaries for supplier due diligence. Use only the "
                  "evidence given. Never claim a control that is not in the evidence. "
                  "Describe L0 and L1 evidence as claimed rather than demonstrated. "
                  "State the gaps plainly. Six short paragraphs maximum, no headings, "
                  "no marketing language. Reply with JSON: {\"body\": str}")
        prompt = (f"Supplier: {org_name}\n\nEvidence held:\n"
                  + json.dumps(facts, indent=2, default=str)
                  + f"\n\nNotable gaps:\n{json.dumps(gaps)}")
        result, error = _call_model(system, prompt, db)
        if result and result.get("body"):
            body, source = result["body"], "model"

    if body is None:
        body = _summary_heuristic(org_name, facts, gaps)

    output = {"body": body, "fingerprint": fingerprint}
    _record(db, tenant_id=tenant_id, kind="control_summary", source=source,
            confidence=0.7 if source == "model" else 0.5, input_hash=fingerprint,
            output=output, latency_ms=int((time.time() - started) * 1000), error=error)
    return {"body": body, "source": source, "fingerprint": fingerprint}


DOMAIN_SENTENCES = {
    "LEG": "Corporate identity is evidenced by {items}.",
    "TAX": "Tax registration is evidenced by {items}.",
    "OWN": "Ownership and integrity controls are evidenced by {items}.",
    "FIN": "Financial standing is evidenced by {items}.",
    "INS": "Insurance cover is evidenced by {items}.",
    "LIC": "Sector authorisation is evidenced by {items}.",
    "QMS": "Management systems are certified as {items}.",
    "SEC": "Information security and privacy are evidenced by {items}.",
    "HSE": "Health, safety and environmental controls are evidenced by {items}.",
    "ESG": "Sustainability and ethics are evidenced by {items}.",
    "PPL": "People and vetting controls are evidenced by {items}.",
    "COM": "Commercial and contractual terms are evidenced by {items}.",
    "TEC": "Technology and product assurance is evidenced by {items}.",
}


def _summary_heuristic(org_name: str, facts: list[dict], gaps: list[str]) -> str:
    by_domain: dict[str, list[dict]] = {}
    for fact in facts:
        by_domain.setdefault(fact["code"][:3], []).append(fact)

    paragraphs = [f"{org_name} holds {len(facts)} credentials in this vault. "
                  "The summary below describes only what is held; it makes no claim "
                  "about controls that are not evidenced."]
    for code, items in sorted(by_domain.items()):
        template = DOMAIN_SENTENCES.get(code)
        if not template:
            continue
        described = []
        for item in sorted(items, key=lambda i: i["code"])[:6]:
            qualifier = ("verified against the issuing source"
                         if item["assurance"] in {"L3", "L4"} else
                         "reviewed by a person" if item["assurance"] == "L2" else
                         "self-declared and not independently verified")
            state = "" if item["status"] == "valid" else f", currently {item['status'].replace('_', ' ')}"
            described.append(f"{item['name']} ({qualifier}{state})")
        paragraphs.append(template.format(items="; ".join(described)))

    unverified = [f for f in facts if f["assurance"] in {"L0", "L1"}]
    if unverified:
        paragraphs.append(
            f"{len(unverified)} of the {len(facts)} credentials are self-declared or "
            "only source-linked. Treat those as claims rather than demonstrated controls.")
    impaired = [f for f in facts if f["status"] in {"expired", "suspended"}]
    if impaired:
        paragraphs.append(
            "Currently impaired: " + ", ".join(f"{f['name']} ({f['status']})"
                                               for f in impaired[:8]) + ".")
    if gaps:
        paragraphs.append("Not held: " + ", ".join(gaps[:10]) + ".")
    return "\n\n".join(paragraphs)


# --------------------------------------------------------------------------
# F21 - questionnaire answering
# --------------------------------------------------------------------------
def draft_answer(db: Session, tenant_id: str, *, question: str,
                 evidence: list[dict], org_name: str) -> dict:
    """Answers only from cited evidence. No evidence, no answer - that is a
    first-class outcome, not a failure (GICC-05 F21.4)."""
    if not evidence:
        return {"answer": None, "confidence": 0.0, "source": "rule",
                "status": "no_evidence",
                "note": "No document in this vault supports an answer to this question."}

    input_hash = _hash("answer", question, json.dumps(evidence, sort_keys=True, default=str))
    cached = _cached(db, tenant_id, "answer", input_hash)
    if cached:
        return cached

    started = time.time()
    source, error, answer = "heuristic", None, None

    if is_enabled(db):
        system = ("You draft supplier questionnaire answers strictly from supplied "
                  "evidence. Never state a control that the evidence does not show. "
                  "If the evidence only partly answers the question, say what is held "
                  "and what is not. Two or three sentences. Reply with JSON: "
                  '{"answer": str, "confidence": 0-1, "fully_answered": bool}')
        prompt = (f"Supplier: {org_name}\nQuestion: {question}\n\nEvidence:\n"
                  + json.dumps(evidence, indent=2, default=str))
        result, error = _call_model(system, prompt, db)
        if result and result.get("answer"):
            answer = result["answer"]
            confidence = float(result.get("confidence", 0.6))
            source = "model"

    if answer is None:
        primary = evidence[0]
        others = evidence[1:3]
        answer = (f"Yes. {org_name} holds {primary['name']}"
                  + (f" ({primary['certificate_number']})" if primary.get("certificate_number") else "")
                  + (f", issued by {primary['issuer']}" if primary.get("issuer") else "")
                  + (f", valid to {primary['expiry']}" if primary.get("expiry") else "")
                  + f". Assurance level {primary['assurance']}"
                  + (" — verified against the issuing source."
                     if primary["assurance"] in {"L3", "L4"} else
                     " — not independently verified.")
                  + (" Supporting evidence: " + ", ".join(o["name"] for o in others) + "."
                     if others else ""))
        confidence = 0.55

    output = {"answer": answer, "confidence": round(confidence, 2), "source": source,
              "status": "drafted"}
    _record(db, tenant_id=tenant_id, kind="answer", source=source,
            confidence=output["confidence"], input_hash=input_hash, output=output,
            input_summary=question[:200],
            latency_ms=int((time.time() - started) * 1000), error=error)
    return output
