"""Business-key duplicate classification (SPEC.md §6, §13).

Key = confirmed buyer + confirmed supplier + conservatively normalized invoice
number. Exact-byte repeats are handled earlier by document hash (Phase 2+);
this module classifies re-rendered or re-typed bills.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .money import money
from .normalize import loose_identifier, norm_identifier
from .snapshot import Snapshot


class DuplicateKind(StrEnum):
    NONE = "NONE"
    DUPLICATE_BILL = "DUPLICATE_BILL"
    INVOICE_IDENTITY_CONFLICT = "INVOICE_IDENTITY_CONFLICT"
    SUSPECTED_DUPLICATE = "SUSPECTED_DUPLICATE"


@dataclass(frozen=True)
class KnownBill:
    bill_id: str
    buyer_id: str
    supplier_id: str
    invoice_number: str
    gross_amount: Decimal | None
    source: str  # e.g. "snapshot S1 ledger (POSTED, UNPAID)" or "workbench case"


@dataclass(frozen=True)
class DuplicateResult:
    kind: DuplicateKind
    match: KnownBill | None = None


def bills_from_snapshot(snap: Snapshot) -> list[KnownBill]:
    return [
        KnownBill(b.bill_id, b.buyer_id, b.supplier_id, b.invoice_number, b.gross_amount,
                  f"snapshot {snap.snapshot_id} ledger ({b.state}, {b.payment_status})")
        for b in snap.prior_invoices
    ]


def classify(buyer_id: str, supplier_id: str, invoice_number: str, gross: Decimal | None,
             known: list[KnownBill]) -> DuplicateResult:
    scoped = [k for k in known if k.buyer_id == buyer_id and k.supplier_id == supplier_id]
    key = norm_identifier(invoice_number)
    for k in scoped:
        if norm_identifier(k.invoice_number) == key:
            same_amount = gross is not None and k.gross_amount is not None and money(gross) == money(k.gross_amount)
            return DuplicateResult(DuplicateKind.DUPLICATE_BILL if same_amount else DuplicateKind.INVOICE_IDENTITY_CONFLICT, k)
    loose = loose_identifier(invoice_number)
    for k in scoped:
        if loose and loose_identifier(k.invoice_number) == loose:
            return DuplicateResult(DuplicateKind.SUSPECTED_DUPLICATE, k)
    return DuplicateResult(DuplicateKind.NONE)
