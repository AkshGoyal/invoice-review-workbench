"""SQLite schema via SQLAlchemy 2 (SPEC.md §13).

WAL mode, foreign keys and a busy timeout; one short session per operation.
Revisions, documents and stage events are append-only; only run state and the
case's current-revision pointer are updated in place.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspace"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String)
    active_snapshot_id: Mapped[str] = mapped_column(String)
    provider_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SnapshotRecord(Base):
    __tablename__ = "procurement_snapshot"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # e.g. S1
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String)
    content: Mapped[dict] = mapped_column(JSON)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (UniqueConstraint("workspace_id", "sha256"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"))
    sha256: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String)  # display only; never used as a path
    path: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base):
    __tablename__ = "run"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id"), nullable=True)
    submission_token: Mapped[str] = mapped_column(String, unique=True)
    filename: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)  # QUEUED RUNNING COMPLETED FAILED INTERRUPTED
    reused_from_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    retry_of_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("invoice_case.id"), nullable=True)
    revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_kind: Mapped[str | None] = mapped_column(String, nullable=True)  # NEW_BILL / DUPLICATE_ATTEMPT / UNRESOLVED
    error_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_calls: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StageEvent(Base):
    __tablename__ = "stage_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"))
    seq: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)  # STARTED COMPLETED FAILED SKIPPED INFO
    message: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class InvoiceCase(Base):
    """One unique bill: resolved buyer + supplier + normalized invoice number within a workspace."""
    __tablename__ = "invoice_case"
    __table_args__ = (UniqueConstraint("workspace_id", "buyer_id", "supplier_id", "invoice_key"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"))
    buyer_id: Mapped[str] = mapped_column(String)
    supplier_id: Mapped[str] = mapped_column(String)
    invoice_key: Mapped[str] = mapped_column(String)
    invoice_number: Mapped[str] = mapped_column(String)
    gross_amount: Mapped[str | None] = mapped_column(String, nullable=True)  # Decimal string
    first_document_id: Mapped[str] = mapped_column(ForeignKey("document.id"))
    current_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Revision(Base):
    """Immutable record of one evaluation: inputs, provenance and decision."""
    __tablename__ = "revision"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("invoice_case.id"), nullable=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"))
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id"))
    parent_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String)  # ORIGINAL (Phase 3 adds CORRECTION / RECHECK)
    snapshot_id: Mapped[str] = mapped_column(String)
    snapshot_hash: Mapped[str] = mapped_column(String)
    policy_version: Mapped[str] = mapped_column(String)
    provenance: Mapped[dict] = mapped_column(JSON)  # models, prompt/schema hashes, OCR reuse
    invoice: Mapped[dict] = mapped_column(JSON)
    decision: Mapped[dict] = mapped_column(JSON)
    recommendation: Mapped[str] = mapped_column(String)
    primary_exception: Mapped[str | None] = mapped_column(String, nullable=True)
    is_structured_input: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 15, "check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(engine)
    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
