"""S1 seed validates and carries the SPEC.md §8 facts; inconsistent seeds fail with row-level errors."""

from decimal import Decimal

import pytest

from src.domain.snapshot import BillState, SnapshotValidationError, validate_snapshot

# SPEC.md §8, stated independently of the seed file and the implementation.
PO101_ORDERED = Decimal(1000)
PO101_RECEIVED = Decimal(600)
AP099_QTY = Decimal(400)
PO101_CAP = Decimal("236000.00")
AP099_GROSS = Decimal("94400.00")


def test_s1_validates_and_matches_spec_facts(s1):
    po101 = s1.po("PO-101")
    line = s1.lines_of("PO-101")[0]
    assert (line.sku, line.unit, line.net_unit_price, line.quantity) == ("CARTON-5P", "EA", Decimal("200.00"), PO101_ORDERED)
    assert po101.gross_cap == PO101_CAP
    assert sum(r.received_qty for r in s1.receipts_for("PO-101", 10)) == PO101_RECEIVED
    billed = s1.billed_lines_for("PO-101", 10, BillState.POSTED)
    assert [b.quantity for b in billed] == [AP099_QTY]
    assert s1.bill("BILL-AP-099").gross_amount == AP099_GROSS
    assert s1.bill("BILL-AP-099").payment_status == "UNPAID"  # unpaid is still billed

    po102 = s1.po("PO-102")
    assert po102.gross_cap == Decimal("118000.00")
    assert sum(r.received_qty for r in s1.receipts_for("PO-102", 10)) == Decimal(300)
    assert s1.billed_lines_for("PO-102", 10, BillState.POSTED) == []
    assert s1.content_hash.startswith("sha256:")


@pytest.mark.parametrize(
    "mutate, expected_fragment",
    [
        (lambda r: r["receipt_lines"][0].update(accepted_qty="400"), "accepted_qty 400 exceeds received_qty 350"),
        (lambda r: r["receipt_lines"][0].update(po_number="PO-999"), "unknown PO line PO-999/10"),
        (lambda r: r["po_headers"][0].update(gross_cap="236000.01"), "gross_cap 236000.01 != sum of lines"),
        (lambda r: r["prior_invoices"].append({**r["prior_invoices"][0], "bill_id": "BILL-X"}), "prior_invoices.business_key: duplicate"),
        (lambda r: r["po_headers"][1].update(currency="USD"), "unsupported currency 'USD'"),
        (lambda r: r["prior_invoices"][0].update(tax_amount="14400.01"), "net + tax != gross"),
    ],
)
def test_inconsistent_seed_fails_with_row_level_error(s1_raw, mutate, expected_fragment):
    mutate(s1_raw)
    with pytest.raises(SnapshotValidationError) as exc:
        validate_snapshot(s1_raw)
    assert any(expected_fragment in e for e in exc.value.errors), exc.value.errors
