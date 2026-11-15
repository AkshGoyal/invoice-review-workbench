"""Turn model extraction output into a NormalizedInvoice with verified evidence (SPEC.md §12).

Pure functions. Inputs: the extraction dict (schema extract_schema_v2) and the OCR
blocks as {block_id: (page, text)}. For each field:

* The cited block must exist and contain the copied text (whitespace/case-insensitive).
* If the text appears in a different block instead, the evidence is re-anchored there
  and a warning is recorded.
* If the text appears nowhere in the OCR, the value is dropped to unknown with a
  warning. A value the document does not support is never passed to the rules.

Numbers and dates are parsed here in Python, never by the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .invoice import Evidence, Evidenced, InputSource, InvoiceLine, NormalizedInvoice, PrintedParty
from .normalize import norm_text

BlockMap = dict[str, tuple[int, str]]


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", norm_text(s)).strip()


def _squash_tight(s: str) -> str:
    """Also ignore markdown/table punctuation and spaces, for OCR text like '| 20,000.00 |'."""
    return re.sub(r"[\s|*_`]", "", norm_text(s))


@dataclass
class EvidenceReport:
    supported: int = 0
    reanchored: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    parse_failures: list[str] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        return ([f"Evidence re-anchored: {x}" for x in self.reanchored]
                + [f"Value not found in OCR text, treated as unknown: {x}" for x in self.unsupported]
                + [f"Could not parse printed value, treated as unknown: {x}" for x in self.parse_failures])


def _contains(block_text: str, text: str) -> bool:
    return _squash(text) in _squash(block_text) or _squash_tight(text) in _squash_tight(block_text)


def locate(name: str, text: str | None, block_id: str | None, blocks: BlockMap, report: EvidenceReport) -> Evidence | None:
    if not text:
        return None
    if block_id and block_id in blocks and _contains(blocks[block_id][1], text):
        report.supported += 1
        page, _ = blocks[block_id]
        return Evidence(page=page, block_ids=[block_id], quote=blocks[block_id][1])
    for bid, (page, btext) in blocks.items():
        if _contains(btext, text):
            report.supported += 1
            report.reanchored.append(f"{name} cited {block_id or 'no block'}, found in {bid}")
            return Evidence(page=page, block_ids=[bid], quote=btext)
    report.unsupported.append(f"{name}={text!r}")
    return None


# ---- parsers (conservative: anything unclear -> None) ----

_AMOUNT = re.compile(r"^-?\d+(\.\d+)?$")


def parse_decimal(text: str) -> Decimal | None:
    t = re.sub(r"(?i)\b(inr|rs\.?)\b|₹", "", text).replace(",", "").replace(" ", "").strip()
    if not _AMOUNT.match(t):
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def parse_int(text: str) -> int | None:
    t = text.strip()
    return int(t) if t.isdigit() else None


_DATE_FORMATS = ["%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"]
_NUMERIC_DATE = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$")


def parse_date(text: str) -> date | None:
    t = re.sub(r"\s+", " ", text.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    m = _NUMERIC_DATE.match(t)
    if m:
        d, mth, y = (int(x) for x in m.groups())
        # Indian convention is DD/MM/YYYY; if both readings are valid and differ, it is ambiguous.
        if d <= 12 and mth <= 12 and d != mth:
            return None
        try:
            return date(y, mth, d)
        except ValueError:
            return None
    return None


def parse_unit(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).upper()


def _ev_field(name: str, raw: dict | None, blocks: BlockMap, report: EvidenceReport, parser=None) -> Evidenced:
    raw = raw or {}
    text = raw.get("text")
    if text is None or str(text).strip() == "":
        return Evidenced()
    text = str(text).strip()
    evidence = locate(name, text, raw.get("block_id"), blocks, report)
    if evidence is None:
        return Evidenced(raw=text)  # unsupported: value withheld
    value = text
    if parser is not None:
        value = parser(text)
        if value is None:
            report.parse_failures.append(f"{name}={text!r}")
            return Evidenced(raw=text, evidence=evidence)
    return Evidenced(raw=text, value=value, evidence=evidence)


def _enum_field(name: str, raw: dict | None, blocks: BlockMap, report: EvidenceReport, unknown: str) -> Evidenced:
    raw = raw or {}
    value = raw.get("value")
    if value in (None, unknown):
        return Evidenced(raw=value)
    bid = raw.get("block_id")
    if not bid or bid not in blocks:
        report.unsupported.append(f"{name}={value} (no valid block cited)")
        return Evidenced(raw=value)
    page, text = blocks[bid]
    report.supported += 1
    return Evidenced(raw=value, value=value, evidence=Evidence(page=page, block_ids=[bid], quote=text))


def _party(name: str, raw: dict | None, blocks: BlockMap, report: EvidenceReport) -> PrintedParty:
    raw = raw or {}
    return PrintedParty(
        name=_ev_field(f"{name}.name", raw.get("name"), blocks, report),
        registration_id=_ev_field(f"{name}.registration_id", raw.get("registration_id"), blocks, report),
        address=_ev_field(f"{name}.address", raw.get("address"), blocks, report),
    )


def _anchor_line_amounts(line: InvoiceLine, report: EvidenceReport) -> InvoiceLine:
    """Per-line tax/gross must come from the line's own row; otherwise they were copied from invoice totals."""
    row_blocks = {b for f in (line.quantity, line.unit_price, line.net_amount, line.description)
                  if f.evidence for b in f.evidence.block_ids}
    updates = {}
    for name in ("tax_amount", "gross_amount"):
        f = getattr(line, name)
        if f.evidence and row_blocks and not set(f.evidence.block_ids) & row_blocks:
            report.unsupported.append(f"{line.source_line_id}.{name}={f.raw!r} (not printed in the line's row)")
            updates[name] = Evidenced(raw=f.raw)
    return line.model_copy(update=updates) if updates else line


def _warnings(raw: list, blocks: BlockMap, report: EvidenceReport) -> list[str]:
    out = []
    for w in raw:
        if isinstance(w, dict):
            where = blocks.get(w.get("block_id") or "")
            loc = f" (page {where[0]}: \"{where[1]}\")" if where else ""
            out.append(f"{w.get('kind')}: {w.get('detail', '').strip()}{loc}")
        elif isinstance(w, str) and w.strip():
            out.append(w.strip())
    return out


def to_normalized_invoice(extracted: dict, blocks: BlockMap, *, input_label: str) -> tuple[NormalizedInvoice, EvidenceReport]:
    report = EvidenceReport()
    lines = []
    for i, ln in enumerate(extracted.get("lines") or [], start=1):
        lid = f"L{i}"
        specs = {}
        for s in ln.get("specifications") or []:
            if locate(f"{lid}.spec.{s.get('attribute')}", s.get("text"), s.get("block_id"), blocks, report):
                specs[norm_text(s.get("attribute", ""))] = str(s.get("value", "")).strip()
        line = InvoiceLine(
            source_line_id=lid,
            printed_po_line=_ev_field(f"{lid}.printed_po_line", ln.get("printed_po_line"), blocks, report, parse_int),
            supplier_item_code=_ev_field(f"{lid}.supplier_item_code", ln.get("supplier_item_code"), blocks, report),
            internal_sku=_ev_field(f"{lid}.internal_sku", ln.get("internal_sku"), blocks, report),
            description=_ev_field(f"{lid}.description", ln.get("description"), blocks, report),
            specifications=specs,
            quantity=_ev_field(f"{lid}.quantity", ln.get("quantity"), blocks, report, parse_decimal),
            unit=_ev_field(f"{lid}.unit", ln.get("unit"), blocks, report, parse_unit),
            unit_price=_ev_field(f"{lid}.unit_price", ln.get("unit_price"), blocks, report, parse_decimal),
            net_amount=_ev_field(f"{lid}.net_amount", ln.get("net_amount"), blocks, report, parse_decimal),
            tax_amount=_ev_field(f"{lid}.tax_amount", ln.get("tax_amount"), blocks, report, parse_decimal),
            gross_amount=_ev_field(f"{lid}.gross_amount", ln.get("gross_amount"), blocks, report, parse_decimal),
        )
        lines.append(_anchor_line_amounts(line, report))

    def f(name, parser=None):
        return _ev_field(name, extracted.get(name), blocks, report, parser)

    ship = _party("ship_to", extracted.get("ship_to"), blocks, report)
    inv = NormalizedInvoice(
        input_source=InputSource.MODEL_EXTRACTION,
        input_label=input_label,
        document_type=_enum_field("document_type", extracted.get("document_type"), blocks, report, "OTHER"),
        supplier=_party("supplier", extracted.get("supplier"), blocks, report),
        bill_to=_party("bill_to", extracted.get("bill_to"), blocks, report),
        ship_to=ship if ship.name.known else None,
        invoice_number=f("invoice_number"),
        invoice_date=f("invoice_date", parse_date),
        po_reference=f("po_reference"),
        currency=f("currency", parse_unit),
        subtotal=f("subtotal", parse_decimal),
        tax=f("tax", parse_decimal),
        charges=f("charges", parse_decimal),
        discount=f("discount", parse_decimal),
        total=f("total", parse_decimal),
        tax_basis=_enum_field("tax_basis", extracted.get("tax_basis"), blocks, report, "UNKNOWN"),
        lines=lines,
        warnings=_warnings(extracted.get("warnings") or [], blocks, report),
    )
    inv = inv.model_copy(update={"evidence_notes": report.warnings})
    return inv, report
