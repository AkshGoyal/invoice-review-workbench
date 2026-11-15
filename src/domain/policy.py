"""Explicit demo policy (config/policy_v2.json). Loading is I/O-free given a dict."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    policy_version: str
    supported_currency: str
    supported_document_types: list[str]
    supported_tax_bases: list[str]
    line_arithmetic_tolerance: Decimal
    invoice_arithmetic_tolerance: Decimal
    unit_price_tolerance: Decimal
    overbilling_quantity_tolerance: Decimal
    po_overspend_tolerance: Decimal
    max_quantity_decimals: int
    max_po_candidates: int
    max_text_suggestions: int
