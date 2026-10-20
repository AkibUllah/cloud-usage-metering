"""Engine/session helpers plus two type decorators that keep money and time exact and portable."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import Request
from sqlalchemy import DateTime, Numeric, String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite returns naive values)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Money(TypeDecorator[Decimal]):
    """Exact decimals everywhere.

    Postgres gets NUMERIC(24, 8). SQLite has no decimal type and would round-trip through float,
    so there the value is stored as its canonical string and parsed back into a Decimal.
    """

    impl = Numeric(24, 8)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(48))
        return dialect.type_descriptor(Numeric(24, 8))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = Decimal(str(value))
        return str(value) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(str(value))


def make_engine(database_url: str) -> Engine:
    kwargs: dict = {"future": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_pre_ping"] = True
    return create_engine(database_url, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    db: Session = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()
