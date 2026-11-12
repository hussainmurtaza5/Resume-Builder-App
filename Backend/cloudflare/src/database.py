"""Database engine/session configuration.

Local development and plain ``uvicorn`` runs use a SQLite file next to this
package. Serverless deployments (Vercel) should set ``DATABASE_URL`` to a
hosted Postgres connection string, because the function filesystem is
read-only apart from a per-instance ``/tmp``.
"""

import json
import logging
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE_PATH = BACKEND_ROOT / "resume_builder.db"
VERCEL_SQLITE_PATH = "/tmp/resume_builder.db"


def _database_url() -> str:
    """Resolve the SQLAlchemy URL from DATABASE_URL or a local SQLite file."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        # Managed Postgres providers hand out ``postgres://`` URLs, which
        # SQLAlchemy no longer accepts and cannot route to the psycopg driver.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    if os.getenv("VERCEL"):
        logger.warning(
            "DATABASE_URL is not set; falling back to a temporary SQLite file in "
            "/tmp. Data will not survive a cold start. Set DATABASE_URL to a "
            "Postgres URL for persistent storage."
        )
        return f"sqlite:///{VERCEL_SQLITE_PATH}"

    return f"sqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"


SQLALCHEMY_DATABASE_URL = _database_url()


def _create_engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    # Serverless invocations are short lived, so pooled connections would sit
    # idle and be closed by the provider (Neon, PgBouncer) between requests.
    return create_engine(url, poolclass=NullPool)


engine = _create_engine(SQLALCHEMY_DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def configure_worker_database(binding):
    """Configure SQLAlchemy to use the current Cloudflare D1 binding."""
    global engine, SessionLocal

    # Cloudflare-only dependency, imported lazily so the app can run elsewhere.
    from sqlalchemy_cloudflare_d1 import create_engine_from_binding

    engine = create_engine_from_binding(binding)
    engine.dialect._json_serializer = json.dumps
    engine.dialect._json_deserializer = json.loads
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine
