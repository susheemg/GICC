"""Platform analytics - GICC-06 F25.

Every figure is a live aggregate over the operational tables. No warehouse, no
scheduled rollup that can silently go stale. That stops being viable somewhere
past a few thousand tenants; the migration path is a nightly rollup into
platform_metric_daily, and the query layer is contained here so that swap
touches one module.

Charts render as inline SVG. The application has no JavaScript build step and
should not acquire one for an internal console — and a server-rendered chart
prints correctly, which a canvas library does not guarantee.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import (AccessGrant, AccessRequest, AgentCallLog, AiInference,
                      AiProviderConfig, AuditEvent, AuthenticitySignal,
                      BreakGlassSession, ClientAccount, Document, DocumentType,
                      DocumentVersion, Notification, OnboardingProgress,
                      Questionnaire, ShareLinkView, Tenant, TrustScore, User,
                      VerificationCheck, now)
from . import onboarding
from .documents import health_band, health_score

# GICC-06 §1.1 — directional reference points from published 2026 research.
TARGET_ACTIVATION = 45.0
BENCHMARK_MEDIAN = 37.0
BENCHMARK_TOP_QUARTILE = 68.0
ACTIVATION_WINDOW_DAYS = 14

PERIODS = {"7": "Last 7 days", "30": "Last 30 days", "90": "Last 90 days",
           "365": "Last year", "all": "All time"}


def cutoff(period: str) -> datetime | None:
    if period == "all":
        return None
    try:
        return now() - timedelta(days=int(period))
    except ValueError:
        return now() - timedelta(days=30)


# --------------------------------------------------------------------------
# SVG charts
# --------------------------------------------------------------------------
NAVY, STEEL, OK, WARN, BAD, LINE, MUTED = (
    "#1F3A70", "#3E6FB0", "#1B7F5A", "#B8860B", "#B3261E", "#DDE2EA", "#6B7280")


def _svg(body: str, width: int, height: int) -> str:
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'preserveAspectRatio="xMidYMid meet" '
            f'font-family="Carlito, Calibri, Helvetica, sans-serif">{body}</svg>')


def bar_chart(pairs: list[tuple[str, float]], *, height: int = 190,
              colour: str = STEEL, suffix: str = "") -> str:
    """Horizontal bars. Labels sit outside, so a long category name does not
    have to fit inside its own bar."""
    if not pairs:
        return '<p class="small muted">No data in this period.</p>'
    width, label_w, pad = 640, 200, 8
    row_h = max(18, min(30, (height - 10) // max(len(pairs), 1)))
    height = row_h * len(pairs) + 10
    top = max((abs(v) for _, v in pairs), default=1) or 1
    bar_max = width - label_w - 70
    parts = []
    for index, (label, value) in enumerate(pairs):
        y = index * row_h + 4
        length = max(1, abs(value) / top * bar_max)
        parts.append(f'<text x="{label_w - 8}" y="{y + row_h * 0.68}" font-size="11" '
                     f'fill="#1A1D23" text-anchor="end">{_esc(label)[:34]}</text>')
        parts.append(f'<rect x="{label_w}" y="{y + 3}" width="{length}" '
                     f'height="{row_h - 9}" fill="{colour}" rx="1"/>')
        parts.append(f'<text x="{label_w + length + 6}" y="{y + row_h * 0.68}" '
                     f'font-size="10.5" fill="{MUTED}">'
                     f'{_fmt(value)}{suffix}</text>')
    return _svg("".join(parts), width, height)


def line_chart(points: list[tuple[str, float]], *, height: int = 200,
               suffix: str = "", target: float | None = None) -> str:
    if len(points) < 2:
        return '<p class="small muted">Not enough periods to plot a trend yet.</p>'
    width, pad_l, pad_b, pad_t = 640, 40, 26, 14
    top = max(max(v for _, v in points), target or 0) or 1
    top = top * 1.15
    plot_w, plot_h = width - pad_l - 16, height - pad_b - pad_t
    step = plot_w / (len(points) - 1)

    def xy(index, value):
        return pad_l + index * step, pad_t + plot_h - (value / top * plot_h)

    parts = []
    for fraction in (0, 0.5, 1.0):
        y = pad_t + plot_h - fraction * plot_h
        parts.append(f'<line x1="{pad_l}" y1="{y}" x2="{width - 16}" y2="{y}" '
                     f'stroke="{LINE}" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{y + 3.5}" font-size="9.5" '
                     f'fill="{MUTED}" text-anchor="end">{_fmt(top * fraction)}</text>')
    if target is not None:
        y = pad_t + plot_h - (target / top * plot_h)
        parts.append(f'<line x1="{pad_l}" y1="{y}" x2="{width - 16}" y2="{y}" '
                     f'stroke="{OK}" stroke-width="1.2" stroke-dasharray="5 3"/>')
        parts.append(f'<text x="{width - 18}" y="{y - 4}" font-size="9.5" fill="{OK}" '
                     f'text-anchor="end">target {_fmt(target)}{suffix}</text>')

    coords = [xy(i, v) for i, (_, v) in enumerate(points)]
    path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
    parts.append(f'<path d="{path}" fill="none" stroke="{STEEL}" stroke-width="2"/>')
    for (x, y), (label, value) in zip(coords, points):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{NAVY}"/>')
        parts.append(f'<text x="{x:.1f}" y="{y - 8:.1f}" font-size="9.5" fill="{NAVY}" '
                     f'text-anchor="middle">{_fmt(value)}{suffix}</text>')
    for index, (label, _) in enumerate(points):
        x = pad_l + index * step
        parts.append(f'<text x="{x:.1f}" y="{height - 8}" font-size="9.5" '
                     f'fill="{MUTED}" text-anchor="middle">{_esc(label)}</text>')
    return _svg("".join(parts), width, height)


def funnel_chart(stages: list[tuple[str, int]]) -> str:
    """Drop-off is the point of a funnel, so the conversion between each pair of
    stages is drawn larger than the counts themselves."""
    if not stages or not stages[0][1]:
        return '<p class="small muted">No registrations in this period.</p>'
    width, row_h = 640, 52
    height = len(stages) * row_h + 12
    top = stages[0][1] or 1
    bar_max = width - 230
    parts = []
    for index, (label, count) in enumerate(stages):
        y = index * row_h + 6
        length = max(2, count / top * bar_max)
        share = count / top * 100
        colour = STEEL if index else NAVY
        parts.append(f'<text x="182" y="{y + 20}" font-size="11.5" fill="#1A1D23" '
                     f'text-anchor="end" font-weight="600">{_esc(label)}</text>')
        parts.append(f'<rect x="190" y="{y + 6}" width="{length}" height="26" '
                     f'fill="{colour}" rx="1"/>')
        parts.append(f'<text x="{190 + length + 8}" y="{y + 24}" font-size="12" '
                     f'fill="{NAVY}" font-weight="700">{count}</text>')
        parts.append(f'<text x="{190 + length + 8}" y="{y + 36}" font-size="9" '
                     f'fill="{MUTED}">{share:.0f}% of registered</text>')
        if index:
            previous = stages[index - 1][1] or 1
            conversion = count / previous * 100
            tone = BAD if conversion < 40 else (WARN if conversion < 65 else OK)
            parts.append(f'<text x="182" y="{y - 4}" font-size="10" fill="{tone}" '
                         f'text-anchor="end" font-weight="600">'
                         f'↓ {conversion:.0f}%</text>')
    return _svg("".join(parts), width, height)


def stacked_bar(segments: list[tuple[str, int, str]], *, height: int = 46) -> str:
    total = sum(count for _, count, _ in segments) or 1
    width = 640
    parts, x = [], 0
    for label, count, colour in segments:
        w = count / total * width
        if w <= 0:
            continue
        parts.append(f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="22" '
                     f'fill="{colour}"/>')
        if w > 44:
            parts.append(f'<text x="{x + w / 2:.1f}" y="15" font-size="10" '
                         f'fill="#FFFFFF" text-anchor="middle">{count}</text>')
        parts.append(f'<text x="{x:.1f}" y="36" font-size="9.5" fill="{MUTED}">'
                     f'{_esc(label)}</text>')
        x += w
    return _svg("".join(parts), width, height)


def _fmt(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
def funnel(db: Session, since: datetime | None) -> dict:
    query = select(OnboardingProgress)
    if since is not None:
        query = query.where(OnboardingProgress.registered_at >= since)
    rows = list(db.scalars(query).all())
    tenant_ids = [r.tenant_id for r in rows]

    configured = sum(1 for r in rows if r.buyer_types)
    with_document = 0
    if tenant_ids:
        with_document = len({t for (t,) in db.execute(
            select(Document.tenant_id).where(and_(
                Document.tenant_id.in_(tenant_ids),
                Document.deleted_at.is_(None))).distinct()).all()})
    m1 = sum(1 for r in rows if r.m1_at)
    m2 = sum(1 for r in rows if r.m2_at)
    m3 = sum(1 for r in rows if r.m3_at)

    stages = [("Registered", len(rows)), ("Told us their buyers", configured),
              ("First document", with_document), ("M1 verified credential", m1),
              ("M2 client grant", m2), ("M3 buyer read", m3)]

    worst, worst_rate = None, 101.0
    for index in range(1, len(stages)):
        previous = stages[index - 1][1]
        if previous < 3:
            continue
        rate = stages[index][1] / previous * 100
        if rate < worst_rate:
            worst, worst_rate = (stages[index - 1][0], stages[index][0], rate), rate

    return {"stages": stages, "chart": funnel_chart(stages), "worst": worst,
            "registered": len(rows)}


def activation(db: Session) -> dict:
    """Fixed windows, measured against the target. A cohort still inside its
    window is excluded — counting it would flatter the number."""
    rows = list(db.scalars(select(OnboardingProgress)).all())
    result = {}
    for window in (7, 14, 30):
        eligible = [r for r in rows
                    if r.registered_at <= now() - timedelta(days=window)]
        activated = [r for r in eligible if r.m2_at
                     and (r.m2_at - r.registered_at).days <= window]
        rate = (len(activated) / len(eligible) * 100) if eligible else 0.0
        result[window] = {"eligible": len(eligible), "activated": len(activated),
                          "rate": round(rate, 1)}

    ttv = {}
    for milestone in ("m1", "m2", "m3"):
        minutes = sorted(
            int(((getattr(r, f"{milestone}_at") - r.registered_at).total_seconds()) // 60)
            for r in rows if getattr(r, f"{milestone}_at"))
        ttv[milestone] = {
            "count": len(minutes),
            "median": minutes[len(minutes) // 2] if minutes else None,
            "fastest": minutes[0] if minutes else None,
        }

    tiers = Counter()
    for row in rows:
        if not row.m1_at:
            continue
        minutes = (row.m1_at - row.registered_at).total_seconds() / 60
        if minutes < 5:
            tiers["Under 5 min — excellent"] += 1
        elif minutes < 20:
            tiers["5 to 20 min — typical"] += 1
        elif minutes < 60:
            tiers["20 to 60 min — too long"] += 1
        else:
            tiers["Over an hour — needs assistance"] += 1

    order = ["Under 5 min — excellent", "5 to 20 min — typical",
             "20 to 60 min — too long", "Over an hour — needs assistance"]
    colours = [OK, STEEL, WARN, BAD]
    segments = [(label, tiers.get(label, 0), colour)
                for label, colour in zip(order, colours) if tiers.get(label)]

    return {"windows": result, "ttv": ttv, "tier_chart": stacked_bar(segments),
            "tiers": [(label, tiers.get(label, 0)) for label in order],
            "target": TARGET_ACTIVATION, "median_benchmark": BENCHMARK_MEDIAN,
            "top_quartile": BENCHMARK_TOP_QUARTILE, "window": ACTIVATION_WINDOW_DAYS}


def cohorts(db: Session, months: int = 6) -> dict:
    rows = list(db.scalars(select(OnboardingProgress)).all())
    buckets: dict[str, list] = defaultdict(list)
    for row in rows:
        buckets[row.registered_at.strftime("%b %Y")].append(row)

    ordered = sorted(buckets.items(),
                     key=lambda kv: datetime.strptime(kv[0], "%b %Y"))[-months:]
    table, series = [], []
    for label, members in ordered:
        mature = [m for m in members
                  if m.registered_at <= now() - timedelta(days=ACTIVATION_WINDOW_DAYS)]
        activated = [m for m in mature if m.m2_at]
        rate = (len(activated) / len(mature) * 100) if mature else 0.0
        table.append({"cohort": label, "registered": len(members),
                      "mature": len(mature), "activated": len(activated),
                      "rate": round(rate, 1)})
        if mature:
            series.append((label, round(rate, 1)))
    return {"table": table,
            "chart": line_chart(series, suffix="%", target=TARGET_ACTIVATION)}


def onboarding_steps(db: Session) -> dict:
    import json as _json

    rows = list(db.scalars(select(OnboardingProgress)).all())
    stats = []
    for number, meta in onboarding.STEPS.items():
        started = completed = skipped = 0
        durations = []
        for row in rows:
            try:
                steps = _json.loads(row.steps_json or "{}")
            except ValueError:
                continue
            entry = steps.get(str(number))
            if not entry:
                continue
            started += 1
            if entry.get("status") == "complete":
                completed += 1
            elif entry.get("status") == "skipped":
                skipped += 1
            begin, end = entry.get("started_at"), entry.get("completed_at")
            if begin and end:
                try:
                    delta = (datetime.fromisoformat(end) - datetime.fromisoformat(begin))
                    durations.append(int(delta.total_seconds() // 60))
                except ValueError:
                    pass
        durations.sort()
        stats.append({
            "number": number, "title": meta["title"], "started": started,
            "completed": completed, "skipped": skipped,
            "rate": round(completed / started * 100, 1) if started else 0.0,
            "skip_rate": round(skipped / started * 100, 1) if started else 0.0,
            "median_minutes": durations[len(durations) // 2] if durations else None,
        })
    return {"steps": stats,
            "chart": bar_chart([(s["title"], s["completed"]) for s in stats]),
            "dismissed": sum(1 for r in rows if r.dismissed),
            "completed": sum(1 for r in rows if r.completed_at)}


def estate(db: Session) -> dict:
    domain_counts = Counter()
    type_counts = Counter()
    status_counts = Counter()
    for document, dtype in db.execute(
            select(Document, DocumentType)
            .join(DocumentType, Document.document_type_id == DocumentType.id)
            .where(Document.deleted_at.is_(None))).all():
        domain_counts[dtype.domain_name] += 1
        type_counts[f"{dtype.code} {dtype.name}"] += 1
        status_counts[document.derived_status] += 1

    ladder = {level: db.scalar(select(func.count(DocumentVersion.id)).where(and_(
        DocumentVersion.status == "active",
        DocumentVersion.assurance_level == level))) or 0
        for level in ("L0", "L1", "L2", "L3", "L4")}

    status_order = [("valid", OK), ("expiring_soon", WARN), ("expired", BAD),
                    ("suspended", BAD), ("missing", MUTED)]
    return {
        "domain_chart": bar_chart(sorted(domain_counts.items(),
                                         key=lambda kv: -kv[1])[:13]),
        "top_types": type_counts.most_common(12),
        "ladder": ladder,
        "ladder_chart": stacked_bar([
            ("L0", ladder["L0"], MUTED), ("L1", ladder["L1"], WARN),
            ("L2", ladder["L2"], "#8C6D1F"), ("L3", ladder["L3"], OK),
            ("L4", ladder["L4"], NAVY)]),
        "status_chart": stacked_bar([(k.replace("_", " "), status_counts.get(k, 0), c)
                                     for k, c in status_order if status_counts.get(k)]),
        "status": dict(status_counts),
        "documents": sum(domain_counts.values()),
        "verified_share": round(
            (ladder["L3"] + ladder["L4"]) / max(sum(ladder.values()), 1) * 100, 1),
    }


def verification_health(db: Session, since: datetime | None) -> dict:
    query = select(VerificationCheck)
    if since is not None:
        query = query.where(VerificationCheck.checked_at >= since)
    checks = list(db.scalars(query).all())
    results = Counter(c.result for c in checks)
    simulated = sum(1 for c in checks if c.simulated)
    negative = sum(1 for c in checks if c.result in {
        "no_match", "not_found", "suspended", "withdrawn", "expired", "link_dead"})
    inconclusive = sum(1 for c in checks if c.result in {"unreachable", "error"})
    stale = db.scalar(select(func.count(VerificationCheck.id)).where(
        VerificationCheck.next_check_due < now())) or 0
    return {
        "total": len(checks), "simulated": simulated,
        "simulated_share": round(simulated / max(len(checks), 1) * 100, 1),
        "negative": negative,
        "negative_share": round(negative / max(len(checks), 1) * 100, 1),
        "inconclusive": inconclusive, "stale": stale,
        "chart": bar_chart([(k.replace("_", " "), v)
                            for k, v in results.most_common(9)]),
    }


def disclosure(db: Session, since: datetime | None) -> dict:
    grants = list(db.scalars(select(AccessGrant)).all())
    active = [g for g in grants if g.status == "active"]
    unused = [g for g in active if g.last_used_at is None]
    view_query = select(ShareLinkView)
    if since is not None:
        view_query = view_query.where(ShareLinkView.viewed_at >= since)
    views = list(db.scalars(view_query).all())
    actions = Counter(v.action for v in views)

    daily = Counter()
    for view in views:
        daily[view.viewed_at.strftime("%d %b")] += 1
    recent = sorted(daily.items(),
                    key=lambda kv: datetime.strptime(kv[0] + " 2026", "%d %b %Y"))[-10:]

    return {
        "grants": len(grants), "active": len(active),
        "unused": len(unused),
        "unused_share": round(len(unused) / max(len(active), 1) * 100, 1),
        "clients": db.scalar(select(func.count(ClientAccount.id))) or 0,
        "requests_open": db.scalar(select(func.count(AccessRequest.id)).where(
            AccessRequest.status == "open")) or 0,
        "views": len(views), "actions": dict(actions),
        "trend": line_chart(recent),
    }


def trust_distribution(db: Session) -> dict:
    latest: dict[str, TrustScore] = {}
    for score in db.scalars(select(TrustScore).order_by(TrustScore.computed_at)).all():
        latest[score.tenant_id] = score
    bands = Counter(s.band for s in latest.values() if not s.withheld_reason)
    order = [("Strong", OK), ("Adequate", STEEL), ("Developing", WARN), ("Weak", BAD)]
    scores = [s.composite for s in latest.values() if not s.withheld_reason]
    domain_totals: dict[str, list[float]] = defaultdict(list)
    for score in latest.values():
        for domain in score.domains:
            domain_totals[domain.label].append(domain.score)
    return {
        "bands": dict(bands),
        "chart": stacked_bar([(label, bands.get(label, 0), colour)
                              for label, colour in order if bands.get(label)]),
        "mean": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "withheld": sum(1 for s in latest.values() if s.withheld_reason),
        "domain_chart": bar_chart(
            sorted(((label, round(sum(v) / len(v), 1))
                    for label, v in domain_totals.items()), key=lambda kv: -kv[1]),
            suffix=""),
    }


def ai_usage(db: Session, since: datetime | None) -> dict:
    query = select(AiInference)
    if since is not None:
        query = query.where(AiInference.created_at >= since)
    rows = list(db.scalars(query).all())
    by_kind = Counter(r.kind for r in rows)
    by_source = Counter(r.source for r in rows)
    errors = sum(1 for r in rows if r.error)
    latencies = sorted(r.latency_ms for r in rows if r.latency_ms)
    providers = []
    for config in db.scalars(select(AiProviderConfig)).all():
        providers.append({"label": config.label, "provider": config.provider,
                          "model": config.model, "enabled": config.enabled,
                          "calls": config.calls_made,
                          "tokens": config.tokens_in + config.tokens_out,
                          "tested_ok": config.last_test_ok})
    return {
        "total": len(rows), "by_kind": dict(by_kind), "by_source": dict(by_source),
        "errors": errors,
        "error_rate": round(errors / max(len(rows), 1) * 100, 1),
        "model_share": round(by_source.get("model", 0) / max(len(rows), 1) * 100, 1),
        "median_latency": latencies[len(latencies) // 2] if latencies else 0,
        "chart": bar_chart([(k.replace("_", " "), v) for k, v in by_kind.most_common()]),
        "providers": providers,
        "agent_calls": db.scalar(select(func.count(AgentCallLog.id)).where(
            AgentCallLog.occurred_at >= (since or datetime(1970, 1, 1)))) or 0,
        "questionnaires": db.scalar(select(func.count(Questionnaire.id))) or 0,
    }


def risk(db: Session, since: datetime | None) -> dict:
    signals = list(db.scalars(select(AuthenticitySignal).where(
        AuthenticitySignal.status == "open")).all())
    by_severity = Counter(s.severity for s in signals)
    by_code = Counter(s.code for s in signals)
    glass_query = select(BreakGlassSession)
    if since is not None:
        glass_query = glass_query.where(BreakGlassSession.started_at >= since)
    return {
        "open_signals": len(signals), "by_severity": dict(by_severity),
        "chart": bar_chart([(k.replace("_", " "), v) for k, v in by_code.most_common(8)],
                           colour=WARN),
        "suspended": db.scalar(select(func.count(Tenant.id)).where(
            Tenant.lifecycle_state == "suspended")) or 0,
        "closing": db.scalar(select(func.count(Tenant.id)).where(
            Tenant.lifecycle_state == "closing")) or 0,
        "holds": db.scalar(select(func.count(Document.id)).where(
            Document.legal_hold.is_(True))) or 0,
        "break_glass": len(list(db.scalars(glass_query).all())),
    }


def health(db: Session) -> dict:
    rows = []
    for tenant in db.scalars(select(Tenant).where(
            Tenant.lifecycle_state.in_(["active", "closing"]))).all():
        score, signals = health_score(db, tenant)
        band, tone = health_band(score)
        rows.append({"tenant": tenant, "score": score, "band": band, "tone": tone,
                     "signals": signals[:2]})
    bands = Counter(r["band"] for r in rows)
    order = [("Healthy", OK), ("At risk", WARN), ("Declining", WARN), ("Critical", BAD)]
    rows.sort(key=lambda r: r["score"])
    return {
        "bands": dict(bands),
        "chart": stacked_bar([(label, bands.get(label, 0), colour)
                              for label, colour in order if bands.get(label)]),
        "at_risk": [r for r in rows if r["score"] < 70][:12],
        "mean": round(sum(r["score"] for r in rows) / max(len(rows), 1), 1),
    }


def headline(db: Session, since: datetime | None) -> dict:
    tenant_query = select(func.count(Tenant.id))
    new_tenants = db.scalar(
        tenant_query.where(Tenant.created_at >= since) if since is not None
        else tenant_query) or 0
    event_query = select(func.count(AuditEvent.id))
    events = db.scalar(
        event_query.where(AuditEvent.occurred_at >= since) if since is not None
        else event_query) or 0
    return {
        "tenants": db.scalar(select(func.count(Tenant.id))) or 0,
        "active": db.scalar(select(func.count(Tenant.id)).where(
            Tenant.lifecycle_state == "active")) or 0,
        "new_tenants": new_tenants,
        "users": db.scalar(select(func.count(User.id))) or 0,
        "documents": db.scalar(select(func.count(Document.id)).where(
            Document.deleted_at.is_(None))) or 0,
        "versions": db.scalar(select(func.count(DocumentVersion.id))) or 0,
        "events": events,
        "notifications": db.scalar(select(func.count(Notification.id))) or 0,
    }


def compile_all(db: Session, period: str = "30") -> dict:
    since = cutoff(period)
    return {
        "period": period, "period_label": PERIODS.get(period, "Last 30 days"),
        "since": since, "generated_at": now(),
        "headline": headline(db, since),
        "funnel": funnel(db, since),
        "activation": activation(db),
        "cohorts": cohorts(db),
        "steps": onboarding_steps(db),
        "estate": estate(db),
        "verification": verification_health(db, since),
        "disclosure": disclosure(db, since),
        "trust": trust_distribution(db),
        "ai": ai_usage(db, since),
        "risk": risk(db, since),
        "health": health(db),
    }


def as_json(payload: dict) -> dict:
    """Export without the SVG blobs, which are presentation rather than data."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items()
                    if not (isinstance(v, str) and v.lstrip().startswith("<svg"))
                    and k not in {"chart", "trend", "domain_chart", "status_chart",
                                  "ladder_chart", "tier_chart"}}
        if isinstance(value, list):
            return [strip(v) for v in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if hasattr(value, "name") and hasattr(value, "slug"):
            return {"id": value.id, "name": value.name, "slug": value.slug}
        return value
    return strip(payload)
