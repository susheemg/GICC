"""Renders the two one-page notes to PDF.

Both are A4 single pages, laid out for a reader who will give them ninety
seconds. Conclusion first, evidence after; every number carries its assumption
in the same line of sight.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from weasyprint import CSS, HTML

OUT = Path("/mnt/user-data/outputs")
TODAY = date.today().strftime("%d %B %Y")

CSS_TEXT = """
@page { size: A4; margin: 9mm 10mm 8mm 10mm; }
html { font-family: Carlito, Calibri, "Helvetica Neue", Helvetica, Arial, sans-serif;
       font-size: 8pt; line-height: 1.26; color: #1A1D23; }
body { margin: 0; }
.bar { background: #1F3A70; color: #fff; padding: 3.4mm 5mm; margin: -9mm -10mm 3mm -10mm; }
.eyebrow { font-size: 7pt; letter-spacing: .18em; text-transform: uppercase;
           color: #9FB6D8; font-weight: 700; }
h1 { font-size: 13.6pt; margin: 1mm 0 .8mm; font-weight: 700; letter-spacing: -.01em;
     line-height: 1.15; }
.sub { font-size: 7.8pt; color: #C7D4E8; }
h2 { font-size: 8.8pt; color: #1F3A70; margin: 2.6mm 0 1.2mm; padding-bottom: .7mm;
     border-bottom: 1.6px solid #1F3A70; letter-spacing: -.005em; }
h3 { font-size: 7.9pt; color: #16294F; margin: 0 0 .8mm; }
p { margin: 0 0 1.4mm; }
.lede { background: #E8EEF7; border-left: 3px solid #3E6FB0; padding: 2.4mm 3.4mm;
        font-size: 8.6pt; color: #16294F; margin-bottom: 2.6mm; }
.lede b { color: #1F3A70; }
.grid { display: flex; gap: 2.4mm; margin-bottom: 2.4mm; }
.grid > * { flex: 1; }
.col2 { column-count: 2; column-gap: 5mm; }
.col2 h2 { break-after: avoid; } .col2 ul { break-inside: avoid; }
table { width: 100%; border-collapse: collapse; font-size: 7.4pt; margin-bottom: 1.8mm; }
th { background: #1F3A70; color: #fff; text-align: left; padding: 1mm 1.6mm;
     font-size: 7.1pt; font-weight: 600; letter-spacing: .02em; }
td { padding: 1mm 1.6mm; border-bottom: 1px solid #E3E6EB; vertical-align: top; }
tr:nth-child(even) td { background: #FAFBFC; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.kpi { border: 1px solid #DDE2EA; padding: 1.8mm 2.4mm; }
.kpi b { display: block; font-size: 13pt; color: #1F3A70; line-height: 1.05; }
.kpi span { font-size: 7.2pt; color: #6B7280; }
.chip { display: inline-block; font-size: 6.9pt; font-weight: 700; padding: .4mm 1.6mm;
        border-radius: 1px; background: #E8EEF7; color: #1F3A70; margin-right: 1mm; }
.chip.ok { background: #E4F2EC; color: #1B7F5A; }
.chip.warn { background: #FBF2DE; color: #8C6D1F; }
ul { margin: 0 0 1.4mm; padding-left: 3.2mm; }
li { margin-bottom: .6mm; }
.foot { border-top: 1px solid #DDE2EA; padding-top: 1.2mm; margin-top: 1.8mm;
        font-size: 7pt; color: #6B7280; display: flex; justify-content: space-between; }
.mono { font-family: "DejaVu Sans Mono", Menlo, Consolas, monospace; font-size: 7.2pt; }
.muted { color: #6B7280; }
.box { border: 1px solid #DDE2EA; padding: 2mm 2.6mm; }
.box.accent { border-left: 3px solid #B8860B; background: #FDFAF3; }
.box h3 { color: #8C6D1F; }
.ladder { display: flex; gap: 1mm; align-items: flex-end; height: 7mm; margin-bottom: 1mm; }
.ladder i { display: block; width: 4mm; background: #C8D6E9; }
.ladder i.a { height: 2mm; background: #9AA1AC; }
.ladder i.b { height: 3.4mm; background: #B8860B; }
.ladder i.c { height: 4.8mm; background: #B8860B; }
.ladder i.d { height: 6.2mm; background: #1B7F5A; }
.ladder i.e { height: 7mm; background: #1F3A70; }
"""


def render(name: str, body: str) -> Path:
    html = f"<!doctype html><html><head><meta charset='utf-8'></head><body>{body}</body></html>"
    path = OUT / name
    HTML(string=html).write_pdf(path, stylesheets=[CSS(string=CSS_TEXT)])
    return path


# --------------------------------------------------------------------------
VALUE_NOTE = f"""
<div class="bar">
  <div class="eyebrow">Concept &amp; value note · Confidential · For discussion</div>
  <h1>Bloomberg already knows this market — and already owns the piece that was missing</h1>
  <div class="sub">GICC · Global Identity &amp; Certificates Center &nbsp;·&nbsp; Investor · Co-builder · Pilot client &nbsp;·&nbsp; {TODAY}</div>
</div>

<div class="lede">
  <b>Proposition:</b> Bloomberg takes a founding stake in a supplier-assurance platform,
  co-builds it on Entity Data Services, and pilots it inside the Chief Risk Office. The
  working platform exists today; what it lacks is exactly what Bloomberg already has —
  verified entity and registry data, and a route to the buyers who need it. Three roles,
  one asset: <b>a new recurring revenue line addressed to a buyer Bloomberg does not sell
  to yet.</b>
</div>

<h2>Address the elephant first: this is not Entity Exchange again</h2>
<div class="grid">
  <div class="box accent" style="flex:1.15">
    <h3>What was tried, and what actually failed</h3>
    <p style="margin:0">Entity Exchange (2016–2019) moved KYC documents between buy-side
      and sell-side counterparties. It was withdrawn for insufficient demand into a market
      already held by Refinitiv, Dow Jones, LexisNexis and Bureau van Dijk. The lesson is
      specific: <b>a document exchange without verification is email with a nicer interface</b>,
      and it needs both sides present before either gets value.</p>
  </div>
  <div class="box">
    <h3>Four things are different now</h3>
    <ul style="margin-bottom:0">
      <li><b>Different product.</b> The claim is verification, not exchange — every
        credential carries the assurance level its evidence supports, computed and never
        assigned</li>
      <li><b>Different buyer.</b> Third-party risk and procurement, not KYC operations</li>
      <li><b>Different driver.</b> DORA registers, CSDDD and EUDR did not exist in 2016</li>
      <li><b>No cold start.</b> The supplier gets renewal management, questionnaire
        automation and a public trust page before a single buyer joins</li>
    </ul>
  </div>
</div>

<h2>The three roles, and what each one is worth</h2>
<table>
  <thead><tr><th style="width:16%">Role</th><th style="width:42%">What Bloomberg does</th><th>What Bloomberg gets</th></tr></thead>
  <tbody>
    <tr><td><b>Investor</b></td>
      <td>Founding stake at pre-seed; the platform is already built, so capital buys
        distribution and connectors rather than a first version</td>
      <td>A recurring revenue line in supplier assurance — a segment adjacent to Enterprise
        Data with no current Bloomberg product and no incumbent of scale</td></tr>
    <tr><td><b>Co-builder</b></td>
      <td>Entity Data Services supplies the registry, ownership and LEI verification the
        platform currently simulates; the CRO office supplies the TPRM domain design</td>
      <td>A second commercial use for entity data assets already built and maintained,
        sold to a new buyer without new collection cost</td></tr>
    <tr><td><b>Pilot client</b></td>
      <td>25 suppliers in one business unit, 90 days, measured against the current
        evidence-collection baseline</td>
      <td>Proof, a reference customer, and a lower internal cost of third-party assurance
        — the pilot pays for itself or it stops</td></tr>
  </tbody>
</table>

<h2>Why the co-build is the real argument</h2>
<div class="grid">
  <div class="box">
    <h3>The platform's largest gap</h3>
    <p style="margin:0">Verification connectors are <b>simulated</b>. The pipeline,
      scheduling, assurance computation, downgrade rules and audit are all built and tested
      — what is missing is a live authoritative source behind them. That is a data problem,
      not a software problem, and it is the single thing standing between the product and
      its own claim.</p>
  </div>
  <div class="box">
    <h3>Bloomberg's existing answer</h3>
    <p style="margin:0">Entity Data Services already matches and verifies entity records
      against corporate registries, regulators and exchanges, and monitors them for filings
      and status changes. Point it at supplier credentials instead of counterparties and
      the platform's L3 rung becomes real on day one. <b>Neither side has to build what the
      other already has.</b></p>
  </div>
</div>

<h2>Revenue shape — illustrative, on stated assumptions</h2>
<table>
  <thead><tr><th style="width:22%">Line</th><th>Who pays</th><th class="num">Unit</th><th class="num">At 500 suppliers</th></tr></thead>
  <tbody>
    <tr><td><b>Supplier subscription</b></td><td>The supplier, for the vault, renewals and questionnaire automation</td>
      <td class="num">£1.2k / yr</td><td class="num">£600k</td></tr>
    <tr><td><b>Buyer seat</b></td><td>The buyer, for continuous monitoring across their supplier base</td>
      <td class="num">£18k / yr × 12</td><td class="num">£216k</td></tr>
    <tr><td><b>Verification calls</b></td><td>Metered, revenue-shared on Bloomberg entity data</td>
      <td class="num">£0.40 / check</td><td class="num">£72k</td></tr>
    <tr><td><b>Machine channel</b></td><td>API and MCP access for buyer-side AI agents</td>
      <td class="num">tiered</td><td class="num">£90k</td></tr>
  </tbody>
</table>
<p class="muted" style="font-size:7.2pt;margin-top:-1mm">A model for discussion, not a
forecast. Pricing, attach rates and the revenue share are placeholders to be set with
Bloomberg before any business case is made. The supplier line is the one that compounds:
suppliers arrive because a buyer asked, and stay because the vault is useful without them.</p>

<h2>What exists today, and what it still needs</h2>
<div class="grid">
  <div class="kpi"><b>198</b><span>document types, 13 risk domains</span></div>
  <div class="kpi"><b>207</b><span>automated checks passing</span></div>
  <div class="kpi"><b>8</b><span>MCP tools for agent access</span></div>
  <div class="kpi"><b>0</b><span>live verification connectors — the ask</span></div>
</div>

<div class="grid">
  <div class="box">
    <h3>90 days to a decision</h3>
    <ul style="margin-bottom:0">
      <li><b>Weeks 1–3</b> — connect Entity Data Services to one verification route</li>
      <li><b>Weeks 4–8</b> — onboard 25 CRO suppliers; measure against baseline</li>
      <li><b>Weeks 9–12</b> — one review cycle end to end</li>
      <li><b>Gate</b> — proceed only if analyst hours per supplier fall by a third and the
        connector holds at L3</li>
    </ul>
  </div>
  <div class="box">
    <h3>Stated plainly, before anyone asks</h3>
    <ul style="margin-bottom:0">
      <li>CSRF protection, MFA and a migration framework are required before production</li>
      <li>AI proposes and a human confirms — no model output can set an assurance level</li>
      <li>Ownership, IP and the employment relationship need settling in writing before
        commercial discussion begins</li>
    </ul>
  </div>
</div>

<div class="foot">
  <span>GICC · Concept &amp; value note · v2.0</span>
  <span>Full specification: GICC-00 Master Specification, 67 pp. · Feature set: separate one-pager</span>
</div>
"""

# --------------------------------------------------------------------------
FEATURES = f"""
<div class="bar">
  <div class="eyebrow">Product feature set · Confidential</div>
  <h1>Nine modules, one rule: evidence is verified, disclosure is controlled</h1>
  <div class="sub">GICC · Global Identity &amp; Certificates Center &nbsp;·&nbsp; {TODAY}
    &nbsp;·&nbsp; 38 tables · 126 routes · 207 automated checks</div>
</div>

<div class="col2">

<h2>1 · Credential vault</h2>
<ul>
  <li><b>198 document types</b> across 13 risk domains, each with issuer, validity,
    verification route, sensitivity class and criticality</li>
  <li>Immutable versions: one active version per document, enforced at the database</li>
  <li>Scheduled activation with no coverage gap; supersession is atomic</li>
  <li>Forward-only rollback — history stays append-only</li>
  <li>Expired versions retained as evidence of the period they covered</li>
</ul>

<h2>2 · Verification and the assurance ladder</h2>
<ul>
  <li><span class="chip">L0</span>self-declared <span class="chip warn">L1</span>source-linked
    <span class="chip warn">L2</span>human-verified <span class="chip ok">L3</span>machine-verified
    <span class="chip">L4</span>cryptographic</li>
  <li>Pluggable connectors per route: registry, IAF CertSearch, issuer portal, callback,
    manual, signature validation</li>
  <li>Holder name matched against the registered legal entity</li>
  <li>Recheck cadence 1–180 days by route; adverse status reaches grantees in 24 h</li>
  <li><b>An inconclusive check never downgrades</b> — an outage cannot mass-downgrade the
    platform</li>
</ul>

<h2>3 · Controlled disclosure</h2>
<ul>
  <li>14 seeded certificate groups; membership by explicit document or type-prefix rule</li>
  <li>Group policy is a <b>ceiling</b>: NDA, watermark, download, maximum duration</li>
  <li>Sensitivity ceiling excludes above-class documents even when a rule would include them</li>
  <li>Share links with expiry, view cap and instant revocation; token stored hashed</li>
  <li>Every viewer identifies themselves before the first document opens</li>
  <li>Watermarked rendering; read receipts showing who opened what, when</li>
</ul>

<h2>4 · Buyer requirements and conformance</h2>
<ul>
  <li>Requirement profiles: type, mandatory flag, minimum assurance, maximum age,
    minimum cover, weight</li>
  <li>Deterministic evaluation <b>citing the exact version read</b> — reproducible months later</li>
  <li>Gaps ranked by how many buyers demand each missing type</li>
</ul>

<h2>5 · Questionnaire automation</h2>
<ul>
  <li>Upload CSV, TSV, Markdown or text; questions mapped to catalogue types</li>
  <li>Answers drafted <b>only</b> from held evidence, each citing a document version</li>
  <li>No evidence is a first-class outcome — never answered from general knowledge</li>
  <li>Approved answers bank for reuse, so the fifth questionnaire is nearly free</li>
  <li>Export with answers, citations, assurance levels and version IDs</li>
</ul>

<h2>6 · Trust Index</h2>
<ul>
  <li>0–100 across eight risk domains — identity, regulatory, financial, insurance,
    security, resilience, safety, delivery</li>
  <li>Deterministic and <b>algorithm-versioned</b>; historical scores never recomputed</li>
  <li>Every point drills through to the document version that produced it</li>
  <li>One next action named — the change that moves the score most</li>
  <li>Measures evidence quality, not the company. Withheld while a tenant is under review</li>
</ul>

<h2>7 · AI layer — the model proposes, the rules decide</h2>
<ul>
  <li>Upload and auto-classify against the catalogue, with confidence and alternatives</li>
  <li>Metadata extraction; fields below the confidence floor are left <b>blank, not guessed</b></li>
  <li>Control summary grounded in held evidence, stating gaps plainly</li>
  <li>Authenticity signals — including cross-tenant duplicate certificate numbers, which no
    single-tenant system can see</li>
  <li>Six LLM providers, credentials encrypted, live-tested before they can be enabled</li>
  <li>Every AI feature has a deterministic fallback; no model output sets an assurance level</li>
</ul>

<h2>8 · Machine channel</h2>
<ul>
  <li>REST API v1, 11 endpoints, 7 scopes, OpenAPI documented</li>
  <li>MCP server (protocol 2026-07-28): 8 tools, 3 resources, 2 prompts</li>
  <li>A grant-bound key sees the narrower of token scope and grant — always</li>
  <li>Special-category documents blocked from the machine channel entirely</li>
  <li>Mutating agent calls require an explicit consent round-trip</li>
</ul>

<h2>9 · Governance, lifecycle and platform</h2>
<ul>
  <li>Hash-chained audit log; chain verification exposed in the interface</li>
  <li>Plain-language activity feed covering team, buyers, agents, platform and automatic actions</li>
  <li>Notification engine: 11 kinds, deduplicated, one action per message</li>
  <li>Four roles plus a sensitive-access flag; capabilities checked, not role names</li>
  <li>Operator console with break-glass — time-boxed, reason-bound, visible to the supplier</li>
  <li>Guided five-step registration; platform analytics with funnel, activation and cohorts</li>
  <li><b>11 designed exit paths.</b> Every one notifies grantees and preserves their evidence</li>
</ul>

</div>

<div class="grid" style="margin-top:2mm">
  <div class="box accent" style="flex:2">
    <h3>The five rules the product will not break</h3>
    <p style="margin:0">Assurance is computed, never assigned · a human can reach L2 but never
      L3 or L4 · access is resolved fresh on every read by a single resolver · an answer
      without a citation is not an answer · exit obligations run to the buyer, who keeps a
      download window even where the grant forbade one.</p>
  </div>
  <div class="box">
    <h3>Deployment</h3>
    <p style="margin:0" class="muted">Python · FastAPI · SQLAlchemy · SQLite or PostgreSQL.
      No JavaScript build step. One command to run, one blueprint to deploy. Ships as a
      package or as a single 11,000-line file.</p>
  </div>
</div>

<div class="foot">
  <span>GICC · Product feature set · v1.0</span>
  <span>Every feature listed is implemented and covered by the automated suite.</span>
</div>
"""


if __name__ == "__main__":
    for name, body in (("GICC-Concept-and-Value-Note.pdf", VALUE_NOTE),
                       ("GICC-Product-Feature-Set.pdf", FEATURES)):
        path = render(name, body)
        from pypdf import PdfReader
        pages = len(PdfReader(str(path)).pages)
        flag = "" if pages == 1 else f"  ⚠ {pages} pages — trim to fit"
        print(f"  {path.name}  {path.stat().st_size / 1024:.0f} KB  {pages} page{flag}")
