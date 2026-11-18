"""Database engine and session helpers."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


@lru_cache(maxsize=4)
def engine_for(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, future=True)


def get_engine() -> Engine:
    return engine_for(get_settings().database_url)


@lru_cache(maxsize=4)
def _factory(url: str) -> sessionmaker:
    return sessionmaker(bind=engine_for(url), expire_on_commit=False, future=True)


def session_factory() -> sessionmaker:
    return _factory(get_settings().database_url)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
