"""Fail-closed boundaries: plausible bugs that would produce a false 'ready'.

Each test changes one input relative to H1 and asserts the specific hold.
Expected numbers are derived from SPEC.md §8 facts, not from the code.
"""

import copy

import pytest

from src.domain.decisions import combine
from src.domain.invoice import NormalizedInvoice
from src.domain.results import CheckStatus, Outcome, Recommendation

from .conftest import build_invoice, checks_by_code, run

RECEIPT_AVAILABLE = 600 - 400  # received - prior billed on PO-101/10
ORDER_AVAILABLE = 1000 - 400


def test_exact_receipt_balance_passes(h1_raw, s1, policy):
    result = run(build_invoice(h1_raw, [str(RECEIPT_AVAILABLE)]), s1, policy)
    assert result.recommendation == Recommendation.READY_FOR_FINANCE_REVIEW


def test_order_capacity_sufficient_but_receipt_short_by_one_unit(h1_raw, s1, policy):
    """Catches checking receipts against ordered quantity, or ignoring prior billing on receipts."""
    qty = RECEIPT_AVAILABLE + 1  # 201 EA: within 600 ordered-unbilled, within 600 received in total
    result = run(build_invoice(h1_raw, [str(qty)]), s1, policy)

    [quantity] = checks_by_code(result, "PO_LINE_QUANTITY")
    [amount] = checks_by_code(result, "PO_AMOUNT")
    [receipt] = checks_by_code(result, "RECEIPT")
    assert quantity.outcome == Outcome.PASS
    assert amount.outcome == Outcome.PASS
    assert receipt.outcome == Outcome.REVIEW
    assert receipt.issue_code == "RECEIPT_SHORTFALL"
    assert receipt.difference == "1 EA unsupported"
    assert result.commercial_status == CheckStatus.MATCHED
    assert result.receiving_status == CheckStatus.NEEDS_REVIEW
    assert result.recommendation == Recommendation.NEEDS_REVIEW
    assert result.primary_exception == "RECEIPT_SHORTFALL"
    assert result.next_owner == "Stores"


def test_e4_shape_reports_100_unit_shortfall(h1_raw, s1, policy):
    result = run(build_invoice(h1_raw, ["300"], number="AP-207"), s1, policy)
    [receipt] = checks_by_code(result, "RECEIPT")
    assert receipt.issue_code == "RECEIPT_SHORTFALL"
    assert receipt.difference == f"{300 - RECEIPT_AVAILABLE} EA unsupported"
    assert result.recommendation == Recommendation.NEEDS_REVIEW


def test_rows_on_same_po_line_are_aggregated(h1_raw, s1, policy):
    """150 + 60 each fit 200 alone; together 210 must not."""
    result = run(build_invoice(h1_raw, ["150", "60"]), s1, policy)
    [receipt] = checks_by_code(result, "RECEIPT")
    assert receipt.outcome == Outcome.REVIEW
    assert result.po_context.lines[0].invoice_qty == 210
    assert result.recommendation == Recommendation.NEEDS_REVIEW


def test_order_quantity_one_unit_over(h1_raw, s1, policy):
    result = run(build_invoice(h1_raw, [str(ORDER_AVAILABLE + 1)]), s1, policy)
    [quantity] = checks_by_code(result, "PO_LINE_QUANTITY")
    assert quantity.issue_code == "QUANTITY_EXCEEDS_ORDER"
    assert quantity.difference == "1 EA over"


@pytest.mark.parametrize("total, outcome", [("141600.00", Outcome.PASS), ("141600.01", Outcome.REVIEW)])
def test_po_amount_boundary_to_the_paise(h1_raw, s1, policy, total, outcome):
    # Only PO_AMOUNT is asserted; arithmetic will separately disagree with this overridden total.
    result = run(build_invoice(h1_raw, ["100"], total_override=total), s1, policy)
    [amount] = checks_by_code(result, "PO_AMOUNT")
    assert amount.outcome == outcome
    assert result.recommendation != Recommendation.READY_FOR_FINANCE_REVIEW or outcome == Outcome.PASS


def test_unit_price_variance_holds_even_when_totals_fit(h1_raw, s1, policy):
    result = run(build_invoice(h1_raw, ["100"], price="200.01"), s1, policy)
    [price] = checks_by_code(result, "UNIT_PRICE")
    assert price.issue_code == "PRICE_VARIANCE"
    assert [c.outcome for c in checks_by_code(result, "INVOICE_ARITHMETIC")] == [Outcome.PASS]
    assert result.recommendation == Recommendation.NEEDS_REVIEW


def test_missing_tax_is_not_zero(h1_raw, s1, policy):
    raw = copy.deepcopy(h1_raw)
    raw["tax"] = {"raw": None, "value": None, "evidence": None}
    raw["total"]["value"] = "20000.00"
    result = run(NormalizedInvoice.model_validate(raw), s1, policy)
    assert checks_by_code(result, "INVOICE_ARITHMETIC")[0].issue_code == "TAX_MISSING"
    assert result.recommendation == Recommendation.NEEDS_REVIEW


def test_value_without_evidence_prevents_readiness(h1_raw, s1, policy):
    raw = copy.deepcopy(h1_raw)
    raw["total"]["evidence"] = None
    result = run(NormalizedInvoice.model_validate(raw), s1, policy)
    assert checks_by_code(result, "REQUIRED_EVIDENCE")[0].issue_code == "EVIDENCE_MISSING"
    assert result.recommendation == Recommendation.NEEDS_REVIEW


def test_wrong_bill_to_not_rescued_by_ship_to(h1_raw, s1, policy):
    raw = copy.deepcopy(h1_raw)
    raw["bill_to"] = {"name": {"raw": "Northern Star Metals Private Limited", "value": "Northern Star Metals Private Limited",
                               "evidence": {"page": 1, "block_ids": ["p1-b4"], "quote": "Bill To: Northern Star Metals Private Limited"}}}
    result = run(NormalizedInvoice.model_validate(raw), s1, policy)
    [buyer] = checks_by_code(result, "BUYER_IDENTITY")
    assert buyer.outcome == Outcome.REVIEW
    assert "Ship-to was not used" in buyer.reason
    assert checks_by_code(result, "PO_RESOLUTION")[0].outcome == Outcome.NOT_RUN
    assert checks_by_code(result, "RECEIPT")[0].outcome == Outcome.NOT_RUN
    assert result.recommendation == Recommendation.NEEDS_REVIEW


def test_wrong_specification_with_correct_code_holds(h1_raw, s1, policy):
    raw = copy.deepcopy(h1_raw)
    raw["lines"][0]["specifications"] = {"ply": "3"}
    result = run(NormalizedInvoice.model_validate(raw), s1, policy)
    assert checks_by_code(result, "LINE_MATCH")[0].issue_code == "SPECIFICATION_CONFLICT"
    assert checks_by_code(result, "RECEIPT")[0].outcome == Outcome.NOT_RUN
    assert result.recommendation == Recommendation.NEEDS_REVIEW


def test_rerendered_prior_bill_is_blocked_as_duplicate(h1_raw, s1, policy):
    result = run(build_invoice(h1_raw, ["400"], number="AP-099"), s1, policy)
    [dup] = checks_by_code(result, "DUPLICATE")
    assert dup.issue_code == "DUPLICATE_BILL"
    assert result.recommendation == Recommendation.BLOCKED


def test_empty_check_list_is_never_ready():
    commercial, receiving, rec, _primary, missing = combine([])
    assert rec == Recommendation.NEEDS_REVIEW
    assert "RECEIPT" in missing
