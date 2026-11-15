"""Independent commercial and receiving checks (SPEC.md §6, §7).

Each check is a small pure function returning CheckResult(s). Checks never
compensate for each other: a total that fits cannot excuse a wrong price, item,
party or missing receipt. Missing inputs yield REVIEW or NOT_RUN, never PASS.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .duplicates import DuplicateKind, DuplicateResult
from .invoice import Evidenced, InvoiceLine, NormalizedInvoice
from .money import ZERO, fmt_inr, fmt_qty, money
from .policy import Policy
from .results import (
    Category, CheckResult, LineMatch, MatchResult, Outcome, Owner, PendingExposure, POContext, POLineContext,
)
from .snapshot import BillState, MatchPolicy, Party, PartyStatus, POHeader, POStatus, Snapshot

C, R = Category.COMMERCIAL, Category.RECEIVING


def ev(field: Evidenced, name: str) -> list[str]:
    if field.evidence is None:
        return []
    e = field.evidence
    blocks = ",".join(e.block_ids)
    return [f"{name}: page {e.page}{' [' + blocks + ']' if blocks else ''} \"{e.quote}\""]


def not_run(code: str, category: Category, deps: list[str], reason: str, subject: str | None = None) -> CheckResult:
    return CheckResult(code=code, category=category, outcome=Outcome.NOT_RUN, subject=subject,
                       dependency_codes=deps, reason=reason)


# --------------------------------------------------------------------------- document scope and evidence


def check_scope(inv: NormalizedInvoice, policy: Policy) -> CheckResult:
    problems = []
    doc_type = inv.document_type.value
    if doc_type is None or doc_type not in policy.supported_document_types:
        problems.append(f"document type {doc_type or 'unknown'} (supported: {', '.join(policy.supported_document_types)})")
    if inv.currency.value != policy.supported_currency:
        problems.append(f"currency {inv.currency.value or 'unknown'} (supported: {policy.supported_currency})")
    if inv.tax_basis.value not in policy.supported_tax_bases:
        problems.append(f"tax basis {inv.tax_basis.value or 'unknown'} (supported: {', '.join(policy.supported_tax_bases)})")
    refs = ev(inv.document_type, "document_type") + ev(inv.currency, "currency") + ev(inv.tax_basis, "tax_basis")
    if problems:
        return CheckResult(code="SCOPE", category=C, outcome=Outcome.REVIEW, issue_code="UNSUPPORTED_SCOPE",
                           observed="; ".join(problems), reason="Outside the supported scope: " + "; ".join(problems),
                           evidence_refs=refs, owner=Owner.AP,
                           next_action="Route to the existing AP process for this document type, currency or tax basis.")
    return CheckResult(code="SCOPE", category=C, outcome=Outcome.PASS, observed=f"{doc_type}, {inv.currency.value}, tax {inv.tax_basis.value}",
                       reason="Document type, currency and tax basis are supported.", evidence_refs=refs)


def check_required_evidence(inv: NormalizedInvoice) -> CheckResult:
    """Mandatory fields must have a value and source evidence. Unknowns are never filled in."""
    required: list[tuple[str, Evidenced]] = [
        ("invoice_number", inv.invoice_number), ("invoice_date", inv.invoice_date),
        ("supplier.name", inv.supplier.name), ("bill_to.name", inv.bill_to.name),
        ("currency", inv.currency), ("tax", inv.tax), ("total", inv.total),
    ]
    for line in inv.lines:
        for name in ("quantity", "unit", "unit_price", "net_amount"):
            required.append((f"{line.source_line_id}.{name}", getattr(line, name)))
    missing = [n for n, f in required if f.value is None]
    unsupported = [n for n, f in required if f.value is not None and f.evidence is None]
    if not inv.lines:
        missing.append("lines")
    if missing or unsupported:
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing))
        if unsupported:
            parts.append("no source evidence: " + ", ".join(unsupported))
        return CheckResult(code="REQUIRED_EVIDENCE", category=C, outcome=Outcome.REVIEW,
                           issue_code="FIELD_MISSING" if missing else "EVIDENCE_MISSING",
                           observed="; ".join(parts), reason="Mandatory invoice fields are unknown or unsupported (" + "; ".join(parts) + ").",
                           owner=Owner.AP, next_action="Request a clearer or corrected invoice; do not infer values from the PO or filename.")
    return CheckResult(code="REQUIRED_EVIDENCE", category=C, outcome=Outcome.PASS,
                       observed=f"{len(required)} mandatory fields present with source evidence",
                       reason="All mandatory fields have values and source evidence.")


def check_document_warnings(inv: NormalizedInvoice) -> CheckResult:
    """Conflicting values, several POs/invoices or embedded instructions flagged during extraction."""
    if inv.warnings:
        return CheckResult(code="DOCUMENT_WARNINGS", category=C, outcome=Outcome.REVIEW, issue_code="DOCUMENT_ANOMALY",
                           observed="; ".join(inv.warnings),
                           reason="The document reading flagged: " + "; ".join(inv.warnings),
                           owner=Owner.AP, next_action="AP reviews the flagged text on the source page before relying on the extraction.")
    return CheckResult(code="DOCUMENT_WARNINGS", category=C, outcome=Outcome.PASS,
                       reason="No conflicting values, multiple orders or embedded instructions were flagged.")


# --------------------------------------------------------------------------- parties


def check_party(code: str, role: str, match: MatchResult, party: Party | None, inv: NormalizedInvoice) -> list[CheckResult]:
    printed = inv.bill_to if code == "BUYER_IDENTITY" else inv.supplier
    refs = ev(printed.name, f"{role} name") + ev(printed.registration_id, f"{role} ID")
    if not match.resolved:
        owner = Owner.AP if code == "BUYER_IDENTITY" else Owner.PROCUREMENT
        action = ("Request corrected billing or a documented buyer legal-entity verification. Ship-to alone is insufficient."
                  if code == "BUYER_IDENTITY"
                  else "Procurement verifies the supplier against the vendor master and a supporting reference.")
        reason = f"{role.capitalize()} identity not confirmed: {match.basis}."
        if code == "BUYER_IDENTITY" and inv.ship_to and inv.ship_to.name.value:
            reason += " Ship-to was not used to establish the buyer."
        return [CheckResult(code=code, category=C, outcome=Outcome.REVIEW, issue_code=match.issue_code,
                            observed=printed.name.value, reason=reason, evidence_refs=refs, owner=owner, next_action=action)]
    out = [CheckResult(code=code, category=C, outcome=Outcome.PASS, expected=party.legal_name if party else None,
                       observed=printed.name.value, reason=f"{role.capitalize()} confirmed as {match.selected_id} by {match.basis}.",
                       evidence_refs=refs)]
    if party is not None and party.status != PartyStatus.ACTIVE:
        out.append(CheckResult(code=f"{code}_STATUS", category=C, outcome=Outcome.BLOCK, issue_code=f"{role.upper()}_{party.status}",
                               observed=str(party.status), expected="ACTIVE",
                               reason=f"{party.legal_name} is {party.status} in snapshot master data.",
                               owner=Owner.PROCUREMENT, next_action="Procurement reviews the master-record status before this bill can proceed."))
    return out


# --------------------------------------------------------------------------- duplicates


def check_duplicate(result: DuplicateResult, inv: NormalizedInvoice) -> CheckResult:
    refs = ev(inv.invoice_number, "invoice_number")
    k = result.match
    if result.kind == DuplicateKind.NONE:
        return CheckResult(code="DUPLICATE", category=C, outcome=Outcome.PASS, observed=inv.invoice_number.value,
                           reason="No recorded bill has this buyer, supplier and invoice number.", evidence_refs=refs)
    if result.kind == DuplicateKind.DUPLICATE_BILL:
        return CheckResult(code="DUPLICATE", category=C, outcome=Outcome.BLOCK, issue_code="DUPLICATE_BILL",
                           expected="New invoice number", observed=f"{inv.invoice_number.value} = {k.bill_id} ({k.source})",
                           reason=f"Invoice {inv.invoice_number.value} is already recorded as {k.bill_id} for the same buyer and supplier with the same gross {fmt_inr(k.gross_amount)}.",
                           evidence_refs=refs, owner=Owner.AP,
                           next_action=f"Link this attempt to existing bill {k.bill_id}; do not create a new payable.")
    if result.kind == DuplicateKind.INVOICE_IDENTITY_CONFLICT:
        return CheckResult(code="DUPLICATE", category=C, outcome=Outcome.REVIEW, issue_code="INVOICE_IDENTITY_CONFLICT",
                           expected=f"{k.bill_id} gross {fmt_inr(k.gross_amount)}", observed=f"gross {fmt_inr(inv.total.value)}",
                           reason=f"Invoice number {inv.invoice_number.value} is already recorded as {k.bill_id}, but the amount differs.",
                           evidence_refs=refs, owner=Owner.AP,
                           next_action="Contact the supplier: confirm whether this replaces the recorded bill. The recorded bill is not overwritten.")
    return CheckResult(code="DUPLICATE", category=C, outcome=Outcome.REVIEW, issue_code="SUSPECTED_DUPLICATE",
                       observed=f"{inv.invoice_number.value} resembles {k.invoice_number} ({k.bill_id})",
                       reason=f"Invoice number {inv.invoice_number.value} differs from recorded {k.invoice_number} only by punctuation or spacing.",
                       evidence_refs=refs, owner=Owner.AP, next_action="AP confirms whether this is the same bill before proceeding.")


# --------------------------------------------------------------------------- purchase order


def check_po_resolution(match: MatchResult, inv: NormalizedInvoice) -> CheckResult:
    refs = ev(inv.po_reference, "po_reference")
    if match.resolved:
        return CheckResult(code="PO_RESOLUTION", category=C, outcome=Outcome.PASS, observed=match.selected_id,
                           reason=f"Order {match.selected_id} selected: {match.basis}.", evidence_refs=refs)
    actions = {
        "PO_AMBIGUOUS": "Compare the candidate orders and confirm one using a delivery or procurement reference; then recheck.",
        "PO_CONFIRMATION_REQUIRED": "Confirm the proposed order using a delivery or procurement reference; then recheck.",
        "PO_PARTY_CONFLICT": "Procurement confirms the correct order; the supplier may need to reissue the invoice. No substitute PO is chosen automatically.",
        "PO_NOT_FOUND": "Procurement provides the correct PO reference or confirms a non-PO process.",
    }
    return CheckResult(code="PO_RESOLUTION", category=C, outcome=Outcome.REVIEW, issue_code=match.issue_code,
                       observed=inv.po_reference.value or "no PO reference printed", reason=match.basis,
                       evidence_refs=refs, owner=Owner.PROCUREMENT,
                       next_action=actions.get(match.issue_code or "", "Procurement confirms the order."))


def check_po_status(po: POHeader, inv: NormalizedInvoice, policy: Policy) -> CheckResult:
    if po.status != POStatus.OPEN:
        return CheckResult(code="PO_STATUS", category=C, outcome=Outcome.BLOCK, issue_code="PO_NOT_OPEN",
                           expected="OPEN", observed=str(po.status),
                           reason=f"{po.po_number} is {po.status}; this submission cannot proceed against it.",
                           owner=Owner.PROCUREMENT, next_action="Procurement confirms the correct open order or reopens per its own process.")
    if po.currency != policy.supported_currency:
        return CheckResult(code="PO_STATUS", category=C, outcome=Outcome.REVIEW, issue_code="CURRENCY_UNSUPPORTED",
                           observed=po.currency, reason=f"{po.po_number} currency is {po.currency}.", owner=Owner.AP,
                           next_action="Route to the existing AP process for foreign currency.")
    inv_date = inv.invoice_date.value
    if inv_date is not None and po.po_date > inv_date:
        return CheckResult(code="PO_STATUS", category=C, outcome=Outcome.REVIEW, issue_code="PO_AFTER_INVOICE",
                           expected=f"PO dated on or before {inv_date}", observed=str(po.po_date),
                           reason=f"{po.po_number} is dated after the invoice.", owner=Owner.PROCUREMENT,
                           next_action="Procurement confirms whether the order reference is correct.")
    return CheckResult(code="PO_STATUS", category=C, outcome=Outcome.PASS,
                       observed=f"{po.po_number} v{po.version} {po.status}, {po.currency}, dated {po.po_date}",
                       reason="Order is open, in INR and dated on or before the invoice.")


# --------------------------------------------------------------------------- lines


def check_line_match(lm: LineMatch, line: InvoiceLine) -> CheckResult:
    refs = ev(line.description, "description") + ev(line.supplier_item_code, "supplier code") + ev(line.printed_po_line, "PO line")
    if lm.resolved:
        conv = "" if lm.conversion_factor == 1 else f"; {lm.invoice_unit} x {lm.conversion_factor} -> {lm.base_unit} ({lm.conversion_source})"
        return CheckResult(code="LINE_MATCH", category=C, outcome=Outcome.PASS, subject=lm.source_line_id,
                           observed=f"{line.description.value or ''} -> {lm.po_number}/{lm.po_line_no} {lm.sku}",
                           reason=f"Matched by {lm.method}{conv}.", evidence_refs=refs)
    owners = {"UNIT_CONVERSION_REQUIRED": Owner.PROCUREMENT, "SPECIFICATION_CONFLICT": Owner.PROCUREMENT}
    actions = {
        "SPECIFICATION_CONFLICT": "Procurement checks which item was delivered and billed; the supplier corrects the invoice if needed.",
        "UNIT_CONVERSION_REQUIRED": "Request an approved unit conversion for this item; do not assume pack weight or size.",
        "ITEM_UNRESOLVED": "Confirm the item against the PO specification with a supporting reference; a suggestion is not a match.",
        "ITEM_UNVERIFIED": "Confirm the item against the PO specification with a supporting reference.",
        "PO_LINE_AMBIGUOUS": "Identify the correct PO line or delivery schedule.",
    }
    return CheckResult(code="LINE_MATCH", category=C, outcome=Outcome.REVIEW, subject=lm.source_line_id,
                       issue_code=lm.issue_code, observed=line.description.value, reason=lm.detail, evidence_refs=refs,
                       owner=owners.get(lm.issue_code or "", Owner.PROCUREMENT),
                       next_action=actions.get(lm.issue_code or "", "Procurement resolves the item and PO line."))


def check_unit_price(lm: LineMatch, line: InvoiceLine, snap: Snapshot, policy: Policy) -> CheckResult:
    po_line = next(l for l in snap.lines_of(lm.po_number) if l.line_no == lm.po_line_no)
    price = line.unit_price.value
    refs = ev(line.unit_price, "unit_price")
    if price is None:
        return CheckResult(code="UNIT_PRICE", category=C, outcome=Outcome.REVIEW, subject=lm.source_line_id,
                           issue_code="FIELD_MISSING", reason="Invoice unit price is not stated.", owner=Owner.AP,
                           next_action="Request a corrected invoice with the net unit price.")
    expected = po_line.net_unit_price * lm.conversion_factor  # price per invoice unit
    diff = price - expected
    if abs(diff) > policy.unit_price_tolerance:
        return CheckResult(code="UNIT_PRICE", category=C, outcome=Outcome.REVIEW, subject=lm.source_line_id,
                           issue_code="PRICE_VARIANCE", expected=f"{fmt_inr(expected)} per {lm.invoice_unit}",
                           observed=f"{fmt_inr(price)} per {lm.invoice_unit}", difference=fmt_inr(diff),
                           reason=f"Unit price differs from the approved PO price on {lm.po_number}/{lm.po_line_no} (tolerance {fmt_inr(policy.unit_price_tolerance)}).",
                           evidence_refs=refs, owner=Owner.PROCUREMENT,
                           next_action="Procurement confirms the agreed price; the supplier issues a corrected invoice if the PO price stands.")
    return CheckResult(code="UNIT_PRICE", category=C, outcome=Outcome.PASS, subject=lm.source_line_id,
                       expected=fmt_inr(expected), observed=fmt_inr(price), difference=fmt_inr(diff),
                       reason=f"Unit price equals the approved PO price on {lm.po_number}/{lm.po_line_no}.", evidence_refs=refs)


def check_line_arithmetic(line: InvoiceLine, policy: Policy) -> CheckResult:
    q, p, n = line.quantity.value, line.unit_price.value, line.net_amount.value
    refs = ev(line.quantity, "quantity") + ev(line.unit_price, "unit_price") + ev(line.net_amount, "net_amount")
    if q is None or p is None or n is None:
        return CheckResult(code="LINE_ARITHMETIC", category=C, outcome=Outcome.REVIEW, subject=line.source_line_id,
                           issue_code="FIELD_MISSING", reason="Quantity, unit price or net amount is not stated.",
                           owner=Owner.AP, next_action="Request a corrected invoice with complete line values.")
    computed = money(q * p)
    diff = n - computed
    outcome = Outcome.PASS if abs(diff) <= policy.line_arithmetic_tolerance else Outcome.REVIEW
    return CheckResult(code="LINE_ARITHMETIC", category=C, outcome=outcome, subject=line.source_line_id,
                       issue_code=None if outcome == Outcome.PASS else "LINE_ARITHMETIC_MISMATCH",
                       expected=f"{fmt_qty(q)} x {fmt_inr(p)} = {fmt_inr(computed)}", observed=fmt_inr(n), difference=fmt_inr(diff),
                       reason=("Line amount equals quantity x unit price." if outcome == Outcome.PASS
                               else f"Line amount differs from quantity x unit price by more than {fmt_inr(policy.line_arithmetic_tolerance)}."),
                       evidence_refs=refs,
                       owner=None if outcome == Outcome.PASS else Owner.AP,
                       next_action=None if outcome == Outcome.PASS else "Request a corrected invoice from the supplier.")


def check_invoice_arithmetic(inv: NormalizedInvoice, policy: Policy) -> CheckResult:
    nets = [l.net_amount.value for l in inv.lines]
    tax, total = inv.tax.value, inv.total.value
    refs = ev(inv.subtotal, "subtotal") + ev(inv.tax, "tax") + ev(inv.charges, "charges") + ev(inv.discount, "discount") + ev(inv.total, "total")
    review = dict(code="INVOICE_ARITHMETIC", category=C, outcome=Outcome.REVIEW, evidence_refs=refs, owner=Owner.AP)
    if any(n is None for n in nets) or total is None:
        return CheckResult(**review, issue_code="FIELD_MISSING", reason="Line net amounts or the invoice total are not stated.",
                           next_action="Request a corrected invoice with complete totals.")
    if tax is None:
        return CheckResult(**review, issue_code="TAX_MISSING", reason="Tax is not stated. Missing tax is not treated as zero.",
                           next_action="Request a corrected invoice that states tax separately.")
    charges = inv.charges.value if inv.charges.value is not None else ZERO
    discount = inv.discount.value if inv.discount.value is not None else ZERO
    if charges != ZERO or discount != ZERO:
        return CheckResult(**review, issue_code="CHARGES_OR_DISCOUNT_UNSUPPORTED",
                           observed=f"charges {fmt_inr(charges)}, discount {fmt_inr(discount)}",
                           reason="Freight, other charges or discounts are not supported by automatic checks in this demo.",
                           next_action="AP reviews the charge or discount against the PO terms manually.")
    line_sum = sum(nets, ZERO)
    problems = []
    if inv.subtotal.value is not None and abs(inv.subtotal.value - line_sum) > policy.invoice_arithmetic_tolerance:
        problems.append(f"subtotal {fmt_inr(inv.subtotal.value)} vs line sum {fmt_inr(line_sum)}")
    computed_total = line_sum + tax + charges - discount
    diff = total - computed_total
    if abs(diff) > policy.invoice_arithmetic_tolerance:
        problems.append(f"total {fmt_inr(total)} vs lines + tax + charges - discount {fmt_inr(computed_total)}")
    if problems:
        return CheckResult(**review, issue_code="INVOICE_ARITHMETIC_MISMATCH", expected=fmt_inr(computed_total),
                           observed=fmt_inr(total), difference=fmt_inr(diff),
                           reason="Invoice totals do not reconcile: " + "; ".join(problems) + f" (tolerance {fmt_inr(policy.invoice_arithmetic_tolerance)}).",
                           next_action="Request a corrected invoice from the supplier.")
    return CheckResult(code="INVOICE_ARITHMETIC", category=C, outcome=Outcome.PASS,
                       expected=f"{fmt_inr(line_sum)} + tax {fmt_inr(tax)} = {fmt_inr(computed_total)}",
                       observed=fmt_inr(total), difference=fmt_inr(diff),
                       reason="Line sum, tax and declared total reconcile.", evidence_refs=refs)


# --------------------------------------------------------------------------- PO capacity and receipts


def build_po_context(po: POHeader, matches: list[LineMatch], inv: NormalizedInvoice, snap: Snapshot) -> POContext:
    """Balances per PO line. Invoice rows mapped to the same PO line are aggregated."""
    agg: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for lm in matches:
        if lm.resolved and lm.base_quantity is not None:
            agg[lm.po_line_no] += lm.base_quantity
    lines = []
    for pl in snap.lines_of(po.po_number):
        posted = snap.billed_lines_for(po.po_number, pl.line_no, BillState.POSTED)
        billed = sum((b.quantity for b in posted), ZERO)
        receipts = snap.receipts_for(po.po_number, pl.line_no)
        received = sum((r.received_qty for r in receipts), ZERO)
        accepted = (sum((r.accepted_qty or ZERO for r in receipts), ZERO)
                    if pl.match_policy == MatchPolicy.ACCEPTANCE_REQUIRED else None)
        pool = accepted if accepted is not None else received
        lines.append(POLineContext(
            po_number=po.po_number, line_no=pl.line_no, sku=pl.sku, unit=pl.unit, match_policy=str(pl.match_policy),
            net_unit_price=pl.net_unit_price, ordered_qty=pl.quantity, prior_billed_qty=billed,
            remaining_ordered_qty=pl.quantity - billed, received_qty=received, accepted_qty=accepted,
            receipt_available_qty=pool - billed,
            receipt_refs=[f"{r.receipt_id} {r.receipt_date} {fmt_qty(r.received_qty)} {pl.unit}" for r in receipts],
            prior_bill_refs=[f"{b.bill_id} {fmt_qty(b.quantity)} {pl.unit}" for b in posted],
            invoice_qty=agg.get(pl.line_no, ZERO),
        ))
    posted_bills = {pl.bill_id for pl in snap.prior_invoice_lines if pl.po_number == po.po_number
                    and (b := snap.bill(pl.bill_id)) is not None and b.state == BillState.POSTED}
    prior_gross = sum((pl.gross_amount for pl in snap.prior_invoice_lines
                       if pl.po_number == po.po_number and pl.bill_id in posted_bills), ZERO)
    pending = []
    for b in snap.prior_invoices:
        if b.state == BillState.DRAFT and any(pl.bill_id == b.bill_id and pl.po_number == po.po_number for pl in snap.prior_invoice_lines):
            pending.append(PendingExposure(bill_id=b.bill_id, invoice_number=b.invoice_number, gross_amount=b.gross_amount,
                                           note="Draft in ledger: shown as exposure, not counted as billed"))
    return POContext(po_number=po.po_number, po_version=po.version, po_date=po.po_date, status=str(po.status),
                     gross_cap=po.gross_cap, prior_gross=prior_gross, remaining_gross=po.gross_cap - prior_gross,
                     invoice_gross=inv.total.value, lines=lines, pending=pending)


def check_po_quantities(ctx: POContext, policy: Policy) -> list[CheckResult]:
    out = []
    for lc in ctx.lines:
        if lc.invoice_qty == ZERO:
            continue
        subject = f"{lc.po_number}/{lc.line_no}"
        if lc.remaining_ordered_qty < ZERO:
            out.append(CheckResult(code="PO_LINE_QUANTITY", category=C, outcome=Outcome.REVIEW, subject=subject,
                                   issue_code="LEDGER_INCONSISTENT",
                                   observed=f"prior billed {fmt_qty(lc.prior_billed_qty)} > ordered {fmt_qty(lc.ordered_qty)}",
                                   reason="Existing data inconsistency: prior billing already exceeds the ordered quantity.",
                                   owner=Owner.AP, next_action="AP and procurement reconcile the ledger before this bill proceeds."))
            continue
        excess = lc.invoice_qty - lc.remaining_ordered_qty
        ok = excess <= policy.overbilling_quantity_tolerance
        out.append(CheckResult(
            code="PO_LINE_QUANTITY", category=C, outcome=Outcome.PASS if ok else Outcome.REVIEW, subject=subject,
            issue_code=None if ok else "QUANTITY_EXCEEDS_ORDER",
            expected=f"<= {fmt_qty(lc.remaining_ordered_qty)} {lc.unit} (ordered {fmt_qty(lc.ordered_qty)} - prior billed {fmt_qty(lc.prior_billed_qty)})",
            observed=f"{fmt_qty(lc.invoice_qty)} {lc.unit}",
            difference=None if ok else f"{fmt_qty(excess)} {lc.unit} over",
            reason=(f"Invoice quantity fits the {fmt_qty(lc.remaining_ordered_qty)} {lc.unit} ordered and not yet billed." if ok
                    else f"Invoice quantity exceeds the ordered-and-unbilled quantity by {fmt_qty(excess)} {lc.unit}."),
            evidence_refs=lc.prior_bill_refs,
            owner=None if ok else Owner.PROCUREMENT,
            next_action=None if ok else "Procurement confirms an order amendment, or the supplier corrects the quantity."))
    return out


def check_po_amount(ctx: POContext, policy: Policy) -> CheckResult:
    gross = ctx.invoice_gross
    if gross is None:
        return CheckResult(code="PO_AMOUNT", category=C, outcome=Outcome.REVIEW, subject=ctx.po_number, issue_code="FIELD_MISSING",
                           reason="Invoice gross total is unknown, so the PO cap cannot be checked.", owner=Owner.AP,
                           next_action="Request a corrected invoice with a total.")
    excess = gross - ctx.remaining_gross
    ok = excess <= policy.po_overspend_tolerance
    return CheckResult(
        code="PO_AMOUNT", category=C, outcome=Outcome.PASS if ok else Outcome.REVIEW, subject=ctx.po_number,
        issue_code=None if ok else "PO_AMOUNT_EXCEEDED",
        expected=f"<= {fmt_inr(ctx.remaining_gross)} (cap {fmt_inr(ctx.gross_cap)} - prior recorded {fmt_inr(ctx.prior_gross)})",
        observed=fmt_inr(gross), difference=None if ok else f"{fmt_inr(excess)} over",
        reason=(f"Invoice gross fits the {fmt_inr(ctx.remaining_gross)} remaining under the tax-inclusive PO cap." if ok
                else f"Invoice gross exceeds the remaining PO cap by {fmt_inr(excess)}."),
        owner=None if ok else Owner.PROCUREMENT,
        next_action=None if ok else "Procurement confirms an order amendment, or the supplier corrects the bill.")


def check_receipts(ctx: POContext, snap: Snapshot) -> list[CheckResult]:
    out = []
    for lc in ctx.lines:
        if lc.invoice_qty == ZERO:
            continue
        subject = f"{lc.po_number}/{lc.line_no}"
        policy = MatchPolicy(lc.match_policy)
        if policy == MatchPolicy.PO_ONLY:
            out.append(CheckResult(code="RECEIPT", category=R, outcome=Outcome.NOT_REQUIRED, subject=subject,
                                   reason=f"PO line policy is PO_ONLY (snapshot {snap.snapshot_id}); receipt matching not required."))
            continue
        acceptance = policy == MatchPolicy.ACCEPTANCE_REQUIRED
        pool_name = "accepted" if acceptance else "received"
        pool = lc.accepted_qty if acceptance else lc.received_qty
        complete = snap.coverage.acceptance_complete if acceptance else snap.coverage.receipts_complete
        if not lc.receipt_refs:
            out.append(CheckResult(
                code="RECEIPT", category=R, outcome=Outcome.REVIEW, subject=subject, issue_code="RECEIPT_UNCONFIRMED",
                expected=f"{fmt_qty(lc.invoice_qty)} {lc.unit} {pool_name} and unbilled", observed="no receipt record in snapshot",
                reason=("No receipt record for this PO line in the snapshot" + ("" if complete else " (receipt coverage is incomplete)")
                        + ". This is not proof of non-delivery."),
                owner=Owner.STORES, next_action="Stores confirms the delivery or updates the receipt record."))
            continue
        if lc.receipt_available_qty < ZERO:
            out.append(CheckResult(code="RECEIPT", category=R, outcome=Outcome.REVIEW, subject=subject, issue_code="LEDGER_INCONSISTENT",
                                   observed=f"prior billed {fmt_qty(lc.prior_billed_qty)} > {pool_name} {fmt_qty(pool)}",
                                   reason=f"Existing data inconsistency: prior billing exceeds the {pool_name} quantity.",
                                   evidence_refs=lc.receipt_refs + lc.prior_bill_refs, owner=Owner.STORES,
                                   next_action="Stores and AP reconcile receipts against prior bills."))
            continue
        shortfall = lc.invoice_qty - lc.receipt_available_qty
        expected = (f"<= {fmt_qty(lc.receipt_available_qty)} {lc.unit} ({pool_name} {fmt_qty(pool)} - prior billed {fmt_qty(lc.prior_billed_qty)})")
        if shortfall > ZERO:
            code = "ACCEPTANCE_PENDING_OR_SHORT" if acceptance else "RECEIPT_SHORTFALL"
            out.append(CheckResult(
                code="RECEIPT", category=R, outcome=Outcome.REVIEW, subject=subject, issue_code=code,
                expected=expected, observed=f"{fmt_qty(lc.invoice_qty)} {lc.unit}", difference=f"{fmt_qty(shortfall)} {lc.unit} unsupported",
                reason=(f"Only {fmt_qty(lc.receipt_available_qty)} {lc.unit} are {pool_name} and not yet billed; "
                        f"{fmt_qty(shortfall)} {lc.unit} of this invoice lack {pool_name} evidence."
                        + ("" if complete else " Receipt coverage is incomplete.")),
                evidence_refs=lc.receipt_refs + lc.prior_bill_refs,
                owner=Owner.QUALITY if acceptance else Owner.STORES,
                next_action=("Quality resolves the acceptance record." if acceptance
                             else f"Stores confirms whether the remaining {fmt_qty(shortfall)} {lc.unit} were delivered, or the supplier corrects the bill.")))
            continue
        out.append(CheckResult(
            code="RECEIPT", category=R, outcome=Outcome.PASS, subject=subject, expected=expected,
            observed=f"{fmt_qty(lc.invoice_qty)} {lc.unit}",
            reason=f"{fmt_qty(lc.receipt_available_qty)} {lc.unit} are {pool_name} and not yet billed; the invoice quantity is covered.",
            evidence_refs=lc.receipt_refs + lc.prior_bill_refs))
    return out
