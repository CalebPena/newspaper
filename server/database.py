import os
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Edition(Base):
    __tablename__ = "editions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    printer_id: Mapped[str] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="scheduled", index=True)
    print_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    claim_token: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    object_key: Mapped[str] = mapped_column(String(200))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Printer(Base):
    __tablename__ = "printers"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    software_version: Mapped[str | None] = mapped_column(String(500))
    error: Mapped[str | None] = mapped_column(Text)


def connect_database():
    url = os.environ["DATABASE_URL"]
    if url.startswith(("postgres://", "postgresql://")):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    engine = create_engine(url, pool_pre_ping=True)
    return engine, sessionmaker(engine, expire_on_commit=False)
