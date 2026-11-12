"""Lightweight, idempotent schema migrations for the SQLite database.

SQLAlchemy's ``create_all`` only creates *missing* tables, it never alters
existing ones.  These helpers add the columns introduced by the resume section
features to databases that were created before this change.

These are SQLite-only (they use ``PRAGMA``); they are skipped on other
backends such as the Postgres database used in production.
"""

from sqlalchemy import text

# Columns added to the `resumes` table.
RESUME_COLUMNS = [
    ("title", "VARCHAR(150)"),
    ("location", "VARCHAR(120)"),
    ("linkedin_url", "VARCHAR(255)"),
    ("github_url", "VARCHAR(255)"),
    ("portfolio_url", "VARCHAR(255)"),
]

# Columns added to the `experiences` table.
EXPERIENCE_COLUMNS = [
    ("location", "VARCHAR(120)"),
    ("start_date", "VARCHAR(20)"),
    ("end_date", "VARCHAR(20)"),
    ("is_current", "BOOLEAN NOT NULL DEFAULT 0"),
    ("description", "TEXT"),
    ("bullets", "JSON NOT NULL DEFAULT '[]'"),
    ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
]

# Tables -> new columns.
MIGRATIONS = {
    "resumes": RESUME_COLUMNS,
    "experiences": EXPERIENCE_COLUMNS,
}


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    ).fetchall()
    return {row[0] for row in rows}


def _column_names(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def run_migrations(engine) -> list[str]:
    """Add any missing columns. Returns the list of applied changes."""
    if engine.dialect.name != "sqlite":
        # Fresh Postgres databases get complete tables from create_all().
        return []

    applied: list[str] = []

    with engine.begin() as conn:
        tables = _table_names(conn)

        for table, columns in MIGRATIONS.items():
            if table not in tables:
                # Table does not exist yet -> create_all() will build it complete.
                continue

            existing = _column_names(conn, table)
            for name, ddl in columns:
                if name in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                applied.append(f"{table}.{name}")

        # Backfill JSON columns so they never contain NULL.
        if "experiences" in tables:
            conn.execute(
                text(
                    "UPDATE experiences SET bullets = '[]' "
                    "WHERE bullets IS NULL OR TRIM(bullets) = ''"
                )
            )

    return applied
