"""Pure decision path: normalized invoice + snapshot + policy -> DecisionResult.

Order: scope and evidence -> parties -> duplicate -> PO -> lines -> amounts and
quantities -> receipts -> combine. Dependent checks emit NOT_RUN with their
dependency codes when an upstream identity is unresolved. Arithmetic checks
depend only on the document, so they always run.

No I/O: the caller supplies the evaluation time, known bills and revision ID.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .decisions import combine
from .duplicates import KnownBill, bills_from_snapshot, classify
from .invoice import InputSource, NormalizedInvoice
from .matching import resolve_lines, resolve_party, resolve_po
from .policy import Policy
from .results import (
    Category, CheckResult, DecisionResult, ExecutionState, MatchResult, Outcome, Recommendation,
)
from .rules import (
    check_document_warnings, check_duplicate, check_invoice_arithmetic, check_line_arithmetic, check_line_match, check_party,
    check_po_amount, check_po_quantities, check_po_resolution, check_po_status, check_receipts,
    check_required_evidence, check_scope, check_unit_price, build_po_context, not_run,
)
from .snapshot import Snapshot

C, R = Category.COMMERCIAL, Category.RECEIVING


def evaluate(
    inv: NormalizedInvoice,
    snap: Snapshot,
    policy: Policy,
    *,
    evaluated_at: datetime,
    known_bills: list[KnownBill] | None = None,
    revision_id: str | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> DecisionResult:
    stage = on_stage or (lambda _name: None)
    stage("RESOLVE_PARTIES")
    checks: list[CheckResult] = [check_scope(inv, policy), check_required_evidence(inv), check_document_warnings(inv)]

    buyer_m = resolve_party(inv.bill_to, snap.buyers, snap, limit=policy.max_text_suggestions)
    supplier_m = resolve_party(inv.supplier, snap.suppliers, snap, limit=policy.max_text_suggestions)
    buyer = next((p for p in snap.buyers if p.party_id == buyer_m.selected_id), None)
    supplier = next((p for p in snap.suppliers if p.party_id == supplier_m.selected_id), None)
    checks += check_party("BUYER_IDENTITY", "buyer", buyer_m, buyer, inv)
    checks += check_party("SUPPLIER_IDENTITY", "supplier", supplier_m, supplier, inv)
    parties_ok = buyer_m.resolved and supplier_m.resolved
    party_deps = [c for c, m in (("BUYER_IDENTITY", buyer_m), ("SUPPLIER_IDENTITY", supplier_m)) if not m.resolved]

    # Duplicate detection needs confirmed identities: an unresolved party cannot be deduplicated.
    if parties_ok and inv.invoice_number.value:
        bills = bills_from_snapshot(snap) + list(known_bills or [])
        checks.append(check_duplicate(classify(buyer.party_id, supplier.party_id, inv.invoice_number.value,
                                               inv.total.value, bills), inv))
    else:
        deps = party_deps or ["REQUIRED_EVIDENCE"]
        checks.append(not_run("DUPLICATE", C, deps, "Business-key duplicate check needs confirmed buyer, supplier and invoice number."))

    stage("MATCH_ORDER_AND_LINES")
    po_m: MatchResult | None = None
    po = None
    if parties_ok:
        po_m = resolve_po(inv, buyer.party_id, supplier.party_id, snap,
                          currency=policy.supported_currency, limit=policy.max_po_candidates)
        checks.append(check_po_resolution(po_m, inv))
        if po_m.resolved:
            po = snap.po(po_m.selected_id)
    else:
        checks.append(not_run("PO_RESOLUTION", C, party_deps, "Order matching starts only after buyer and supplier are confirmed."))

    line_matches = []
    po_ctx = None
    if po is not None:
        checks.append(check_po_status(po, inv, policy))
        line_matches = resolve_lines(inv, po, supplier.party_id, snap, limit=policy.max_text_suggestions)
        by_id = {lm.source_line_id: lm for lm in line_matches}
        for line in inv.lines:
            lm = by_id[line.source_line_id]
            checks.append(check_line_match(lm, line))
            if lm.resolved:
                checks.append(check_unit_price(lm, line, snap, policy))
            else:
                checks.append(not_run("UNIT_PRICE", C, ["LINE_MATCH"], "Price comparison needs a verified PO line.", line.source_line_id))
        stage("CHECK_AMOUNTS_AND_RECEIPTS")
        po_ctx = build_po_context(po, line_matches, inv, snap)
        all_lines_ok = all(lm.resolved for lm in line_matches) and bool(line_matches)
        if all_lines_ok:
            checks += check_po_quantities(po_ctx, policy)
            checks.append(check_po_amount(po_ctx, policy))
            checks += check_receipts(po_ctx, snap)
        else:
            # Partial capacity results would understate the bill; hold all capacity checks.
            checks.append(not_run("PO_LINE_QUANTITY", C, ["LINE_MATCH"], "Quantity capacity is checked once every invoice line maps to a verified PO line."))
            checks.append(not_run("PO_AMOUNT", C, ["LINE_MATCH"], "PO amount is checked once every invoice line maps to a verified PO line."))
            checks.append(not_run("RECEIPT", R, ["LINE_MATCH"], "Receipt evidence is checked once every invoice line maps to a verified PO line."))
    else:
        dep = ["PO_RESOLUTION"] if parties_ok else party_deps
        checks.append(not_run("PO_STATUS", C, dep, "Needs a confirmed order."))
        for line in inv.lines:
            checks.append(not_run("LINE_MATCH", C, dep, "Line matching needs a confirmed order.", line.source_line_id))
            checks.append(not_run("UNIT_PRICE", C, dep, "Price comparison needs a confirmed order.", line.source_line_id))
        checks.append(not_run("PO_LINE_QUANTITY", C, dep, "Needs a confirmed order."))
        checks.append(not_run("PO_AMOUNT", C, dep, "Needs a confirmed order."))
        checks.append(not_run("RECEIPT", R, dep, "Needs a confirmed order."))

    if po is None:
        stage("CHECK_AMOUNTS_AND_RECEIPTS")
    for line in inv.lines:
        checks.append(check_line_arithmetic(line, policy))
    checks.append(check_invoice_arithmetic(inv, policy))

    commercial, receiving, rec, primary, missing = combine(checks)
    if missing:
        checks.append(CheckResult(code="CHECK_COVERAGE", category=C, outcome=Outcome.REVIEW,
                                  issue_code="MANDATORY_CHECK_MISSING", observed=", ".join(missing),
                                  reason="Mandatory checks did not run: " + ", ".join(missing), next_action="Investigate the evaluation."))

    if rec == Recommendation.READY_FOR_FINANCE_REVIEW:
        headline = f"Checks passed against snapshot {snap.snapshot_id}. Ready for finance review."
        next_action, next_owner = "Finance reviews the evidence packet and performs approval in its own system.", None
    else:
        verb = "Blocked" if rec == Recommendation.BLOCKED else "Needs review"
        headline = f"{verb}: {primary.reason}" if primary else f"{verb}."
        next_action = primary.next_action if primary else None
        next_owner = primary.owner if primary else None

    scope_limits = [
        "Recommendation only. Payment approval, reservation and ERP posting remain with finance.",
        f"Checked against procurement snapshot {snap.snapshot_id} as of {snap.as_of.isoformat()}; later receipts or bills are not reflected.",
        "Other pending invoices on the same PO are not reserved; finance must validate combined exposure.",
    ]
    if inv.input_source == InputSource.STRUCTURED_FIXTURE:
        scope_limits.insert(0, "Structured-input domain check: fields were supplied as a fixture, not read from a PDF by a model.")

    return DecisionResult(
        execution_state=ExecutionState.COMPLETED, commercial_status=commercial, receiving_status=receiving,
        recommendation=rec, headline=headline,
        primary_exception=(primary.issue_code or primary.code) if primary else None,
        next_action=next_action, next_owner=next_owner, checks=checks,
        buyer_match=buyer_m, supplier_match=supplier_m, po_match=po_m, line_matches=line_matches, po_context=po_ctx,
        evaluated_at=evaluated_at, snapshot_id=snap.snapshot_id, snapshot_version=snap.version,
        snapshot_as_of=snap.as_of, snapshot_hash=snap.content_hash, policy_version=policy.policy_version,
        revision_id=revision_id, input_source=str(inv.input_source), input_label=inv.input_label,
        scope_limits=scope_limits,
    )
