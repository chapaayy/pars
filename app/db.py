from __future__ import annotations

import os
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, UniqueConstraint, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(80))
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # rss, telegram
    title: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(900))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(250))
    category: Mapped[str] = mapped_column(String(80), default="Другое")
    summary: Mapped[str] = mapped_column(Text, default="")
    relevance: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    items: Mapped[list[FeedItem]] = relationship(back_populates="topic")


class FeedItem(Base):
    __tablename__ = "feed_items"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_source_external"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"))
    external_id: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(1600), default="")
    digest: Mapped[str] = mapped_column(String(64), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    relevance: Mapped[int] = mapped_column(Integer, default=0)
    credibility: Mapped[int] = mapped_column(Integer, default=0)
    novelty: Mapped[int] = mapped_column(Integer, default=0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[Source | None] = relationship()
    topic: Mapped[Topic | None] = relationship(back_populates="items")


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"))
    title: Mapped[str] = mapped_column(String(250), default="")
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    telegram_message_id: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    topic: Mapped[Topic | None] = relationship()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


def make_engine():
    return create_engine(os.getenv("DATABASE_URL", "sqlite:////tmp/newsroom-dev.db"), pool_pre_ping=True)


engine = make_engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def initialize_database():
    Base.metadata.create_all(engine)
    # Compatibility with early installations before Source.archived was introduced.
    columns = {row["name"] for row in inspect(engine).get_columns("sources")}
    if "archived" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE sources ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE"))
