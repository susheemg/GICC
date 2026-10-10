"""Renders the specification's figures as SVG.

Mermaid CLI needs a headless Chrome that is not available here, so the diagrams
are drawn directly. That also means they use the document palette rather than
Mermaid's defaults.
"""
from __future__ import annotations

import html
from pathlib import Path

OUT = Path("/tmp/figs")
OUT.mkdir(exist_ok=True)

NAVY = "#1F3A70"
STEEL = "#3E6FB0"
SOFT = "#E8EEF7"
LINE = "#C8D6E9"
INK = "#1A1D23"
MUTED = "#6B7280"
OK = "#1B7F5A"
WARN = "#B8860B"
BAD = "#B3261E"
FONT = "Carlito, Calibri, Helvetica, Arial, sans-serif"


def esc(text: str) -> str:
    return html.escape(str(text))


def wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


class Canvas:
    def __init__(self, width: int, height: int, title: str = ""):
        self.w, self.h, self.title = width, height, title
        self.parts: list[str] = []

    def rect(self, x, y, w, h, *, fill="#FFFFFF", stroke=LINE, rx=2, sw=1, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def text(self, x, y, content, *, size=11, fill=INK, weight="normal",
             anchor="middle", family=FONT):
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}" text-anchor="{anchor}">'
            f'{esc(content)}</text>')

    def box(self, x, y, w, h, label, *, fill="#FFFFFF", stroke=LINE, sw=1,
            size=11, weight="600", text_fill=INK, chars=None, sub=None):
        self.rect(x, y, w, h, fill=fill, stroke=stroke, sw=sw)
        chars = chars or max(8, int(w / (size * 0.52)))
        lines = wrap(label, chars)
        if sub:
            lines = lines + [sub]
        total = len(lines) * (size + 3)
        start = y + h / 2 - total / 2 + size
        for index, line in enumerate(lines):
            is_sub = sub is not None and index == len(lines) - 1
            self.text(x + w / 2, start + index * (size + 3), line,
                      size=size - 2 if is_sub else size,
                      fill=MUTED if is_sub else text_fill,
                      weight="normal" if is_sub else weight)

    def diamond(self, cx, cy, w, h, label, *, fill="#FFF9E8", stroke=WARN, size=10):
        pts = f"{cx},{cy-h/2} {cx+w/2},{cy} {cx},{cy+h/2} {cx-w/2},{cy}"
        self.parts.append(f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" '
                          f'stroke-width="1"/>')
        lines = wrap(label, int(w / (size * 0.6)))
        start = cy - len(lines) * (size + 2) / 2 + size
        for index, line in enumerate(lines):
            self.text(cx, start + index * (size + 2), line, size=size, weight="600")

    def arrow(self, x1, y1, x2, y2, *, label=None, colour=STEEL, dash=None,
              label_offset=-6, label_side="middle", size=9):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour}" '
            f'stroke-width="1.4" marker-end="url(#arrow)"{d}/>')
        if label:
            self.text((x1 + x2) / 2, (y1 + y2) / 2 + label_offset, label,
                      size=size, fill=MUTED, anchor=label_side)

    def elbow(self, x1, y1, x2, y2, *, via="h", label=None, colour=STEEL, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        if via == "h":
            path = f"M {x1} {y1} L {(x1+x2)/2} {y1} L {(x1+x2)/2} {y2} L {x2} {y2}"
        else:
            path = f"M {x1} {y1} L {x1} {(y1+y2)/2} L {x2} {(y1+y2)/2} L {x2} {y2}"
        self.parts.append(f'<path d="{path}" fill="none" stroke="{colour}" '
                          f'stroke-width="1.4" marker-end="url(#arrow)"{d}/>')
        if label:
            self.text((x1 + x2) / 2, (y1 + y2) / 2 - 5, label, size=9, fill=MUTED)

    def route(self, points, *, label=None, colour=STEEL, dash=None):
        path = "M " + " L ".join(f"{x} {y}" for x, y in points)
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<path d="{path}" fill="none" stroke="{colour}" '
                          f'stroke-width="1.4" marker-end="url(#arrow)"{d}/>')
        if label:
            mid = points[len(points) // 2]
            self.text(mid[0], mid[1] - 5, label, size=9, fill=MUTED)

    def save(self, name: str) -> Path:
        header = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
            f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">'
            f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{STEEL}"/></marker></defs>'
            f'<rect width="{self.w}" height="{self.h}" fill="#FFFFFF"/>')
        path = OUT / name
        path.write_text(header + "".join(self.parts) + "</svg>")
        return path


# --------------------------------------------------------------------------
# Figure 1 — entity relationship clusters
# --------------------------------------------------------------------------
def fig_erd():
    c = Canvas(1160, 700)
    clusters = [
        (20, 20, 260, 150, "Tenancy & identity", NAVY,
         ["organisation", "tenant", "user_account", "entity_identifier",
          "platform_admin", "break_glass_session"]),
        (300, 20, 260, 150, "Catalogue & documents", NAVY,
         ["document_type", "document", "document_version",
          "verification_check", "authenticity_signal"]),
        (580, 20, 260, 150, "Disclosure", NAVY,
         ["certificate_group", "group_item", "client_account", "client_contact",
          "access_grant", "share_link", "share_link_view", "access_request"]),
        (860, 20, 280, 150, "Conformance & trust", NAVY,
         ["requirement_profile", "requirement_item", "assessment",
          "assessment_result", "trust_score", "trust_domain_score"]),
        (20, 200, 260, 130, "Questionnaires", STEEL,
         ["questionnaire", "questionnaire_item", "answer_bank"]),
        (300, 200, 260, 130, "AI layer", STEEL,
         ["ai_provider_config", "ai_inference", "control_summary"]),
        (580, 200, 260, 130, "Machine access", STEEL,
         ["api_client", "agent_call_log"]),
        (860, 200, 280, 130, "Operations & audit", STEEL,
         ["task", "notification", "notification_preference", "audit_event"]),
    ]
    for x, y, w, h, title, colour, tables in clusters:
        c.rect(x, y, w, h, fill="#FFFFFF", stroke=colour, sw=1.5)
        c.rect(x, y, w, 22, fill=colour, stroke=colour)
        c.text(x + w / 2, y + 15, title, size=11, fill="#FFFFFF", weight="600")
        for index, table in enumerate(tables):
            c.text(x + 10, y + 38 + index * 15, table, size=9.5, anchor="start",
                   family="SF Mono, Menlo, Consolas, monospace")

    c.text(580, 380, "Every tenant-scoped table carries tenant_id. 37 tables in total.",
           size=11, fill=MUTED)

    # key relationships
    rel_y = 415
    c.rect(20, rel_y, 1120, 255, fill="#FAFBFC", stroke=LINE)
    c.text(40, rel_y + 22, "Load-bearing relationships and constraints",
           size=11.5, weight="600", anchor="start", fill=NAVY)
    rows = [
        ("document → document_version", "one active version, enforced by a partial unique index"),
        ("access_grant → certificate_group", "grant terms copied from group policy at creation, then immutable"),
        ("group_item → document_type", "prefix rules: SEC- captures a domain, SEC-001 a single type"),
        ("assessment_result → document_version", "cited_version_id makes an evaluation reproducible"),
        ("trust_domain_score → document_version", "every point traces to the version that produced it"),
        ("audit_event", "per-tenant seq, hash chain over opaque identifiers, survives purge"),
        ("share_link → access_grant", "token hashed, never stored; revoking the grant kills the link"),
        ("api_client → access_grant", "a grant-bound key sees the narrower of scope and grant"),
    ]
    for index, (left, right) in enumerate(rows):
        y = rel_y + 48 + index * 25
        c.text(40, y, left, size=9.5, anchor="start", weight="600",
               family="SF Mono, Menlo, Consolas, monospace")
        c.text(400, y, right, size=10, anchor="start", fill=MUTED)
    return c.save("fig1-erd.svg")


# --------------------------------------------------------------------------
# Figure 2 — lifecycle map
# --------------------------------------------------------------------------
def fig_lifecycle():
    c = Canvas(1160, 330)
    stages = [
        ("S0", "Discover"), ("S1", "Register &\nidentity"), ("S2", "Configure"),
        ("S3", "Populate"), ("S4", "Verify"), ("S5", "Package"),
        ("S6", "Onboard &\ndisclose"),
    ]
    x, y, w, h, gap = 22, 40, 148, 62, 12
    positions = []
    for index, (code, name) in enumerate(stages):
        bx = x + index * (w + gap)
        positions.append(bx)
        c.rect(bx, y, w, h, fill=SOFT, stroke=STEEL)
        c.text(bx + w / 2, y + 22, code, size=10, fill=NAVY, weight="700")
        for line_index, line in enumerate(name.split("\n")):
            c.text(bx + w / 2, y + 40 + line_index * 13, line, size=10.5)
        if index:
            c.arrow(positions[index - 1] + w, y + h / 2, bx - 2, y + h / 2)

    y2 = y + 120
    second = [("S7", "Operate"), ("S8", "Respond\n& win"), ("S9", "Machine\nchannel"),
              ("S10", "Expand"), ("S11", "Decline"), ("S12", "Exit &\ndisposition"),
              ("S13", "Reactivate")]
    positions2 = []
    for index, (code, name) in enumerate(second):
        bx = x + index * (w + gap)
        positions2.append(bx)
        fill = "#FBE9E7" if code in {"S11", "S12"} else SOFT
        stroke = BAD if code in {"S11", "S12"} else STEEL
        c.rect(bx, y2, w, h, fill=fill, stroke=stroke)
        c.text(bx + w / 2, y2 + 22, code, size=10, fill=NAVY, weight="700")
        for line_index, line in enumerate(name.split("\n")):
            c.text(bx + w / 2, y2 + 40 + line_index * 13, line, size=10.5)
        if index:
            c.arrow(positions2[index - 1] + w, y2 + h / 2, bx - 2, y2 + h / 2)

    c.route([(positions[6] + w / 2, y + h), (positions[6] + w / 2, y + h + 20),
             (positions2[0] + w / 2, y + h + 20), (positions2[0] + w / 2, y2 - 2)])
    c.route([(positions2[4] + w / 2, y2 + h), (positions2[4] + w / 2, y2 + h + 26),
             (positions2[0] + w / 2, y2 + h + 26), (positions2[0] + w / 2, y2 + h + 2)],
            dash="4 3", colour=OK)
    c.text(positions2[2] + w / 2, y2 + h + 40, "recovered before exit", size=9, fill=OK)

    c.text(580, 22, "Supplier lifecycle — 14 stages, 58 processes",
           size=12, weight="600", fill=NAVY)
    c.text(580, 315,
           "Milestones: M1 first L3 credential (24h) · M2 first client grant (7d) "
           "· M3 first client read (14d)", size=10, fill=MUTED)
    return c.save("fig2-lifecycle.svg")


# --------------------------------------------------------------------------
# Figure 3 — upload with classification
# --------------------------------------------------------------------------
def fig_upload():
    c = Canvas(1160, 700)
    col1, col2, col3 = 60, 460, 850
    w, h = 250, 48

    def node(x, y, label, *, fill="#FFFFFF", stroke=LINE, sub=None):
        c.box(x, y, w, h, label, fill=fill, stroke=stroke, size=10.5, sub=sub)
        return (x + w / 2, y, x + w / 2, y + h)

    c.text(580, 26, "Upload, classification and publication (P-12, P-13, P-15)",
           size=12, weight="600", fill=NAVY)

    steps1 = [
        (50, "Upload file", "#FFFFFF", LINE, None),
        (120, "Content-type and size check", "#FFFFFF", LINE, None),
        (190, "SHA-256, content-addressed store", "#FFFFFF", LINE, "identical bytes stored once"),
        (270, "Text extraction", SOFT, STEEL, "pypdf · OOXML · decode"),
    ]
    for y, label, fill, stroke, sub in steps1:
        node(col1, y, label, fill=fill, stroke=stroke, sub=sub)
    for a, b in zip(steps1, steps1[1:]):
        c.arrow(col1 + w / 2, a[0] + h, col1 + w / 2, b[0] - 2)

    c.diamond(col1 + w / 2, 370, 220, 60, "Provider configured?")
    c.arrow(col1 + w / 2, 270 + h, col1 + w / 2, 340)

    # Branches sit side by side so the join below is symmetrical.
    bw = 118
    node_left, node_right = col1 - 8, col1 + w - bw + 8
    c.box(node_left, 430, bw, h, "Model classify", fill="#EAF3EE", stroke=OK,
          size=10, sub="source = model")
    c.box(node_right, 430, bw, h, "Keyword and regex", fill="#FFFFFF", stroke=LINE,
          size=10, sub="source = heuristic")
    c.route([(col1 + w / 2 - 110, 370), (node_left + bw / 2, 370),
             (node_left + bw / 2, 428)], label="yes")
    c.route([(col1 + w / 2 + 110, 370), (node_right + bw / 2, 370),
             (node_right + bw / 2, 428)], label="no")

    node(col1, 540, "ai_inference row", fill=SOFT, stroke=STEEL,
         sub="source · model · confidence · hash")
    c.route([(node_left + bw / 2, 430 + h), (node_left + bw / 2, 515),
             (col1 + w / 2, 515), (col1 + w / 2, 538)])
    c.route([(node_right + bw / 2, 430 + h), (node_right + bw / 2, 515),
             (col1 + w / 2, 515), (col1 + w / 2, 538)])

    node(col1, 620, "Drop fields below the confidence floor",
         fill="#FBF2DE", stroke=WARN, sub="a blank beats a wrong pre-fill")
    c.arrow(col1 + w / 2, 540 + h, col1 + w / 2, 618)

    steps2 = [
        (50, "Show proposal with alternatives", None, "confidence and rationale shown"),
        (130, "New version, or a new document?", None, "never a silent duplicate"),
        (210, "Human confirms every field", "#EAF3EE", "nothing is fact until confirmed"),
        (290, "create_version", None, None),
        (360, "version_diff vs outgoing", None, None),
        (430, "Material change?", "#FBF2DE", "rule, never a model judgement"),
        (510, "publish_version", SOFT, "atomic supersession"),
    ]
    for y, label, fill, sub in steps2:
        node(col2, y, label, fill=fill or "#FFFFFF",
             stroke=OK if fill == "#EAF3EE" else (WARN if fill == "#FBF2DE" else
                                                  (STEEL if fill == SOFT else LINE)),
             sub=sub)
    for a, b in zip(steps2, steps2[1:]):
        c.arrow(col2 + w / 2, a[0] + h, col2 + w / 2, b[0] - 2)
    c.route([(col1 + w, 644), (col2 - 30, 644), (col2 - 30, 74), (col2 - 2, 74)])

    steps3 = [
        (50, "run_check", "#EAF3EE", OK, "assurance recomputed"),
        (130, "authenticity.evaluate", "#FBF2DE", WARN, "signals raised, never assurance"),
        (210, "notifications", "#FFFFFF", LINE, "uploaded · adverse · signal"),
        (290, "run_maintenance", "#FFFFFF", LINE, "status · readiness · tasks"),
        (360, "trust.compute", SOFT, STEEL, "algorithm-versioned"),
    ]
    for y, label, fill, stroke, sub in steps3:
        node(col3, y, label, fill=fill, stroke=stroke, sub=sub)
    for a, b in zip(steps3, steps3[1:]):
        c.arrow(col3 + w / 2, a[0] + h, col3 + w / 2, b[0] - 2)
    c.route([(col2 + w, 534), (col3 - 30, 534), (col3 - 30, 74), (col3 - 2, 74)])

    c.rect(850, 480, 250, 120, fill="#FAFBFC", stroke=LINE)
    c.text(975, 502, "Invariants on this path", size=10.5, weight="600", fill=NAVY)
    for index, line in enumerate([
            "C1  extraction proposes, a human confirms",
            "C2  exactly one active version",
            "C3  material change is a rule",
            "G1  the model never sets assurance",
            "G5  a signal never moves the ladder"]):
        c.text(864, 524 + index * 15, line, size=9, anchor="start", fill=MUTED)
    return c.save("fig3-upload.svg")


# --------------------------------------------------------------------------
# Figure 4 — verification pipeline (sequence)
# --------------------------------------------------------------------------
def fig_verification():
    c = Canvas(1160, 560)
    actors = ["Scheduler", "Worker", "Authoritative\nsource", "document_version", "Grantees"]
    xs = [110, 330, 560, 800, 1040]
    c.text(580, 26, "Verification pipeline and assurance computation (P-17, P-20)",
           size=12, weight="600", fill=NAVY)
    for x, name in zip(xs, actors):
        c.rect(x - 85, 45, 170, 40, fill=NAVY, stroke=NAVY)
        for index, line in enumerate(name.split("\n")):
            c.text(x, 62 + index * 13, line, size=10, fill="#FFFFFF", weight="600")
        c.parts.append(f'<line x1="{x}" y1="85" x2="{x}" y2="510" stroke="{LINE}" '
                       f'stroke-width="1" stroke-dasharray="4 4"/>')

    def msg(y, a, b, label, *, colour=STEEL, note=None):
        c.arrow(xs[a], y, xs[b] - (6 if b > a else -6), y, colour=colour)
        mid = (xs[a] + xs[b]) / 2
        c.text(mid, y - 7, label, size=9.5, fill=INK)
        if note:
            c.text(mid, y + 12, note, size=8.5, fill=MUTED)

    msg(115, 0, 1, "dequeue where next_check_due <= now")
    c.rect(250, 135, 160, 26, fill="#FAFBFC", stroke=LINE)
    c.text(330, 152, "check source_health", size=9.5)

    c.rect(60, 180, 1040, 80, fill="#FBF2DE", stroke=WARN, rx=2)
    c.text(80, 198, "alt  source unhealthy", size=9.5, weight="600", fill=WARN,
           anchor="start")
    msg(228, 1, 3, "result = error", note="assurance UNCHANGED — marked stale, not downgraded")

    c.rect(60, 272, 1040, 210, fill="#EAF3EE", stroke=OK, rx=2)
    c.text(80, 290, "alt  source healthy", size=9.5, weight="600", fill=OK, anchor="start")
    msg(318, 1, 2, "query(holder, number, scheme)")
    msg(352, 2, 1, "response")
    msg(388, 1, 3, "match → L3   ·   signature valid → L4", colour=OK)
    msg(424, 1, 3, "suspended / withdrawn / expired → L1", colour=BAD)
    msg(458, 1, 4, "notify every grantee within 24 hours", colour=BAD)

    c.rect(60, 495, 1040, 46, fill="#FAFBFC", stroke=LINE)
    c.text(80, 514, "A3  An inconclusive check never downgrades. Only a completed "
                    "negative result does — otherwise one connector outage",
           size=9.5, anchor="start", fill=INK)
    c.text(80, 530, "mass-downgrades every supplier on the platform and destroys "
                    "trust in the ladder permanently.",
           size=9.5, anchor="start", fill=INK)
    return c.save("fig4-verification.svg")


# --------------------------------------------------------------------------
# Figure 5 — version state machine
# --------------------------------------------------------------------------
def fig_states():
    c = Canvas(1160, 420)
    c.text(580, 26, "Document version state machine (F5)", size=12, weight="600", fill=NAVY)
    states = {
        "draft": (110, 160, "#FFFFFF", LINE),
        "scheduled": (350, 80, SOFT, STEEL),
        "active": (350, 240, "#EAF3EE", OK),
        "superseded": (660, 240, "#FAFBFC", MUTED),
        "withdrawn": (660, 80, "#FBE9E7", BAD),
    }
    w, h = 160, 52
    for name, (x, y, fill, stroke) in states.items():
        c.box(x, y, w, h, name, fill=fill, stroke=stroke, size=12)

    c.arrow(110 + w, 176, 350 - 2, 112, label=None)
    c.text(300, 128, "publish · future date", size=9, fill=MUTED)
    c.arrow(110 + w, 196, 350 - 2, 260, label=None)
    c.text(300, 240, "publish · today or past", size=9, fill=MUTED)
    c.arrow(350 + w / 2, 80 + h, 350 + w / 2, 240 - 2)
    c.text(438, 176, "sweep on the", size=9, fill=MUTED, anchor="start")
    c.text(438, 188, "effective date", size=9, fill=MUTED, anchor="start")
    c.arrow(350 + w, 266, 660 - 2, 266, label="a newer version activates")
    c.arrow(350 + w, 250, 660 - 2, 118, colour=BAD)
    c.text(600, 196, "withdraw", size=9, fill=BAD)

    c.route([(660 + w / 2, 240 + h), (660 + w / 2, 318), (430 + w / 2 - 40, 318),
             (430 + w / 2 - 40, 240 + h + 2)], colour=BAD, dash="5 4")
    c.text(700, 336, "rollback creates a NEW version", size=9, fill=BAD, anchor="start")

    c.rect(60, 352, 1040, 48, fill="#FAFBFC", stroke=LINE)
    c.text(76, 372, "C2  Exactly one active version, enforced by uq_version_active — "
                    "a unique index on document_id filtered to status = 'active'.",
           size=9.5, weight="600", anchor="start", fill=NAVY)
    c.text(76, 389, "Rollback is forward-only: restoring a superseded version creates a "
                    "new one reusing the old file, so history stays append-only.",
           size=9.5, anchor="start", fill=MUTED)
    return c.save("fig5-states.svg")


# --------------------------------------------------------------------------
# Figure 6 — buyer disclosure (sequence)
# --------------------------------------------------------------------------
def fig_disclosure():
    c = Canvas(1160, 600)
    actors = ["Vendor Sales", "GICC core", "Client Reviewer", "Audit log"]
    xs = [140, 430, 760, 1030]
    c.text(580, 26, "Client onboarding and disclosure (P-24 to P-30)",
           size=12, weight="600", fill=NAVY)
    for x, name in zip(xs, actors):
        c.rect(x - 95, 45, 190, 34, fill=NAVY, stroke=NAVY)
        c.text(x, 67, name, size=10.5, fill="#FFFFFF", weight="600")
        c.parts.append(f'<line x1="{x}" y1="79" x2="{x}" y2="530" stroke="{LINE}" '
                       f'stroke-width="1" stroke-dasharray="4 4"/>')

    def msg(y, a, b, label, *, colour=STEEL, note=None):
        c.arrow(xs[a], y, xs[b] - (6 if b > a else -6), y, colour=colour)
        mid = (xs[a] + xs[b]) / 2
        c.text(mid, y - 7, label, size=9.5)
        if note:
            c.text(mid, y + 12, note, size=8.5, fill=MUTED)

    msg(110, 0, 1, "register client and contact")
    msg(150, 0, 1, "create grant", note="scope · level · days · purpose")
    c.rect(345, 178, 170, 26, fill="#FBF2DE", stroke=WARN)
    c.text(430, 195, "apply policy ceiling", size=9.5, weight="600")
    msg(228, 1, 3, "grant.created", colour=MUTED)
    msg(266, 0, 1, "create share link")
    c.text(430, 292, "token shown once — only the hash is stored", size=8.5, fill=MUTED)
    msg(320, 2, 1, "open /s/{token}")
    msg(356, 1, 2, "identification gate", note="plus NDA text where the group requires it")
    msg(400, 2, 1, "email, name, acceptance")
    msg(434, 1, 3, "agreement.accepted | viewer.identified", colour=MUTED)
    msg(470, 2, 1, "open a document")
    c.rect(330, 486, 200, 28, fill="#EAF3EE", stroke=OK)
    c.text(430, 504, "re-resolve access at read time", size=9.5, weight="600")

    c.rect(600, 545, 500, 44, fill="#FAFBFC", stroke=LINE)
    c.text(616, 564, "D2  Access is resolved fresh on every read, by one resolver.",
           size=9.5, weight="600", anchor="start", fill=NAVY)
    c.text(616, 579, "Web, REST and MCP all call services/access.py. A second "
                     "implementation is a breach waiting to happen.",
           size=9, anchor="start", fill=MUTED)
    return c.save("fig6-disclosure.svg")


# --------------------------------------------------------------------------
# Figure 7 — AI proposal layer
# --------------------------------------------------------------------------
def fig_ai():
    c = Canvas(1160, 430)
    c.text(580, 26, "The AI proposal layer — the model proposes, the rules decide (G1)",
           size=12, weight="600", fill=NAVY)
    w, h = 170, 54
    c.box(40, 90, w, h, "Upload", size=11)
    c.box(240, 90, w, h, "Text extraction", sub="pypdf · OOXML", fill=SOFT, stroke=STEEL)
    c.diamond(530, 117, 190, 62, "Provider configured?")
    c.box(680, 40, w, h, "Model call", sub="source = model", fill="#EAF3EE", stroke=OK)
    c.box(680, 140, w, h, "Heuristic", sub="source = heuristic", fill="#FFFFFF")
    c.box(910, 90, w, h, "ai_inference", sub="provenance recorded", fill=SOFT, stroke=STEEL)

    c.arrow(210, 117, 238, 117)
    c.arrow(410, 117, 436, 117)
    c.route([(625, 117), (655, 117), (655, 67), (678, 67)], label="yes")
    c.route([(625, 117), (655, 117), (655, 167), (678, 167)], label="no")
    c.arrow(850, 67, 908, 100)
    c.arrow(850, 167, 908, 134)

    c.box(240, 250, w, h, "Human confirmation", fill="#EAF3EE", stroke=OK, size=11)
    c.box(470, 250, w, h, "document_version", sub="fact", fill=NAVY, stroke=NAVY,
          text_fill="#FFFFFF")
    c.box(700, 250, w, h, "Deterministic verification", fill="#FFFFFF")
    c.box(930, 250, w, h, "Assurance ladder", fill=SOFT, stroke=STEEL, size=11)

    c.route([(995, 144), (995, 200), (325, 200), (325, 248)])
    c.arrow(410, 277, 468, 277)
    c.arrow(640, 277, 698, 277)
    c.arrow(870, 277, 928, 277)

    c.box(700, 350, w, 46, "Authenticity signals", fill="#FBF2DE", stroke=WARN, size=10.5)
    c.route([(995, 144), (1120, 144), (1120, 373), (872, 373)], dash="5 4", colour=WARN,
            label=None)
    c.text(1050, 340, "signals only", size=9, fill=WARN)
    c.parts.append(f'<line x1="870" y1="350" x2="1010" y2="306" stroke="{BAD}" '
                   f'stroke-width="1.4" stroke-dasharray="5 4"/>')
    c.parts.append(f'<line x1="928" y1="316" x2="952" y2="340" stroke="{BAD}" '
                   f'stroke-width="2"/>')
    c.parts.append(f'<line x1="952" y1="316" x2="928" y2="340" stroke="{BAD}" '
                   f'stroke-width="2"/>')
    c.text(760, 410, "A signal informs a human. It never feeds the ladder.",
           size=10, fill=BAD, anchor="start")

    c.rect(40, 340, 620, 74, fill="#FAFBFC", stroke=LINE)
    c.text(56, 360, "No model output may raise an assurance level, alter a Trust Index, "
                    "or answer a buyer without a citation.",
           size=9.5, anchor="start", fill=INK)
    c.text(56, 378, "Every feature has a deterministic fallback: the product runs with no "
                    "provider configured, with identical",
           size=9.5, anchor="start", fill=MUTED)
    c.text(56, 393, "functionality minus fluency. An AI outage must never block an upload.",
           size=9.5, anchor="start", fill=MUTED)
    return c.save("fig7-ai.svg")


# --------------------------------------------------------------------------
# Figure 8 — access control planes
# --------------------------------------------------------------------------
def fig_planes():
    c = Canvas(1160, 420)
    c.text(580, 26, "Three access-control planes, deliberately separate (B1)",
           size=12, weight="600", fill=NAVY)
    planes = [
        (40, "Roles", "People inside the supplier", NAVY,
         ["Owner — everything, including closure and API keys",
          "Compliance — credentials, versions, verification, groups",
          "Sales — clients, grants, share links, requests",
          "Viewer — read only",
          "",
          "sensitive_access flag, orthogonal to role, is required",
          "for Confidential and Special-category documents.",
          "Capabilities are checked, not role names."]),
        (415, "Grants", "People outside the supplier", STEEL,
         ["Scope — group, document or whole profile",
          "Permission — view · view_download · view_download_bulk",
          "Sensitivity ceiling excludes above-class documents",
          "Duration capped by group policy",
          "NDA gate, watermark, revocation",
          "",
          "A buyer never receives a role.",
          "Resolved fresh on every read."]),
        (790, "Scopes", "Machines", OK,
         ["profile:read · documents:read · verification:read",
          "requirements:read · requirements:evaluate",
          "events:subscribe · grants:request",
          "",
          "A grant-bound key sees the narrower of token",
          "scope and grant — always.",
          "Special-category documents are blocked entirely.",
          "Mutating calls require a consent round-trip."]),
    ]
    for x, title, subtitle, colour, lines in planes:
        c.rect(x, 55, 330, 300, fill="#FFFFFF", stroke=colour, sw=1.5)
        c.rect(x, 55, 330, 44, fill=colour, stroke=colour)
        c.text(x + 165, 76, title, size=14, fill="#FFFFFF", weight="700")
        c.text(x + 165, 92, subtitle, size=9.5, fill="#FFFFFF")
        for index, line in enumerate(lines):
            c.text(x + 16, 122 + index * 19, line, size=9.5, anchor="start",
                   fill=INK if line and not line.startswith(("A buyer", "Resolved",
                                                             "Capabilities")) else MUTED)
    c.text(580, 385, "An employee never receives a grant. A buyer never receives a role. "
                     "Mixing the planes produces privilege",
           size=10, fill=MUTED)
    c.text(580, 402, "escalation that no amount of testing will find.",
           size=10, fill=MUTED)
    return c.save("fig8-planes.svg")


if __name__ == "__main__":
    for builder in (fig_erd, fig_lifecycle, fig_upload, fig_verification,
                    fig_states, fig_disclosure, fig_ai, fig_planes):
        path = builder()
        print(f"  {path.name}  {path.stat().st_size / 1024:.0f} KB")
