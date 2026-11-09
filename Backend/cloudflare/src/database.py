import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

SQLALCHEMY_DATABASE_URL = "sqlite:///./resume_builder.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def configure_worker_database(binding):
    """Configure SQLAlchemy to use the current Cloudflare D1 binding."""
    global engine, SessionLocal

    from sqlalchemy_cloudflare_d1 import create_engine_from_binding

    engine = create_engine_from_binding(binding)
    engine.dialect._json_serializer = json.dumps
    engine.dialect._json_deserializer = json.loads
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine