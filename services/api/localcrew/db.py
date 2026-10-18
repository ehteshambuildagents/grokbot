import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=10)


@lru_cache
def SessionLocal():
    return sessionmaker(engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Session:
    s = SessionLocal()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db():
    s = SessionLocal()()
    try:
        yield s
    finally:
        s.close()
