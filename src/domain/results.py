"""Match, check and decision contracts (SPEC.md §7, §12).

Execution state, per-category check status and the overall recommendation are
kept as separate fields so a technical failure can never read as a business
outcome and vice versa.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MatchStatus(StrEnum):
    EXACT = "EXACT"
    CURATED_ALIAS = "CURATED_ALIAS"
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    AMBIGUOUS = "AMBIGUOUS"
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"


RESOLVED = {MatchStatus.EXACT, MatchStatus.CURATED_ALIAS, MatchStatus.HUMAN_CONFIRMED}


class Candidate(_Model):
    candidate_id: str
    label: str
    reasons: list[str] = []


class MatchResult(_Model):
    status: MatchStatus
    selected_id: str | None = None
    basis: str = ""
    issue_code: str | None = None
    candidates: list[Candidate] = []
    more_candidates: int = 0
    source_version: str = ""
    evidence: list[str] = []

    @property
    def resolved(self) -> bool:
        return self.status in RESOLVED and self.selected_id is not None and self.issue_code is None


class LineMatch(_Model):
    source_line_id: str
    status: MatchStatus
    po_number: str | None = None
    po_line_no: int | None = None
    sku: str | None = None
    method: str = ""
    issue_code: str | None = None
    detail: str = ""
    invoice_unit: str | None = None
    base_unit: str | None = None
    conversion_factor: Decimal | None = None
    conversion_source: str | None = None
    base_quantity: Decimal | None = None
    candidates: list[Candidate] = []

    @property
    def resolved(self) -> bool:
        return self.status in RESOLVED and self.po_line_no is not None and self.issue_code is None


class Outcome(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"
    NOT_RUN = "NOT_RUN"
    NOT_REQUIRED = "NOT_REQUIRED"


class Category(StrEnum):
    COMMERCIAL = "COMMERCIAL"
    RECEIVING = "RECEIVING"


class Owner(StrEnum):
    AP = "Accounts payable"
    PROCUREMENT = "Procurement"
    STORES = "Stores"
    QUALITY = "Quality"
    FINANCE = "Finance"


class CheckResult(_Model):
    code: str
    category: Category
    outcome: Outcome
    subject: str | None = None
    issue_code: str | None = None
    expected: str | None = None
    observed: str | None = None
    difference: str | None = None
    reason: str
    evidence_refs: list[str] = []
    owner: Owner | None = None
    next_action: str | None = None
    dependency_codes: list[str] = []


class ExecutionState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class CheckStatus(StrEnum):
    MATCHED = "MATCHED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"
    NOT_REQUIRED = "NOT_REQUIRED"


class Recommendation(StrEnum):
    READY_FOR_FINANCE_REVIEW = "READY_FOR_FINANCE_REVIEW"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"


class POLineContext(_Model):
    po_number: str
    line_no: int
    sku: str
    unit: str
    match_policy: str
    net_unit_price: Decimal
    ordered_qty: Decimal
    prior_billed_qty: Decimal
    remaining_ordered_qty: Decimal
    received_qty: Decimal
    accepted_qty: Decimal | None
    receipt_available_qty: Decimal
    receipt_refs: list[str]
    prior_bill_refs: list[str]
    invoice_qty: Decimal


class PendingExposure(_Model):
    bill_id: str
    invoice_number: str
    gross_amount: Decimal
    note: str


class POContext(_Model):
    po_number: str
    po_version: int
    po_date: date
    status: str
    gross_cap: Decimal
    prior_gross: Decimal
    remaining_gross: Decimal
    invoice_gross: Decimal | None
    lines: list[POLineContext]
    pending: list[PendingExposure] = []


class DecisionResult(_Model):
    execution_state: ExecutionState
    commercial_status: CheckStatus
    receiving_status: CheckStatus
    recommendation: Recommendation
    headline: str
    primary_exception: str | None
    next_action: str | None
    next_owner: Owner | None
    checks: list[CheckResult]
    buyer_match: MatchResult
    supplier_match: MatchResult
    po_match: MatchResult | None
    line_matches: list[LineMatch]
    po_context: POContext | None
    evaluated_at: datetime
    snapshot_id: str
    snapshot_version: int
    snapshot_as_of: datetime
    snapshot_hash: str
    policy_version: str
    revision_id: str | None
    input_source: str
    input_label: str
    scope_limits: list[str]
