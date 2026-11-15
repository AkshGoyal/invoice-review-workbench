"""H1 structured-input domain check (SPEC.md §8). Expected values are computed here from the spec."""

from decimal import Decimal

from src.domain.decisions import MANDATORY_CODES
from src.domain.invoice import NormalizedInvoice
from src.domain.results import CheckStatus, Outcome, Recommendation

from .conftest import run

# Independent arithmetic from SPEC.md §8.
EXPECTED_ORDER_REMAINING = Decimal(1000) - Decimal(400)  # 600 EA
EXPECTED_RECEIPT_REMAINING = Decimal(600) - Decimal(400)  # 200 EA
EXPECTED_GROSS_REMAINING = Decimal("236000.00") - Decimal("94400.00")  # 141,600.00
H1_QTY = Decimal(100)
H1_GROSS = Decimal(100) * Decimal("200.00") * Decimal("1.18")  # 23,600.00


def test_h1_is_ready_for_finance_review(h1_raw, s1, policy):
    result = run(NormalizedInvoice.model_validate(h1_raw), s1, policy)

    assert result.recommendation == Recommendation.READY_FOR_FINANCE_REVIEW
    assert result.commercial_status == CheckStatus.MATCHED
    assert result.receiving_status == CheckStatus.MATCHED
    assert result.headline == "Checks passed against snapshot S1. Ready for finance review."
    assert "approved" not in result.headline.lower()
    assert result.primary_exception is None

    ctx = result.po_context
    assert ctx.po_number == "PO-101"
    line = ctx.lines[0]
    assert line.remaining_ordered_qty == EXPECTED_ORDER_REMAINING
    assert line.receipt_available_qty == EXPECTED_RECEIPT_REMAINING
    assert line.invoice_qty == H1_QTY
    assert ctx.remaining_gross == EXPECTED_GROSS_REMAINING
    assert ctx.invoice_gross == H1_GROSS

    assert all(c.outcome == Outcome.PASS for c in result.checks), [c for c in result.checks if c.outcome != Outcome.PASS]
    assert set(MANDATORY_CODES) <= {c.code for c in result.checks}
    assert result.buyer_match.selected_id == "BUY-NORTHSTAR"
    assert result.supplier_match.selected_id == "SUP-APEX"
    assert result.line_matches[0].po_line_no == 10
    assert any("Structured-input domain check" in s for s in result.scope_limits)


def test_h1_does_not_mutate_snapshot(h1_raw, s1, policy):
    before = s1.model_dump()
    run(NormalizedInvoice.model_validate(h1_raw), s1, policy)
    assert s1.model_dump() == before
