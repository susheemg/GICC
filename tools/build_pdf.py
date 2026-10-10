"""Renders GICC-00 to a print-quality PDF.

Markdown → HTML → WeasyPrint. Mermaid fenced blocks are replaced with the SVG
figures drawn by make_figures.py, since Mermaid's CLI needs a headless Chrome
that is not available here.
"""
from __future__ import annotations

import base64
import re
from datetime import date
from pathlib import Path

import markdown
from weasyprint import CSS, HTML

SRC = Path("/mnt/user-data/outputs/GICC-00-Master-Specification.md")
FIGS = Path("/tmp/figs")
OUT = Path("/mnt/user-data/outputs/GICC-00-Master-Specification.pdf")

# Mermaid blocks in document order → figure file and caption.
# Order must match the mermaid blocks as they appear in the source, not the
# order the figures were drawn in.
FIGURES = [
    ("fig1-erd.svg", "Figure 2 — Entity clusters and load-bearing relationships"),
    ("fig5-states.svg", "Figure 3 — Document version state machine"),
    ("fig4-verification.svg", "Figure 4 — Verification pipeline and assurance computation"),
    ("fig7-ai.svg", "Figure 5 — The AI proposal layer"),
    ("fig2-lifecycle.svg", "Figure 6 — Supplier lifecycle, 14 stages"),
    ("fig3-upload.svg", "Figure 7 — Upload, classification and publication"),
    ("fig6-disclosure.svg", "Figure 8 — Client onboarding and disclosure"),
]

EXTRA_FIGURES = {
    # Inserted rather than replacing a Mermaid block.
    "### 2.1 Actor catalogue": ("fig8-planes.svg",
                                "Figure 1 — Three access-control planes"),
}


def embed(name: str) -> str:
    data = base64.b64encode((FIGS / name).read_bytes()).decode()
    return f"data:image/svg+xml;base64,{data}"


def prepare(text: str) -> str:
    """Swap fenced mermaid blocks for figures, in document order."""
    blocks = list(re.finditer(r"```mermaid\n.*?\n```", text, re.S))
    if len(blocks) != len(FIGURES):
        print(f"  note: {len(blocks)} mermaid blocks, {len(FIGURES)} figures defined")

    out, cursor = [], 0
    for index, block in enumerate(blocks):
        out.append(text[cursor:block.start()])
        if index < len(FIGURES):
            name, caption = FIGURES[index]
            out.append(f'\n<figure class="fig">\n<img src="{embed(name)}" '
                       f'alt="{caption}"/>\n<figcaption>{caption}</figcaption>\n'
                       f'</figure>\n')
        cursor = block.end()
    out.append(text[cursor:])
    text = "".join(out)

    for anchor, (name, caption) in EXTRA_FIGURES.items():
        figure = (f'\n<figure class="fig">\n<img src="{embed(name)}" alt="{caption}"/>\n'
                  f'<figcaption>{caption}</figcaption>\n</figure>\n\n{anchor}')
        text = text.replace(anchor, figure, 1)
    return text


def build_html(body_md: str) -> str:
    body = markdown.markdown(
        body_md,
        extensions=["tables", "fenced_code", "attr_list", "md_in_html", "sane_lists"],
        output_format="html5")

    # Blockquotes carrying an INVARIANT become callouts.
    body = re.sub(r"<blockquote>\s*<p><strong>(INVARIANT [^<]+)</strong>",
                  r'<blockquote class="invariant"><p><strong>\1</strong>', body)

    today = date.today().strftime("%d %B %Y")
    cover = """
<div class="cover">
  <div class="cover-rule"></div>
  <div class="cover-eyebrow">Master Specification · GICC-00</div>
  <h1 class="cover-title">Global Identity &amp;<br/>Certificates Center</h1>
  <div class="cover-sub">Functional, Data, Application, Process<br/>and Technical Design</div>
  <div class="cover-meta">
    <div><span>Version</span>1.1</div>
    <div><span>Date</span>__TODAY__</div>
    <div><span>Status</span>Design-as-built</div>
    <div><span>Audience</span>Development, architecture, QA, risk, compliance, audit</div>
    <div><span>Supersedes</span>GICC-01 · GICC-02 · GICC-03 · GICC-04 · GICC-05</div>
  </div>
  <div class="cover-stats">
    <div><b>198</b>document types</div><div><b>38</b>database tables</div>
    <div><b>126</b>HTTP routes</div><div><b>144</b>requirements</div>
    <div><b>58</b>processes</div><div><b>37</b>controls</div>
    <div><b>25</b>invariants</div><div><b>207</b>automated checks</div>
  </div>
  <div class="cover-foot">Every requirement marked <i>Built</i> is implemented and covered
    by the automated suite. Requirements marked <i>Specified</i> are designed and not yet
    implemented. Part 10 states the gaps in full.</div>
</div>
<div class="toc-page">
  <h2 class="toc-h">Contents</h2>
  {toc_html}
</div>
"""

    # Build a contents list from the top-level headings.
    entries = re.findall(r'<h2[^>]*>(Part \d+ — [^<]+|Appendix [A-Z] — [^<]+)</h2>', body)
    items = "".join(f'<li>{e}</li>' for e in entries)
    toc = f'<ol class="toc">{items}</ol>'
    cover = cover.replace("{toc_html}", toc).replace("__TODAY__", today)

    return f"<!doctype html><html><head><meta charset='utf-8'></head><body>{cover}{body}</body></html>"


CSS_TEXT = """
@page {
  size: A4;
  margin: 20mm 17mm 18mm 17mm;
  @top-left {
    content: "GICC-00 Master Specification";
    font-family: Carlito, Calibri, Helvetica, sans-serif;
    font-size: 7.5pt; color: #6B7280;
  }
  @top-right {
    content: "v1.0 · Design-as-built";
    font-family: Carlito, Calibri, Helvetica, sans-serif;
    font-size: 7.5pt; color: #6B7280;
  }
  @bottom-right {
    content: counter(page);
    font-family: Carlito, Calibri, Helvetica, sans-serif;
    font-size: 8.5pt; color: #1F3A70; font-weight: 600;
  }
  @bottom-left {
    content: "Confidential";
    font-family: Carlito, Calibri, Helvetica, sans-serif;
    font-size: 7.5pt; color: #9AA1AC;
  }
}
@page :first { margin: 0; @top-left { content: ""; } @top-right { content: ""; }
               @bottom-left { content: ""; } @bottom-right { content: ""; } }

html { font-family: Carlito, Calibri, "Helvetica Neue", Helvetica, Arial, sans-serif;
       font-size: 9.5pt; line-height: 1.45; color: #1A1D23; }
body { margin: 0; }

/* ---------- cover ---------- */
.cover { page-break-after: always; padding: 34mm 20mm 20mm 20mm; height: 100%;
         box-sizing: border-box; position: relative; }
.cover-rule { position: absolute; top: 0; left: 0; right: 0; height: 14mm;
              background: #1F3A70; }
.cover-eyebrow { font-size: 8.5pt; letter-spacing: .16em; text-transform: uppercase;
                 color: #3E6FB0; font-weight: 700; margin-bottom: 6mm; }
.cover-title { font-size: 33pt; line-height: 1.1; color: #1F3A70; font-weight: 700;
               letter-spacing: -.02em; margin: 0 0 5mm 0; }
.cover-sub { font-size: 13pt; color: #1A1D23; line-height: 1.35; margin-bottom: 12mm; }
.cover-meta { border-top: 1px solid #C8D6E9; padding-top: 4mm; margin-bottom: 10mm; }
.cover-meta div { font-size: 9.5pt; padding: 1.4mm 0; }
.cover-meta span { display: inline-block; width: 30mm; color: #6B7280; font-size: 8.5pt;
                   text-transform: uppercase; letter-spacing: .06em; }
.cover-stats { display: flex; flex-wrap: wrap; gap: 3mm; margin-bottom: 10mm; }
.cover-stats div { width: 38mm; border: 1px solid #DDE2EA; padding: 2.5mm 3mm;
                   font-size: 8pt; color: #6B7280; }
.cover-stats b { display: block; font-size: 17pt; color: #1F3A70; line-height: 1.1;
                 margin-bottom: .8mm; }
.cover-foot { border-left: 3px solid #3E6FB0; background: #E8EEF7; padding: 3mm 4mm;
              font-size: 8.5pt; color: #16294F; }

/* ---------- contents ---------- */
.toc-page { page-break-after: always; padding-top: 4mm; }
.toc-h { font-size: 19pt; color: #1F3A70; border-bottom: 2px solid #1F3A70;
         padding-bottom: 2mm; margin-bottom: 6mm; }
ol.toc { list-style: none; counter-reset: toc; padding: 0; }
ol.toc li { counter-increment: toc; font-size: 11pt; padding: 2.6mm 0;
            border-bottom: 1px dotted #DDE2EA; color: #1F3A70; font-weight: 600; }

/* ---------- headings ---------- */
h1 { font-size: 20pt; color: #1F3A70; margin: 0 0 4mm 0; page-break-after: avoid; }
h2 { font-size: 15pt; color: #1F3A70; margin: 9mm 0 3mm 0; padding-bottom: 1.6mm;
     border-bottom: 2px solid #1F3A70; page-break-after: avoid; }
h2:first-of-type { margin-top: 0; }
h3 { font-size: 11.5pt; color: #16294F; margin: 6mm 0 2mm 0; page-break-after: avoid; }
h4 { font-size: 10pt; color: #3E6FB0; margin: 4mm 0 1.5mm 0; page-break-after: avoid;
     text-transform: uppercase; letter-spacing: .05em; }
p { margin: 0 0 2.4mm 0; orphans: 3; widows: 3; }

/* Each Part starts a fresh page. */
h2 { page-break-before: always; }
h2:first-of-type { page-break-before: avoid; }

/* ---------- tables ---------- */
table { width: 100%; border-collapse: collapse; margin: 2.5mm 0 4mm 0;
        font-size: 8.2pt; page-break-inside: auto; }
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th { background: #1F3A70; color: #fff; text-align: left; padding: 1.6mm 2mm;
     font-weight: 600; font-size: 7.8pt; letter-spacing: .02em; }
td { padding: 1.5mm 2mm; border-bottom: 1px solid #E3E6EB; vertical-align: top; }
tbody tr:nth-child(even) { background: #FAFBFC; }

/* ---------- code ---------- */
code { font-family: "DejaVu Sans Mono", Menlo, Consolas, monospace; font-size: 8pt;
       background: #F1F4F8; padding: .3mm 1mm; border-radius: 1px; color: #16294F; }
pre { background: #16294F; color: #D7E1F2; padding: 3mm 4mm; border-radius: 2px;
      font-size: 7.6pt; line-height: 1.4; overflow-wrap: break-word;
      white-space: pre-wrap; page-break-inside: avoid; margin: 2.5mm 0 4mm 0; }
pre code { background: none; color: inherit; padding: 0; font-size: 7.6pt; }

/* ---------- callouts ---------- */
blockquote { margin: 3mm 0; padding: 2.5mm 4mm; background: #E8EEF7;
             border-left: 3px solid #3E6FB0; font-size: 9pt; page-break-inside: avoid; }
blockquote p { margin: 0 0 1.5mm 0; }
blockquote p:last-child { margin-bottom: 0; }
blockquote.invariant { background: #FBF2DE; border-left-color: #B8860B; }
blockquote.invariant strong { color: #7A5B08; }

/* ---------- figures ---------- */
figure.fig { margin: 4mm 0 5mm 0; page-break-inside: avoid; text-align: center; }
figure.fig img { width: 100%; max-width: 176mm; }
figcaption { font-size: 8pt; color: #6B7280; margin-top: 1.5mm;
             border-top: 1px solid #DDE2EA; padding-top: 1.5mm; }

ul, ol { margin: 0 0 2.5mm 0; padding-left: 5mm; }
li { margin-bottom: 1mm; }
hr { border: none; border-top: 1px solid #DDE2EA; margin: 6mm 0; }
strong { color: #16294F; }
a { color: #3E6FB0; text-decoration: none; }
"""


def main() -> None:
    text = prepare(SRC.read_text())
    html = build_html(text)
    Path("/tmp/spec.html").write_text(html)
    HTML(string=html, base_url="/tmp").write_pdf(OUT, stylesheets=[CSS(string=CSS_TEXT)])
    size = OUT.stat().st_size / 1024 / 1024
    from pypdf import PdfReader
    pages = len(PdfReader(str(OUT)).pages)
    print(f"{OUT}\n  {pages} pages, {size:.1f} MB")


if __name__ == "__main__":
    main()
