"""Runtime configuration. Everything is environment-driven so the same image
runs locally and on Render without code changes."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

# On Render, mount a persistent disk at /var/data and set DATA_DIR=/var/data.
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "gicc.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
SESSION_COOKIE = "gicc_session"
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", 60 * 60 * 12))

APP_NAME = "TSP"
APP_LONG_NAME = "Trusted Supplier Program"
APP_VERSION = "1.0.0"
MCP_PROTOCOL_VERSION = "2026-07-28"

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", 25 * 1024 * 1024))
ALLOWED_UPLOAD_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
}

# Expiry reminder ladder (days before expiry) - GICC-04 P-31
REMINDER_LADDER = [90, 60, 30, 7, 0]
EXPIRING_SOON_DAYS = int(os.getenv("EXPIRING_SOON_DAYS", 90))

# Set LIVE_LINK_CHECKS=1 to let the verification worker make real outbound
# requests when testing an authentication link. Off by default.
LIVE_LINK_CHECKS = os.getenv("LIVE_LINK_CHECKS", "0") == "1"

# Platform operator bootstrap. Change these before any real deployment.
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@gicc.platform")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")
# The operator account is seeded once, when the table is empty. Changing
# ADMIN_PASSWORD afterwards therefore did nothing, and an operator who did not
# know the password the database was built with had no way back in. Setting
# ADMIN_RESET=1 rewrites the account from ADMIN_EMAIL and ADMIN_PASSWORD at
# startup. It is env-driven rather than a CLI so it works in the single-file
# build too, where there is no package to run a module from.
ADMIN_RESET = os.getenv("ADMIN_RESET", "") == "1"
ADMIN_NAME = os.getenv("ADMIN_NAME", "Platform Operator")
BREAK_GLASS_MINUTES = int(os.getenv("BREAK_GLASS_MINUTES", 240))

# --------------------------------------------------------------------------
# AI layer - GICC-05. Off unless a key is present; the product runs identically
# without one, using deterministic heuristics.
# --------------------------------------------------------------------------
AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "claude-sonnet-4-6")
AI_MAX_INPUT_CHARS = int(os.getenv("AI_MAX_INPUT_CHARS", 12000))
MIN_FIELD_CONFIDENCE = float(os.getenv("MIN_FIELD_CONFIDENCE", 0.6))
# Sensitivity classes never sent to a model provider.
AI_EXCLUDED_SENSITIVITY = {"S"}

# Notification engine
SWEEP_MINUTES = int(os.getenv("SWEEP_MINUTES", 60))
NOTIFY_TRANSPORT = os.getenv("NOTIFY_TRANSPORT", "log")   # log | smtp | off
SMTP_URL = os.getenv("SMTP_URL", "")
NOTIFY_FROM = os.getenv("NOTIFY_FROM", "gicc@example.invalid")

# Trust Index
TRUST_ALGORITHM_VERSION = "1.0.0"

# Job runner - GICC-07. Concurrency is bounded because fifty parallel model
# calls is a bill, not a feature.
JOB_CONCURRENCY = int(os.getenv("JOB_CONCURRENCY", 4))
MAX_AI_CALLS_PER_JOB = int(os.getenv("MAX_AI_CALLS_PER_JOB", 120))
MAX_BATCH_FILES = int(os.getenv("MAX_BATCH_FILES", 50))
MAX_IMPORT_ROWS = int(os.getenv("MAX_IMPORT_ROWS", 10000))

# IAF CertSearch. Automated collection is off by default: the site's terms
# govern it and IAF licenses the data commercially, so it is a deployment
# decision rather than a default. A licensed key is what earns L3.
CERTSEARCH_ALLOW_SCRAPE = os.getenv("CERTSEARCH_ALLOW_SCRAPE", "") == "1"
CERTSEARCH_API_KEY = os.getenv("CERTSEARCH_API_KEY", "")

# Cloud folder sync. Register an OAuth app with each provider and set the
# redirect URI to <PUBLIC_BASE_URL>/connections/<provider>/callback. Unset
# means the provider is shown as not configured; the demo folder still works.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_TENANT = os.getenv("MICROSOFT_TENANT", "common")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
CLOUD_SYNC_MAX_FILES = int(os.getenv("CLOUD_SYNC_MAX_FILES", 200))
CLOUD_SYNC_LOOP_MINUTES = int(os.getenv("CLOUD_SYNC_LOOP_MINUTES", 5))

# OpenCorporates. Without a token the public directory falls back to the local
# register extract, and says so rather than pretending to be live.
OPENCORPORATES_TOKEN = os.getenv("OPENCORPORATES_TOKEN", "")
OPENCORPORATES_BASE = os.getenv("OPENCORPORATES_BASE",
                                "https://api.opencorporates.com/v0.4")

SEED_DEMO = os.getenv("SEED_DEMO", "1") == "1"
DEMO_EMAIL = os.getenv("DEMO_EMAIL", "demo@northgate.example")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "demo1234")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
