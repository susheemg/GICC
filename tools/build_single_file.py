"""Builds a single-file edition of GICC from the package source.

Generated rather than hand-written, so the one-file build can never drift away
from the tested one: run this, then run the same smoke suite against it.

What it does:
  * concatenates the modules in dependency order
  * strips relative imports and rewrites qualified calls (audit.record ->
    audit_record) so the flattened namespace has no collisions
  * renames router handlers with a module prefix, since several of them share a
    name with the service function they call
  * embeds the templates in a Jinja DictLoader and the stylesheet in a route,
    removing the last reason to need files on disk
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
OUT = ROOT / "gicc_single_file.py"

# Modules in dependency order.
MODULES = [
    "config.py", "db.py", "models.py", "catalogue.py", "security.py",
    "services/audit.py", "services/documents.py", "services/verification.py",
    "services/access.py", "services/requirements.py", "services/roles.py",
    "services/activity.py", "services/providers.py", "services/ai.py",
    "services/textextract.py",
    "services/authenticity.py", "services/notifications.py", "services/trust.py",
    "services/questionnaire.py", "services/onboarding.py", "services/analytics.py",
    "migrations.py", "services/jobs.py", "services/importer.py",
    "services/claims.py", "services/catalogue_admin.py", "services/assets.py",
    "services/batch.py", "services/buyers.py", "services/certsearch.py", "services/directory.py", "services/authority.py",
    "services/cloudsync.py", "services/cloudsync_demo.py",
    "demo_directory.py",
    "seed.py", "demo.py",
    "__main__.py",  # placeholder, replaced by the assembled main below
]

# intelligence.py before web_documents.py: literal /documents/upload must be
# registered ahead of the /documents/{id} wildcard.
# batch_web.py before web_documents.py for the same reason intelligence.py is:
# the literal /documents/bulk must beat the /documents/{id} wildcard.
ROUTERS = ["auth.py", "web_core.py", "onboarding_web.py", "intelligence.py",
           "batch_web.py", "web_documents.py", "assets_web.py",
           "web_disclosure.py", "share.py", "api.py", "mcp.py",
           "admin.py", "admin_import.py", "claim.py", "buyer.py", "certsearch_web.py", "public.py", "connections.py"]

# Qualified reference -> flat name.
def _module_call_rewrites() -> list[tuple[str, str]]:
    """Rules for the GICC-07 modules, derived from what each module actually
    defines.

    A blanket `batch\\.` -> `batch_` rule looked simpler and was wrong: it also
    rewrote the local variable access `batch.id` into the bare name `batch_id`,
    which then failed at import. Matching only the function names each module
    exports means a local variable that happens to share a module's name is
    left alone.
    """
    rules = []
    for module, alias, prefix in [
        ("services/assets.py", "assets", "asset_"),
        ("services/catalogue_admin.py", "catalogue_admin", "cat_"),
        ("services/importer.py", "importer", "imp_"),
        ("services/claims.py", "claims", "claim_"),
        ("services/batch.py", "batch", "batch_"),
        ("services/jobs.py", "jobs", "job_"),
        ("services/buyers.py", "buyers", "buyer_"),
        ("services/certsearch.py", "certsearch", "cs_"),
        ("services/directory.py", "directory", "dir_"),
        ("services/authority.py", "authority", "auth_"),
        ("services/cloudsync.py", "cloudsync", "cloud_"),
        ("services/cloudsync_demo.py", "cloudsync_demo", "demo_drive_"),
        ("migrations.py", "migrations", "migrate_"),
    ]:
        source = (APP / module).read_text()
        names = sorted({n for n in re.findall(r"^def (\w+)\(", source, re.M)},
                       key=len, reverse=True)
        if names:
            joined = "|".join(re.escape(n) for n in names)
            rules.append((rf"\b{alias}\.({joined})\b", prefix + r"\1"))

        # Constants are renamed by MODULE_CONSTANT_RENAMES, or keep their own
        # name when the map does not mention them. Deriving a prefix here
        # independently produced references to names that were never defined.
        renames = MODULE_CONSTANT_RENAMES.get(module, {})
        for constant in re.findall(r"^([A-Z][A-Z0-9_]*)\s*[:=]", source, re.M):
            target = renames.get(constant, constant)
            rules.append((rf"\b{alias}\.{re.escape(constant)}\b", target))
    return rules


REWRITES = [
    (r"\baudit\.record\(", "audit_record("),
    (r"\baudit\.verify_chain\(", "verify_chain("),
    (r"\bdocsvc\.", ""),
    (r"\breqsvc\.", ""),
    (r"\bactivity_service\.feed\(", "activity_feed("),
    (r"\bactivity_service\.summary\(", "activity_summary("),
    (r"\bactivity_service\.buyer_receipts\(", "buyer_receipts("),
    (r"\bverification\.", ""),
    (r"\baccess\.can_download\(", "grant_allows_download("),
    (r"\baccess\.", ""),
    (r"\broles\.require\(", "require_capability("),
    (r"\broles\.can_read_sensitive\(", "can_read_sensitive("),
    (r"\broles\.role_label\(", "role_label("),
    (r"\broles\.can\(", "can_do("),
    (r"\broles\.", ""),
    (r"\bqsvc\.", "q_"),
    (r"\bnotifications\.KINDS\b", "NOTIFICATION_KINDS"),
    (r"\bnotifications\.", "notify_"),
    (r"\bauthenticity\.", "auth_"),
    (r"\btrust\.DOMAINS\b", "TRUST_DOMAINS"),
    (r"\btrust\.", "trust_"),
    (r"\btextextract\.", "text_"),
    (r"\bonboarding_service\.TOTAL_STEPS\b", "ONBOARDING_TOTAL_STEPS"),
    (r"\bonboarding\.TOTAL_STEPS\b", "ONBOARDING_TOTAL_STEPS"),
    (r"\bonboarding_service\.STEPS\b", "ONBOARDING_STEPS"),
    (r"\bonboarding\.STEPS\b", "ONBOARDING_STEPS"),
    (r"\bonboarding\.BUYER_TYPES\b", "BUYER_TYPES"),
    (r"\bonboarding\.SECTORS\b", "SECTORS"),
    (r"\bonboarding_service\.", "onb_"),
    (r"\bonboarding\.", "onb_"),
    (r"\banalytics\.PERIODS\b", "ANALYTICS_PERIODS"),
    (r"\banalytics\.", "an_"),
    (r"\bproviders\.PROVIDERS\b", "PROVIDER_CATALOGUE"),
    (r"\bproviders\.", "prov_"),
    (r"\bai\.", "ai_"),
    (r"\bcat\.", ""),
]


# Module-level constants that clash once flattened.
MODULE_CONSTANT_RENAMES = {
    "services/trust.py": {"DOMAINS": "TRUST_DOMAINS"},
    "services/questionnaire.py": {"KEYWORDS": "QUESTION_KEYWORDS"},
    "services/providers.py": {"PROVIDERS": "PROVIDER_CATALOGUE"},
    "services/onboarding.py": {"STEPS": "ONBOARDING_STEPS",
                               "TOTAL_STEPS": "ONBOARDING_TOTAL_STEPS"},
    "services/analytics.py": {"PERIODS": "ANALYTICS_PERIODS"},
    "services/notifications.py": {"KINDS": "NOTIFICATION_KINDS"},
    "services/activity.py": {"TEMPLATES": "ACTIVITY_TEMPLATES"},
    # GICC-07 modules. Each of these collides with a name already in the flat
    # namespace, and the build fails loudly rather than silently overwriting.
    "services/importer.py": {"PERMITTED_FIELDS": "IMPORT_PERMITTED_FIELDS",
                             "PROHIBITED_PATTERNS": "IMPORT_PROHIBITED_PATTERNS",
                             "HEADER_HINTS": "IMPORT_HEADER_HINTS",
                             "MAX_ROWS": "IMPORT_MAX_ROWS",
                             "ALPHA3": "IMPORT_ALPHA3",
                             "COUNTRY_NAMES": "IMPORT_COUNTRY_NAMES",
                             "COUNTRY_PATTERN": "IMPORT_COUNTRY_PATTERN",
                             "REGISTRY_PATTERN": "IMPORT_REGISTRY_PATTERN"},
    "services/claims.py": {"AUTO_APPROVE": "CLAIM_AUTO_APPROVE",
                           "REVIEW_FLOOR": "CLAIM_REVIEW_FLOOR",
                           "COOLDOWN_DAYS": "CLAIM_COOLDOWN_DAYS",
                           "FREE_EMAIL_DOMAINS": "CLAIM_FREE_EMAIL_DOMAINS",
                           "KNOWN_REGISTRIES": "CLAIM_KNOWN_REGISTRIES"},
    "services/assets.py": {"STATUS_RANK": "ASSET_STATUS_RANK",
                           "ASSURANCE_RANK": "ASSET_ASSURANCE_RANK",
                           "KNOWN_ACCREDITORS": "ASSET_KNOWN_ACCREDITORS"},
    "services/batch.py": {"KIND": "BATCH_KIND", "MAX_FILES": "BATCH_MAX_FILES",
                          "FILE_THRESHOLD": "BATCH_FILE_THRESHOLD",
                          "PROPOSE_THRESHOLD": "BATCH_PROPOSE_THRESHOLD"},
    "services/jobs.py": {"_HANDLERS": "JOB_HANDLERS"},
    "services/certsearch.py": {"SEARCH_BASE": "CERTSEARCH_BASE",
                               "TRANSPORT_CEILING": "CERTSEARCH_CEILING",
                               "STANDARDS": "CERTSEARCH_STANDARDS",
                               "COMPARABLE": "CERTSEARCH_COMPARABLE",
                               "LABELS": "CERTSEARCH_LABELS",
                               "ACTIVE_WORDS": "CERTSEARCH_ACTIVE_WORDS",
                               "DEAD_WORDS": "CERTSEARCH_DEAD_WORDS"},
    "services/authority.py": {"AUTHORITIES": "AUTH_AUTHORITIES",
                              "BRAND_TOKENS": "AUTH_BRAND_TOKENS",
                              "URL_PATTERN": "AUTH_URL_PATTERN"},
    "services/cloudsync.py": {"MAX_FILE_BYTES": "CLOUD_MAX_FILE_BYTES",
                              "SUPPORTED": "CLOUD_SUPPORTED",
                              "SCHEDULES": "CLOUD_SCHEDULES",
                              "PROVIDER_LABEL": "CLOUD_PROVIDER_LABEL",
                              "PROVIDERS": "CLOUD_PROVIDERS"},
    "services/cloudsync_demo.py": {"ROOT": "DEMO_DRIVE_ROOT",
                                   "MANIFEST": "DEMO_DRIVE_MANIFEST"},
    "services/directory.py": {"MAX_RESULTS": "DIRECTORY_MAX_RESULTS",
                              "MIN_QUERY": "DIRECTORY_MIN_QUERY"},
    "services/buyers.py": {"MAX_RESULTS": "BUYER_MAX_RESULTS",
                           "MIN_QUERY": "BUYER_MIN_QUERY",
                           "SEARCH_LIMIT_PER_DAY": "BUYER_SEARCH_LIMIT_PER_DAY"},
    "migrations.py": {"SCHEMA_VERSION": "MIGRATE_SCHEMA_VERSION",
                      "MANUAL_STEPS": "MIGRATE_MANUAL_STEPS",
                      "SQLITE_DEFAULTS": "MIGRATE_SQLITE_DEFAULTS"},
}

REWRITES += _module_call_rewrites()

# Definitions that must be renamed to match the rewrites above.
DEF_RENAMES = {
    "services/audit.py": {"record": "audit_record"},
    "services/ai.py": {"status": "ai_status", "classify_document": "ai_classify_document",
                       "version_diff": "ai_version_diff", "is_enabled": "ai_is_enabled",
                       "control_summary": "ai_control_summary",
                       "draft_answer": "ai_draft_answer"},
    "services/textextract.py": {"extract": "text_extract"},
    "services/authenticity.py": {"evaluate": "auth_evaluate", "resolve": "auth_resolve",
                                 "open_signals": "auth_open_signals",
                                 "for_version": "auth_for_version",
                                 "high_count": "auth_high_count"},
    "services/notifications.py": {"emit": "notify_emit", "sweep": "notify_sweep",
                                  "inbox": "notify_inbox", "mark_read": "notify_mark_read",
                                  "recipients": "notify_recipients",
                                  "unread_count": "notify_unread_count",
                                  "sweep_all": "notify_sweep_all",
                                  "deliver_pending": "notify_deliver_pending"},
    "services/trust.py": {"compute": "trust_compute", "latest": "trust_latest",
                          "history": "trust_history", "band_for": "trust_band_for",
                          "band_tone": "trust_band_tone",
                          "next_best_action": "trust_next_best_action"},
    "services/questionnaire.py": {"parse": "q_parse", "build": "q_build",
                                  "approve": "q_approve", "map_types": "q_map_types",
                                  "export_csv": "q_export_csv",
                                  "question_hash": "q_question_hash"},
    "services/access.py": {"can_download": "grant_allows_download"},
    "services/roles.py": {"require": "require_capability", "can": "can_do"},
    "services/activity.py": {"feed": "activity_feed", "summary": "activity_summary"},
}

# Router handlers whose names collide with the service functions they call.
HANDLER_PREFIX = {
    "onboarding_web.py": "ob_",
    "intelligence.py": "ix_",
    "web_documents.py": "wd_",
    "web_disclosure.py": "wx_",
    "api.py": "api_",
    "admin.py": "adm_",
    "share.py": "sh_",
    "web_core.py": "wc_",
    "auth.py": "au_",
    "mcp.py": "mcp_",
    "batch_web.py": "bw_",
    "assets_web.py": "aw_",
    "admin_import.py": "ai_",
    "claim.py": "cl_",
    "buyer.py": "by_",
    "certsearch_web.py": "csw_",
    "public.py": "pub_",
    "connections.py": "conn_",
}

IMPORT_LINE = re.compile(r"^\s*from \.+[\w.]* import .*?$|^\s*import \.+.*$", re.M)
MULTILINE_IMPORT = re.compile(r"^\s*from \.+[\w.]* import \([^)]*\)\s*$", re.M | re.S)
FUTURE = re.compile(r"^from __future__ import annotations\s*$", re.M)


LOCAL_IMPORT = re.compile(r"^[ \t]*from \.(\w+) import ([\w, ]+)", re.M)
ALIASED_IMPORT = re.compile(r"from \.+[\w.]* import (\w+) as (\w+)", re.M)

RENAMED_NAMES = {name for mapping in DEF_RENAMES.values() for name in mapping} | {
    name for mapping in MODULE_CONSTANT_RENAMES.values() for name in mapping}


def check_aliased_imports(name: str, source: str) -> None:
    """`from ..services import notifications as notify` survives the strip as a
    dangling `notify`, because the rewrite rules key off the real module name.
    Aliasing a module whose functions we prefix breaks the flattened build."""
    # Aliases with an explicit REWRITES rule are safe; everything else dangles.
    allowed = {alias for pattern, _replacement in REWRITES
               for alias in [pattern.replace(r"\b", "").replace(r"\.", "").rstrip("(")]}
    allowed |= {"cat", "reqsvc", "docsvc", "qsvc", "activity_service"}
    for match in ALIASED_IMPORT.finditer(source):
        # Aliases handled by an explicit REWRITES rule are fine; anything else
        # is a dangling name once the import line is stripped.
        if match.group(2) not in allowed:
            raise SystemExit(
                f"BUILD FAILED - {name} aliases '{match.group(1)}' as "
                f"'{match.group(2)}'. Stripping the import leaves that name "
                "dangling. Import it under its own name, or add a REWRITES rule.")


def check_local_imports(name: str, source: str, renamed: set[str]) -> None:
    """A function-local `from .module import thing` is fine in a package. Once
    flattened it is fatal *if* `thing` is one of the names we rename, because
    the strip removes the import and leaves a dangling reference. Importing a
    name we do not touch is harmless, so only the renamed ones fail the build."""
    for match in LOCAL_IMPORT.finditer(source):
        for imported in (n.strip() for n in match.group(2).split(",")):
            if imported not in renamed:
                continue
            raise SystemExit(
                f"BUILD FAILED - {name} does 'from .{match.group(1)} import "
                f"{imported}', and '{imported}' is renamed during flattening, so the "
                f"reference dangles. Use 'from . import {match.group(1)}' and the "
                f"qualified name instead.")


def strip_module(source: str) -> str:
    source = MULTILINE_IMPORT.sub("", source)
    source = IMPORT_LINE.sub("", source)
    source = FUTURE.sub("", source)
    return source


def apply_rewrites(source: str) -> str:
    """Rewrites code only. String literals are protected first: without this,
    "verification.html" becomes "html" and action codes like "access.denied"
    lose their prefix."""
    import io
    import tokenize

    protected: list[str] = []

    def hide(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"\x00STR{len(protected) - 1}\x00"

    pieces = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        pieces.append((token.type, token.string, token.start, token.end, token.line))

    # Rebuild with string tokens replaced by placeholders, preserving layout.
    lines = source.splitlines(keepends=True)
    replacements: list[tuple[int, int, int, int, str]] = []
    for token_type, string, start, end, _line in pieces:
        if token_type == tokenize.STRING:
            protected.append(string)
            replacements.append((start[0], start[1], end[0], end[1],
                                 f"\x00STR{len(protected) - 1}\x00"))

    for start_row, start_col, end_row, end_col, placeholder in reversed(replacements):
        if start_row == end_row:
            line = lines[start_row - 1]
            lines[start_row - 1] = line[:start_col] + placeholder + line[end_col:]
        else:
            head = lines[start_row - 1][:start_col]
            tail = lines[end_row - 1][end_col:]
            lines[start_row - 1:end_row] = [head + placeholder + tail]

    source = "".join(lines)
    for pattern, replacement in REWRITES:
        source = re.sub(pattern, replacement, source)
    for index, original in enumerate(protected):
        source = source.replace(f"\x00STR{index}\x00", original)
    return source


def rename_defs(source: str, renames: dict[str, str]) -> str:
    """Renames the definition and every call to it inside its own module - a
    module calling its own helper does so unqualified, so renaming only the def
    leaves a dangling reference.

    The negative lookbehind matters: `_cipher().encrypt(...)` is a method on a
    Fernet object, not our module-level encrypt(), and rewriting it produces an
    AttributeError at runtime rather than a build error.
    """
    for old, new in renames.items():
        source = re.sub(rf"(?<![.\w]){old}\(", f"{new}(", source)
        source = re.sub(rf"^def {new}\(", f"def {new}(", source, flags=re.M)
    return source


def prefix_handlers(source: str, prefix: str, reserved: set[str]) -> str:
    """Router handlers are only ever referenced by decorator, so renaming them
    is safe and removes every collision with the service layer."""
    renamed: list[str] = []

    def replace(match: re.Match) -> str:
        renamed.append(match.group(2))
        return f"{match.group(1)}def {prefix}{match.group(2)}("

    source = re.sub(r"(@router\.\w+\([^\n]*\)\n(?:@router\.\w+\([^\n]*\)\n)*"
                    r"(?:async )?)def (\w+)\(", replace, source)
    # A handler occasionally calls another handler in its own module - the MCP
    # discovery document is reused by the initialize response, for one. But a
    # handler far more often shares a name with the service function it calls
    # (wd_create_version calls create_version), so only rewrite names the
    # service layer does not define.
    for name in renamed:
        if name in reserved:
            continue
        source = re.sub(rf"\b{name}\(", f"{prefix}{name}(", source)
    return source


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


DEF_PATTERN = re.compile(r"^def (\w+)\(", re.M)

# Modules whose entire public surface is prefixed by a REWRITES rule. Listing
# individual functions there was fragile: any new public function broke the
# build, so every public def in these modules is renamed automatically.
PREFIXED_MODULES = {
    "demo_directory.py": "directory_",
    "services/jobs.py": "job_",
    "services/importer.py": "imp_",
    "services/claims.py": "claim_",
    "services/catalogue_admin.py": "cat_",
    "services/assets.py": "asset_",
    "services/batch.py": "batch_",
    "services/buyers.py": "buyer_",
    "services/certsearch.py": "cs_",
    "services/directory.py": "dir_",
    "services/authority.py": "auth_",
    "services/cloudsync.py": "cloud_",
    "services/cloudsync_demo.py": "demo_drive_",
    "migrations.py": "migrate_",
    "services/notifications.py": "notify_",
    "services/trust.py": "trust_",
    "services/authenticity.py": "auth_",
    "services/questionnaire.py": "q_",
    "services/ai.py": "ai_",
    "services/textextract.py": "text_",
    "services/providers.py": "prov_",
    "services/onboarding.py": "onb_",
    "services/analytics.py": "an_",
}


def auto_renames(source: str, prefix: str) -> dict[str, str]:
    return {name: f"{prefix}{name}" for name in DEF_PATTERN.findall(source)
            if not name.startswith("_")}


def build() -> str:
    parts: list[str] = []
    service_names: set[str] = set()

    parts.append(HEADER)

    parts.append(PRELUDE)

    for name in MODULES:
        if name == "__main__.py":
            continue
        source = read(APP / name)
        check_local_imports(name, source, RENAMED_NAMES)
        check_aliased_imports(name, source)
        if name == "seed.py":
            # demo.py and demo_directory.py flatten into this namespace, so the
            # module reference disappears along with the import. Matching the
            # exact two-line string was brittle - editing the import list in
            # seed.py silently skipped the rewrite and the build failed only at
            # startup. These are regex-based and independent of each other.
            source = re.sub(r"^\s*from \. import demo(?:, demo_directory)?\s*$",
                            "", source, flags=re.M)
            source = source.replace("demo.build(db)", "build(db)")
            source = source.replace("demo_directory.build(db)",
                                    "directory_build(db)")
        if name == "config.py":
            # In the package, BASE_DIR is app/ and the data directory sits
            # beside it. In a single file there is no package directory, so
            # the data directory belongs next to the file itself.
            source = source.replace("ROOT_DIR = BASE_DIR.parent",
                                    "ROOT_DIR = BASE_DIR")
        source = strip_module(source)
        renames = dict(DEF_RENAMES.get(name, {}))
        if name in PREFIXED_MODULES:
            renames = {**auto_renames(source, PREFIXED_MODULES[name]), **renames}
        source = rename_defs(source, renames)
        for old, new in MODULE_CONSTANT_RENAMES.get(name, {}).items():
            source = re.sub(rf"\b{old}\b", new, source)
        source = apply_rewrites(source)
        service_names.update(DEF_PATTERN.findall(source))
        parts.append(banner(name) + source)

    # Assets before the app, since the template environment needs them.
    parts.append(banner("templates") + build_templates())
    parts.append(banner("stylesheet") + build_stylesheet())
    parts.append(banner("directory data") + build_directory_data())

    # main.py, minus its own imports and its router registration block.
    main_source = read(APP / "main.py")
    main_source = main_source.split("from .routers import")[0]
    main_source = strip_module(main_source)
    main_source = apply_rewrites(main_source)
    main_source = main_source.replace(
        'app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")',
        "")
    main_source = main_source.replace(
        'templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))',
        "templates = Jinja2Templates(env=Environment(\n"
        "    loader=DictLoader(HTML_TEMPLATES), autoescape=select_autoescape([\"html\"])))")
    parts.append(banner("application") + main_source + STATIC_ROUTE)

    for name in ROUTERS:
        source = read(APP / "routers" / name)
        check_local_imports(f"routers/{name}", source, RENAMED_NAMES)
        check_aliased_imports(f"routers/{name}", source)
        source = strip_module(source)
        # Qualified module references are rewritten first: doing it after the
        # handler prefix stacks both, producing onb_ob_dismiss.
        source = apply_rewrites(source)
        source = prefix_handlers(source, HANDLER_PREFIX[name], service_names)
        source = source.replace("router = APIRouter()",
                                f"router = APIRouter()  # {name}")
        # each router built its own APIRouter; keep them distinct
        variable = name.replace(".py", "_router")
        source = re.sub(r"^router = APIRouter\(", f"{variable} = APIRouter(", source,
                        flags=re.M)
        source = source.replace("@router.", f"@{variable}.")
        parts.append(banner(f"routes: {name}") + source)

    includes = "\n".join(f"app.include_router({n.replace('.py', '_router')})"
                         for n in ROUTERS)
    parts.append(banner("wiring") + includes + FOOTER)

    return "\n\n".join(parts)


def banner(title: str) -> str:
    line = "=" * 74
    return f"# {line}\n# {title}\n# {line}\n"


def build_templates() -> str:
    entries = []
    for path in sorted((APP / "templates").glob("*.html")):
        body = read(path)
        entries.append(f"    {path.name!r}: {body!r},")
    return "HTML_TEMPLATES = {\n" + "\n".join(entries) + "\n}\n"


def build_directory_data() -> str:
    """The demo directory travels as base64 inside the file.

    It is already gzipped CSV at about 155 KB, so base64 costs roughly 50 KB
    more and buys a single file that still runs anywhere. Reading it from disk
    would mean the single-file build was not one.
    """
    import base64

    source = APP / "data" / "mca_companies.csv.gz"
    if not source.exists():
        return "DIRECTORY_CSV_GZ = b\"\"\n"
    encoded = base64.b64encode(source.read_bytes()).decode()
    chunks = "\n".join(f'    "{encoded[i:i + 96]}"'
                        for i in range(0, len(encoded), 96))
    return ("import base64 as _b64\n\n"
            "DIRECTORY_CSV_GZ = _b64.b64decode(\n" + chunks + "\n)\n")


def build_stylesheet() -> str:
    css = read(APP / "static" / "css" / "brata.css")
    return f"BRATA_CSS = {css!r}\n"


HEADER = '''"""
GICC - Global Identity & Certificates Center
Single-file edition. Generated from the package source by tools/build_single_file.py.

Deploy on Render
----------------
1. Put this file and requirements.txt in a Git repository.
2. Render -> New -> Web Service, point it at the repo.
3. Build command:  pip install -r requirements.txt
   Start command:  uvicorn gicc_single_file:app --host 0.0.0.0 --port $PORT
   Health check:   /healthz
4. Add a persistent disk mounted at /var/data and set DATA_DIR=/var/data.
   Without the disk, the database and every uploaded document are wiped on
   each deploy.
5. Set SECRET_KEY, and change ADMIN_PASSWORD before anyone else finds it.

Run locally
-----------
    pip install fastapi "uvicorn[standard]" sqlalchemy jinja2 itsdangerous \\
                python-multipart httpx
    uvicorn gicc_single_file:app --reload --port 8000

Demo sign-ins (SEED_DEMO=1, the default)
----------------------------------------
    Supplier owner       demo@northgate.example / demo1234
    Compliance manager   compliance@northgate.example / demo1234
    Sales                bids@northgate.example / demo1234
    Platform operator    admin@gicc.platform / admin1234   at /admin/login

Set SEED_DEMO=0 for a clean instance.
"""
'''

PRELUDE = '''from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import shutil
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import (APIRouter, Depends, FastAPI, File, Form, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.templating import Jinja2Templates
from jinja2 import DictLoader, Environment, select_autoescape
from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint, and_,
                        create_engine, delete, desc, event, func, or_, select, text)
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, mapped_column,
                            relationship, sessionmaker)
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
'''

STATIC_ROUTE = '''

@app.get("/static/css/brata.css", include_in_schema=False)
def stylesheet():
    """The stylesheet is embedded rather than served from disk, so this file
    needs nothing beside it to run."""
    return Response(BRATA_CSS, media_type="text/css",
                    headers={"Cache-Control": "public, max-age=3600"})
'''

FOOTER = '''


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
'''


ASSIGN_PATTERN = re.compile(r"^([A-Za-z_]\w*)\s*(?::.*?)?=(?!=)", re.M)


def report_collisions(source: str) -> list[str]:
    """Flattening turns two module-level names into one. A collision that
    happens to typecheck - two dicts, say - fails silently at runtime, so the
    build refuses to stay quiet about it."""
    seen: dict[str, int] = {}
    for name in DEF_PATTERN.findall(source) + ASSIGN_PATTERN.findall(source):
        seen[name] = seen.get(name, 0) + 1
    ignore = {"router", "app", "templates", "parts", "source", "line"}

    # Constants collide loudly enough to notice. Module-level *functions* were
    # the real hazard: two modules each defining `_normalise` flattened to one
    # definition that typechecked and failed at the first call with a confusing
    # argument-count error. DEF_PATTERN is anchored at column zero, so nested
    # helpers are not counted.
    functions = {}
    for name in DEF_PATTERN.findall(source):
        functions[name] = functions.get(name, 0) + 1

    return sorted({n for n, count in seen.items()
                   if count > 1 and n.isupper() and n not in ignore}
                  | {n for n, count in functions.items()
                     if count > 1 and n not in ignore})


if __name__ == "__main__":
    built = build()
    collisions = report_collisions(built)
    if collisions:
        print("BUILD FAILED - these names are defined more than once. Flattening "
              "makes the later definition silently win:")
        for name in collisions:
            print(f"  {name}")
        print("Rename one of each pair, or add it to MODULE_CONSTANT_RENAMES.")
        raise SystemExit(1)
    OUT.write_text(built, encoding="utf-8")
    lines = built.count("\n")
    print(f"wrote {OUT} — {lines:,} lines, {OUT.stat().st_size / 1024:.0f} KB")
