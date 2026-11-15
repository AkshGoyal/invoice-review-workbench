"""Combine a complete check set into a recommendation (SPEC.md §7).

READY_FOR_FINANCE_REVIEW requires every mandatory check code to be present and
every check to be PASS (or NOT_REQUIRED for receiving). Any NOT_RUN, REVIEW or
missing mandatory check prevents readiness; an empty list can never be ready.
"""

from __future__ import annotations

from .results import Category, CheckResult, CheckStatus, Outcome, Recommendation

MANDATORY_CODES = (
    "SCOPE", "REQUIRED_EVIDENCE", "BUYER_IDENTITY", "SUPPLIER_IDENTITY", "DUPLICATE",
    "PO_RESOLUTION", "PO_STATUS", "LINE_MATCH", "UNIT_PRICE", "LINE_ARITHMETIC",
    "INVOICE_ARITHMETIC", "PO_LINE_QUANTITY", "PO_AMOUNT", "RECEIPT",
)


def category_status(checks: list[CheckResult], category: Category) -> CheckStatus:
    relevant = [c for c in checks if c.category == category]
    if not relevant:
        return CheckStatus.NEEDS_REVIEW
    if any(c.outcome == Outcome.BLOCK for c in relevant):
        return CheckStatus.BLOCKED
    if any(c.outcome in (Outcome.REVIEW, Outcome.NOT_RUN) for c in relevant):
        return CheckStatus.NEEDS_REVIEW
    if all(c.outcome == Outcome.NOT_REQUIRED for c in relevant):
        return CheckStatus.NOT_REQUIRED
    return CheckStatus.MATCHED


def missing_mandatory(checks: list[CheckResult]) -> list[str]:
    present = {c.code for c in checks}
    return [code for code in MANDATORY_CODES if code not in present]


def primary_exception(checks: list[CheckResult]) -> CheckResult | None:
    for outcome in (Outcome.BLOCK, Outcome.REVIEW, Outcome.NOT_RUN):
        for c in checks:
            if c.outcome == outcome:
                return c
    return None


def combine(checks: list[CheckResult]) -> tuple[CheckStatus, CheckStatus, Recommendation, CheckResult | None, list[str]]:
    commercial = category_status(checks, Category.COMMERCIAL)
    receiving = category_status(checks, Category.RECEIVING)
    missing = missing_mandatory(checks)
    if CheckStatus.BLOCKED in (commercial, receiving):
        rec = Recommendation.BLOCKED
    elif (commercial == CheckStatus.MATCHED
          and receiving in (CheckStatus.MATCHED, CheckStatus.NOT_REQUIRED)
          and not missing):
        rec = Recommendation.READY_FOR_FINANCE_REVIEW
    else:
        rec = Recommendation.NEEDS_REVIEW
    return commercial, receiving, rec, primary_exception(checks), missing
