"""Validate a seed snapshot and print its key balances.

Usage: uv run python -m scripts.validate_snapshot S1
"""

from __future__ import annotations

import sys

from src.domain.money import ZERO, fmt_inr, fmt_qty
from src.domain.snapshot import BillState, SnapshotValidationError
from src.services.loaders import load_snapshot


def main(argv: list[str]) -> int:
    snapshot_id = argv[0] if argv else "S1"
    try:
        snap = load_snapshot(snapshot_id)
    except SnapshotValidationError as exc:
        print(f"INVALID snapshot {snapshot_id}:")
        for e in exc.errors:
            print(f"  - {e}")
        return 1
    print(f"Snapshot {snap.snapshot_id} v{snap.version} OK  ({snap.label})")
    print(f"  as of {snap.as_of.isoformat()}  hash {snap.content_hash[:23]}...")
    print(f"  coverage: {snap.coverage.model_dump()}")
    for po in snap.po_headers:
        prior_gross = sum((l.gross_amount for l in snap.prior_invoice_lines
                           if l.po_number == po.po_number and snap.bill(l.bill_id).state == BillState.POSTED), ZERO)
        print(f"  {po.po_number} ({po.status}, {po.po_date}) cap {fmt_inr(po.gross_cap)}, "
              f"prior recorded {fmt_inr(prior_gross)}, remaining {fmt_inr(po.gross_cap - prior_gross)}")
        for line in snap.lines_of(po.po_number):
            billed = sum((b.quantity for b in snap.billed_lines_for(po.po_number, line.line_no, BillState.POSTED)), ZERO)
            received = sum((r.received_qty for r in snap.receipts_for(po.po_number, line.line_no)), ZERO)
            print(f"    line {line.line_no} {line.sku}: ordered {fmt_qty(line.quantity)}, received {fmt_qty(received)}, "
                  f"billed {fmt_qty(billed)} -> ordered-unbilled {fmt_qty(line.quantity - billed)}, "
                  f"received-unbilled {fmt_qty(received - billed)} {line.unit} [{line.match_policy}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
