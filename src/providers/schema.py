"""Versioned JSON schema for extraction (strict mode: every key required, nulls for unknowns).

The model copies printed text and cites a block; Python parses numbers and dates.
"""

from __future__ import annotations

import hashlib
import json

SCHEMA_VERSION = "extract_schema_v2"

NULL_STR = {"type": ["string", "null"]}


def _field() -> dict:
    return {
        "type": "object",
        "properties": {"text": NULL_STR, "block_id": NULL_STR},
        "required": ["text", "block_id"],
        "additionalProperties": False,
    }


def _enum_field(values: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {"value": {"type": "string", "enum": values}, "block_id": NULL_STR},
        "required": ["value", "block_id"],
        "additionalProperties": False,
    }


def _party() -> dict:
    return {
        "type": "object",
        "properties": {"name": _field(), "registration_id": _field(), "address": _field()},
        "required": ["name", "registration_id", "address"],
        "additionalProperties": False,
    }


_SPEC = {
    "type": "object",
    "properties": {"attribute": {"type": "string"}, "value": {"type": "string"}, "text": {"type": "string"}, "block_id": NULL_STR},
    "required": ["attribute", "value", "text", "block_id"],
    "additionalProperties": False,
}

WARNING_KINDS = ["CONFLICTING_VALUES", "MULTIPLE_PO_REFERENCES", "MULTIPLE_INVOICES", "EMBEDDED_INSTRUCTIONS"]

_WARNING = {
    "type": "object",
    "properties": {"kind": {"type": "string", "enum": WARNING_KINDS}, "detail": {"type": "string"}, "block_id": NULL_STR},
    "required": ["kind", "detail", "block_id"],
    "additionalProperties": False,
}

_LINE_FIELDS = ["printed_po_line", "supplier_item_code", "internal_sku", "description", "quantity", "unit",
                "unit_price", "net_amount", "tax_amount", "gross_amount"]

_LINE = {
    "type": "object",
    "properties": {**{k: _field() for k in _LINE_FIELDS}, "specifications": {"type": "array", "items": _SPEC}},
    "required": _LINE_FIELDS + ["specifications"],
    "additionalProperties": False,
}

_SCALARS = ["invoice_number", "invoice_date", "po_reference", "currency", "subtotal", "tax", "charges", "discount", "total"]

EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "document_type": _enum_field(["INVOICE", "CREDIT_NOTE", "OTHER"]),
        "supplier": _party(),
        "bill_to": _party(),
        "ship_to": _party(),
        **{k: _field() for k in _SCALARS},
        "tax_basis": _enum_field(["EXCLUSIVE", "INCLUSIVE", "UNKNOWN"]),
        "lines": {"type": "array", "items": _LINE},
        "warnings": {"type": "array", "items": _WARNING},
    },
    "required": ["document_type", "supplier", "bill_to", "ship_to", *_SCALARS, "tax_basis", "lines", "warnings"],
    "additionalProperties": False,
}


def schema_hash() -> str:
    return hashlib.sha256(json.dumps(EXTRACTION_SCHEMA, sort_keys=True).encode()).hexdigest()[:12]
