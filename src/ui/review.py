"""Review screen: decision first, then source evidence, parties, PO, lines and checks."""

from __future__ import annotations

import json
from pathlib import Path

import pypdfium2 as pdfium
import streamlit as st

from src.ui.common import (
    ATTEMPT_LABEL, CHECK_LABEL, OUTCOME_ICON, REC_COLOR, REC_LABEL, RUN_STATE_LABEL, STATUS_LABEL, current_workspace,
    get_workbench, money, sidebar,
)

bench = get_workbench()
ws = current_workspace(bench)
sidebar(bench, ws)

st.title("Review")

runs = [r for r in bench.runs(ws) if r.state != "QUEUED"]
if not runs:
    st.info("No runs in this workspace yet. Process an invoice first.")
    st.stop()

ids = [r.id for r in runs]
wanted = st.query_params.get("run")
idx = ids.index(wanted) if wanted in ids else 0
label = {r.id: f"{r.filename} · {r.created_at:%d %b %H:%M:%S} · {RUN_STATE_LABEL.get(r.state, r.state)} · {r.id}" for r in runs}
choice = st.selectbox("Run", ids, index=idx, format_func=label.get)
if choice != wanted:
    st.query_params["run"] = choice

view = bench.run_view(choice)
run, rev = view.run, view.revision

if rev is None:
    st.error(f"Execution {run.state.lower()}: {run.error_kind} · {run.error_message}  \nNo business recommendation exists for this run.")
    st.stop()

d = rev.decision
inv = rev.invoice

# ---------------------------------------------------------------- decision first
st.markdown(f"## :{REC_COLOR[rev.recommendation]}[{REC_LABEL[rev.recommendation]}]")
st.markdown(f"**{d['headline']}**")
if d.get("next_action"):
    st.markdown(f"**Who acts next:** {d.get('next_owner') or 'Finance'} — {d['next_action']}")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Commercial checks", STATUS_LABEL[d["commercial_status"]])
c2.metric("Receiving checks", STATUS_LABEL[d["receiving_status"]])
c3.metric("Snapshot", f"{d['snapshot_id']} v{d['snapshot_version']}")
c4.metric("Attempt", ATTEMPT_LABEL.get(run.attempt_kind or "", run.attempt_kind or "—"))
st.caption(f"Snapshot as of {d['snapshot_as_of']} · evaluated {d['evaluated_at']} · {d['policy_version']} · revision `{rev.id}`"
           + (f" · this run reused the result of `{run.reused_from_run_id}`" if run.reused_from_run_id else ""))

problems = [c for c in d["checks"] if c["outcome"] in ("BLOCK", "REVIEW")]
if problems:
    st.markdown("#### What needs attention")
    for c in problems:
        st.markdown(f"- {OUTCOME_ICON[c['outcome']]} **{CHECK_LABEL.get(c['code'], c['code'])}**"
                    f"{' [' + c['subject'] + ']' if c.get('subject') else ''} `{c.get('issue_code') or ''}` — {c['reason']}  \n"
                    f"  ↳ *{c.get('owner') or ''}:* {c.get('next_action') or ''}")

# ---------------------------------------------------------------- evidence
st.divider()
left, right = st.columns([1, 1])
with left:
    st.markdown("#### Source document")
    if view.document and Path(view.document.path).exists():
        pdf = pdfium.PdfDocument(view.document.path)
        page_no = st.number_input("Page", 1, len(pdf), 1) if len(pdf) > 1 else 1
        st.image(pdf[page_no - 1].render(scale=1.4).to_pil(), width="stretch")
        st.caption(f"{view.document.filename} · SHA-256 {view.document.sha256[:12]} · {view.document.page_count} page(s)")
with right:
    st.markdown("#### Extracted values and evidence")

    def row(name, f):
        ev = (f or {}).get("evidence") or {}
        val = (f or {}).get("value")
        return {"Field": name, "Value": "unknown" if val is None else str(val),
                "Page": ev.get("page", ""), "Source text": ev.get("quote", "")}

    rows = [row("Invoice number", inv["invoice_number"]), row("Invoice date", inv["invoice_date"]),
            row("PO reference", inv["po_reference"]), row("Supplier", inv["supplier"]["name"]),
            row("Supplier ID", inv["supplier"]["registration_id"]), row("Bill to (buyer)", inv["bill_to"]["name"]),
            row("Buyer ID", inv["bill_to"]["registration_id"])]
    if inv.get("ship_to"):
        rows.append(row("Ship to (not used for buyer identity)", inv["ship_to"]["name"]))
    rows += [row("Currency", inv["currency"]), row("Subtotal", inv["subtotal"]), row("Tax", inv["tax"]),
             row("Charges", inv["charges"]), row("Discount", inv["discount"]), row("Total", inv["total"]),
             row("Tax basis", inv["tax_basis"]), row("Document type", inv["document_type"])]
    st.dataframe(rows, hide_index=True, width="stretch")
    notes = inv.get("evidence_notes") or []
    if notes:
        with st.expander(f"Evidence notes ({len(notes)})"):
            for n in notes:
                st.markdown(f"- {n}")
    prov = rev.provenance
    st.caption(f"OCR: {prov['ocr']['engine']} via {prov['ocr']['carrier_model']}{' (reused)' if prov['ocr']['reused'] else ''} · "
               f"Extraction: {prov['extraction']['model']} · prompt {prov['extraction']['prompt_version']} · "
               f"schema {prov['extraction']['schema']}")

# ---------------------------------------------------------------- parties and PO
st.divider()
p1, p2 = st.columns(2)
for col, title, key, printed in ((p1, "Supplier", "supplier_match", inv["supplier"]), (p2, "Buyer (bill-to)", "buyer_match", inv["bill_to"])):
    m = d[key]
    with col:
        st.markdown(f"#### {title}")
        st.markdown(f"Printed: **{(printed['name'] or {}).get('value') or 'unknown'}** · ID {(printed['registration_id'] or {}).get('value') or 'none'}")
        st.markdown(f"Result: `{m['status']}` {('→ **' + m['selected_id'] + '**') if m.get('selected_id') else ''}  \n{m['basis']}")
        for c in m.get("candidates") or []:
            st.markdown(f"- Candidate **{c['label']}** (`{c['candidate_id']}`): " + "; ".join(c["reasons"]))

st.markdown("#### Purchase order")
po = d.get("po_match")
if po:
    st.markdown(f"Printed reference: **{(inv['po_reference'] or {}).get('value') or 'none'}** · result `{po['status']}`"
                f"{(' → **' + po['selected_id'] + '**') if po.get('selected_id') else ''}  \n{po['basis']}")
    for c in po.get("candidates") or []:
        with st.container(border=True):
            st.markdown(f"**{c['label']}**")
            for r in c["reasons"]:
                st.markdown(f"- {r}")
else:
    st.markdown("Not evaluated: buyer and supplier must be confirmed first.")

ctx = d.get("po_context")
if ctx:
    st.markdown(f"Gross cap {money(ctx['gross_cap'])} − prior recorded {money(ctx['prior_gross'])} = "
                f"**{money(ctx['remaining_gross'])} remaining**; this invoice {money(ctx['invoice_gross'])}")
    lines = []
    for lc in ctx["lines"]:
        lines.append({"PO line": f"{lc['po_number']}/{lc['line_no']}", "Item": lc["sku"], "Policy": lc["match_policy"],
                      "Ordered": lc["ordered_qty"], "Prior billed": lc["prior_billed_qty"],
                      "Ordered, unbilled": lc["remaining_ordered_qty"], "Received": lc["received_qty"],
                      "Received, unbilled": lc["receipt_available_qty"], "This invoice": lc["invoice_qty"],
                      "Unit": lc["unit"], "PO price": money(lc["net_unit_price"]),
                      "Receipts": ", ".join(lc["receipt_refs"]) or "none", "Prior bills": ", ".join(lc["prior_bill_refs"]) or "none"})
    st.dataframe(lines, hide_index=True, width="stretch")
    if ctx.get("pending"):
        st.caption("Other pending bills on this PO (exposure, not reserved): "
                   + "; ".join(f"{p['invoice_number']} {money(p['gross_amount'])}" for p in ctx["pending"]))

st.markdown("#### Invoice lines")
lm_by = {lm["source_line_id"]: lm for lm in d["line_matches"]}
line_rows = []
for ln in inv["lines"]:
    lm = lm_by.get(ln["source_line_id"], {})
    line_rows.append({"Line": ln["source_line_id"], "Printed item": (ln["description"] or {}).get("value"),
                      "Supplier code": (ln["supplier_item_code"] or {}).get("value"),
                      "Qty": (ln["quantity"] or {}).get("value"), "Unit": (ln["unit"] or {}).get("value"),
                      "Unit price": money((ln["unit_price"] or {}).get("value")), "Net": money((ln["net_amount"] or {}).get("value")),
                      "Mapped PO line": f"{lm.get('po_number')}/{lm.get('po_line_no')}" if lm.get("po_line_no") else "—",
                      "Match": lm.get("issue_code") or lm.get("status", "not evaluated"), "Basis": lm.get("method") or lm.get("detail", "")})
st.dataframe(line_rows, hide_index=True, width="stretch")

# ---------------------------------------------------------------- checks
st.markdown("#### All checks")
check_rows = [{"Result": OUTCOME_ICON[c["outcome"]], "Check": CHECK_LABEL.get(c["code"], c["code"]), "Subject": c.get("subject") or "",
               "Area": c["category"].title(), "Expected": c.get("expected") or "", "Observed": c.get("observed") or "",
               "Difference": c.get("difference") or "", "Reason": c["reason"], "Owner": c.get("owner") or "",
               "Next action": c.get("next_action") or ""} for c in d["checks"]]
st.dataframe(check_rows, hide_index=True, width="stretch")

with st.expander("Scope of this recommendation"):
    for s in d["scope_limits"]:
        st.markdown(f"- {s}")

packet = {"run": {"id": run.id, "filename": run.filename, "state": run.state, "attempt": run.attempt_kind,
                  "reused_from_run_id": run.reused_from_run_id, "submitted_at": run.created_at.isoformat()},
          "document": {"sha256": view.document.sha256 if view.document else None},
          "revision": {"id": rev.id, "kind": rev.kind, "created_at": rev.created_at.isoformat(), "provenance": rev.provenance},
          "invoice": inv, "decision": d}
st.download_button("Download evidence JSON", json.dumps(packet, indent=2, default=str),
                   file_name=f"evidence_{(inv['invoice_number'] or {}).get('value') or run.id}_{rev.id}.json", mime="application/json")
