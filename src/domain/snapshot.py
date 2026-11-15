"""Typed, versioned, read-only procurement snapshot (SPEC.md §2, §13).

The snapshot carries upstream facts the app consumes but never edits: parties,
aliases, items, supplier item mappings, unit conversions, PO headers/lines,
receipts (with acceptance where applicable), the prior-invoice ledger and
labelled supporting references. ``validate_snapshot`` returns row-level errors
for inconsistent data instead of silently repairing it.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from .money import ZERO, money
from .normalize import norm_identifier, norm_text


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PartyStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    BLOCKED = "BLOCKED"


class MatchPolicy(StrEnum):
    PO_ONLY = "PO_ONLY"
    RECEIPT_REQUIRED = "RECEIPT_REQUIRED"
    ACCEPTANCE_REQUIRED = "ACCEPTANCE_REQUIRED"


class POStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class BillState(StrEnum):
    POSTED = "POSTED"  # recorded in the AP ledger, paid or unpaid
    DRAFT = "DRAFT"  # pending exposure only; not counted as billed


class PaymentStatus(StrEnum):
    PAID = "PAID"
    UNPAID = "UNPAID"


class Party(_Frozen):
    party_id: str
    legal_name: str
    registration_id: str | None = None
    status: PartyStatus = PartyStatus.ACTIVE
    address: str | None = None


class PartyAlias(_Frozen):
    party_id: str
    alias: str
    approved: bool = True
    source: str


class Item(_Frozen):
    sku: str
    description: str
    specifications: dict[str, str] = {}
    base_unit: str


class MappingKind(StrEnum):
    CODE = "CODE"
    DESCRIPTION = "DESCRIPTION"


class SupplierItemMapping(_Frozen):
    mapping_id: str
    supplier_id: str
    kind: MappingKind
    value: str
    sku: str
    version: int = 1


class UnitConversion(_Frozen):
    sku: str
    from_unit: str
    to_unit: str
    factor: Decimal
    source: str


class POHeader(_Frozen):
    po_number: str
    version: int
    buyer_id: str
    supplier_id: str
    currency: str
    po_date: date
    status: POStatus
    gross_cap: Decimal


class POLine(_Frozen):
    po_number: str
    line_no: int
    sku: str
    specifications: dict[str, str] = {}
    unit: str
    quantity: Decimal
    net_unit_price: Decimal
    tax_rate_pct: Decimal
    match_policy: MatchPolicy


class ReceiptLine(_Frozen):
    receipt_id: str
    po_number: str
    line_no: int
    receipt_date: date
    received_qty: Decimal  # net posted (returns already reflected upstream)
    accepted_qty: Decimal | None = None


class PriorInvoice(_Frozen):
    bill_id: str
    buyer_id: str
    supplier_id: str
    invoice_number: str
    invoice_date: date
    state: BillState
    payment_status: PaymentStatus
    net_amount: Decimal
    tax_amount: Decimal
    gross_amount: Decimal


class PriorInvoiceLine(_Frozen):
    bill_id: str
    po_number: str
    line_no: int
    quantity: Decimal
    net_amount: Decimal
    gross_amount: Decimal


class SupportingReference(_Frozen):
    ref_id: str
    kind: str
    summary: str
    links: dict[str, str] = {}
    label: str = "Fictional supporting note for demo"


class Coverage(_Frozen):
    receipts_complete: bool
    acceptance_complete: bool
    prior_invoices_complete: bool


class Snapshot(_Frozen):
    snapshot_id: str
    version: int
    source: str
    label: str
    as_of: datetime
    coverage: Coverage
    buyers: list[Party]
    suppliers: list[Party]
    party_aliases: list[PartyAlias]
    items: list[Item]
    supplier_item_mappings: list[SupplierItemMapping]
    unit_conversions: list[UnitConversion]
    po_headers: list[POHeader]
    po_lines: list[POLine]
    receipt_lines: list[ReceiptLine]
    prior_invoices: list[PriorInvoice]
    prior_invoice_lines: list[PriorInvoiceLine]
    supporting_references: list[SupportingReference]
    content_hash: str = ""

    # ---- read-only lookups (data is small; linear scans keep this obvious) ----

    def item(self, sku: str) -> Item | None:
        return next((i for i in self.items if i.sku == sku), None)

    def po(self, po_number: str) -> POHeader | None:
        key = norm_identifier(po_number)
        return next((p for p in self.po_headers if norm_identifier(p.po_number) == key), None)

    def lines_of(self, po_number: str) -> list[POLine]:
        return sorted((l for l in self.po_lines if l.po_number == po_number), key=lambda l: l.line_no)

    def receipts_for(self, po_number: str, line_no: int) -> list[ReceiptLine]:
        return [r for r in self.receipt_lines if r.po_number == po_number and r.line_no == line_no]

    def bill(self, bill_id: str) -> PriorInvoice | None:
        return next((b for b in self.prior_invoices if b.bill_id == bill_id), None)

    def billed_lines_for(self, po_number: str, line_no: int, state: BillState) -> list[PriorInvoiceLine]:
        out = []
        for pl in self.prior_invoice_lines:
            if pl.po_number == po_number and pl.line_no == line_no:
                bill = self.bill(pl.bill_id)
                if bill is not None and bill.state == state:
                    out.append(pl)
        return out

    def aliases_for(self, party_id: str) -> list[PartyAlias]:
        return [a for a in self.party_aliases if a.party_id == party_id and a.approved]


class SnapshotValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Snapshot failed validation:\n  " + "\n  ".join(errors))


def content_hash(raw: dict) -> str:
    body = {k: v for k, v in raw.items() if k != "content_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def validate_snapshot(raw: dict, *, supported_currency: str = "INR") -> Snapshot:
    """Parse and check a snapshot. Raises SnapshotValidationError with row-level messages."""
    try:
        snap = Snapshot.model_validate({**raw, "content_hash": content_hash(raw)})
    except ValidationError as exc:
        raise SnapshotValidationError(
            [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        ) from exc

    errors: list[str] = []

    def dupes(name: str, keys: list) -> None:
        for key, n in Counter(keys).items():
            if n > 1:
                errors.append(f"{name}: duplicate key {key!r}")

    buyer_ids = {p.party_id for p in snap.buyers}
    supplier_ids = {p.party_id for p in snap.suppliers}
    dupes("parties", [p.party_id for p in snap.buyers + snap.suppliers])
    dupes("items", [i.sku for i in snap.items])
    dupes("po_headers", [norm_identifier(p.po_number) for p in snap.po_headers])
    dupes("po_lines", [(l.po_number, l.line_no) for l in snap.po_lines])
    dupes("receipt_lines", [r.receipt_id for r in snap.receipt_lines])
    dupes("prior_invoices.bill_id", [b.bill_id for b in snap.prior_invoices])
    dupes(
        "prior_invoices.business_key",
        [(b.buyer_id, b.supplier_id, norm_identifier(b.invoice_number)) for b in snap.prior_invoices],
    )
    dupes("prior_invoice_lines", [(l.bill_id, l.po_number, l.line_no) for l in snap.prior_invoice_lines])
    dupes("supplier_item_mappings", [(m.supplier_id, m.kind, norm_text(m.value)) for m in snap.supplier_item_mappings])
    dupes("supporting_references", [r.ref_id for r in snap.supporting_references])

    for i, a in enumerate(snap.party_aliases):
        if a.party_id not in buyer_ids | supplier_ids:
            errors.append(f"party_aliases[{i}]: unknown party_id {a.party_id!r}")
    skus = {i.sku for i in snap.items}
    for i, m in enumerate(snap.supplier_item_mappings):
        if m.supplier_id not in supplier_ids:
            errors.append(f"supplier_item_mappings[{i}]: unknown supplier_id {m.supplier_id!r}")
        if m.sku not in skus:
            errors.append(f"supplier_item_mappings[{i}]: unknown sku {m.sku!r}")
    for i, c in enumerate(snap.unit_conversions):
        if c.sku not in skus:
            errors.append(f"unit_conversions[{i}]: unknown sku {c.sku!r}")
        if c.factor <= ZERO:
            errors.append(f"unit_conversions[{i}]: factor must be positive")

    po_numbers = {p.po_number for p in snap.po_headers}
    for i, h in enumerate(snap.po_headers):
        if h.buyer_id not in buyer_ids:
            errors.append(f"po_headers[{i}] {h.po_number}: unknown buyer_id {h.buyer_id!r}")
        if h.supplier_id not in supplier_ids:
            errors.append(f"po_headers[{i}] {h.po_number}: unknown supplier_id {h.supplier_id!r}")
        if h.currency != supported_currency:
            errors.append(f"po_headers[{i}] {h.po_number}: unsupported currency {h.currency!r}")
        if h.gross_cap < ZERO:
            errors.append(f"po_headers[{i}] {h.po_number}: negative gross_cap")
        lines = snap.lines_of(h.po_number)
        if not lines:
            errors.append(f"po_headers[{i}] {h.po_number}: has no lines")
        expected_cap = sum(
            (money(l.quantity * l.net_unit_price * (1 + l.tax_rate_pct / 100)) for l in lines), ZERO
        )
        if lines and money(expected_cap) != money(h.gross_cap):
            errors.append(
                f"po_headers[{i}] {h.po_number}: gross_cap {h.gross_cap} != sum of lines incl. tax {expected_cap}"
            )

    for i, l in enumerate(snap.po_lines):
        tag = f"po_lines[{i}] {l.po_number}/{l.line_no}"
        if l.po_number not in po_numbers:
            errors.append(f"{tag}: unknown po_number")
        item = snap.item(l.sku)
        if item is None:
            errors.append(f"{tag}: unknown sku {l.sku!r}")
        elif norm_identifier(item.base_unit) != norm_identifier(l.unit):
            errors.append(f"{tag}: unit {l.unit!r} differs from item base unit {item.base_unit!r}")
        if l.quantity < ZERO or l.net_unit_price < ZERO or l.tax_rate_pct < ZERO:
            errors.append(f"{tag}: negative quantity, price or tax rate")

    line_keys = {(l.po_number, l.line_no): l for l in snap.po_lines}
    for i, r in enumerate(snap.receipt_lines):
        tag = f"receipt_lines[{i}] {r.receipt_id}"
        line = line_keys.get((r.po_number, r.line_no))
        if line is None:
            errors.append(f"{tag}: unknown PO line {r.po_number}/{r.line_no}")
        if r.received_qty < ZERO:
            errors.append(f"{tag}: negative received_qty (adjustments must be reconciled upstream)")
        if r.accepted_qty is not None:
            if r.accepted_qty < ZERO:
                errors.append(f"{tag}: negative accepted_qty")
            if r.accepted_qty > r.received_qty:
                errors.append(f"{tag}: accepted_qty {r.accepted_qty} exceeds received_qty {r.received_qty}")

    for i, b in enumerate(snap.prior_invoices):
        tag = f"prior_invoices[{i}] {b.bill_id}"
        if b.buyer_id not in buyer_ids:
            errors.append(f"{tag}: unknown buyer_id")
        if b.supplier_id not in supplier_ids:
            errors.append(f"{tag}: unknown supplier_id")
        if money(b.net_amount + b.tax_amount) != money(b.gross_amount):
            errors.append(f"{tag}: net + tax != gross")
        lines = [l for l in snap.prior_invoice_lines if l.bill_id == b.bill_id]
        if not lines:
            errors.append(f"{tag}: has no lines")
        elif money(sum((l.gross_amount for l in lines), ZERO)) != money(b.gross_amount):
            errors.append(f"{tag}: line gross total != header gross")

    bill_ids = {b.bill_id for b in snap.prior_invoices}
    for i, l in enumerate(snap.prior_invoice_lines):
        tag = f"prior_invoice_lines[{i}] {l.bill_id}"
        if l.bill_id not in bill_ids:
            errors.append(f"{tag}: unknown bill_id")
        if (l.po_number, l.line_no) not in line_keys:
            errors.append(f"{tag}: unknown PO line {l.po_number}/{l.line_no}")
        if l.quantity < ZERO or l.net_amount < ZERO or l.gross_amount < ZERO:
            errors.append(f"{tag}: negative values (credit treatment is out of scope)")

    if errors:
        raise SnapshotValidationError(errors)
    return snap
