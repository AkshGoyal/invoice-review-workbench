"""Phase 1 structured-input domain check.

Runs the pure matching and decision path on a known structured invoice (no PDF,
no model call) against a seed snapshot, and prints the decision.

Usage:
  uv run python -m scripts.run_structured H1 [--snapshot S1] [--json PATH]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from src.domain.money import fmt_inr, fmt_qty
from src.domain.pipeline import evaluate
from src.services.loaders import load_policy, load_snapshot, load_structured

SYMBOL = {"PASS": "PASS ", "REVIEW": "REVIEW", "BLOCK": "BLOCK", "NOT_RUN": "NOTRUN", "NOT_REQUIRED": "N/REQ"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case", help="Structured fixture ID, e.g. H1")
    ap.add_argument("--snapshot", default="S1")
    ap.add_argument("--json", type=Path, help="Write the full DecisionResult JSON here")
    args = ap.parse_args()

    policy = load_policy()
    snap = load_snapshot(args.snapshot, policy)
    inv = load_structured(args.case)
    result = evaluate(inv, snap, policy, evaluated_at=datetime.now(timezone.utc))

    print("=" * 78)
    print(f"STRUCTURED-INPUT DOMAIN CHECK  |  {inv.input_label}")
    print(f"Snapshot {result.snapshot_id} v{result.snapshot_version} as of {result.snapshot_as_of.isoformat()}  |  {result.policy_version}")
    print("=" * 78)
    print(f"Recommendation : {result.recommendation}")
    print(f"Commercial     : {result.commercial_status}    Receiving: {result.receiving_status}")
    print(f"Headline       : {result.headline}")
    print(f"Next action    : {result.next_action}" + (f"  (owner: {result.next_owner})" if result.next_owner else ""))
    print()
    print(f"Invoice {inv.invoice_number.value} dated {inv.invoice_date.value}, total {fmt_inr(inv.total.value)}")
    print(f"Buyer   : {result.buyer_match.status} {result.buyer_match.selected_id or ''}  ({result.buyer_match.basis})")
    print(f"Supplier: {result.supplier_match.status} {result.supplier_match.selected_id or ''}  ({result.supplier_match.basis})")
    if result.po_match:
        print(f"PO      : {result.po_match.status} {result.po_match.selected_id or ''}  ({result.po_match.basis})")
        for c in result.po_match.candidates:
            print(f"   candidate {c.candidate_id}: " + " | ".join(c.reasons))
    for lm in result.line_matches:
        print(f"Line {lm.source_line_id}: {lm.status} -> {lm.po_number}/{lm.po_line_no} {lm.sku or ''} {lm.issue_code or ''} {lm.detail}")

    ctx = result.po_context
    if ctx:
        print()
        print(f"PO {ctx.po_number} balances (from snapshot, before this invoice):")
        print(f"  Gross cap {fmt_inr(ctx.gross_cap)} - prior recorded {fmt_inr(ctx.prior_gross)} = remaining {fmt_inr(ctx.remaining_gross)}; "
              f"this invoice {fmt_inr(ctx.invoice_gross)}")
        for lc in ctx.lines:
            print(f"  Line {lc.line_no} {lc.sku}: ordered {fmt_qty(lc.ordered_qty)} - billed {fmt_qty(lc.prior_billed_qty)} "
                  f"= {fmt_qty(lc.remaining_ordered_qty)} {lc.unit} remaining on order; received {fmt_qty(lc.received_qty)} - billed "
                  f"{fmt_qty(lc.prior_billed_qty)} = {fmt_qty(lc.receipt_available_qty)} {lc.unit} received and unbilled; this invoice {fmt_qty(lc.invoice_qty)} {lc.unit}")
            print(f"     receipts: {', '.join(lc.receipt_refs) or 'none'}; prior bills: {', '.join(lc.prior_bill_refs) or 'none'}")

    print()
    print("Checks:")
    for c in result.checks:
        subj = f" [{c.subject}]" if c.subject else ""
        issue = f" {c.issue_code}" if c.issue_code else ""
        print(f"  {SYMBOL[c.outcome]}  {c.code}{subj}{issue}: {c.reason}")
        if c.expected or c.observed:
            print(f"          expected {c.expected or '-'} | observed {c.observed or '-'}"
                  + (f" | diff {c.difference}" if c.difference else ""))
    print()
    print("Scope:")
    for s in result.scope_limits:
        print(f"  - {s}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(result.model_dump_json(indent=2))
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
