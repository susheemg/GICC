# Trusted Supplier Program (TSP) — Global Identity & Certificates Center

A supplier-owned credential vault. A company registers once, uploads its certificates,
licences, insurances and attestations, has them **verified against authoritative sources**,
packages them into groups, and grants buyers time-boxed access — through a browser, a REST
API, or an MCP server that AI agents can call.

The differentiator is verification, not storage. Every credential version carries an
assurance level that is computed from evidence and can never be set by hand:

| | Level | Meaning |
|---|---|---|
| ▮▯▯▯▯ | **L0** | Self-declared. Uploaded, nothing checked. |
| ▮▮▯▯▯ | **L1** | Source-linked. An issuing-authority link is recorded and resolves. |
| ▮▮▮▯▯ | **L2** | Human-verified. A reviewer compared it against the source and kept the evidence. |
| ▮▮▮▮▯ | **L3** | Machine-verified. Matched automatically against an authoritative register. |
| ▮▮▮▮▮ | **L4** | Cryptographically verifiable credential or seal. |

Built against the GICC design set: `GICC-01` functional design, `GICC-02` data model,
`GICC-03` application design, `GICC-04` process documentation.

---

## Running it locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open http://127.0.0.1:8000 and sign in.

**Supplier roles** (all `demo1234`) — the same workspace seen four different ways:

| Sign in as | Role | Can do |
|---|---|---|
| `demo@northgate.example` | Owner | Everything, including API keys, closure and purge |
| `compliance@northgate.example` | Compliance | Credentials, versions, verification, groups |
| `bids@northgate.example` | Sales | Clients, grants, share links |
| `readonly@northgate.example` | Viewer | Read-only |

**Platform operator** — `admin@gicc.platform` / `admin1234` at **`/admin/login`**.

### The demo estate

Six synthetic entities, deterministic and entirely fictional. Every domain ends in `.example`,
which RFC 2606 reserves so it can never resolve.

| Entity | Sector | State | Readiness | What it demonstrates |
|---|---|---|---|---|
| **Northgate Data Systems** (GB) | Software for financial services | Active | 77% | The healthy case: 24 credentials, 4 clients, 13 grants, two months of buyer reads and agent traffic, a recertification with real version history |
| **Aventine Pharma Solutions** (NL) | Sterile manufacturing | Active | 82% | Strongest evidence base — GMP, WDA, ISO 13485. EU region, NDA-gated pack |
| **Calderwell Facilities Group** (GB) | Facilities management | Active | 54% | The messy middle: five expired documents, overdue renewals, two open access requests, a closed support session in its log |
| **Harlow Financial Technology** (GB) | Payments | **Closing** | 41% | The exit path mid-flight — grantees already notified, grace window running |
| **Pellworth Consulting** (GB) | Consultancy | **Suspended** | 45% | The fraud case: two certificates rejected by the reviewer, platform legal holds the tenant cannot release |
| **Kestrel Logistics** (AE) | Freight forwarding | Active | 31% | The pre-activation state: credentials uploaded, nothing verified, **no grants at all** — the churn profile the health score exists to catch |

Owners are `owner@<slug>.example`; Northgate uses `demo@` for the Owner instead.

The estate carries roughly 470 audit events, 69 buyer reads, 55 agent calls, 20 open tasks and
three open access requests, all backdated across the last two months so the dashboards and the
activity log read as history rather than an import. All five assurance rungs are populated,
including one L4 (a signed SBOM) and five L2s (self-declared policies a reviewer checked).

Set `SEED_DEMO=0` for a clean instance with no demo data.

The demo tenant (Northgate Data Systems Ltd) is seeded on first start: 22 credentials
across nine domains, one certificate with real version history from a recertification,
three client accounts with live grants, and a full audit trail. Set `SEED_DEMO=0` for a
clean instance.

Run the test suite against a running server:

```bash
python3 tests/smoke_test.py http://127.0.0.1:8000
```

207 checks covering every route, the API, the MCP server, scope isolation, the buyer share
flow, role enforcement, the platform console, break-glass, suspension, the activity log,
AI classification, control summaries, authenticity signals, the Trust Index, the notification
engine, questionnaire automation, LLM provider administration, the guided registration
journey, platform analytics and the complete exit path. It exits non-zero on failure, so it works as a deploy gate. Run it
against a clean database — it registers entities and closes one, so it is not idempotent.

---

## Deploying to Render

1. Push this directory to a Git repository.
2. In Render: **New → Blueprint**, select the repo. `render.yaml` does the rest.
3. Wait for the health check at `/healthz` to go green.

`render.yaml` provisions a persistent disk at `/var/data` and sets `DATA_DIR` to match.
**This matters:** Render's filesystem is otherwise ephemeral, so without the disk both the
SQLite database and every uploaded document vanish on each deploy. A disk requires a paid
instance type.

If you prefer to configure the service by hand instead of using the blueprint:

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/healthz` |
| Disk mount path | `/var/data` |

### Environment variables

| Variable | Default | Notes |
|---|---|---|
| `DATA_DIR` | `./data` | Set to `/var/data` on Render. Holds the database and uploads. |
| `SECRET_KEY` | `dev-only-change-me` | **Change it.** Signs session cookies. `render.yaml` generates one. |
| `SEED_DEMO` | `1` | `0` for a clean production instance. |
| `ADMIN_EMAIL` | `admin@gicc.platform` | Bootstrap platform operator. **Change it.** |
| `ADMIN_PASSWORD` | `admin1234` | **Change it.** Only used to create the first operator. |
| `ANTHROPIC_API_KEY` | unset | Fallback only — prefer configuring a provider at `/admin/ai`. |
| `AI_MODEL` | `claude-sonnet-4-6` | Model for the environment fallback path. |
| `MIN_FIELD_CONFIDENCE` | `0.6` | Extracted fields below this are left blank, not guessed. |
| `SWEEP_MINUTES` | `60` | Notification sweep interval. |
| `NOTIFY_TRANSPORT` | `log` | `log`, `smtp` or `off`. |
| `SMTP_URL` | unset | e.g. `smtp://user:pass@host:587` when transport is `smtp`. |
| `BREAK_GLASS_MINUTES` | `240` | How long an operator's support session lasts before expiring. |
| `LIVE_LINK_CHECKS` | `0` | `1` lets verification make real outbound requests to authentication links. |
| `MAX_UPLOAD_BYTES` | 25 MB | Per-file upload ceiling. |
| `EXPIRING_SOON_DAYS` | `90` | When a credential starts showing as expiring. |

---

## What is real and what is a stand-in

Honesty about this matters more than a longer feature list.

**Real and working:** the version state machine and its immutability guard; the one-active-
version invariant; the assurance computation and its downgrade rules; the permission
resolver; group policy ceilings; grant issue, expiry and revocation; share links with
confidentiality gating, watermarking and per-view logging; the conformance engine with
version citations; the hash-chained audit log; the expiry ladder and task generation; the
REST API; the MCP server; export, closure, legal hold and purge.

**Stand-ins, clearly labelled everywhere they appear:**

- **Verification connectors.** The adapters apply the matching logic a live connector would
  — holder name against the registered legal entity, certificate number present, issuer
  recorded, status not expired — against locally held data. Every check they write carries
  `simulated=True`, which is surfaced in the UI, the REST API and the MCP response. Nothing
  can present a simulated result as a real registry response. Registering a real adapter in
  `ROUTES` (`app/services/verification.py`) is the only change needed; scheduling, assurance
  computation, downgrade rules and audit are unchanged.
  Link-liveness checking is real when `LIVE_LINK_CHECKS=1`.
- **Metadata extraction.** `_extract_hint()` in `app/routers/web_documents.py` proposes
  values from the catalogue rather than reading the file. The confirmation step it feeds is
  real and deliberate: extracted values are proposals until a human confirms them.
- **Notifications.** Written to the audit log rather than emailed. Every notification the
  process design requires has a corresponding event; wiring a mail transport is additive.
- **Malware scanning, SSO/SCIM, webhook delivery, background workers.** Not implemented.
  Maintenance runs on request instead of on a schedule.

---

## AI features

**The model proposes; the rules decide.** Every AI output lands in a proposal layer that a
human or a deterministic engine converts into fact. No model output raises an assurance
level, changes a Trust Index, or answers a buyer without a citation. Full specification in
`GICC-05`.

The whole layer is **off until a provider is configured**, and every feature has a
deterministic fallback — the product behaves identically without one, minus the fluency.

Providers are managed from the platform console at **`/admin/ai`**, so changing a key or a
model needs no redeploy. Six are supported: Anthropic, OpenAI, Azure OpenAI, Google Gemini,
Mistral, and any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, Groq, Together,
OpenRouter). Adding a seventh is one dictionary entry in `services/providers.py`, not a new
code path.

**How credentials are held.** Keys are encrypted at rest with a key derived from `SECRET_KEY`,
and never returned to the browser after saving — the console shows a mask and a SHA-256
fingerprint so an operator can tell *which* key is installed without being able to read it.
Every create, change and delete is audited by fingerprint, never by value. Saving runs a real
call against the endpoint, and a provider that has not passed its test **cannot be enabled**:
finding out a key is wrong during a supplier's document upload is the wrong time.

Rotating `SECRET_KEY` makes stored keys unreadable; the provider reports a decryption failure
and everything falls back to rules until the key is re-entered. This protects a stolen
database file or backup — it does not protect against someone holding both the database and
`SECRET_KEY`, because the application must decrypt in order to make a call. A production
deployment puts these in a KMS and stores only a reference.

`ANTHROPIC_API_KEY` in the environment still works and is used when no provider is configured,
so an instance can come up with a key and no console visit.

| Feature | With a key | Without |
|---|---|---|
| **Upload and auto-classify** (`/documents/upload`) | Model reads the extracted text and proposes a type from the 182-type catalogue, plus issuer, number, dates, scope and cover | Keyword and standard-number matching, regex date and number extraction |
| **Control summary** (`/summary`) | Model writes a narrative grounded in held evidence | Sentence templates per risk domain |
| **Questionnaire answers** (`/questionnaires`) | Model drafts prose from the cited evidence | Structured answer assembled from the citation |
| **Version change summary** | Model phrases it | Deterministic diff — and *material* change is always a rule, never a model judgement, because it notifies grantees |

Text is extracted locally (`pypdf` for PDF, zipped-XML parsing for Office). Fields below
`MIN_FIELD_CONFIDENCE` (0.6) are left blank rather than guessed — a wrong pre-fill that a
human waves through is worse than an empty box. Every call writes an `ai_inference` row with
source, model, confidence, input hash and latency, so a proposal is never indistinguishable
from a verified fact. Identical inputs reuse the cached result.

## Certificate authenticity signals

Eight rules that surface observations warranting a human look. **A signal never changes an
assurance level in either direction** — only a completed verification does that. Signals are
shown to the supplier, never published to buyers, and worded as observations rather than
allegations.

Holder-name mismatch · certificate-number format · unrecognised issuer · implausible dates ·
validity outside scheme norms · missing authentication link · **duplicate certificate number
across entities**.

The last one is the highest-value check in the set and the only one no single-tenant system
can perform: the same certificate number under two different legal entities is the classic
tell. The demo estate contains a worked example.

## Notification engine

Persisted first, delivered second — transports fail, the record of what a supplier was told
must not. Five triggers plus three more: document uploaded, access request received, access
request actioned, certificate due to expire (90/60/30/7 days), certificate expired (daily),
adverse verification, authenticity signal, break-glass session, Trust Index band change.

Deduplicated by key, so the 30-day reminder fires once rather than once per sweep. Per-user
channel preferences at `/notifications`. A background task sweeps every `SWEEP_MINUTES`
(default 60) and can be run on demand. `NOTIFY_TRANSPORT=log` writes structured lines;
set `smtp` with an `SMTP_URL` for real email.

**Every notification names one action.** "Your ISO 27001 expires in 12 days and four clients
rely on it" beats a summary of everything outstanding, because people do not act on reports.

## Questionnaire automation

Upload a buyer questionnaire as CSV, TSV, Markdown or plain text. Each question is mapped to
catalogue types, and **an answer is drafted only where the vault holds supporting evidence —
citing the exact document version.** A question with no evidence is returned as a gap, never
answered from general knowledge. Export as CSV with answers, citations, assurance levels and
version IDs.

Approving an answer banks it. The next questionnaire asking the same thing arrives pre-filled
with its citation refreshed against whatever version is current then — which is what makes the
fifth questionnaire nearly free. The bank travels with the supplier in the account export.

## Trust Index

A 0–100 composite across eight risk domains, **deterministic and versioned**, because a buyer
citing it in a regulatory register needs it reproducible six months later.

| Domain | Weight | Domain | Weight |
|---|---|---|---|
| Security & privacy | 20% | Insurance | 10% |
| Corporate identity | 15% | Resilience | 10% |
| Regulatory & licensing | 15% | Safety & environment | 10% |
| Financial standing | 10% | People & delivery | 10% |

Each domain scores the weighted mean over its *expected* types, where each contributes
`criticality × assurance × freshness`. Expected sets come from a baseline plus requirement
profiles matching the supplier's sector plus whatever they already hold — unioning every
published profile would make a freight forwarder responsible for a medical devices QMS.

Modifiers: +3 registry-verified entity, +2 LEI bound, −5 per unresolved high authenticity
signal (floor −15). Suspended tenants have the score withheld entirely rather than published
during an investigation. Bands: Strong 80+ · Adequate 60+ · Developing 40+ · Weak below.

Every domain drills through to the exact documents and versions that produced it, and the page
names **one** next action — the change that raises the composite most. Historical scores are
never recomputed under a new `algorithm_version`; that would rewrite the trend line buyers
rely on.

**What it is not:** a judgement of the company. It measures coverage and evidence quality. A
firm with three impeccable certificates and nothing else scores low, correctly — the buyer's
question is "can this supplier evidence its controls", not "is this a good company". The UI
says so wherever the score appears.

## Guided self-registration

New suppliers land in a five-step guide rather than an empty dashboard (`/start`):

1. **Confirm your company** — legal name, country, registry number. Verification is
   progressive and never blocks the journey.
2. **Tell us who buys from you** — the only branching question. It decides which six
   documents are recommended instead of showing all 198.
3. **Add your first credential** — upload, classify, verify. **Milestone M1.**
4. **Package what you share** — confirm the seeded groups.
5. **Share with a buyer** — creates the client, the grant and the share link in one
   action. **Milestone M2.**

Every step is skippable and the whole guide is dismissible, but both are recorded: a step
most suppliers skip is a step that should not exist.

**Steps are credited by outcome, not by pressing Next.** Step 3 completes when a version
is published, step 5 when a grant exists — and milestones are stamped by observing what is
true, so a supplier who uploads from the register rather than the guide still gets credit.
Measuring checklist completion instead of outcome delivery is how the industry held a 37%
median activation rate for four years while believing it was improving.

## Platform analytics

`/admin/analytics` — eleven sections answering the questions that change decisions rather
than the ones that fill a dashboard:

- **Funnel** — registered → configured → first document → M1 → M2 → M3, with the worst
  transition named explicitly
- **Activation** — 7/14/30-day windows against a 45% target and the published 37% industry
  median, with cohorts still inside their window excluded
- **Time to value** — median minutes to each milestone, bucketed against the published
  tiers (under 5 minutes excellent, over an hour needs assistance)
- **Cohorts** — monthly activation, so a change to onboarding can be judged
- **Onboarding steps** — started, completed, **skipped**, and median time in step
- **Estate · Verification health · Disclosure · Trust Index · AI · Risk · Tenant health**

All figures are live aggregates over the operational tables — no warehouse, nothing that
can silently go stale. That stops being viable past a few thousand tenants; the migration
path is a nightly rollup, and the query layer is contained in one module so the swap
touches one file. Charts render server-side as inline SVG: no JavaScript build step, and
they print correctly.

Every aggregate drills through to the tenants behind it. Statistics that cannot be drilled
into are decoration.

## Access control

Three planes, deliberately kept apart.

**Roles** govern people inside a supplier. Capabilities are checked, not role names, so adding
a role means editing one table in `services/roles.py` rather than hunting through routers.

| Role | Owns |
|---|---|
| Owner | Everything, including API keys, closure and purge |
| Compliance manager | Credentials, versions, verification, groups, profile |
| Sales and bid | Clients, grants, share links, access requests |
| Viewer | Read-only |

A separate `sensitive_access` flag, orthogonal to role, is required for Confidential and
Special-category documents. A refused action names the role that *can* do it, so the person
knows who to ask rather than just being told no.

**Grants** govern buyers outside the supplier. They never get a role. Scope, permission,
sensitivity ceiling, duration, confidentiality gate, watermark and revocation are all
resolved fresh on every read by the single resolver in `services/access.py`.

**Platform operators** live in their own table with their own sign-in at `/admin/login` and
their own session key. An operator is never "a user with a flag", so a stolen supplier session
cannot escalate into one.

## The platform console

`/admin` — unrestricted by design, accountable by construction:

- **Entities** — every workspace, with duplicate-registry-number adjudication, suspension and
  reinstatement, platform legal holds the tenant cannot release, and internal operator notes.
- **Users across entities** — create, change role, toggle sensitive access, reset passwords,
  disable, and move a user between entities.
- **Break-glass** — reading a workspace requires a session with a stated reason and a ticket
  reference. It is time-boxed and expires on its own. The supplier sees a striped banner for
  its whole duration and an entry in their activity log naming the operator and the reason.
- **AI providers** (`/admin/ai`) — configure, test, enable and rotate LLM credentials across
  six providers. One active at a time: silent failover would make an inference log ambiguous
  about which model produced a proposal. Each configuration is tagged self-hosted or
  third-party, because that distinction decides whether an Article 28 subprocessor question
  arises at all.
- **Catalogue** — retire or restore document types platform-wide.
- **Platform log** — every event across every entity, filterable by entity, actor class and action.

The console is deliberately not navy. A different colour is the cheapest guard against acting
on the wrong entity by mistake.

**Every admin action writes into the affected tenant's own log**, with the operator named.
That is the whole accountability model, and it is why the console and the activity log were
built together: power that the customer can see being used is a different thing from power
they cannot.

## The activity log

`/activity` — everything that happens on a supplier's account, whoever does it, in plain
language rather than action codes. Nothing is filtered out.

Actor classes: **Your team**, **Buyers**, **Agents**, **Platform**, **Automatic**. Counts for
each over the last 30 days sit at the top, and any platform activity raises a banner pointing
straight at the filtered view.

A **read-receipts** panel answers the question suppliers actually ask — has the client looked
at it yet, and which document. Every share link now identifies its viewer before the first
document opens, whether or not confidentiality terms apply; without that, receipts read
"anonymous viewer" and answer nothing.

`/audit` remains as the forensic view with hash-chain verification.

## Design decisions worth knowing before you change anything

**An inconclusive check never downgrades assurance.** Only a completed negative result does.
Without this rule one connector outage mass-downgrades every supplier on the platform and
destroys trust in the ladder permanently. See `compute_assurance()`.

**A human review can reach L2, never L3 or L4.** Those levels are machine-derived only. This
removes both the temptation and the liability.

**Rollback is forward-only.** Restoring an old version creates a new version reusing the old
file. History stays append-only.

**One permission resolver.** `app/services/access.py` is the only code that decides what a
client or agent can see. The browser, the REST API and the MCP server all call it. A second
implementation of these rules would be a breach waiting to happen.

**Grant ≤ group policy, always.** The ceiling is applied at grant creation. Ask for 365 days
on a 90-day group and you get 90, with the adjustment stated plainly.

**Dormancy never silently revokes access.** A buyer may be relying on these documents for
their own regulatory register. Access is withdrawn only through an explicit exit process
with notification.

**Exit obligations run to third parties.** Closing an account notifies every client holding
a grant, and enables download for the exit window even where the original grant forbade it.
Losing your supplier should not mean losing your evidence.

**The audit chain hashes opaque identifiers, not personal data.** `actor_id` and a
`payload_hash`, never `actor_label` or the payload text. That is what lets a purge scrub
personal data from the log while leaving the chain verifiable — otherwise erasure and
evidential integrity are in direct conflict and one of them loses.

**Automated systems may restrict; only a named human may terminate.** Suspension freezes
disclosure, deletes nothing, and is reversible. Every enforcement action records the operator,
the ground and the reason.

**Break-glass expires on its own.** Nobody has to remember to close it, because the failure
mode of "remember to close it" is that nobody does.

**Readiness measures the quality of what you hold, not coverage of what buyers want.** The
suspended fraud case still scores 45% because the documents it holds that were not rejected are
genuinely registry-verified. Coverage is a separate question, answered by the requirement
profiles and the gap list — conflating the two would make both meaningless.

**Provider credentials are platform-level, not per-tenant.** A model provider reads document
text, which makes them a processor under Article 28 for every tenant whose documents it
touches. That decision belongs with whoever signed the DPA, not with each supplier.

**AI never touches the assurance ladder.** Classification, extraction and summarisation all
write to a proposal layer. L3 means "matched against an authoritative source" and nothing else
may confer it — the moment a model can, the ladder stops meaning anything.

**Authenticity signals raise a question, never an answer.** They cannot lower assurance any
more than they can raise it, they are never shown to buyers, and they describe the observation
rather than alleging intent. Suppliers have reputations; an unreviewed algorithmic accusation
is a liability, not a feature.

**Purge keeps the audit skeleton.** Deleting the rows would break every remaining tenant's
chain and destroy its evidential value. Documents, versions, files, clients, grants and
users go; timestamps, actions and hashes stay.

---

## Layout

```
app/
  config.py        environment configuration
  db.py            engine, session, SQLite pragmas
  models.py        ORM — the GICC-02 model, SQLite-honest
  catalogue.py     182 document types, 14 groups, 4 requirement templates
  seed.py          bootstrap and tenant provisioning
  demo.py          the six-entity synthetic estate
  security.py      scrypt hashing, sessions, API keys, dependencies
  services/
    audit.py         hash-chained audit log
    documents.py     version state machine, expiry sweep, scoring
    verification.py  connector framework, assurance ladder
    access.py        the permission resolver
    roles.py         role capabilities and enforcement
    activity.py      plain-language activity feed and read receipts
    onboarding.py    guided journey, branching, milestone observation
    analytics.py     platform statistics and server-rendered SVG charts
    providers.py     LLM provider registry, encrypted credentials, connection test
    ai.py            model provider + deterministic fallbacks, inference log
    textextract.py   PDF and Office text extraction
    authenticity.py  certificate authenticity signals
    notifications.py notification engine, transports, sweep
    trust.py         Trust Index computation
    questionnaire.py questionnaire parsing, mapping, answering
    requirements.py  conformance engine
  routers/
    auth.py          sign in, register
    admin.py         platform console, cross-entity users, break-glass
    web_core.py      dashboard, profile, activity, tasks, audit, settings, lifecycle
    onboarding_web.py the five-step guided journey
    intelligence.py  smart upload, notifications, questionnaires, trust, summary
    web_documents.py register, catalogue, versions, verification
    web_disclosure.py groups, clients, grants, requests, requirements
    share.py         public buyer view, trust page
    api.py           REST API v1
    mcp.py           MCP server (2026-07-28)
  templates/       49 Jinja2 templates
  static/css/      Brata theme
tests/smoke_test.py
```

No JavaScript build step, no bundler, no client-side framework. `brata.css` is the whole
front end.

---

## Machine access

Create a key under **Settings & API**. Bind it to a client account and it sees only that
client's grant — the narrower of the token scope and the grant always wins.

```bash
# REST
curl -H "X-API-Key: gicc_…" https://your-app.onrender.com/api/v1/documents

# MCP
curl -X POST https://your-app.onrender.com/mcp \
  -H "X-API-Key: gicc_…" -H "Content-Type: application/json" \
  -H "Mcp-Method: tools/call" -H "Mcp-Name: list_documents" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"list_documents","arguments":{}}}'
```

Interactive REST reference at `/docs`. MCP discovery at `GET /mcp`.

Eight tools: `get_supplier_profile`, `list_documents`, `get_document_metadata`,
`get_document_content`, `verify_credential`, `list_expiries`, `check_requirements`,
`request_access`. Three resources, two prompts.

Mutating and confidentiality-gated calls return `resultType: "input_required"` with a
`requestState`, and resume when the client sends `inputResponses`. Special-category
documents are blocked from the machine channel entirely. Every agent call is logged with
the acting key, the tool, the grant and the bytes returned.

---

## Known limitations

- **SQLite, single instance.** Fine to a few hundred tenants. The ORM is written against
  the Postgres model in GICC-02; moving over means swapping `DATABASE_URL` and reinstating
  the database-level triggers and row-level security that SQLite cannot enforce.
- **No migrations.** Schema changes need the database rebuilt (`rm -rf data/`). Add Alembic
  before this holds production data.
- **Maintenance is request-triggered**, not scheduled. Expiry sweeps run when the dashboard
  loads or verification is invoked.
- **Session cookies are not `https_only`** so the app runs over plain HTTP locally. Set
  `https_only=True` in `app/main.py` for production.
- **No rate limiting** on the API or MCP endpoints.
- **Audit sequence allocation uses a process-level lock.** Correct on one instance; running
  multiple Render instances would need the sequence allocated in the database instead.
- **Scanned PDFs classify on filename only** — there is no OCR, and the upload page says so
  rather than silently returning a poor proposal.
- **Authenticity signals are untuned.** Thresholds are deliberately conservative and every
  signal routes to a human, but they need calibration against real forgeries before anyone
  acts on them automatically.
- **Sending document text to a model provider makes that provider a processor** under Article
  28. The DPA, subprocessor list and transfer assessment need updating before enabling the AI
  layer for real tenants. Special-category documents are excluded from AI processing entirely.
- **Operators share one session cookie with supplier sign-in.** Break-glass sets `user_id`
  alongside `admin_id` in the same session. A genuinely separate cookie domain or path would
  be stronger, and is what a production deployment should do.
- **No MFA anywhere**, including for platform operators, who most need it.
- **Break-glass has no approval step.** An operator states a reason; nobody counter-signs it.
  Two-person authorisation for access to Confidential and Special-category documents is the
  obvious next control.


## GICC-07 features (F26-F29)

| Feature | Route | Notes |
|---|---|---|
| F26 bulk profile import | `/admin/import` | Upload, confirm mapping, dry run, commit. Reversible until a profile is claimed. |
| F27 profile claim | `/claim` (public) | Five-signal score; the model contributes 10 of 100 points. |
| F28 batch upload | `/documents/bulk` | Up to 50 files. Everything lands as a draft. |
| F29 asset register | `/assets` | Per-instrument calibration, ISO 9001 cl. 7.1.5. |
| Type proposals | `/admin/proposals` | Promote, merge or keep local. |

### Additional invariants

- **K1** The global catalogue is extended only by a named operator. Unmatched
  uploads create a tenant-scoped `TEN-<domain>-nnn` type, excluded from Trust
  Index scoring and cross-tenant matching until promoted.
- **K2** An instance-bearing type resolves to a *set*. Conformance and the Trust
  Index take the worst instance and report the count.
- **K3** AI scores a claim; rules decide it. An adverse signal forces human
  review regardless of score.
- **K4** An out-of-tolerance calibration is not an expiry. It quarantines the
  instrument and opens a mandatory impact assessment covering the period since
  the previous calibration.
- **K5** Permission resolution lives once, in `services/access.py`. Both the
  REST and MCP routers call `access.scoped_documents`.

### Schema migrations

`app/migrations.py` applies additive `ALTER TABLE ADD COLUMN` for any column
present in the model but not the database, plus named data steps that need
judgement. It runs at startup and is a no-op when the schema already matches.
It deliberately does not drop, retype, rename or backfill by inference.

Not Alembic: a `versions/` directory cannot travel inside the single-file build,
and `create_all` already handles new tables.

### Known limitations (unchanged from GICC-00 Part 10)

Verification connectors and the registry lookup used in claim scoring are
**simulated**. Not built: CSRF, MFA, rate limiting, malware scanning,
`https_only` cookies, SSO/SCIM, webhooks, OCR. Sequence allocation uses a
process-level mutex that breaks under horizontal scale. The job runner is
in-process: it resumes on restart but does not distribute across instances.


## Buyer portal (F30)

| Step | Route |
|---|---|
| Register as a buyer | `/register/buyer` |
| Find a supplier | `/buyer/search` |
| Company entry | `/buyer/companies/{id}` |
| Supply base | `/buyer/suppliers` |
| Request access | `POST /buyer/suppliers/{id}/request` |
| Records shared with you | `/buyer/suppliers/{id}` |

A buyer is a `Tenant` with `kind = "buyer"`, not a separate entity type. A
company that both buys and sells is one identity with two capabilities
(`kind = "both"`); duplicating the organisation would break the one thing the
platform exists to establish.

### The visibility ladder (K6)

- **Tier 0, anonymous** - the public credential page only.
- **Tier 1, authenticated buyer** - public-register identity: legal name,
  country, registry number, LEI, VAT, DUNS, sector, incorporation date,
  website, and registered address subject to the rule below. Nothing about the
  credential register: no document count, no expiry, no Trust Index. A count
  alone leaks a judgement.
- **Tier 2, granted buyer** - whatever the grant says, resolved through
  `access.disclosable`.

**K6 - full registered address for claimed profiles, locality only for
unclaimed ones.** A claimed profile belongs to a company that chose to be here.
An imported profile does not. For many small companies the registered address is
a home address; it is on a public register, but republishing it in a searchable
directory is a different act from a single lookup.

**K7 - a grant discloses only published versions.** `access.disclosable` is the
single answer, used by the share link and the buyer portal alike.

Buyer-raised requests flow into the supplier's existing `/requests` queue and
approval path unchanged. Every directory search and profile view is recorded, so
bulk harvesting is visible after the fact and a supplier can be told who has been
looking them up.


## IAF CertSearch connector (F31)

Trigger: **Confirm against IAF CertSearch** on any active version of a
management-system certificate (the eleven `QMS-*` and `SEC-*` codes in
`certsearch.STANDARDS`). It opens the source in a separate window, pre-filled,
and holds a lookup open beside it for the result.

### Transports and their ceilings (K8)

| Transport | Ceiling | Status |
|---|---|---|
| `licensed_api` | **L3** | Slot only — set `CERTSEARCH_API_KEY` and implement the client |
| `assisted` | **L2** | Working default. A named person reads the source and attests |
| `scrape` | **L2** | Implemented, **off** unless `CERTSEARCH_ALLOW_SCRAPE=1`. Untested against the live site |

**K8 — a scrape can never reach L3.** L3 means "matched automatically against
an authoritative register". An HTML page fetched by imitating a browser is not
an attributable response: nobody at IAF can confirm they sent it, the markup can
change without notice, and a bot-challenge page parses as "no certificates" as
easily as a real empty result. The ceiling is applied by rule in
`certsearch.apply`, never chosen by a user, and `simulated=True` is set on every
check that did not come from a licensed API.

**K9 — absence is inconclusive, not negative.** Not every certification body
publishes to CertSearch and coverage varies by scheme and region. `not_found`
leaves the existing assurance level untouched, so a gap in IAF's coverage cannot
look like a supplier's dishonesty.

### Reconciliation

Nine fields compared; four are decisive (certificate number, standard, status,
certified organisation). A decisive disagreement is a `mismatch`; a
non-decisive one is a `partial_match`. Dates tolerate three days of drift as
transcription and flag more as a different certification cycle. The comparison
is identical whether details were pasted, scraped or typed — it is not softer
because a person did the typing.


## Demo directory (real register data)

`app/data/mca_companies.csv.gz` — 2,400 companies sampled from 1,473,784
records of the Indian MCA company master data (open register data). Loaded by
`app/demo_directory.py`; embedded as base64 in the single-file build.

**These are real companies, so they are loaded as unclaimed, import-sourced
profiles and nothing else.** Public-register identity only: legal name, CIN,
ROC, incorporation date, registered address, NIC code. No workspace, no user,
no credentials, no Trust Index, no assurance level.

That is not a shortcut. Attaching an invented ISO 27001 certificate to a named
real business would put a fabricated compliance claim against a company that has
never heard of this platform — and a demo is exactly where that would be seen.
The six fictional `.example` entities in `demo.py` remain the only entities that
hold documents.

Because every one of these is unclaimed, K6 withholds their registered addresses
from buyer search automatically. A visitor sees the rule working rather than
being told about it. Struck-off, amalgamated and dissolved companies are kept
deliberately — a directory in which every company is in good standing teaches a
buyer nothing.

The load appears as a committed `ImportBatch` at `/admin/import`, so the
directory has a provenance an operator can open rather than appearing from
nowhere. `demo_directory.build()` is idempotent.


## Public front door (F32)

`/` is a search page and nothing else: one field, placeholder "Supplier name",
with Sign in and Register as slide-over panels rather than separate pages —
sending somebody to another screen to sign in loses the search they came to run.

`/search` merges two sources:

1. **Members first**, marked with a gold star and a "Trusted Supplier" badge.
   A member is an organisation with a live, active workspace; a suspended one is
   not shown as trusted.
2. **Everyone else** from OpenCorporates when `OPENCORPORATES_TOKEN` is set, and
   from the bundled register extract otherwise. The fallback is named in the
   result header rather than implied to be live.

**K10 — public results carry no address.** Name, jurisdiction, company number,
status and membership only. Opening a result asks the visitor to sign in, and
the identity block appears there. For many small companies the registered
address is a home address, and a public searchable directory is not the same act
as a register lookup. Signing in from a company page returns to that company.

## Navigation

Five destinations, everything else indented beneath one of them:

    Start                 Getting started · Activity
    My Trust profile      Company profile · Verification · Control summary · Public page
    My Documentation      Upload · Catalogue · Bulk upload · Assets · Groups
    Actions needed        Access requests · Requirements · Questionnaires · Notifications
    Account management    Clients · Audit trail · Account lifecycle

`/start` is now a lifecycle overview — five rows, ticks, and a Continue button —
rather than a redirect into the next step. The step pages are unchanged and
still carry the "why this step" note.

The supplier home page no longer carries "Where your buyers are looking" or
"Recent activity"; both moved to `/activity`.


## Cloud folder sync (F33)

`/connections` - connect a Google Drive or OneDrive folder, read-only. Every
file is classified into the catalogue, read for verification links and QR codes,
and versioned. The folder is re-checked on a schedule (hourly, daily, weekly or
manual) and on demand.

| Rule | Why |
|---|---|
| Read-only scopes; no write path exists | The platform has no reason to change a supplier's drive |
| The provider's file id is identity | A rename is the same file; a renewal saved under a new name is not a duplicate |
| Same bytes = no new version | A sharing or metadata change is not a change to the credential |
| First-seen files land as drafts | A classifier's guess is not the supplier's statement |
| Later revisions of a confirmed file publish themselves | That is what keeps the profile current |
| A different kind of document saved over a file is filed separately | The old credential's history is not polluted |
| Deleted at source = kept in the register, with an action | A buyer may rely on it |
| Disconnect deletes tokens, keeps everything filed | Leaving should not erase evidence |

**K11 - a link or QR code says where to check, not that the check passed.**
`services/authority.py` decodes QR codes (OpenCV) and reads printed links, then
classifies each: a recognised authority becomes the version's verification URL
and the normal checks run against it; a look-alike domain (including the
`authority.org.evil.io` subdomain trick) raises an action and blocks automatic
publication; and printed text naming a real authority while the QR goes
elsewhere is flagged as the altered-certificate pattern. None of this earns
assurance on its own.

Per-file revision history at `/connections/{id}/files/{file_id}` shows every
revision seen, its content hash, what it was filed as, the authority links found,
and the register version it produced.


## Design language (F34)

Apple-style system: SF Pro / system font stack with optical kerning, 12px
radii, pill buttons, hairline borders, two shadow tiers, and a
`prefers-reduced-motion` guard on every transition.

**Glass.** The sidebar, top bar, public results header and slide-over panels are
translucent with `backdrop-filter: saturate(180%) blur(18-24px)`, so what is
behind them stays legible as colour and shape.

**Motion.** Content rises 6px on load; panels spring in; buttons compress on
press; meters animate to their value. Timing lives in `--t` / `--ease` tokens.

**Progressive disclosure in the sidebar.** Five section headings are always
visible. A section's pages appear only when it is open, and it opens by default
only where the current page lives or where a badge says something needs
attention. A click on a closed heading opens it; the choice is remembered per
browser in `localStorage` (`tsp.nav`). A new supplier therefore sees five words
and one open section; an established one sees exactly what is live.

**Declutter.** Start no longer repeats the Getting-started steps (one line and a
meter), hides Activation milestones once all three are reached, and merges
retention signals and closest gaps into one column.
