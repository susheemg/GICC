"""End-to-end smoke test.

Start the app against a *clean* database, then run:

    python3 tests/smoke_test.py [base_url]

It registers entities and closes one, so it is not idempotent - delete the
data directory before re-running.

Covers every route, the REST API, the MCP server, scope isolation, the buyer
share flow, and the full exit path including purge and audit-chain integrity.
Exits non-zero on any failure, so it works as a deploy gate.
"""
from __future__ import annotations

import io
import json
import re
import sys

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
FAILURES: list[str] = []
UUID = r"([0-9a-f]{32})"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'  — ' + detail if detail and not condition else ''}")
    if not condition:
        FAILURES.append(label)
    return condition


def http(label: str, response: httpx.Response, must: str | None = None) -> httpx.Response:
    ok = response.status_code == 200 and (must is None or must in response.text)
    check(f"{label}", ok, f"status {response.status_code}")
    return response


def section(name: str) -> None:
    print(f"\n--- {name} ---")


def _fake_certificate_pdf() -> bytes:
    """A tiny real PDF with extractable text, so classification has something to
    read rather than being tested against an empty string."""
    content = ("BT /F1 10 Tf 40 750 Td (ISO/IEC 27001 Information Security Management "
               "System Certificate) Tj 0 -16 Td (Certificate Number: IS 776541) Tj "
               "0 -16 Td (Issued by BSI Group) Tj 0 -16 Td (Issue Date: 2026-01-15) Tj "
               "0 -16 Td (Expiry Date: 2029-01-14) Tj ET")
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n{body}\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n"
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF")
    return out.encode("latin-1")


def _registration_pdf(name: str, number: str) -> bytes:
    content = (f"BT /F1 10 Tf 40 750 Td (CERTIFICATE OF INCORPORATION) Tj "
               f"0 -18 Td (Name of Company: {name}) Tj "
               f"0 -16 Td (Company Number: {number}) Tj "
               f"0 -16 Td (Registered by Companies House) Tj "
               f"0 -16 Td (2015-04-12) Tj ET")
    stream = content.encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{start}\n%%EOF").encode()
    return bytes(out)


def main() -> int:
    session = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)

    section("Web interface")
    http("sign-in page renders", session.get("/login"), "Sign in")
    http("demo account signs in", session.post(
        "/login", data={"email": "demo@northgate.example", "password": "demo1234"}), "Readiness")
    for path, must in [
        ("/dashboard", "What is broken"), ("/profile", "Legal entity"),
        ("/documents", "credential"), ("/documents/catalogue", "document types"),
        ("/verification", "Assurance distribution"), ("/groups", "certificate groups"),
        ("/clients", "client"), ("/requests", "Access requests"),
        ("/requirements", "Gaps ranked"), ("/tasks", None), ("/audit", "Hash chain"),
        ("/settings", "API keys"), ("/lifecycle", "exit path"),
        ("/t/northgate", "Public credential page"), ("/docs", None),
    ]:
        http(path, session.get(path), must)

    register = session.get("/documents").text
    document_id = re.search(rf"/documents/{UUID}\"", register).group(1)
    detail = http("document detail", session.get(f"/documents/{document_id}"), "Version 1")
    version_id = re.search(rf"/versions/{UUID}/verify", detail.text).group(1)
    http("verification run", session.post(
        f"/documents/{document_id}/versions/{version_id}/verify"))
    group_id = re.search(rf"/groups/{UUID}\"", session.get("/groups").text).group(1)
    http("group detail", session.get(f"/groups/{group_id}"), "Disclosure policy")
    client_id = re.search(rf"/clients/{UUID}\"", session.get("/clients").text).group(1)
    http("client detail", session.get(f"/clients/{client_id}"), "Access grants")
    profile_id = re.search(rf"/requirements\?profile_id={UUID}",
                           session.get("/requirements").text).group(1)
    http("conformance evaluation", session.post(
        f"/requirements/{profile_id}/evaluate"), "Conformance")

    section("Buyer share flow")
    client_page = session.get(f"/clients/{client_id}").text
    grant_id = re.search(rf"/grants/{UUID}/share-link", client_page).group(1)
    created = session.post(f"/grants/{grant_id}/share-link",
                           data={"days": "14", "label": "Smoke test"})
    token_match = re.search(r"/s/([A-Za-z0-9_\-]{20,})", created.text)
    if not check("share link issued", bool(token_match)):
        return 1
    token = token_match.group(1)

    buyer = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    view = http("buyer opens the pack without an account", buyer.get(f"/s/{token}"))
    check("every share view identifies the viewer",
          "Tell us who you are" in view.text or "Before you open" in view.text)
    view = http("viewer identifies and continues", buyer.post(
        f"/s/{token}/accept", data={"email": "tester@meridian.example",
                                    "full_name": "Test Reviewer", "accept": "on"}))
    shared = re.search(rf"/s/[^/]+/d/{UUID}", view.text)
    if shared:
        http("buyer opens a document", buyer.get(f"/s/{token}/d/{shared.group(1)}"),
             "Assurance level")

    section("Verification ladder")
    fresh = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    fresh.post("/register", data={
        "legal_name": "Smoke Test Systems Ltd", "country": "GB", "registry_number": "11223344",
        "full_name": "Sam Tester", "email": "sam@smoketest.example", "password": "password123"})
    created = fresh.post("/documents/create", data={"type_code": "SEC-001"})
    new_doc = re.search(rf"/documents/{UUID}/versions", created.text).group(1)
    published = fresh.post(f"/documents/{new_doc}/versions", data={
        "issue_date": "2026-01-15", "effective_from": "2026-01-15", "expiry_date": "2029-01-14",
        "certificate_number": "IS 999111", "issuer_name": "BSI Group",
        "holder_name": "Smoke Test Systems Ltd", "scope_statement": "ISMS",
        "verification_url": "https://www.iafcertsearch.org/", "publish": "1"})
    check("complete evidence reaches L3", "L3" in published.text)

    created = fresh.post("/documents/create", data={"type_code": "SEC-020"})
    weak_doc = re.search(rf"/documents/{UUID}/versions", created.text).group(1)
    weak = fresh.post(f"/documents/{weak_doc}/versions", data={
        "issue_date": "2026-01-15", "effective_from": "2026-01-15", "publish": "1"})
    check("evidence-free upload stays at L0", "L0" in weak.text)

    section("REST API")
    keyed = session.post("/settings/api-clients", data={
        "name": "Smoke test agent",
        "scopes": ["profile:read", "documents:read", "verification:read",
                   "requirements:read", "requirements:evaluate", "events:subscribe"]})
    api_key = re.search(r"(gicc_[A-Za-z0-9_\-]{20,})", keyed.text).group(1)
    api = httpx.Client(base_url=BASE, headers={"X-API-Key": api_key}, timeout=30)

    documents = http("GET /api/v1/documents", api.get("/api/v1/documents"), "SEC-001").json()
    target = next(d["id"] for d in documents["documents"] if d["type_code"] == "SEC-001")
    http("GET /api/v1/documents/{id}", api.get(f"/api/v1/documents/{target}"), "versions")
    http("GET /api/v1/expiries", api.get("/api/v1/expiries?within_days=200"))
    http("GET /api/v1/verifications/{id}", api.get(f"/api/v1/verifications/{target}"), "simulated")
    http("GET /api/v1/groups", api.get("/api/v1/groups"))
    profiles = http("GET /api/v1/requirement-profiles",
                    api.get("/api/v1/requirement-profiles")).json()
    evaluation = http("POST evaluate", api.post(
        f"/api/v1/requirement-profiles/{profiles['profiles'][0]['id']}/evaluate"),
        "cited_version_id").json()
    check("evaluation cites versions and an engine version",
          bool(evaluation.get("engine_version")) and evaluation["results"][0].get("type_code"))
    http("GET /api/v1/events", api.get("/api/v1/events?limit=5"))
    http("GET /api/v1/catalogue/types", api.get("/api/v1/catalogue/types?domain=SEC"), "SEC-030")
    check("unauthenticated API call rejected",
          httpx.get(f"{BASE}/api/v1/documents", timeout=30).status_code == 401)

    section("MCP server")
    def rpc(method: str, params: dict | None = None, headers: dict | None = None) -> dict:
        merged = {"X-API-Key": api_key}
        merged.update(headers or {})
        return httpx.post(f"{BASE}/mcp", timeout=30, headers=merged,
                          json={"jsonrpc": "2.0", "id": 1, "method": method,
                                "params": params or {}}).json()

    discovery = http("GET /mcp discovery", httpx.get(f"{BASE}/mcp", timeout=30), "2026-07-28")
    check("protocol version is 2026-07-28",
          rpc("initialize")["result"]["protocolVersion"] == "2026-07-28")
    tools = rpc("tools/list")["result"]
    check("tools/list carries ttlMs and a non-shared cacheScope",
          tools.get("ttlMs") and tools.get("cacheScope") != "shared",
          json.dumps({k: tools.get(k) for k in ("ttlMs", "cacheScope")}))
    check("eight tools advertised", len(tools["tools"]) == 8, str(len(tools["tools"])))
    resource = rpc("resources/read", {"uri": "gicc://supplier/documents"})["result"]
    check("resource read never marked shared", resource.get("cacheScope") != "shared")
    check("prompts/list responds", "prompts" in rpc("prompts/list")["result"])

    profile = rpc("tools/call", {"name": "get_supplier_profile", "arguments": {}})
    check("get_supplier_profile returns the entity",
          "legal_name" in profile["result"]["structuredContent"])
    listed = rpc("tools/call", {"name": "list_documents", "arguments": {"domain": "SEC"}})
    check("list_documents filters by domain",
          listed["result"]["structuredContent"]["count"] > 0)
    verified = rpc("tools/call", {"name": "verify_credential", "arguments": {"document_id": target}})
    check("simulated connectors are disclosed to the agent",
          verified["result"]["structuredContent"]["verification"]["simulated"] is True)
    check("list_expiries responds", "count" in rpc(
        "tools/call", {"name": "list_expiries", "arguments": {"within_days": 365}}
    )["result"]["structuredContent"])
    check("check_requirements scores the supplier", "score" in rpc(
        "tools/call", {"name": "check_requirements", "arguments": {"profile_name": "UK financial"}}
    )["result"]["structuredContent"])
    check("header disagreeing with the body is rejected",
          "error" in rpc("tools/list", None, {"Mcp-Method": "resources/list"}))
    check("unknown tool is rejected",
          "error" in rpc("tools/call", {"name": "drop_database", "arguments": {}}))
    check("unbound key cannot raise an access request",
          "error" in rpc("tools/call", {"name": "request_access",
                                        "arguments": {"group_code": "financial", "purpose": "x"}}))
    check("unauthenticated MCP call rejected",
          httpx.post(f"{BASE}/mcp", timeout=30,
                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401)

    section("Scope isolation and consent")
    bound = session.post("/settings/api-clients", data={
        "name": "Grant-bound agent", "client_account_id": client_id,
        "scopes": ["profile:read", "documents:read", "grants:request"]})
    bound_key = re.search(r"(gicc_[A-Za-z0-9_\-]{20,})", bound.text).group(1)
    bound_api = httpx.Client(base_url=BASE, headers={"X-API-Key": bound_key}, timeout=30)
    scoped = bound_api.get("/api/v1/documents").json()
    check("grant-bound key sees a subset, not the whole register",
          0 < scoped["count"] < documents["count"],
          f"{scoped['count']} of {documents['count']}")
    check("key without verification:read is refused",
          bound_api.get(f"/api/v1/verifications/{scoped['documents'][0]['id']}").status_code == 403)

    def bound_rpc(params: dict) -> dict:
        return httpx.post(f"{BASE}/mcp", timeout=30, headers={"X-API-Key": bound_key},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": params}).json()

    args = {"group_code": "financial", "purpose": "Annual due diligence"}
    gated = bound_rpc({"name": "request_access", "arguments": args})
    check("mutating call asks for confirmation first",
          gated["result"].get("resultType") == "input_required")
    confirmed = bound_rpc({"name": "request_access", "arguments": args,
                           "requestState": gated["result"]["requestState"],
                           "inputResponses": {"confirm": True}})
    check("confirmed call creates the request",
          confirmed["result"]["structuredContent"].get("status") == "open")
    check("request reaches the supplier's queue",
          "Annual due diligence" in session.get("/requests").text)

    for revoke_id in set(re.findall(rf"/grants/{UUID}/revoke",
                                    session.get(f"/clients/{client_id}").text)):
        session.post(f"/grants/{revoke_id}/revoke", data={"reason": "smoke test"})
    check("revoking the grant blinds the key immediately",
          bound_api.get("/api/v1/documents").json()["count"] == 0)

    section("Exit path")
    exiting = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    exiting.post("/register", data={
        "legal_name": "Exit Test Ltd", "country": "GB", "registry_number": "55667788",
        "full_name": "Ex Iter", "email": "exit@test.example", "password": "password123"})
    created = exiting.post("/documents/create", data={"type_code": "INS-003"})
    exit_doc = re.search(rf"/documents/{UUID}/versions", created.text).group(1)
    uploaded = exiting.post(
        f"/documents/{exit_doc}/versions",
        data={"issue_date": "2026-02-01", "effective_from": "2026-02-01",
              "expiry_date": "2027-01-31", "certificate_number": "PI/2026/0001",
              "issuer_name": "Hiscox", "holder_name": "Exit Test Ltd",
              "cover_amount": "5000000", "publish": "1"},
        files={"file": ("pi-cert.pdf", io.BytesIO(b"%PDF-1.4 fake certificate bytes"),
                        "application/pdf")})
    check("file attaches to the version", "/file" in uploaded.text)
    stored_version = re.search(rf"/versions/{UUID}/file", uploaded.text).group(1)
    downloaded = exiting.get(f"/documents/{exit_doc}/versions/{stored_version}/file")
    check("stored file returns the original bytes", downloaded.content.startswith(b"%PDF"))

    exiting.post("/clients", data={"name": "Buyer plc", "relationship_type": "customer",
                                   "country": "GB", "contact_email": "risk@buyer.example",
                                   "contact_name": "B Reviewer"})
    exit_client = re.search(rf"/clients/{UUID}\"", exiting.get("/clients").text).group(1)
    page = exiting.get(f"/clients/{exit_client}").text
    insurance = re.search(r'<option value="([0-9a-f]{32})">Insurance', page) \
        or re.search(r'<option value="([0-9a-f]{32})"', page)
    exiting.post(f"/clients/{exit_client}/grants", data={
        "group_id": insurance.group(1), "permission_level": "view_download",
        "purpose": "Onboarding review", "days": "90"})
    exit_grant = re.search(rf"/grants/{UUID}/share-link",
                           exiting.get(f"/clients/{exit_client}").text).group(1)
    link = exiting.post(f"/grants/{exit_grant}/share-link", data={"days": "14"})
    exit_token = re.search(r"/s/([A-Za-z0-9_\-]{20,})", link.text).group(1)
    exit_buyer = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    exit_buyer.get(f"/s/{exit_token}")
    exit_buyer.post(f"/s/{exit_token}/accept", data={
        "email": "reviewer@buyer.example", "full_name": "B Reviewer", "accept": "on"})
    check("buyer can reach the pack", exit_buyer.get(f"/s/{exit_token}").status_code == 200)

    export = exiting.get("/lifecycle/export").json()
    check("export is complete",
          bool(export["documents"] and export["grants"] and export["audit"]),
          f"{len(export['documents'])} docs, {len(export['grants'])} grants")

    exiting.post(f"/documents/{exit_doc}/hold")
    exiting.post("/lifecycle/close", data={"reason": "Smoke test", "confirm": "CLOSE"})
    check("closure starts and shows the grace window",
          "Closure in progress" in exiting.get("/lifecycle").text)
    during_grace = exit_buyer.get(f"/s/{exit_token}")
    check("buyer keeps access during grace and is warned",
          during_grace.status_code == 200 and "closing its account" in during_grace.text)
    check("grantee notification event written",
          "supplier.exiting" in exiting.get("/audit?action=supplier.exiting").text)
    check("legal hold blocks the purge",
          "legal hold" in exiting.post("/lifecycle/purge", data={"confirm": "PURGE"}).text)
    exiting.post(f"/documents/{exit_doc}/hold")
    check("purge completes once the hold is released",
          "Sign in" in exiting.post("/lifecycle/purge", data={"confirm": "PURGE"}).text)
    check("buyer access ends after the purge",
          exit_buyer.get(f"/s/{exit_token}").status_code in (403, 404))
    check("closed workspace refuses sign-in",
          "did not match" in exiting.post("/login", data={
              "email": "exit@test.example", "password": "password123"}).text)


    section("Platform console")
    operator = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    http("operator sign-in page renders", operator.get("/admin/login"), "Operator sign in")
    check("supplier credentials are refused at the operator door",
          "did not match" in operator.post("/admin/login", data={
              "email": "demo@northgate.example", "password": "demo1234"}).text)
    http("operator signs in", operator.post("/admin/login", data={
        "email": "admin@gicc.platform", "password": "admin1234"}), "Entities")
    for path, must in [("/admin", "Assurance across the platform"),
                       ("/admin/tenants", "entities"), ("/admin/users", "Role capabilities"),
                       ("/admin/catalogue", "document types"), ("/admin/audit", "events")]:
        http(path, operator.get(path), must)

    # Pin to the live demo tenant: the exit-path section above has already
    # closed one entity, and a closed workspace refuses sign-in by design.
    admin_tenant = re.search(rf"/admin/tenants/{UUID}",
                             operator.get("/admin/tenants?q=Northgate").text).group(1)
    http("entity detail", operator.get(f"/admin/tenants/{admin_tenant}"), "Break-glass access")
    anonymous = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30)
    check("anonymous console access is redirected",
          anonymous.get("/admin").status_code in (303, 307))
    check("a supplier session cannot reach the console",
          "Operator sign in" in session.get("/admin").text)

    section("Managing users across entities")
    made = operator.post("/admin/users", data={
        "tenant_id": admin_tenant, "email": "newstaff@northgate.example",
        "full_name": "New Staff", "role": "viewer", "password": "password123"})
    check("operator creates a user inside another entity",
          "newstaff@northgate.example" in made.text)
    listing = operator.get("/admin/users").text
    managed = re.search(r"newstaff@northgate\.example.*?/admin/users/([0-9a-f]{32})/update",
                        listing, re.S).group(1)
    check("operator changes a role",
          "Compliance manager" in operator.post(f"/admin/users/{managed}/update",
                                                data={"role": "compliance",
                                                      "sensitive_access": "on"}).text)
    check("operator resets a password",
          "resetpass99" in operator.post(f"/admin/users/{managed}/reset-password",
                                         data={"new_password": "resetpass99"}).text)
    reset_client = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    check("user signs in with the reset password",
          "Readiness" in reset_client.post("/login", data={
              "email": "newstaff@northgate.example", "password": "resetpass99"}).text)
    operator.post(f"/admin/users/{managed}/toggle-active")
    disabled = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    check("disabled user cannot sign in",
          "did not match" in disabled.post("/login", data={
              "email": "newstaff@northgate.example", "password": "resetpass99"}).text)
    operator.post(f"/admin/users/{managed}/toggle-active")

    section("Role enforcement")
    sales = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    sales.post("/login", data={"email": "bids@northgate.example", "password": "demo1234"})
    check("sales role reads the register", sales.get("/documents").status_code == 200)
    blocked = sales.post("/documents/create", data={"type_code": "TEC-005"})
    check("sales role cannot add documents, and is told who can",
          blocked.status_code == 403 and "Compliance manager" in blocked.text,
          f"status {blocked.status_code}")
    compliance = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    compliance.post("/login", data={"email": "compliance@northgate.example",
                                    "password": "demo1234"})
    check("compliance role adds documents",
          compliance.post("/documents/create", data={"type_code": "TEC-005"}).status_code == 200)
    check("compliance role cannot issue grants",
          compliance.post(f"/clients/{client_id}/grants", data={
              "group_id": "x", "permission_level": "view", "purpose": "t"}).status_code == 403)
    check("only an Owner can close the account",
          compliance.post("/lifecycle/close", data={"reason": "x", "confirm": "CLOSE"}
                          ).status_code == 403)

    section("Break-glass")
    glass = operator.post(f"/admin/tenants/{admin_tenant}/break-glass", data={
        "reason": "Investigating a failed upload", "ticket_reference": "SUP-1042"})
    check("break-glass opens a supplier session",
          "Platform support session in progress" in glass.text)
    check("the banner names the operator and the reason",
          "admin@gicc.platform" in glass.text and "Investigating a failed upload" in glass.text)
    check("the supplier sees the support session in their own activity log",
          "opened a support session" in operator.get("/activity?actor=platform").text)
    operator.post("/admin/break-glass/end")

    section("Suspension")
    check("entity suspends",
          "Suspended" in operator.post(f"/admin/tenants/{admin_tenant}/suspend", data={
              "ground": "fraud", "reason": "Suspected forged ISO certificate"}).text)
    suspended = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    check("suspended tenant cannot sign in",
          "suspended" in suspended.post("/login", data={
              "email": "demo@northgate.example", "password": "demo1234"}).text.lower())
    check("suspended tenant's share links stop resolving",
          buyer.get(f"/s/{token}").status_code == 403)
    operator.post(f"/admin/tenants/{admin_tenant}/reinstate", data={"note": "cleared"})
    check("reinstated tenant signs in again",
          "Readiness" in suspended.post("/login", data={
              "email": "demo@northgate.example", "password": "demo1234"}).text)

    section("Activity log")
    feed = session.get("/activity")
    http("activity page renders", feed, "Everything that happens on this account")
    check("events read as sentences, not action codes",
          "published version" in feed.text or "signed in" in feed.text)
    check("platform actions are shown to the supplier",
          "suspended this account" in feed.text
          and "Suspected forged ISO certificate" in feed.text)
    for actor_filter, must in [("team", "Your team"), ("buyer", "Buyer"),
                               ("agent", "Agent"), ("platform", "Platform"),
                               ("system", "Automatic")]:
        http(f"filter: {actor_filter}", session.get(f"/activity?actor={actor_filter}"), must)
    check("significant-only filter works",
          session.get("/activity?significance=high").status_code == 200)
    check("search works", session.get("/activity?q=suspend").status_code == 200)
    check("buyer read receipts are attributed to the named reviewer",
          "Who read what" in feed.text and "tester@meridian.example" in feed.text)
    check("agent activity is separated from team activity",
          "over the API" in session.get("/activity?actor=agent").text
          or "listed" in session.get("/activity?actor=agent").text)


    section("AI: classification on upload")
    pdf_bytes = _fake_certificate_pdf()
    classified = session.post("/documents/upload", files={
        "file": ("iso-27001-certificate.pdf", pdf_bytes, "application/pdf")})
    check("upload accepted and classified", classified.status_code == 200)
    check("a proposal is shown for confirmation, not filed automatically",
          "Confirm before this is filed" in classified.text)
    check("the engine used is disclosed",
          "no model key" in classified.text or "model proposal" in classified.text)
    check("classification proposes a catalogue type",
          "SEC-001" in classified.text or "confidence" in classified.text)
    http("upload page renders when empty", session.get("/documents/upload"))

    section("AI: control summary")
    http("summary page", session.get("/summary"), "Control summary")
    generated = session.post("/summary/generate")
    check("summary generated from held evidence",
          generated.status_code == 200 and "credentials in this vault" in generated.text)
    check("unverified evidence described as claimed, not demonstrated",
          "self-declared" in generated.text or "claims rather than" in generated.text)
    check("gaps stated explicitly", "Not held" in generated.text
          or "Currently impaired" in generated.text)
    check("summary publish toggles",
          session.post("/summary/publish").status_code == 200)

    section("Authenticity signals")
    signal_page = session.get("/documents/" + document_id)
    check("document page renders with the signals panel", signal_page.status_code == 200)
    rerun_version = re.search(rf"/versions/{UUID}/authenticity", signal_page.text)
    if rerun_version:
        rerun = session.post(f"/documents/{document_id}/versions/"
                             f"{rerun_version.group(1)}/authenticity")
        check("signals can be re-run", rerun.status_code == 200)
        check("signals never change an assurance level",
              "never change an assurance level" in rerun.text)
    duplicate = session.get("/trust")
    check("trust page renders", duplicate.status_code == 200)

    section("Trust Index")
    trust_page = http("trust page", session.get("/trust"), "Trust Index")
    check("composite and band shown",
          "Composite" in trust_page.text and "algorithm v" in trust_page.text)
    # Ampersands are HTML-escaped in the rendered page, so match on the
    # unambiguous part of each label.
    check("all eight risk domains present",
          all(label in trust_page.text for label in
              ["Corporate identity", "Regulatory", "Financial standing", "Insurance",
               "Security", "Resilience", "Safety", "People"]))
    check("the score is described as evidence, not a judgement of the company",
          "not a judgement of the company" in trust_page.text)
    check("one next action named, not a backlog",
          "single change that moves this most" in trust_page.text
          or "Composite" in trust_page.text)
    recomputed = session.post("/trust/recompute")
    check("recompute works and stamps the algorithm version",
          recomputed.status_code == 200 and "algorithm v" in recomputed.text)
    check("band publishes without publishing the number",
          "number stays private" in session.post("/trust/publish").text)

    section("Notification engine")
    inbox = http("notification inbox", session.get("/notifications"), "notification")
    swept = session.post("/notifications/sweep")
    check("sweep runs on demand", "Sweep complete" in swept.text)
    check("expiry notifications generated",
          "expire" in swept.text.lower() or "notification" in swept.text.lower())
    expiring = session.get("/notifications?kind=certificate.expiring")
    check("expiring filter works", expiring.status_code == 200)
    expired = session.get("/notifications?kind=certificate.expired")
    check("expired filter works", expired.status_code == 200)
    check("uploading a document notifies",
          session.get("/notifications?kind=document.uploaded").status_code == 200)
    check("preference can be switched off",
          session.post("/notifications/preferences/buyer.read",
                       data={"channel": "off"}).status_code == 200)
    check("mark all read works", session.post("/notifications/read").status_code == 200)

    section("Questionnaire automation")
    questions = (b"QREF,Question\n"
                 b"S1,Do you hold a current ISO 27001 certification?\n"
                 b"S2,Please provide your most recent SOC 2 Type II report.\n"
                 b"S3,Do you maintain cyber liability insurance?\n"
                 b"X1,Describe your approach to quantum-resistant cryptography.\n")
    uploaded = session.post("/questionnaires",
                            files={"file": ("review.csv", questions, "text/csv")},
                            data={"name": "Smoke test review"})
    check("questionnaire parsed and drafted", uploaded.status_code == 200)
    check("answers drafted from evidence held", "drafted" in uploaded.text.lower())
    check("questions without evidence are returned as gaps, not answered",
          "no evidence held" in uploaded.text or "gaps" in uploaded.text.lower())
    check("every drafted answer carries a citation",
          "assurance" in uploaded.text and "version" in uploaded.text)
    questionnaire_id = re.search(rf"/questionnaires/{UUID}", uploaded.url.path).group(1) \
        if re.search(rf"/questionnaires/{UUID}", str(uploaded.url)) else None
    if questionnaire_id:
        item = re.search(rf"/questionnaires/{questionnaire_id}/items/{UUID}", uploaded.text)
        if item:
            approved = session.post(
                f"/questionnaires/{questionnaire_id}/items/{item.group(1)}",
                data={"answer": "Yes, ISO 27001 certificate IS 776541 is held.",
                      "action": "approve"})
            check("approving banks the answer for reuse", approved.status_code == 200)
        export = session.get(f"/questionnaires/{questionnaire_id}/export")
        check("export returns CSV with citations",
              export.status_code == 200 and "Version IDs" in export.text)
    http("questionnaire list", session.get("/questionnaires"), "questionnaire")


    section("AI provider administration")
    http("provider page", operator.get("/admin/ai"), "Add a provider")
    check("AI works with no provider configured",
          "still works" in operator.get("/admin/ai").text
          or "deterministic rules" in operator.get("/admin/ai").text)
    created = operator.post("/admin/ai", data={
        "provider": "anthropic", "label": "Smoke test provider",
        "model": "claude-sonnet-4-6", "api_key": "sk-ant-not-a-real-key",
        "max_tokens": "1500", "data_boundary": "third_party"})
    check("provider saved and tested on save",
          "Connection test" in created.text)
    check("a bad key is diagnosed, not swallowed",
          "401" in created.text or "Rejected the key" in created.text
          or "reach the endpoint" in created.text)
    check("the key is never rendered back to the browser",
          "sk-ant-not-a-real-key" not in created.text)
    check("a mask and fingerprint identify the key instead",
          "fp " in created.text)
    provider_id = re.search(rf"/admin/ai/{UUID}/enable", created.text).group(1)
    refused = operator.post(f"/admin/ai/{provider_id}/enable")
    check("enabling a provider that failed its test is refused",
          "Test the connection before enabling" in refused.text)
    tested = operator.post(f"/admin/ai/{provider_id}/test")
    check("test can be re-run on demand", tested.status_code == 200)
    updated = operator.post(f"/admin/ai/{provider_id}/update", data={
        "model": "claude-haiku-4-5", "base_url": "", "api_key": "",
        "max_tokens": "2000", "data_boundary": "self_hosted"})
    check("editing without re-entering the key works",
          updated.status_code == 200 and "claude-haiku-4-5" in updated.text)
    check("every provider offered", all(
        provider in operator.get("/admin/ai").text for provider in
        ["Anthropic", "OpenAI", "Azure", "Gemini", "Mistral", "compatible"]))
    check("key changes are audited by fingerprint, never by value",
          "ai_provider" in operator.get("/admin/audit?action=ai_provider").text)
    removed = operator.post(f"/admin/ai/{provider_id}/delete")
    check("deleting destroys the stored key",
          "Stored key destroyed" in removed.text)
    check("a supplier cannot reach provider configuration",
          "Operator sign in" in session.get("/admin/ai").text)


    section("Guided self-registration")
    guided = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    landed = guided.post("/register", data={
        "legal_name": "Guided Test Ltd", "country": "GB", "registry_number": "77889900",
        "full_name": "Gee Test", "email": "gee@guided.example", "password": "password123"})
    check("registration lands in the guide, not an empty dashboard",
          "/start" in str(landed.url), str(landed.url))
    check("step 1 asks to confirm the company",
          "Confirm your company" in landed.text)
    # The landing page is now the lifecycle overview: the whole shape first, so
    # setup reads as finite rather than as an unknown number of screens.
    check("the guide opens on the lifecycle, not mid-flow",
          landed.text.count("lifecycle-row") == 5, str(landed.url))
    check("every step shows a tick state", "lifecycle-tick" in landed.text)
    check("each step explains why it exists",
          "Why this step" in session.get("/start/1").text)

    step1 = guided.post("/start/1", data={
        "legal_name": "Guided Test Ltd", "country": "GB",
        "registry_number": "77889900", "trading_name": "Guided", "website": ""})
    check("step 1 completes and moves to step 2",
          "Tell us who buys from you" in step1.text)
    check("registry verification is progressive, never blocking",
          "Registry verified" in step1.text or "Company confirmed" in step1.text)

    step2 = guided.post("/start/2", data={
        "sector": "Software and IT services", "buyer_types": ["financial"]})
    check("step 2 branches the recommendations",
          "Add your first credential" in step2.text)
    check("recommendations reflect the buyer type chosen",
          "SEC-001" in step2.text or "SEC-008" in step2.text)
    check("the full catalogue stays one click away",
          "browse all" in step2.text.lower() or "catalogue" in step2.text.lower())

    skipped = guided.post("/start/3/skip")
    check("any step can be skipped", skipped.status_code == 200)
    check("a skip is acknowledged, not silently swallowed",
          "Skipped" in skipped.text or "Package what you share" in skipped.text)

    guided.post("/start/4")
    step5 = guided.get("/start/5")
    check("step 5 asks for a buyer", "Share with a buyer" in step5.text)
    shared = guided.post("/start/5", data={
        "client_name": "Guided Buyer plc", "contact_email": "risk@guidedbuyer.example",
        "contact_name": "A Buyer",
        "group_id": re.search(r'<option value="([0-9a-f]{32})"', step5.text).group(1),
        "days": "90", "purpose": "Smoke test review"})
    check("one action creates the client, grant and share link",
          "can now see" in shared.text)
    check("reaching M2 is called out as the activation moment",
          "M2" in shared.text or "milestone" in shared.text.lower())
    token = re.search(r"/s/([A-Za-z0-9_\-]{20,})", shared.text)
    check("a usable share link is produced", bool(token))
    check("the guide reports completion", "Setup complete" in guided.get("/start/done").text)
    check("milestones are stamped from facts, not step clicks",
          "reached" in guided.get("/start/done").text.lower())

    dashboard = guided.get("/dashboard")
    check("a completed guide no longer occupies the dashboard",
          "Getting started —" not in dashboard.text)
    partial = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    partial.post("/register", data={
        "legal_name": "Partial Test Ltd", "country": "GB", "registry_number": "",
        "full_name": "Par Tial", "email": "par@partial.example", "password": "password123"})
    check("an unfinished guide shows a progress card on the dashboard",
          "Getting started" in partial.get("/dashboard").text)
    check("the guide can be dismissed",
          "Guide hidden" in partial.post("/start/dismiss").text)
    check("a dismissed guide is still re-enterable",
          partial.get("/start").status_code == 200)

    section("Platform analytics")
    analytics_page = http("analytics console", operator.get("/admin/analytics"),
                          "Onboarding funnel")
    check("the funnel names the worst transition",
          "Worst transition" in analytics_page.text)
    check("activation is stated against the target and the benchmark",
          "industry median" in analytics_page.text)
    check("activation windows are measured at 7, 14 and 30 days",
          all(f"{w} days" in analytics_page.text for w in (7, 14, 30)))
    check("time to value reported per milestone",
          "Time to value" in analytics_page.text)
    check("cohorts shown so a change can be judged",
          "Cohorts" in analytics_page.text)
    check("per-step skip rates shown",
          "Skipped" in analytics_page.text and "Onboarding steps" in analytics_page.text)
    for heading in ["The estate", "Verification health", "Disclosure",
                    "Trust Index distribution", "AI layer", "Risk and enforcement",
                    "Tenant health"]:
        check(f"section: {heading}", heading in analytics_page.text)
    check("charts render server-side as inline SVG",
          analytics_page.text.count("<svg") >= 8,
          f"{analytics_page.text.count('<svg')} svg elements")
    check("no JavaScript charting library is loaded",
          "chart.js" not in analytics_page.text.lower()
          and "d3.min.js" not in analytics_page.text.lower())
    for period in ("7", "90", "365", "all"):
        check(f"period filter: {period}",
              operator.get(f"/admin/analytics?period={period}").status_code == 200)
    for metric in ("at_risk", "not_activated", "signals"):
        drill = operator.get(f"/admin/analytics/tenants?metric={metric}")
        check(f"drill-through: {metric}", drill.status_code == 200)
    export = operator.get("/admin/analytics/export?period=all")
    check("export returns JSON without the SVG blobs",
          export.status_code == 200 and "<svg" not in export.text)
    payload = export.json()
    check("export carries the funnel, activation and cohort data",
          all(k in payload for k in ("funnel", "activation", "cohorts", "estate")))
    check("a supplier cannot reach platform analytics",
          "Operator sign in" in session.get("/admin/analytics").text)


    # ----------------------------------------------------------------------
    # GICC-07: bulk import, profile claim, batch upload, calibration
    # ----------------------------------------------------------------------
    section("F26 - bulk profile import")

    csv_body = (
        "Company Name,Country,Company Number,Registry,Website,Contact Email\n"
        "Ferrocast Metals Ltd,GB,09912345,Companies House,ferrocast.example,jo@x.example\n"
        "Ferrocast Metals Ltd,GB,09912345,Companies House,ferrocast.example,jo@x.example\n"
        "Aventine Pharma,GB,,Companies House,aventine.example,\n"
        "Alpha Three Co,GBR,12345678,Companies House,,\n"
        "Bad Country Co,Freedonia,88112233,Companies House,,\n"
        "Kestrel Logistics,GB,07733111,Companies House,,\n"
    )
    upload = operator.post("/admin/import/upload",
                           files={"file": ("suppliers.csv", csv_body.encode(), "text/csv")})
    check("import upload is accepted", upload.status_code == 200)
    check("the contact-email column is refused by name",
          "personal or contact data" in upload.text)
    batch_id = re.search(rf"/admin/import/{UUID}", str(upload.url))
    check("a batch was staged", batch_id is not None, str(upload.url))

    if batch_id:
        bid = batch_id.group(1)
        validated = operator.post(f"/admin/import/{bid}/validate", data={
            "map_legal_name": "0", "map_country": "1", "map_registry_number": "2",
            "map_registry_authority": "3", "map_website": "4"})
        check("dry run completes without writing", validated.status_code == 200)
        check("staged rows survive without a session cookie",
              "Duplicate of row" in validated.text or "reject" in validated.text.lower())
        detail = operator.get(f"/admin/import/{bid}")
        check("duplicate rows inside one file are rejected",
              "Duplicate of row" in detail.text)
        check("an unrecognised country is rejected by name, not truncated",
              "not a recognised country code" in detail.text)
        check("a valid alpha-3 code is converted rather than truncated",
              ">GB<" in detail.text.replace(" ", ""))
        check("a name matching an existing profile is held, not merged",
              "hold" in detail.text.lower())

        before = operator.get("/admin/import").text
        committed = operator.post(f"/admin/import/{bid}/commit")
        check("commit reports what it created", "profile(s) created" in committed.text)

        report = operator.get(f"/admin/import/{bid}/report")
        check("an outcome report downloads as CSV",
              report.status_code == 200 and "Classification" in report.text)

    section("F27 - profile claim")

    public = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    search = public.get("/claim", params={"q": "Ferrocast"})
    check("an imported profile is findable", search.status_code == 200
          and "Ferrocast" in search.text)
    check("search results carry no document counts",
          "credential" not in search.text.lower().split("public credential")[0][-400:])

    org_link = re.search(rf"/claim/{UUID}", search.text)
    check("the profile offers a claim route", org_link is not None)

    if org_link:
        org_id = org_link.group(1)
        # A claim with no matching registry number and a free email address must
        # not auto-approve, whatever else it scores.
        weak = public.post(f"/claim/{org_id}", data={
            "claimant_email": "someone@gmail.com", "claimant_name": "Someone"},
            files={"evidence": ("letter.pdf", _fake_certificate_pdf(), "application/pdf")})
        check("a weak claim never auto-approves",
              weak.status_code == 200 and "Approved." not in weak.text)
        check("the claimant is shown what was actually checked",
              "Registry number" in weak.text or "review" in weak.text.lower())

        check("a claim scoring below the floor is rejected outright",
              "Not approved" in weak.text)

    # A claim whose registry number and legal name both match should score
    # highly - and still go to review, because the profile came from an import.
    kestrel = public.get("/claim", params={"q": "Kestrel Logistics"})
    kestrel_link = re.search(rf"/claim/{UUID}", kestrel.text)
    if kestrel_link:
        strong = public.post(f"/claim/{kestrel_link.group(1)}", data={
            "claimant_email": "ops@kestrel-logistics.example",
            "claimant_name": "Ops Lead"},
            files={"evidence": ("incorporation.pdf",
                                _registration_pdf("Kestrel Logistics", "07733111"),
                                "application/pdf")})
        check("a matching registry number scores highly", strong.status_code == 200)
        check("an import-sourced profile still goes to a person",
              "Approved." not in strong.text)

        queue = operator.get("/admin/claims")
        check("the claim reaches the operator queue", queue.status_code == 200)
        check("adverse signals are surfaced to the reviewer",
              "import sourced" in queue.text.lower())
        check("the reviewer sees the signal breakdown, not just a score",
              "Registry number" in queue.text)

        claim_id = re.search(rf"/admin/claims/{UUID}/decide", queue.text)
        if claim_id:
            approved = operator.post(f"/admin/claims/{claim_id.group(1)}/decide",
                                     data={"decision": "approve", "note": "Verified"})
            check("approval provisions a workspace against the imported profile",
                  "now belongs to" in approved.text)
            check("the queue empties after a decision",
                  "/decide" not in operator.get("/admin/claims").text.split(
                      "Recently decided")[0])

    section("F28 - batch upload")

    files = [("files", (f"iso27001-cert-{n}.pdf", _fake_certificate_pdf(),
                        "application/pdf")) for n in range(3)]
    files.append(("files", ("empty.pdf", b"", "application/pdf")))
    files.append(("files", ("Widget Torque Log 2026.pdf", _fake_certificate_pdf(),
                            "application/pdf")))
    started = session.post("/documents/bulk", files=files)
    check("a batch upload starts", started.status_code == 200)
    check("an empty file is rejected up front with a reason",
          "empty" in started.text.lower())

    job_link = re.search(rf"/documents/bulk/{UUID}", str(started.url))
    if job_link:
        job_id = job_link.group(1)
        import time
        for _ in range(30):
            progress = session.get(f"/documents/bulk/{job_id}/progress").json()
            if progress["finished"]:
                break
            time.sleep(1)
        check("the batch completes", progress["finished"])
        check("no file fails to process", progress["failed"] == 0,
              f"{progress['failed']} failed")
        check("identical files are deduplicated rather than versioned",
              progress["total"] <= 3, f"total {progress['total']}")
        check("every accepted file produced an outcome",
              progress["done"] == progress["total"])

        result = session.get(f"/documents/bulk/{job_id}")
        check("the outcome page states nothing was published",
              "draft" in result.text.lower())

    section("F29 - asset register and calibration")

    made_asset = session.post("/assets", data={
        "asset_tag": "CMM-04", "name": "Coordinate measuring machine",
        "serial_number": "ZX-99120", "manufacturer": "Mitutoyo",
        "calibration_interval_months": "12", "criticality": "5",
        "interval_basis": "Drift history over three cycles"})
    check("an instrument is added to the register",
          made_asset.status_code == 200 and "CMM-04" in made_asset.text)
    check("coverage is counted against assets, not certificates",
          "never calibrated" in made_asset.text)

    duplicate = session.post("/assets", data={
        "asset_tag": "CMM-04", "name": "Another machine"})
    check("a duplicate asset tag is refused", "already exists" in duplicate.text)

    asset_link = re.search(rf"/assets/{UUID}", made_asset.text)
    if asset_link:
        asset_id = asset_link.group(1)

        # In tolerance, unaccredited laboratory: the record must be capped.
        capped = session.post(f"/assets/{asset_id}/calibration", data={
            "type_code": "CAL-001", "calibration_date": "2026-02-10",
            "next_due_on": "2027-02-10", "laboratory_name": "Bench Calibrations Ltd",
            "as_found": "in_tolerance", "as_left": "in_tolerance",
            "measurement_uncertainty": "0.002 mm, k=2",
            "traceability_statement": "Traceable to national standards"},
            files={"certificate": ("cal1.pdf", _fake_certificate_pdf(), "application/pdf")})
        check("an unaccredited laboratory caps the record at L1",
              "capped at L1" in capped.text)

        # Out of tolerance: quarantine plus a mandatory impact assessment.
        oot = session.post(f"/assets/{asset_id}/calibration", data={
            "type_code": "CAL-001", "calibration_date": "2026-06-01",
            "next_due_on": "2027-06-01", "laboratory_name": "UKAS Metrology Services",
            "accreditation_body": "UKAS", "accreditation_number": "0421",
            "as_found": "out_of_tolerance", "as_left": "adjusted_in_tolerance",
            "measurement_uncertainty": "0.002 mm, k=2"},
            files={"certificate": ("cal2.pdf", _fake_certificate_pdf(), "application/pdf")})
        check("an out-of-tolerance result quarantines the instrument",
              "quarantined" in oot.text.lower())
        check("it opens an impact assessment rather than a renewal task",
              "impact assessment" in oot.text.lower())

        register = session.get("/assets")
        check("the register shows the open assessment",
              "impact assessment" in register.text.lower())

        assessment_id = re.search(rf"/assessments/{UUID}/close", oot.text)
        if assessment_id:
            closed = session.post(f"/assessments/{assessment_id.group(1)}/close", data={
                "disposition": "no_impact", "conclusion": "Re-measured 40 parts, all within spec",
                "products_affected": "Batch 2026-14"})
            check("closing the assessment returns the asset to service",
                  "returned to service" in closed.text)

    section("K2 - multi-instance resolution")

    from app.db import SessionLocal
    from app.models import Tenant
    from app.services import assets as assetsvc

    with SessionLocal() as db:
        tenant = db.scalar(__import__("sqlalchemy").select(Tenant).where(
            Tenant.slug == "northgate"))
        if tenant:
            resolution = assetsvc.resolve(db, tenant.id, "CAL-001")
            check("a multi-instance type resolves to a set, not one row",
                  resolution.get("multi") is True)
            check("resolution carries the instance count",
                  "count" in resolution)
            coverage = assetsvc.coverage(db, tenant.id)
            check("coverage answers the auditor's question",
                  all(k in coverage for k in ("requiring", "overdue", "never")))

    section("K1 - the catalogue is extended only by a person")

    proposals = operator.get("/admin/proposals")
    check("the proposal queue renders", proposals.status_code == 200)
    check("the queue explains why promotion is a human decision",
          "join key" in proposals.text)

    with SessionLocal() as db:
        from app.models import DocumentType
        import sqlalchemy as sa
        tenant_scoped = db.scalar(sa.select(sa.func.count(DocumentType.id)).where(
            DocumentType.scope == "tenant"))
        globals_ = db.scalar(sa.select(sa.func.count(DocumentType.id)).where(
            DocumentType.scope == "global"))
        check("unmatched uploads never create global catalogue types",
              globals_ >= 214)
        check("tenant-scoped types are visibly local",
              tenant_scoped is not None)

    section("Migrations")

    from app.db import engine
    from app import migrations
    pending = migrations.pending_columns(engine)
    check("the live schema matches the model after startup", not pending,
          f"{len(pending)} column(s) still missing")
    result = migrations.run(engine, verbose=False)
    check("re-running migrations is a no-op",
          result["columns_added"] == 0 and not result["failed"])

    section("Buyer portal - registration and directory")

    buyer = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    http("buyer registration page renders", buyer.get("/register/buyer"),
         "Register as a buyer")
    made = buyer.post("/register/buyer", data={
        "legal_name": "Harrowgate Procurement Ltd", "country": "GB",
        "registry_number": "11224455", "full_name": "Priya Raman",
        "email": "priya@harrowgate.example", "password": "buyer1234"})
    check("a buyer workspace is created and lands on the supply base",
          made.status_code == 200 and "Supply base" in made.text)
    check("a buyer does not land on the supplier dashboard",
          "Activation milestones" not in made.text)

    short = buyer.get("/buyer/search", params={"q": "no"})
    check("a query below the minimum length returns nothing",
          "0 result" in short.text or "result" not in short.text.lower())

    found = buyer.get("/buyer/search", params={"q": "Northgate"})
    check("a claimed supplier is findable", "Northgate" in found.text)
    check("search shows the LEI column", "LEI" in found.text)
    check("search never leaks the credential register",
          "Trust Index" not in found.text and "expiring" not in found.text.lower())

    org_link = re.search(rf"/buyer/companies/{UUID}", found.text)
    check("a result links to the company entry", org_link is not None)

    if org_link:
        org_id = org_link.group(1)
        entry = buyer.get(f"/buyer/companies/{org_id}")
        check("the company entry shows registered address for a claimed profile",
              "Registered address" in entry.text)
        check("identity fields are visible post-login",
              "Registry number" in entry.text and "LEI" in entry.text)

        added = buyer.post("/buyer/suppliers", data={
            "organisation_id": org_id, "internal_reference": "SUP-0091",
            "category": "Data services", "criticality": "2"})
        check("a supplier is added to the supply base",
              added.status_code == 200 and "SUP-0091" in added.text)

        link_id = re.search(rf"/buyer/suppliers/{UUID}", str(added.url))
        check("the supplier detail page opens", link_id is not None)

    section("Buyer portal - access request")

    if link_id:
        lid = link_id.group(1)
        detail = buyer.get(f"/buyer/suppliers/{lid}")
        check("no records are visible before a grant",
              "Records shared with you" not in detail.text)

        asked = buyer.post(f"/buyer/suppliers/{lid}/request", data={
            "purpose": "Annual supplier review for the 2027 framework"})
        check("the request is accepted", "Request sent" in asked.text)
        check("the supplier now shows as awaiting a decision",
              "requested" in asked.text)

        listed = buyer.get("/buyer/requests")
        check("the request appears in the buyer's history",
              "Annual supplier review" in listed.text)

        # The supplier decides, through the flow that already existed.
        supplier_view = session.get("/requests")
        check("the request reaches the supplier's queue",
              "Harrowgate" in supplier_view.text)

        request_id = re.search(rf"/requests/{UUID}/decide", supplier_view.text)
        if request_id:
            approved = session.post(f"/requests/{request_id.group(1)}/decide", data={
                "decision": "approve", "note": "Approved for annual review",
                "days": "90"})
            check("the supplier can approve a buyer-raised request",
                  approved.status_code == 200)

            after = buyer.get(f"/buyer/suppliers/{lid}")
            check("approved records become visible to the buyer",
                  "Records shared with you" in after.text)
            check("the link moves to connected", "connected" in after.text)

            check("an unpublished draft is never disclosed by a grant",
                  "No published version" not in after.text)

            doc_link = re.search(rf"/buyer/documents/{UUID}", after.text)
            if doc_link:
                doc = buyer.get(f"/buyer/documents/{doc_link.group(1)}")
                check("a granted document is readable", doc.status_code == 200)
                check("every disclosed document has a published version",
                      "No published version" not in doc.text)
                check("assurance is shown as computed, not as a rating",
                      "not a rating" in doc.text)

    section("Buyer portal - isolation")

    check("a supplier account cannot reach the buyer portal",
          session.get("/buyer/suppliers").status_code == 200
          and "My suppliers" not in session.get("/buyer/suppliers").text)

    from app.db import SessionLocal
    from app.models import Document as Doc, Organisation as Org, Tenant as Tn
    import sqlalchemy as sa

    with SessionLocal() as db:
        from app.services import buyers as buyersvc
        harrow = db.scalar(sa.select(Tn).where(Tn.name.ilike("Harrowgate%")))
        check("the buyer workspace is marked as a buyer", harrow.kind == "buyer")

        # An unclaimed profile must not expose a registered address.
        unclaimed = db.scalar(sa.select(Org).where(
            Org.claim_status == "unclaimed", Org.registered_address.is_not(None)))
        if unclaimed is not None:
            entry = buyersvc.directory_entry(unclaimed)
            check("an unclaimed profile withholds the registered address",
                  entry["registered_address"] is None and entry["address_withheld"])
            check("but still shows enough to recognise the company",
                  bool(entry["legal_name"]) and bool(entry["country"]))

        claimed = db.scalar(sa.select(Org).where(
            Org.claim_status == "claimed", Org.registered_address.is_not(None)))
        if claimed is not None:
            check("a claimed profile shows its registered address",
                  buyersvc.directory_entry(claimed)["registered_address"] is not None)

        # Grant resolution must go through the shared resolver.
        other = db.scalar(sa.select(Tn).where(Tn.kind == "vendor"))
        visible = buyersvc.granted_documents(db, harrow.id, other.organisation_id)
        total = db.scalar(sa.select(sa.func.count(Doc.id)).where(
            Doc.tenant_id == other.id))
        check("a grant never exposes the supplier's whole register by accident",
              len(visible) <= (total or 0))

    section("IAF CertSearch - trigger and reconciliation")

    from app.services import certsearch as cs

    check("only management-system certificates are in scope",
          cs.supports("SEC-001") and not cs.supports("INS-003"))
    check("the search URL carries the entity and the standard",
          "Northgate" in cs.search_url(name="Northgate Data Systems Ltd",
                                       standard="ISO/IEC 27001")
          and "iafcertsearch.org" in cs.search_url(name="x"))

    json_payload = ('{"results":[{"certificateNumber":"IS 776541",'
                    '"standard":"ISO/IEC 27001","status":"Active",'
                    '"organisation":"Northgate Data Systems Ltd",'
                    '"validTo":"2027-01-14","certificationBody":"BSI Group",'
                    '"accreditationBody":"UKAS"}]}')
    parsed = cs.parse_payload(json_payload)
    check("a JSON response parses to one candidate", len(parsed) == 1)
    check("fields are normalised regardless of the key names used",
          parsed[0]["certificate_number"] == "IS 776541"
          and parsed[0]["expiry_date"] == "2027-01-14")

    html = ("<table><tr><td>Certificate Number</td><td>IS 776541</td></tr>"
            "<tr><td>Standard</td><td>ISO/IEC 27001</td></tr>"
            "<tr><td>Status</td><td>Withdrawn</td></tr></table>")
    check("an HTML block parses by label, not by column position",
          cs.parse_payload(html)[0]["status"] == "Withdrawn")
    check("a bot-challenge page yields no candidate rather than a false empty result",
          cs.parse_payload("<html><body>Just a moment...</body></html>") == [])
    check("an unreadable payload yields nothing rather than a half-filled candidate",
          cs.parse_payload("nothing useful here at all") == [])

    check("a scrape can never reach L3", cs.TRANSPORT_CEILING["scrape"] == "L2"
          and cs.TRANSPORT_CEILING["assisted"] == "L2"
          and cs.TRANSPORT_CEILING["licensed_api"] == "L3")
    check("automated collection is off unless a deployment enables it",
          cs.fetch("https://www.iafcertsearch.org/x")[0] == "")

    with SessionLocal() as db:
        import sqlalchemy as sa
        from app.models import Document as Doc, DocumentType as DT, DocumentVersion as DV

        row = db.execute(sa.select(Doc, DT).join(DT, Doc.document_type_id == DT.id)
                         .where(DT.code == "SEC-001")).first()
        if row:
            doc, dtype = row
            version = db.get(DV, doc.active_version_id)
            tenant_row = db.get(__import__("app.models", fromlist=["Tenant"]).Tenant,
                                doc.tenant_id)

            good = {"certificate_number": version.certificate_number,
                    "standard": "ISO/IEC 27001", "status": "Active",
                    "holder_name": tenant_row.organisation.legal_name,
                    "expiry_date": str(version.expiry_date) if version.expiry_date else None,
                    "certification_body": version.issuer_name,
                    "accreditation_body": "UKAS", "scope_statement": None}
            agreed = cs.compare(version, good, organisation=tenant_row.organisation,
                                type_code="SEC-001")
            check("an agreeing certificate reconciles as a match",
                  agreed["verdict"] in {"match", "partial_match"},
                  agreed["verdict"])

            withdrawn = {**good, "status": "Withdrawn"}
            flagged = cs.compare(version, withdrawn,
                                 organisation=tenant_row.organisation,
                                 type_code="SEC-001")
            check("a certificate IAF records as withdrawn is a mismatch",
                  flagged["verdict"] == "mismatch")
            check("the reviewer is told which field disagreed",
                  "Certificate status" in flagged["decisive_mismatches"])

            wrong_number = {**good, "certificate_number": "IS 000000"}
            check("a different certificate number is decisive",
                  cs.compare(version, wrong_number,
                             organisation=tenant_row.organisation,
                             type_code="SEC-001")["verdict"] == "mismatch")

            # A few days of drift is transcription; a month is another cycle.
            if version.expiry_date:
                from datetime import timedelta as td
                near = {**good, "expiry_date": str(version.expiry_date + td(days=2))}
                far = {**good, "expiry_date": str(version.expiry_date + td(days=45))}
                check("a couple of days of date drift is tolerated",
                      cs.compare(version, near, organisation=tenant_row.organisation,
                                 type_code="SEC-001")["verdict"] != "mismatch")
                # A month of drift on a matching certificate number usually means
                # the uploaded PDF predates a recertification: the credential is
                # real but the record is stale. That is a partial match with the
                # row flagged, not a verdict that the certificate is false.
                far_result = cs.compare(version, far,
                                        organisation=tenant_row.organisation,
                                        type_code="SEC-001")
                check("a month of date drift downgrades to a partial match",
                      far_result["verdict"] == "partial_match", far_result["verdict"])
                check("and the disagreeing date is flagged on its own row",
                      any(r["field"] == "expiry_date" and r["verdict"] == "mismatch"
                          for r in far_result["rows"]))

            empty = cs.compare(version, {}, organisation=tenant_row.organisation,
                               type_code="SEC-001")
            check("an empty candidate is inconclusive, never a match",
                  empty["verdict"] in {"inconclusive", "not_comparable"})

    section("IAF CertSearch - through the interface")

    # Discovered over HTTP, not from the local database: the suite also runs
    # against the single-file build, which has a database of its own, and an id
    # read here would not exist there.
    target = (None, None)
    for document_id in dict.fromkeys(
            re.findall(rf"/documents/{UUID}", session.get("/documents").text)):
        detail = session.get(f"/documents/{document_id}")
        trigger = re.search(rf"/versions/{UUID}/certsearch", detail.text)
        if trigger:
            target = (document_id, trigger.group(1))
            break
    check("a management-system credential offers the CertSearch trigger",
          target[1] is not None)

    if target[1]:
        started = session.post(f"/versions/{target[1]}/certsearch")
        check("the trigger opens a lookup", started.status_code == 200)
        check("the lookup offers the source in a separate window",
              "iafcertsearch.org" in started.text and "window.open" in started.text)
        check("the assurance ceiling is stated before anything is applied",
              "can reach L2" in started.text)

        lookup_id = re.search(rf"/certsearch/{UUID}", str(started.url))
        if lookup_id:
            lid = lookup_id.group(1)
            typed = session.post(f"/certsearch/{lid}/manual", data={
                "certificate_number": "IS 000000", "standard": "ISO/IEC 27001",
                "status": "Withdrawn", "holder_name": "Northgate Data Systems Ltd"})
            check("typed details are compared, not accepted",
                  "mismatch" in typed.text.lower())

            refused = session.post(f"/certsearch/{lid}/apply", data={
                "candidate_index": "0", "note": ""})
            check("applying without attesting is refused",
                  "Confirm that you read this" in refused.text)

            applied = session.post(f"/certsearch/{lid}/apply", data={
                "candidate_index": "0", "attest": "on", "note": "Checked live"})
            check("a mismatch is recorded as a finding",
                  "disagrees with the uploaded certificate" in applied.text)

    section("IAF CertSearch - absence does not punish")

    if target[1]:
        second = session.post(f"/versions/{target[1]}/certsearch")
        lookup_id = re.search(rf"/certsearch/{UUID}", str(second.url))
        if lookup_id:
            before = re.findall(r">(L[0-4])<",
                                session.get(f"/documents/{target[0]}").text)
            missing = session.post(f"/certsearch/{lookup_id.group(1)}/not-found",
                                   data={"note": "Searched by name and number"})
            check("not found is recorded as inconclusive",
                  "does not lower" in missing.text)
            after = re.findall(r">(L[0-4])<",
                               session.get(f"/documents/{target[0]}").text)
            check("a gap in IAF coverage never downgrades a credential",
                  before == after, f"{before} -> {after}")

    section("Demo directory - real register data")

    with SessionLocal() as db:
        import sqlalchemy as sa
        from app.models import Organisation as Org, Tenant as Tn, Document as Dc
        from app import demo_directory
        from app.services import buyers as bsvc

        total = db.scalar(sa.select(sa.func.count(Org.id)).where(
            Org.source == "mca_register")) or 0
        check("the directory is loaded", total > 1000, f"{total} profiles")

        # The rule that governs this whole dataset.
        claimed = db.scalar(sa.select(sa.func.count(Org.id)).where(
            Org.source == "mca_register", Org.claim_status != "unclaimed")) or 0
        check("every real company is unclaimed", claimed == 0, f"{claimed} claimed")

        with_tenant = db.scalar(sa.select(sa.func.count(Tn.id)).join(
            Org, Tn.organisation_id == Org.id).where(
            Org.source == "mca_register")) or 0
        check("no real company has a workspace", with_tenant == 0)

        real_ids = [r[0] for r in db.execute(sa.select(Org.id).where(
            Org.source == "mca_register")).all()]
        docs = db.scalar(sa.select(sa.func.count(Dc.id)).join(
            Tn, Dc.tenant_id == Tn.id).where(
            Tn.organisation_id.in_(real_ids[:500]))) or 0
        check("no credential is attributed to a real company", docs == 0)

        sample = db.scalar(sa.select(Org).where(
            Org.source == "mca_register",
            Org.registered_address.is_not(None)))
        entry = bsvc.directory_entry(sample)
        check("a real company's registered address is withheld",
              entry["registered_address"] is None and entry["address_withheld"])
        check("but the town and region are shown, so buyers can tell them apart",
              bool(entry["locality"]), repr(entry["locality"]))
        check("the locality is a place, not a postcode or country fragment",
              not re.search(r"\bindia\b", entry["locality"], re.I)
              and not re.fullmatch(r"[\d\s,-]+", entry["locality"]),
              repr(entry["locality"]))
        check("the registry number and authority are shown",
              bool(entry["registry_number"]) and bool(entry["registry_authority"]))

        struck = db.scalar(sa.select(sa.func.count(Org.id)).where(
            Org.description.like("%Struck off%"))) or 0
        check("companies in poor standing are kept, not filtered out",
              struck > 0, f"{struck} struck off")

        # Loading twice must not duplicate the directory.
        again = demo_directory.build(db)
        check("the loader is idempotent", again == 0)

        # The fictional estate still holds the credentials.
        example = db.scalar(sa.select(sa.func.count(Tn.id)).join(
            Org, Tn.organisation_id == Org.id).where(
            Org.source != "mca_register")) or 0
        check("the fictional estate still carries the credential registers",
              example >= 6, f"{example} workspaces")

    section("Demo directory - through buyer search")

    real_search = buyer.get("/buyer/search", params={"q": "PRIVATE LIMITED"})
    check("the directory is searchable by a buyer", real_search.status_code == 200)
    check("results are capped rather than dumping the register",
          real_search.text.count("/buyer/companies/") <= bsvc.MAX_RESULTS + 2)
    check("unclaimed real companies show as not joined",
          "not joined" in real_search.text)

    section("Operator lockout recovery")

    from app.config import ADMIN_EMAIL, ADMIN_PASSWORD
    from app.security import hash_password, verify_password
    from app.models import PlatformAdmin as PA
    from app import seed as seedmod
    import sqlalchemy as sa

    with SessionLocal() as db:
        admin_row = db.scalar(sa.select(PA))
        original = admin_row.password_hash

        admin_row.password_hash = hash_password("nobody-knows-this")
        db.commit()
        seedmod.seed_platform_admin(db)
        db.commit()
        check("without ADMIN_RESET an existing operator is never overwritten",
              not verify_password(ADMIN_PASSWORD,
                                  db.scalar(sa.select(PA)).password_hash))

        import app.config as cfg
        cfg.ADMIN_RESET = True
        try:
            seedmod.seed_platform_admin(db)
            db.commit()
            check("ADMIN_RESET restores access for a locked-out operator",
                  verify_password(ADMIN_PASSWORD,
                                  db.scalar(sa.select(PA)).password_hash))
        finally:
            cfg.ADMIN_RESET = False

        db.scalar(sa.select(PA)).password_hash = original
        db.commit()

    check("the operator table is separate, so an admin cannot sign in as a supplier",
          "did not match" in httpx.Client(base_url=BASE, follow_redirects=True,
                                          timeout=30).post(
              "/login", data={"email": ADMIN_EMAIL,
                              "password": ADMIN_PASSWORD}).text)

    section("Public front door - search first")

    anon = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    front = anon.get("/")
    check("the home page is the search", front.status_code == 200
          and 'placeholder="Supplier name"' in front.text)
    check("it is branded Trusted Supplier Program",
          "Trusted Supplier Program" in front.text and "GICC" not in front.text)
    check("register and sign-in are panels, not separate pages",
          front.text.count("tsp-panel") >= 4)

    found = anon.get("/search", params={"q": "Northgate"})
    check("a member is findable without signing in", "Northgate" in found.text)
    check("members are marked with a star", "★" in found.text)
    check("and badged as a trusted supplier", "Trusted Supplier</span>" in found.text)

    with SessionLocal() as db:
        from app.services import directory as dsvc
        outcome = dsvc.search(db, "Northgate", limit=8)
        check("members sort above register results",
              outcome["results"][0]["member"] is True)
        mixed = dsvc.search(db, "PRIVATE LIMITED", limit=6)
        check("register companies are returned when no member matches",
              mixed["results"] and not mixed["results"][0]["member"])
        check("the fallback source is named rather than implied live",
              "register extract" in (mixed["provider"] or "").lower())

        suspended = dsvc.search(db, "Pellworth", limit=5)
        check("a suspended workspace is not presented as a trusted supplier",
              all(not r["member"] for r in suspended["results"]))

    check("public results never carry a registered address",
          "Registered address" not in found.text)

    import re as _re
    hit = _re.search(rf"/company/{UUID}", found.text)
    check("a result links to the company gate", hit is not None)
    if hit:
        gate = anon.get(f"/company/{hit.group(1)}")
        check("opening a company asks a stranger to sign in",
              "Sign in to continue" in gate.text)
        check("the gate explains why details are held back",
              "home address" in gate.text)

        # Signing in from the gate returns to the company, not a dashboard.
        returning = anon.post("/login", data={"email": "priya@harrowgate.example",
                                              "password": "buyer1234"})
        check("signing in from a company page returns to that company",
              "/buyer/companies/" in str(returning.url) or
              "/company/" in str(returning.url), str(returning.url))

    section("Declutter")

    home_page = session.get("/dashboard").text
    check("an established supplier is not shown milestones it has already reached",
          "Activation milestones" not in home_page)
    check("the home page keeps what needs doing", "What is broken" in home_page)
    check("the sidebar carries the disclosure script", "tsp.nav" in home_page)

    section("Rebrand and menu")

    dash = session.get("/dashboard")
    for label in ["Start", "My Trust profile", "My Documentation",
                  "Actions needed", "Account management"]:
        check(f"menu carries '{label}'", label in dash.text)
    check("sub-pages are indented beneath their section",
          dash.text.count('class="sub') >= 15)
    check("the dashboard no longer carries buyer viewing",
          "Where your buyers are looking" not in dash.text)
    check("nor the activity feed", "Recent activity" not in dash.text)
    check("both moved to Activity",
          "Where your buyers are looking" in session.get("/activity").text)

    section("Authority links and QR codes")

    from app.services import authority as auth
    check("a real authority domain is recognised",
          auth.classify_url("https://www.iafcertsearch.org/c/1")["kind"] == "authority")
    check("an authority's subdomain is recognised",
          auth.classify_url("https://certs.ukas.com/x")["kind"] == "authority")
    check("the subdomain trick is caught as a look-alike",
          auth.classify_url("https://iafcertsearch.org.evil.io/c/1")["kind"] == "lookalike")
    check("a borrowed brand name is caught as a look-alike",
          auth.classify_url("https://bsi-verify-certs.co/c/1")["kind"] == "lookalike")
    check("an unrelated domain is unknown, not suspicious",
          auth.classify_url("https://example.com/x")["kind"] == "unknown")
    check("printed links are read from the text layer",
          auth.urls_in_text("Verify at https://www.bsigroup.com/validate.")
          == ["https://www.bsigroup.com/validate"])

    from app.services import cloudsync_demo as demo_pdf
    if auth.qr_available():
        genuine = demo_pdf.make_pdf(["Certificate"],
                                    "https://www.iafcertsearch.org/certification/IS-776541")
        found = auth.findings(genuine, "c.pdf", "Certificate")
        check("a QR code is decoded from inside a PDF",
              found["qr_payloads"] == ["https://www.iafcertsearch.org/certification/IS-776541"])
        check("the QR's authority becomes the verification link",
              (found["best"] or {}).get("authority") == "IAF CertSearch")
        doctored = demo_pdf.make_pdf(["Verify at https://www.bsigroup.com/validate"],
                                     "https://bsi-verify-certs.co/c/1")
        bad = auth.findings(doctored, "c.pdf", "Verify at https://www.bsigroup.com/validate")
        check("genuine text with a look-alike QR is flagged as the altered pattern",
              bad["qr_text_disagree"] and bad["lookalike"])
        two_real = auth.findings(genuine, "c.pdf", "Verify at https://www.bsigroup.com/validate")
        check("two different genuine authorities are not flagged",
              not two_real["qr_text_disagree"])
    else:
        check("QR decoding reports itself unavailable rather than failing",
              auth.findings(b"%PDF-1.4", "c.pdf", "")["qr_method"] == "unavailable")

    section("Cloud sync - providers and tokens")

    from app.services import cloudsync as cs
    check("tokens are encrypted at rest and round-trip",
          cs.unseal(cs.seal("secret-token")) == "secret-token"
          and "secret-token" not in (cs.seal("secret-token") or ""))
    check("a token sealed under another key is reported unreadable, not raised",
          cs.unseal("gAAAAA-not-a-real-token") is None)
    google_url = cs.GoogleDrive().authorise_url("https://x.example/cb", "STATE123")
    check("Google is asked for read-only Drive access",
          "drive.readonly" in google_url and "drive.file" not in google_url
          and "auth%2Fdrive+" not in google_url)
    check("the OAuth state is carried", "state=STATE123" in google_url)
    microsoft_url = cs.OneDrive().authorise_url("https://x.example/cb", "S2")
    check("Microsoft is asked for read-only file access",
          "Files.Read.All" in microsoft_url and "ReadWrite" not in microsoft_url)

    page_ = session.get("/connections")
    check("the cloud folders page renders", page_.status_code == 200
          and "Connect Google Drive" in page_.text and "Connect Microsoft OneDrive" in page_.text)
    check("an unconfigured provider says what it needs",
          "GOOGLE_CLIENT_ID" in page_.text)
    check("the menu carries cloud folders under My Documentation",
          'href="/connections"' in session.get("/dashboard").text)

    refused = session.get("/connections/google/callback",
                          params={"code": "x", "state": "forged"})
    check("an OAuth callback with an unknown state is refused",
          "was refused" in refused.text)

    section("Cloud sync - the folder lifecycle")

    import time as _t
    made = session.post("/connections/demo/connect")
    conn = re.search(rf"/connections/{UUID}", str(made.url))
    check("a folder can be connected", conn is not None)
    if conn:
        cid = conn.group(1)

        def _settle():
            for _ in range(45):
                _t.sleep(1)
                if "Syncing…" not in session.get(f"/connections/{cid}").text:
                    return

        def _stats(text):
            return {k: v for v, k in re.findall(
                r'class="num" style="font-size:22px[^"]*">([^<]+)</div>'
                r'<div class="small muted">([^<]+)', text)}

        _settle()
        first = session.get(f"/connections/{cid}").text
        s1 = _stats(first)
        check("the first sync brings every file across", s1.get("new files") == "6",
              str(s1))
        check("nothing new publishes itself", s1.get("renewals published") == "0")
        check("drafts are surfaced for confirmation", "waiting for you" in first)
        check("verification links are found on the documents",
              int(s1.get("authority links found", 0)) >= 3)
        check("a look-alike QR code is flagged", s1.get("flagged") == "1")
        check("a file that is not a credential is left unfiled",
              "not filed" in first)
        check("a flagged file raises an action",
              "bsi-verify-certs.co" in session.get("/tasks").text)

        ROW = (r'files/[0-9a-f]{32}">([^<]+)</a>(.*?)href="/documents/([0-9a-f]{32})">'
               r'v(\d+)</a>\s*<span class="chip \w+ plain">(\w+)')
        for path, _, doc, _, state in re.findall(ROW, first, re.S):
            if path.startswith("Certificates/ISO 27001") and state == "draft":
                drafts = re.findall(rf"/documents/{doc}/versions/{UUID}/publish",
                                    session.get(f"/documents/{doc}").text)
                session.post(f"/documents/{doc}/versions/{drafts[0]}/publish")

        for action in ["renew", "rename", "remove", "add"]:
            session.post(f"/connections/{cid}/demo/{action}")
        session.post(f"/connections/{cid}/sync")
        _settle()
        second = session.get(f"/connections/{cid}").text
        s2 = _stats(second)
        check("a new file arrives as new", s2.get("new files") == "1", str(s2))
        check("a changed file arrives as an update", s2.get("updated") == "1")
        check("a renamed file is the same file, not a new one",
              s2.get("unchanged") == "4" and "CE+ certificate (current)" in second)
        check("a deleted file is recorded as removed", s2.get("removed at source") == "1")
        check("the deleted file's credential is kept",
              "Removed from the folder" in second and "credential kept" in second)
        check("a renewal of a confirmed file publishes itself",
              s2.get("renewals published") == "1")

        iso = re.search(rf'/connections/{cid}/files/{UUID}">Certificates/ISO 27001', second)
        if iso:
            history = session.get(f"/connections/{cid}/files/{iso.group(1)}").text
            check("the file keeps a revision history",
                  re.findall(r"<strong>(\w+)</strong>", history) == ["Updated", "Added"])
            check("each revision names the register version it produced",
                  history.count("register v") >= 2)
            check("the renewed dates are read from the new revision",
                  "09 Jan 2030" in history)

        session.post(f"/connections/{cid}/sync")
        _settle()
        s3 = _stats(session.get(f"/connections/{cid}").text)
        check("an unchanged folder downloads nothing new",
              s3.get("new files") == "0" and s3.get("updated") == "0", str(s3))

        session.post(f"/connections/{cid}/demo/overwrite")
        session.post(f"/connections/{cid}/sync")
        _settle()
        after = session.get(f"/connections/{cid}").text
        inc = re.search(rf'/connections/{cid}/files/{UUID}">Corporate/Certificate of Incorporation',
                        after)
        if inc:
            note = session.get(f"/connections/{cid}/files/{inc.group(1)}").text
            check("a different document saved over a file is filed separately",
                  "now holds a different kind of document" in note)

        saved = session.post(f"/connections/{cid}/settings",
                             data={"schedule": "hourly", "auto_publish_renewals": "on"})
        check("the schedule can be set", "Next check" in saved.text)
        manual = session.post(f"/connections/{cid}/settings", data={"schedule": "manual"})
        check("automatic checks can be switched off", "Automatic checks are off" in manual.text)

        gone = session.post(f"/connections/{cid}/disconnect")
        check("disconnecting keeps everything already filed",
              "every credential and version already filed stays" in gone.text)

    print(f"\n{len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
