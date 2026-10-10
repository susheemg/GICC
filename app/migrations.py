"""Schema migrations.

Not Alembic. Alembic needs a migrations directory and a CLI, and this
application ships as a single file as well as a package — a versions/ folder
cannot travel inside one .py. What is needed in practice is narrower than what
Alembic does: `create_all` already handles new tables, so the gap is new
*columns* on tables that already hold data.

This runner closes exactly that gap. It compares the ORM metadata against the
live schema and issues additive `ALTER TABLE ... ADD COLUMN` for anything
missing, then records the applied version.

What it deliberately does not do: drop columns, change types, rename, or
backfill by inference. Those are destructive or ambiguous, and doing them
silently is worse than refusing. Each is a named step in MANUAL_STEPS, applied
once and recorded, so a change that needs judgement gets judgement.
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .db import Base

SCHEMA_VERSION = 6

# Types that SQLite accepts in ADD COLUMN. Postgres is more permissive; the
# generated DDL is valid on both.
SQLITE_DEFAULTS = {
    "VARCHAR": "''", "TEXT": "''", "INTEGER": "0", "FLOAT": "0",
    "BOOLEAN": "0", "DATE": "NULL", "DATETIME": "NULL",
}


def _column_ddl(column) -> str:
    """A column added to a populated table cannot be NOT NULL without a
    default, so added columns are nullable regardless of the model. The model
    still enforces the constraint on new rows through the ORM."""
    type_sql = column.type.compile()
    default = ""
    if column.default is not None and getattr(column.default, "is_scalar", False):
        value = column.default.arg
        if isinstance(value, bool):
            default = f" DEFAULT {1 if value else 0}"
        elif isinstance(value, (int, float)):
            default = f" DEFAULT {value}"
        elif isinstance(value, str):
            default = f" DEFAULT '{value}'"
    return f"{column.name} {type_sql}{default}"


def pending_columns(engine: Engine) -> list[tuple[str, str]]:
    """Returns (table, ddl) for every column in the model but not the database."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    pending = []
    for name, table in Base.metadata.tables.items():
        if name not in existing_tables:
            continue                      # create_all will make it whole
        live = {c["name"] for c in inspector.get_columns(name)}
        for column in table.columns:
            if column.name not in live:
                pending.append((name, _column_ddl(column)))
    return pending


def _applied_version(engine: Engine) -> int:
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  id INTEGER PRIMARY KEY, version INTEGER NOT NULL,"
            "  applied_at TEXT NOT NULL)"))
        row = connection.execute(text(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")).first()
    return row[0] if row else 0


def _record_version(engine: Engine, version: int) -> None:
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO schema_version (version, applied_at) VALUES (:v, :a)"),
            {"v": version, "a": stamp})


def run(engine: Engine, *, verbose: bool = True) -> dict:
    """Applies additive changes. Safe to run on every start: it is a no-op when
    the schema already matches."""
    from_version = _applied_version(engine)
    pending = pending_columns(engine)

    applied, failed = [], []
    for table_name, ddl in pending:
        statement = f"ALTER TABLE {table_name} ADD COLUMN {ddl}"
        try:
            with engine.begin() as connection:
                connection.execute(text(statement))
            applied.append(statement)
        except Exception as exc:
            # A failure here must be loud and must not stop the application from
            # starting on the columns it does have.
            failed.append(f"{statement} :: {type(exc).__name__}: {exc}")

    for step in MANUAL_STEPS:
        if step["version"] > from_version:
            try:
                with engine.begin() as connection:
                    for statement in step["statements"]:
                        connection.execute(text(statement))
                applied.append(f"step {step['version']}: {step['name']}")
            except Exception as exc:
                failed.append(f"step {step['version']} {step['name']} :: {exc}")

    if SCHEMA_VERSION > from_version:
        _record_version(engine, SCHEMA_VERSION)

    result = {"from_version": from_version, "to_version": SCHEMA_VERSION,
              "columns_added": len([a for a in applied if a.startswith("ALTER")]),
              "steps_applied": len([a for a in applied if a.startswith("step")]),
              "failed": failed}
    if verbose and (applied or failed):
        print(f"[migrate] {result['columns_added']} column(s), "
              f"{result['steps_applied']} step(s), {len(failed)} failure(s)", flush=True)
        for line in failed:
            print(f"[migrate] FAILED {line}", flush=True)
    return result


# Data changes that need judgement rather than inference. Applied once, in
# order, recorded by version.
MANUAL_STEPS = [
    {
        "version": 2,
        "name": "existing organisations are claimed by their own registration",
        "statements": [
            "UPDATE organisation SET claim_status = 'claimed' "
            "  WHERE claim_status IS NULL OR claim_status = ''",
            "UPDATE organisation SET source = 'registration' "
            "  WHERE source IS NULL OR source = ''",
        ],
    },
    {
        "version": 4,
        "name": "every existing workspace is a supplier until it says otherwise",
        "statements": [
            "UPDATE tenant SET kind = 'vendor' WHERE kind IS NULL OR kind = ''",
            "UPDATE access_request SET origin = 'mcp' WHERE origin IS NULL OR origin = ''",
        ],
    },
    {
        "version": 3,
        "name": "every existing document type is global and single-instance",
        "statements": [
            "UPDATE document_type SET scope = 'global' "
            "  WHERE scope IS NULL OR scope = ''",
            "UPDATE document_type SET multi_instance = 0 WHERE multi_instance IS NULL",
            "UPDATE document_type SET requires_asset = 0 WHERE requires_asset IS NULL",
        ],
    },
]
