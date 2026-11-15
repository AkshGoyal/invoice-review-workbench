"""Normalized invoice contract consumed by the domain rules (SPEC.md §12).

Every scalar keeps its raw text, a nullable normalized value and source evidence
(page, block IDs, quote). Unknown stays ``None``; nothing is inferred from the PO.
In Phase 1 this is filled from a structured fixture; in Phase 2 from model
extraction after evidence validation.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    page: int
    block_ids: list[str] = []
    quote: str


class Evidenced(BaseModel, Generic[T]):
    model_config = ConfigDict(frozen=True, extra="forbid")
    raw: str | None = None
    value: T | None = None
    evidence: Evidence | None = None

    @property
    def known(self) -> bool:
        return self.value is not None


class InputSource(StrEnum):
    STRUCTURED_FIXTURE = "STRUCTURED_FIXTURE"  # Phase 1 domain check: no PDF, no model
    MODEL_EXTRACTION = "MODEL_EXTRACTION"


class PrintedParty(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Evidenced[str] = Evidenced()
    registration_id: Evidenced[str] = Evidenced()
    address: Evidenced[str] = Evidenced()


class InvoiceLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_line_id: str
    printed_po_line: Evidenced[int] = Evidenced()
    supplier_item_code: Evidenced[str] = Evidenced()
    internal_sku: Evidenced[str] = Evidenced()
    description: Evidenced[str] = Evidenced()
    specifications: dict[str, str] = {}
    quantity: Evidenced[Decimal] = Evidenced()
    unit: Evidenced[str] = Evidenced()
    unit_price: Evidenced[Decimal] = Evidenced()
    net_amount: Evidenced[Decimal] = Evidenced()
    tax_amount: Evidenced[Decimal] = Evidenced()
    gross_amount: Evidenced[Decimal] = Evidenced()


class NormalizedInvoice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: str = "invoice_v2"
    input_source: InputSource
    input_label: str
    document_type: Evidenced[str] = Evidenced()
    supplier: PrintedParty = PrintedParty()
    bill_to: PrintedParty = PrintedParty()
    ship_to: PrintedParty | None = None
    invoice_number: Evidenced[str] = Evidenced()
    invoice_date: Evidenced[date] = Evidenced()
    po_reference: Evidenced[str] = Evidenced()
    currency: Evidenced[str] = Evidenced()
    subtotal: Evidenced[Decimal] = Evidenced()
    tax: Evidenced[Decimal] = Evidenced()
    charges: Evidenced[Decimal] = Evidenced()
    discount: Evidenced[Decimal] = Evidenced()
    total: Evidenced[Decimal] = Evidenced()
    tax_basis: Evidenced[str] = Evidenced()
    lines: list[InvoiceLine] = []
    warnings: list[str] = []  # document-level concerns raised by extraction: route to review
    evidence_notes: list[str] = []  # evidence re-anchoring / unsupported values: informational
