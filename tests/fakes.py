"""Test-only provider adapter. Never used by the app or demo; it cannot stand in for live acceptance."""

from __future__ import annotations

import copy

from src.providers.base import Budget, OcrPage, OcrResult, ProviderError, ProviderErrorKind
from src.providers.openrouter import to_blocks

H1_OCR_TEXT = """# Apex Packaging Private Limited
Unit 7, Fictional Packaging Park, Nashik 422000
Reg. ID: SYN-REG-AP-0042
# TAX INVOICE
Invoice No: AP-204
Invoice Date: 01-Nov-2026
PO Ref: PO-101
Currency: INR
**Bill To: Northstar Manufacturing Private Limited**
Plot 14, Fictional Industrial Estate, Pune 411000
Reg. ID: SYN-REG-NS-0001
**Ship To: Northstar Manufacturing Private Limited - Plant 2**
| PO Line | Item Code | Description | Spec | Qty | Unit | Unit Price | Amount |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 10 | BX-5P | CORR CARTON 5PLY | 5-ply | 100 | EA | 200.00 | 20,000.00 |
Subtotal 20,000.00
GST @ 18% 3,600.00
Freight / Other charges 0.00
Discount 0.00
Total (INR) 23,600.00
Prices are exclusive of GST. Tax shown separately above.
"""


def f(text, block):
    return {"text": text, "block_id": block}


NULL = {"text": None, "block_id": None}

# Block IDs follow to_blocks() on H1_OCR_TEXT (table separator row dropped).
H1_EXTRACTION = {
    "document_type": {"value": "INVOICE", "block_id": "p1-b4"},
    "supplier": {"name": f("Apex Packaging Private Limited", "p1-b1"), "registration_id": f("SYN-REG-AP-0042", "p1-b3"),
                 "address": f("Unit 7, Fictional Packaging Park, Nashik 422000", "p1-b2")},
    "bill_to": {"name": f("Northstar Manufacturing Private Limited", "p1-b9"), "registration_id": f("SYN-REG-NS-0001", "p1-b11"),
                "address": f("Plot 14, Fictional Industrial Estate, Pune 411000", "p1-b10")},
    "ship_to": {"name": f("Northstar Manufacturing Private Limited - Plant 2", "p1-b12"), "registration_id": NULL, "address": NULL},
    "invoice_number": f("AP-204", "p1-b5"),
    "invoice_date": f("01-Nov-2026", "p1-b6"),
    "po_reference": f("PO-101", "p1-b7"),
    "currency": f("INR", "p1-b8"),
    "subtotal": f("20,000.00", "p1-b15"),
    "tax": f("3,600.00", "p1-b16"),
    "charges": f("0.00", "p1-b17"),
    "discount": f("0.00", "p1-b18"),
    "total": f("23,600.00", "p1-b19"),
    "tax_basis": {"value": "EXCLUSIVE", "block_id": "p1-b20"},
    "lines": [{
        "printed_po_line": f("10", "p1-b14"), "supplier_item_code": f("BX-5P", "p1-b14"), "internal_sku": NULL,
        "description": f("CORR CARTON 5PLY", "p1-b14"),
        "specifications": [{"attribute": "ply", "value": "5", "text": "5-ply", "block_id": "p1-b14"}],
        "quantity": f("100", "p1-b14"), "unit": f("EA", "p1-b14"), "unit_price": f("200.00", "p1-b14"),
        "net_amount": f("20,000.00", "p1-b14"), "tax_amount": NULL, "gross_amount": NULL,
    }],
    "warnings": [],
}


class FakeReader:
    def __init__(self, text: str = H1_OCR_TEXT, fail: ProviderErrorKind | None = None):
        self.text, self.fail, self.calls = text, fail, 0

    def read_document(self, pdf_bytes, filename, budget: Budget, *, on_event=None):
        budget.take_call()
        self.calls += 1
        if self.fail:
            raise ProviderError(self.fail, "simulated provider failure")
        if on_event:
            on_event("Page 1 read (fake)")
        page = OcrPage(page=1, text=self.text, blocks=to_blocks(1, self.text))
        return OcrResult(provider="fake", engine="fake-ocr", carrier_model="fake", pages=[page], calls=1), [{"page": 1}]


class FakeExtractor:
    model_id = "fake-extractor"
    prompt_version = "extract_v2"
    prompt_hash = "fake"

    def __init__(self, output: dict | None = None, fail: ProviderErrorKind | None = None):
        self.output = copy.deepcopy(output or H1_EXTRACTION)
        self.fail, self.calls = fail, 0

    def extract_invoice(self, ocr, budget: Budget):
        budget.take_call()
        self.calls += 1
        if self.fail:
            raise ProviderError(self.fail, "simulated extraction failure")
        return copy.deepcopy(self.output), {"model": "fake", "attempt": 1}


def factory(reader=None, extractor=None):
    r, e = reader or FakeReader(), extractor or FakeExtractor()
    return (lambda _settings: (r, e)), r, e
