"""Intake, one bounded worker, stage events and persisted results (SPEC.md §13, §15).

The worker never touches the UI. The UI polls stored stage events. Each stage
event is written when the work actually happens. A provider failure ends the run
as FAILED with no business recommendation.
"""

from __future__ import annotations

import json
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import func, select

from src.config import Settings
from src.domain.duplicates import KnownBill
from src.domain.extraction import to_normalized_invoice
from src.domain.normalize import norm_identifier
from src.domain.pipeline import evaluate
from src.domain.policy import Policy
from src.domain.results import ExecutionState, Recommendation
from src.domain.snapshot import Snapshot, validate_snapshot
from src.persistence.db import (
    Document, InvoiceCase, Revision, Run, SnapshotRecord, StageEvent, Workspace, make_engine, make_sessionmaker, new_id, now,
)
from src.providers.base import Budget, OcrResult, ProviderError, ProviderErrorKind
from src.providers.schema import SCHEMA_VERSION, schema_hash

from .documents import InputRejected, validate_pdf
from .loaders import load_snapshot_raw

STAGES = [
    ("VALIDATE_FILE", "Validate file"),
    ("READ_DOCUMENT", "Read document (OCR)"),
    ("EXTRACT_FIELDS", "Extract fields"),
    ("VERIFY_EVIDENCE", "Verify evidence"),
    ("RESOLVE_PARTIES", "Resolve buyer and supplier"),
    ("MATCH_ORDER_AND_LINES", "Match order and lines"),
    ("CHECK_AMOUNTS_AND_RECEIPTS", "Check amounts and receipts"),
    ("SAVE_RESULT", "Save result"),
]
STAGE_LABEL = dict(STAGES)


class QueueBusy(RuntimeError):
    pass


ProviderFactory = Callable[[Settings], tuple[object, object]]  # -> (reader, extractor)


def openrouter_factory(settings: Settings):
    from src.providers.openrouter import OpenRouterClient, OpenRouterExtractor, OpenRouterReader
    client = OpenRouterClient(settings)
    return OpenRouterReader(client), OpenRouterExtractor(client)


@dataclass
class RunView:
    run: Run
    events: list[StageEvent]
    document: Document | None
    revision: Revision | None
    case: InvoiceCase | None


class Workbench:
    def __init__(self, settings: Settings, policy: Policy, provider_factory: ProviderFactory = openrouter_factory,
                 *, recover_on_start: bool = True):
        self.settings = settings
        self.policy = policy
        self.provider_factory = provider_factory
        self.engine = make_engine(settings.db_path)
        self.Session = make_sessionmaker(self.engine)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="invoice-worker")
        self._intake_lock = threading.Lock()
        # Only the app process recovers; helper scripts sharing the DB must not interrupt the app's live runs.
        self.interrupted_on_start = self._recover_interrupted() if recover_on_start else 0

    # ------------------------------------------------------------------ setup

    def _recover_interrupted(self) -> int:
        """Runs left QUEUED/RUNNING by a previous process are marked INTERRUPTED, never silently resumed."""
        with self.Session.begin() as s:
            stale = s.scalars(select(Run).where(Run.state.in_(["QUEUED", "RUNNING"]))).all()
            for r in stale:
                r.state = ExecutionState.INTERRUPTED.value
                r.finished_at = now()
                r.error_kind = "INTERRUPTED"
                r.error_message = "The service restarted before this run finished. Retry to process it again."
                self._event(s, r.id, "SAVE_RESULT", "FAILED", "Interrupted by service restart; no recommendation was produced.")
            return len(stale)

    def ensure_snapshot(self, snapshot_id: str) -> None:
        with self.Session.begin() as s:
            if s.get(SnapshotRecord, snapshot_id) is None:
                raw = load_snapshot_raw(snapshot_id)
                snap = validate_snapshot(raw, supported_currency=self.policy.supported_currency)
                s.add(SnapshotRecord(id=snap.snapshot_id, version=snap.version, source=snap.source, as_of=snap.as_of,
                                     content_hash=snap.content_hash, content=raw))

    def snapshot(self, snapshot_id: str) -> Snapshot:
        with self.Session() as s:
            rec = s.get(SnapshotRecord, snapshot_id)
            return validate_snapshot(rec.content, supported_currency=self.policy.supported_currency)

    def create_workspace(self, label: str, snapshot_id: str = "S1") -> str:
        self.ensure_snapshot(snapshot_id)
        wid = new_id("ws")
        with self.Session.begin() as s:
            s.add(Workspace(id=wid, label=label, active_snapshot_id=snapshot_id))
        return wid

    def workspace(self, workspace_id: str) -> Workspace | None:
        with self.Session() as s:
            return s.get(Workspace, workspace_id)

    def list_workspaces(self) -> list[Workspace]:
        with self.Session() as s:
            return list(s.scalars(select(Workspace).order_by(Workspace.created_at.desc())))

    # ------------------------------------------------------------------ events

    @staticmethod
    def _event(s, run_id: str, stage: str, status: str, message: str = "") -> None:
        seq = (s.scalar(select(func.max(StageEvent.seq)).where(StageEvent.run_id == run_id)) or 0) + 1
        s.add(StageEvent(run_id=run_id, seq=seq, stage=stage, status=status, message=message, at=now()))

    def emit(self, run_id: str, stage: str, status: str, message: str = "") -> None:
        with self.Session.begin() as s:
            self._event(s, run_id, stage, status, message)

    # ------------------------------------------------------------------ intake

    def submit(self, workspace_id: str, filename: str, data: bytes, token: str) -> str:
        """Idempotent on token. Returns a run ID. Raises QueueBusy when the queue is full."""
        safe_name = re.sub(r"[^\w.\- ]", "_", filename)[:120] or "upload.pdf"
        with self._intake_lock:
            with self.Session.begin() as s:
                existing = s.scalar(select(Run).where(Run.submission_token == token))
                if existing:
                    return existing.id
                run_id = new_id("run")
                run = Run(id=run_id, workspace_id=workspace_id, submission_token=token, filename=safe_name,
                          state="RUNNING", created_at=now(), started_at=now())
                s.add(run)
                s.flush()
                self._event(s, run_id, "VALIDATE_FILE", "STARTED")
                try:
                    meta = validate_pdf(data, max_bytes=self.settings.max_upload_bytes, max_pages=self.settings.max_pages)
                except InputRejected as exc:
                    run.state, run.error_kind, run.error_message, run.finished_at = "FAILED", "INPUT", str(exc), now()
                    self._event(s, run_id, "VALIDATE_FILE", "FAILED", f"{exc} No model call was made.")
                    return run_id

                doc = s.scalar(select(Document).where(Document.workspace_id == workspace_id, Document.sha256 == meta.sha256))
                if doc is not None:
                    prior = s.scalar(select(Run).where(Run.document_id == doc.id, Run.state == "COMPLETED",
                                                       Run.reused_from_run_id.is_(None)).order_by(Run.created_at))
                    if prior is not None:
                        run.document_id = doc.id
                        run.state, run.finished_at = "COMPLETED", now()
                        run.reused_from_run_id, run.case_id, run.revision_id = prior.id, prior.case_id, prior.revision_id
                        run.attempt_kind = "EXACT_REPEAT"
                        self._event(s, run_id, "VALIDATE_FILE", "COMPLETED",
                                    f"{meta.page_count} page(s), {meta.size_bytes:,} bytes, SHA-256 {meta.sha256[:12]}")
                        self._event(s, run_id, "SAVE_RESULT", "INFO",
                                    f"Identical file already processed in run {prior.id}. Existing result reused; no model calls.")
                        return run_id
                else:
                    path = self.settings.data_dir / "documents" / workspace_id / f"{meta.sha256}.pdf"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not path.exists():
                        path.write_bytes(data)
                    doc = Document(id=new_id("doc"), workspace_id=workspace_id, sha256=meta.sha256, filename=safe_name,
                                   path=str(path), size_bytes=meta.size_bytes, page_count=meta.page_count)
                    s.add(doc)
                    s.flush()
                run.document_id = doc.id
                self._event(s, run_id, "VALIDATE_FILE", "COMPLETED",
                            f"{meta.page_count} page(s), {meta.size_bytes:,} bytes, SHA-256 {meta.sha256[:12]}")

                active = s.scalar(select(func.count()).select_from(Run).where(Run.state.in_(["QUEUED", "RUNNING"]),
                                                                              Run.id != run_id)) or 0
                if active >= self.settings.queue_limit:
                    run.state, run.error_kind, run.finished_at = "FAILED", "BUSY", now()
                    run.error_message = f"{active} runs are already queued; please retry shortly."
                    self._event(s, run_id, "READ_DOCUMENT", "SKIPPED", run.error_message)
                    raise QueueBusy(run.error_message)
                run.state = "QUEUED"
            self.executor.submit(self._execute_safely, run_id)
            return run_id

    def retry(self, run_id: str, token: str) -> str:
        with self.Session() as s:
            old = s.get(Run, run_id)
            doc = s.get(Document, old.document_id) if old.document_id else None
        if doc is None:
            raise InputRejected("The original file was not stored, so this run cannot be retried. Upload it again.")
        new_run = self.submit(old.workspace_id, old.filename, Path(doc.path).read_bytes(), token)
        with self.Session.begin() as s:
            s.get(Run, new_run).retry_of_run_id = run_id
        return new_run

    # ------------------------------------------------------------------ execution

    def _execute_safely(self, run_id: str) -> None:
        try:
            self.execute(run_id)
        except Exception as exc:  # last-resort guard: record truthfully, never leave RUNNING
            self._fail(run_id, "INTERNAL", f"{type(exc).__name__}: {exc}", "SAVE_RESULT")
            traceback.print_exc()

    def _fail(self, run_id: str, kind: str, message: str, stage: str) -> None:
        with self.Session.begin() as s:
            run = s.get(Run, run_id)
            run.state, run.error_kind, run.error_message, run.finished_at = "FAILED", kind, message, now()
            self._event(s, run_id, stage, "FAILED", f"{message} No recommendation was produced.")

    def execute(self, run_id: str) -> None:
        with self.Session.begin() as s:
            run = s.get(Run, run_id)
            run.state, run.started_at = "RUNNING", now()
            ws = s.get(Workspace, run.workspace_id)
            doc = s.get(Document, run.document_id)
            remaining_calls = self.settings.provider_call_cap_per_workspace - ws.provider_calls_used
            snapshot_id, ws_id = ws.active_snapshot_id, ws.id
            doc_id, doc_path, doc_sha, filename = doc.id, Path(doc.path), doc.sha256, doc.filename

        budget = Budget(deadline_s=self.settings.run_deadline_s, call_cap=max(0, remaining_calls))
        stage = "READ_DOCUMENT"
        try:
            reader, extractor = self.provider_factory(self.settings)

            # ---- OCR (reused only for the same file hash, engine and carrier model)
            self.emit(run_id, stage, "STARTED")
            art_dir = self.settings.data_dir / "artifacts" / doc_sha
            art_dir.mkdir(parents=True, exist_ok=True)
            ocr_key = re.sub(r"[^\w.-]", "_", f"ocr-{getattr(self.settings, 'ocr_engine', 'x')}-{getattr(self.settings, 'ocr_carrier_model', 'x')}")
            ocr_path = art_dir / f"{ocr_key}.json"
            if ocr_path.exists():
                ocr = OcrResult.model_validate_json(ocr_path.read_text()).model_copy(update={"reused": True})
                self.emit(run_id, stage, "COMPLETED", f"Reused OCR saved from an earlier attempt on this identical file "
                                                      f"({ocr.engine} via {ocr.carrier_model}); no OCR call made.")
            else:
                ocr, ocr_meta = reader.read_document(doc_path.read_bytes(), filename, budget,
                                                     on_event=lambda m: self.emit(run_id, stage, "INFO", m))
                ocr_path.write_text(ocr.model_dump_json(indent=2))
                (art_dir / f"{ocr_key}.meta.json").write_text(json.dumps(ocr_meta, indent=2, default=str))
                self.emit(run_id, stage, "COMPLETED", f"{len(ocr.pages)} page(s) read with {ocr.engine}; {ocr.calls} call(s)")

            # ---- extraction
            stage = "EXTRACT_FIELDS"
            self.emit(run_id, stage, "STARTED")
            extracted, ext_meta = extractor.extract_invoice(ocr, budget)
            (art_dir / f"extract-{run_id}.json").write_text(json.dumps({"output": extracted, "meta": ext_meta}, indent=2, default=str))
            self.emit(run_id, stage, "COMPLETED",
                      f"{len(extracted.get('lines') or [])} line(s) extracted by {extractor.model_id} (attempt {ext_meta.get('attempt', 1)})")
        except ProviderError as exc:
            self._account_calls(ws_id, run_id, budget.calls_used)
            self._fail(run_id, exc.kind.value, exc.message, stage)
            return
        self._account_calls(ws_id, run_id, budget.calls_used)

        # ---- evidence
        stage = "VERIFY_EVIDENCE"
        self.emit(run_id, stage, "STARTED")
        blocks = {b.block_id: (p.page, b.text) for p in ocr.pages for b in p.blocks}
        inv, report = to_normalized_invoice(extracted, blocks, input_label=f"{filename} (uploaded PDF)")
        self.emit(run_id, stage, "COMPLETED",
                  f"{report.supported} value(s) found in OCR text; {len(report.reanchored)} re-anchored; "
                  f"{len(report.unsupported)} unsupported -> unknown; {len(report.parse_failures)} unparseable -> unknown")

        # ---- deterministic decision
        snap = self.snapshot(snapshot_id)
        known = self._known_bills(ws_id)
        current = {"stage": None}

        def on_stage(name: str) -> None:
            if current["stage"]:
                self.emit(run_id, current["stage"], "COMPLETED")
            current["stage"] = name
            self.emit(run_id, name, "STARTED")

        revision_id = new_id("rev")
        result = evaluate(inv, snap, self.policy, evaluated_at=datetime.now(timezone.utc), known_bills=known,
                          revision_id=revision_id, on_stage=on_stage)
        if current["stage"]:
            self.emit(run_id, current["stage"], "COMPLETED")

        # ---- persist: one short transaction for revision, case and run state
        with self.Session.begin() as s:
            self._event(s, run_id, "SAVE_RESULT", "STARTED")
            run = s.get(Run, run_id)
            case, attempt_kind = self._case_for(s, ws_id, doc_id, result, inv)
            provenance = {
                "ocr": {"provider": ocr.provider, "engine": ocr.engine, "carrier_model": ocr.carrier_model, "reused": ocr.reused},
                "extraction": {"model": extractor.model_id, "served_by": ext_meta.get("model"), "provider": ext_meta.get("provider"),
                               "prompt_version": extractor.prompt_version, "prompt_hash": getattr(extractor, "prompt_hash", None),
                               "schema": SCHEMA_VERSION, "schema_hash": schema_hash(), "attempt": ext_meta.get("attempt")},
                "evidence": {"supported": report.supported, "reanchored": report.reanchored, "unsupported": report.unsupported,
                             "parse_failures": report.parse_failures},
            }
            s.add(Revision(
                id=revision_id, workspace_id=ws_id, case_id=case.id if case else None, run_id=run_id, document_id=doc_id,
                kind="ORIGINAL", snapshot_id=result.snapshot_id, snapshot_hash=result.snapshot_hash,
                policy_version=result.policy_version, provenance=provenance, invoice=json.loads(inv.model_dump_json()),
                decision=json.loads(result.model_dump_json()), recommendation=result.recommendation.value,
                primary_exception=result.primary_exception, is_structured_input=False,
            ))
            if case is not None and attempt_kind == "NEW_BILL":
                case.current_revision_id = revision_id
            run.case_id = case.id if case else None
            run.revision_id, run.attempt_kind = revision_id, attempt_kind
            run.state, run.finished_at = "COMPLETED", now()
            self._event(s, run_id, "SAVE_RESULT", "COMPLETED",
                        f"{result.recommendation.value.replace('_', ' ').title()}; revision {revision_id}; {attempt_kind.replace('_', ' ').lower()}")

    def _account_calls(self, ws_id: str, run_id: str, calls: int) -> None:
        with self.Session.begin() as s:
            s.get(Workspace, ws_id).provider_calls_used += calls
            s.get(Run, run_id).provider_calls += calls

    def _known_bills(self, ws_id: str) -> list[KnownBill]:
        from decimal import Decimal
        with self.Session() as s:
            cases = s.scalars(select(InvoiceCase).where(InvoiceCase.workspace_id == ws_id)).all()
            return [KnownBill(c.id, c.buyer_id, c.supplier_id, c.invoice_number,
                              Decimal(c.gross_amount) if c.gross_amount else None,
                              f"workbench bill {c.invoice_number} (first seen {c.created_at:%Y-%m-%d %H:%M} UTC)") for c in cases]

    @staticmethod
    def _case_for(s, ws_id: str, doc_id: str, result, inv) -> tuple[InvoiceCase | None, str]:
        dup = next((c for c in result.checks if c.code == "DUPLICATE"), None)
        if not (result.buyer_match.resolved and result.supplier_match.resolved and inv.invoice_number.value):
            return None, "UNRESOLVED"
        key = norm_identifier(inv.invoice_number.value)
        existing = s.scalar(select(InvoiceCase).where(
            InvoiceCase.workspace_id == ws_id, InvoiceCase.buyer_id == result.buyer_match.selected_id,
            InvoiceCase.supplier_id == result.supplier_match.selected_id, InvoiceCase.invoice_key == key))
        if existing is not None:
            return existing, "DUPLICATE_ATTEMPT"
        if dup is not None and dup.issue_code in ("DUPLICATE_BILL", "INVOICE_IDENTITY_CONFLICT", "SUSPECTED_DUPLICATE"):
            return None, "DUPLICATE_ATTEMPT"  # matches a bill in the snapshot ledger; no new payable record
        case = InvoiceCase(id=new_id("case"), workspace_id=ws_id, buyer_id=result.buyer_match.selected_id,
                           supplier_id=result.supplier_match.selected_id, invoice_key=key, invoice_number=inv.invoice_number.value,
                           gross_amount=str(inv.total.value) if inv.total.value is not None else None, first_document_id=doc_id)
        s.add(case)
        s.flush()
        return case, "NEW_BILL"

    # ------------------------------------------------------------------ reads

    def run_view(self, run_id: str) -> RunView | None:
        with self.Session() as s:
            run = s.get(Run, run_id)
            if run is None:
                return None
            events = list(s.scalars(select(StageEvent).where(StageEvent.run_id == run_id).order_by(StageEvent.seq)))
            doc = s.get(Document, run.document_id) if run.document_id else None
            rev = s.get(Revision, run.revision_id) if run.revision_id else None
            case = s.get(InvoiceCase, run.case_id) if run.case_id else None
            return RunView(run, events, doc, rev, case)

    def runs(self, workspace_id: str) -> list[Run]:
        with self.Session() as s:
            return list(s.scalars(select(Run).where(Run.workspace_id == workspace_id).order_by(Run.created_at.desc())))

    def revision(self, revision_id: str) -> Revision | None:
        with self.Session() as s:
            return s.get(Revision, revision_id)

    def counts(self, workspace_id: str) -> dict:
        with self.Session() as s:
            def c(model, *where):
                return s.scalar(select(func.count()).select_from(model).where(*where)) or 0
            return {
                "unique_bills": c(InvoiceCase, InvoiceCase.workspace_id == workspace_id),
                "attempts": c(Run, Run.workspace_id == workspace_id),
                "revisions": c(Revision, Revision.workspace_id == workspace_id),
                "documents": c(Document, Document.workspace_id == workspace_id),
            }

    def wait_idle(self, timeout: float = 300) -> None:
        """Test helper: block until the worker has drained."""
        self.executor.submit(lambda: None).result(timeout=timeout)
