"""Live evaluation: real OpenRouter calls through the full workflow, one fresh workspace per file.

Usage: uv run python -m scripts.live_eval H1_digital H1_scan H1_two_page [--repeat N]
Compares extracted critical fields with the known H1 values (data/fixtures/structured/H1.json)
and prints decision, latency and calls. Results are stored in the app database (visible in History).
"""

from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path

from src.config import load_settings
from src.services.loaders import load_policy, load_structured_raw
from src.services.workflow import Workbench

PDF_DIR = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "pdf"
CRITICAL = ["invoice_number", "invoice_date", "po_reference", "currency", "subtotal", "tax", "charges", "discount", "total",
            "tax_basis", "document_type", ("supplier", "name"), ("supplier", "registration_id"), ("bill_to", "name"),
            ("bill_to", "registration_id")]
LINE = ["printed_po_line", "supplier_item_code", "description", "quantity", "unit", "unit_price", "net_amount"]


def get(d, key):
    if isinstance(key, tuple):
        d = d.get(key[0]) or {}
        key = key[1]
    return (d.get(key) or {}).get("value")


def norm(v):
    return None if v is None else str(v).replace(",", "").strip().upper().rstrip("0").rstrip(".") if any(c.isdigit() for c in str(v)) and "." in str(v) else str(v).strip().upper()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    expected = load_structured_raw("H1")
    bench = Workbench(load_settings(), load_policy(), recover_on_start=False)
    bench.ensure_snapshot("S1")
    for name in args.files:
        for i in range(args.repeat):
            ws = bench.create_workspace(f"Live eval {name} #{i + 1} {time.strftime('%d %b %H:%M')}")
            data = (PDF_DIR / f"{name}.pdf").read_bytes()
            t0 = time.monotonic()
            run_id = bench.submit(ws, f"{name}.pdf", data, uuid.uuid4().hex)
            bench.wait_idle()
            secs = time.monotonic() - t0
            v = bench.run_view(run_id)
            if v.revision is None:
                print(f"{name} #{i + 1}: {v.run.state} {v.run.error_kind}: {v.run.error_message} ({secs:.1f}s)")
                continue
            inv = v.revision.invoice
            fields = [(str(k), get(expected, k), get(inv, k)) for k in CRITICAL]
            for li, (exp_line, got_line) in enumerate(zip(expected["lines"], inv["lines"]), start=1):
                fields += [(f"L{li}.{k}", get(exp_line, k), get(got_line, k)) for k in LINE]
            fields.append(("line_count", len(expected["lines"]), len(inv["lines"])))
            mism = [(k, e, g) for k, e, g in fields if norm(e) != norm(g)]
            ev = v.revision.provenance["evidence"]
            print(f"{name} #{i + 1}: {v.revision.recommendation} primary={v.revision.primary_exception} | "
                  f"fields {len(fields) - len(mism)}/{len(fields)} agree | {secs:.1f}s | calls {v.run.provider_calls} | "
                  f"reanchored {len(ev['reanchored'])} unsupported {len(ev['unsupported'])} | run {run_id} ws {ws}")
            for k, e, g in mism:
                print(f"    MISMATCH {k}: expected {e!r} got {g!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
