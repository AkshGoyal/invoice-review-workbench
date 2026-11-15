"""Generate synthetic invoice PDFs (SPEC.md §8). All companies and values are fictional.

Usage: uv run python -m scripts.make_fixtures
Writes data/fixtures/pdf/{H1_digital,H1_scan,H1_two_page}.pdf deterministically.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageEnhance, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "pdf"
BANNER = "SYNTHETIC DEMO DOCUMENT - fictional companies and values - not a real invoice"


@dataclass
class Line:
    po_line: str
    code: str
    description: str
    spec: str
    qty: str
    unit: str
    price: str
    amount: str


@dataclass
class InvoiceSpec:
    invoice_no: str
    invoice_date: str
    po_ref: str | None
    supplier_name: str = "Apex Packaging Private Limited"
    supplier_reg: str | None = "SYN-REG-AP-0042"
    supplier_addr: str = "Unit 7, Fictional Packaging Park, Nashik 422000"
    bill_to: str = "Northstar Manufacturing Private Limited"
    bill_to_reg: str | None = "SYN-REG-NS-0001"
    bill_to_addr: str = "Plot 14, Fictional Industrial Estate, Pune 411000"
    ship_to: str = "Northstar Manufacturing Private Limited - Plant 2"
    ship_to_addr: str = "Gate 3, Fictional Industrial Estate, Pune 411000"
    lines: list[Line] = field(default_factory=list)
    subtotal: str = "0.00"
    tax_label: str = "GST @ 18%"
    tax: str = "0.00"
    charges: str = "0.00"
    discount: str = "0.00"
    total: str = "0.00"


H1 = InvoiceSpec(
    invoice_no="AP-204", invoice_date="01-Nov-2026", po_ref="PO-101",
    lines=[Line("10", "BX-5P", "CORR CARTON 5PLY", "5-ply", "100", "EA", "200.00", "20,000.00")],
    subtotal="20,000.00", tax="3,600.00", total="23,600.00",
)


def _header(c: canvas.Canvas, s: InvoiceSpec, width: float, top: float) -> float:
    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, top, s.supplier_name)
    c.setFont("Helvetica", 9)
    y = top - 6 * mm
    c.drawString(20 * mm, y, s.supplier_addr)
    if s.supplier_reg:
        y -= 4.5 * mm
        c.drawString(20 * mm, y, f"Reg. ID: {s.supplier_reg}")
    c.setFont("Helvetica-Bold", 18)
    c.drawRightString(width - 20 * mm, top, "TAX INVOICE")
    c.setFont("Helvetica", 10)
    meta = [f"Invoice No: {s.invoice_no}", f"Invoice Date: {s.invoice_date}"]
    if s.po_ref:
        meta.append(f"PO Ref: {s.po_ref}")
    meta.append("Currency: INR")
    my = top - 7 * mm
    for m in meta:
        c.drawRightString(width - 20 * mm, my, m)
        my -= 5 * mm

    y = min(y, my) - 10 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y, f"Bill To: {s.bill_to}")
    c.drawString(110 * mm, y, f"Ship To: {s.ship_to}")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, y - 5 * mm, s.bill_to_addr)
    c.drawString(110 * mm, y - 5 * mm, s.ship_to_addr)
    if s.bill_to_reg:
        c.drawString(20 * mm, y - 10 * mm, f"Reg. ID: {s.bill_to_reg}")
    return y - 20 * mm


COLS = [("PO Line", 20), ("Item Code", 38), ("Description", 62), ("Spec", 108), ("Qty", 128),
        ("Unit", 142), ("Unit Price", 172), ("Amount", 192)]
RIGHT = {"Qty", "Unit Price", "Amount"}


def _table(c: canvas.Canvas, s: InvoiceSpec, y: float) -> float:
    c.setFont("Helvetica-Bold", 9)
    for name, x in COLS:
        (c.drawRightString if name in RIGHT else c.drawString)(x * mm, y, name)
    c.line(20 * mm, y - 2 * mm, 192 * mm, y - 2 * mm)
    c.setFont("Helvetica", 9)
    y -= 7 * mm
    for ln in s.lines:
        vals = [ln.po_line, ln.code, ln.description, ln.spec, ln.qty, ln.unit, ln.price, ln.amount]
        for (name, x), v in zip(COLS, vals):
            (c.drawRightString if name in RIGHT else c.drawString)(x * mm, y, v)
        y -= 6 * mm
    c.line(20 * mm, y + 2 * mm, 192 * mm, y + 2 * mm)
    return y - 6 * mm


def _totals(c: canvas.Canvas, s: InvoiceSpec, y: float) -> float:
    rows = [("Subtotal", s.subtotal), (s.tax_label, s.tax), ("Freight / Other charges", s.charges),
            ("Discount", s.discount)]
    c.setFont("Helvetica", 10)
    for label, val in rows:
        c.drawString(120 * mm, y, label)
        c.drawRightString(192 * mm, y, val)
        y -= 5.5 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(120 * mm, y - 1 * mm, "Total (INR)")
    c.drawRightString(192 * mm, y - 1 * mm, s.total)
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, y - 10 * mm, "Prices are exclusive of GST. Tax shown separately above.")
    return y - 18 * mm


def _footer(c: canvas.Canvas, page: int, pages: int) -> None:
    c.setFont("Helvetica-Oblique", 7)
    c.drawString(20 * mm, 12 * mm, BANNER)
    c.drawRightString(192 * mm, 12 * mm, f"Page {page} of {pages}")


def _terms(c: canvas.Canvas, y: float) -> None:
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y, "Terms")
    c.setFont("Helvetica", 8)
    for i, t in enumerate([
        "Payment due 45 days from invoice date.",
        "Bank: Fictional Bank, A/C SYN-000-0000 (synthetic). Quote invoice number on remittance.",
        "Goods delivered against the referenced purchase order.",
    ]):
        c.drawString(20 * mm, y - (5 + 4.5 * i) * mm, t)


def render(spec: InvoiceSpec, path: Path, *, two_page: bool = False) -> None:
    width, height = A4
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle(f"Invoice {spec.invoice_no} (synthetic)")
    c.setAuthor("Synthetic fixture generator")
    pages = 2 if two_page else 1
    y = _header(c, spec, width, height - 25 * mm)
    y = _table(c, spec, y)
    if two_page:
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(20 * mm, y, "Continued on page 2: totals and terms")
        _footer(c, 1, pages)
        c.showPage()
        c.setFont("Helvetica-Bold", 10)
        c.drawString(20 * mm, height - 25 * mm, f"Invoice No: {spec.invoice_no} (continued)")
        y = _totals(c, spec, height - 40 * mm)
        _terms(c, y)
        _footer(c, 2, pages)
    else:
        y = _totals(c, spec, y)
        _terms(c, y)
        _footer(c, 1, pages)
    c.showPage()
    c.save()


def scan(src: Path, dst: Path, *, dpi: int = 150, angle: float = 0.7) -> None:
    """Rasterize to a slightly rotated, softened grayscale image-only PDF (readable scan)."""
    doc = pdfium.PdfDocument(str(src))
    images = []
    for page in doc:
        img = page.render(scale=dpi / 72).to_pil().convert("L")
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        img = ImageEnhance.Contrast(img).enhance(0.85)
        img = ImageEnhance.Brightness(img).enhance(0.97)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=55)
        images.append(Image.open(io.BytesIO(buf.getvalue())).convert("L"))
    images[0].save(dst, "PDF", resolution=dpi, save_all=True, append_images=images[1:])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    render(H1, OUT / "H1_digital.pdf")
    render(H1, OUT / "H1_two_page.pdf", two_page=True)
    scan(OUT / "H1_digital.pdf", OUT / "H1_scan.pdf")
    for p in sorted(OUT.glob("H1_*.pdf")):
        print(f"{p.relative_to(OUT.parents[2])}  {p.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
