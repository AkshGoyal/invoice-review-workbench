"""History screen: every attempt, with unique bills counted separately."""

from __future__ import annotations

import streamlit as st

from src.ui.common import ATTEMPT_LABEL, REC_LABEL, RUN_STATE_LABEL, current_workspace, get_workbench, money, open_review, sidebar

bench = get_workbench()
ws = current_workspace(bench)
sidebar(bench, ws)

st.title("History")
counts = bench.counts(ws)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Unique bills", counts["unique_bills"])
c2.metric("Processing attempts", counts["attempts"])
c3.metric("Review revisions", counts["revisions"])
c4.metric("Stored documents", counts["documents"])

runs = bench.runs(ws)
if not runs:
    st.info("No runs yet.")
    st.stop()

rows = []
for r in runs:
    rev = bench.revision(r.revision_id) if r.revision_id else None
    inv = rev.invoice if rev else {}
    d = rev.decision if rev else {}
    rows.append({
        "Submitted (UTC)": r.created_at.strftime("%d %b %H:%M:%S"),
        "File": r.filename,
        "Invoice": ((inv.get("invoice_number") or {}).get("value")) or "—",
        "Supplier": ((inv.get("supplier") or {}).get("name") or {}).get("value") or "—",
        "Amount": money((inv.get("total") or {}).get("value")) if rev else "—",
        "Execution": RUN_STATE_LABEL.get(r.state, r.state),
        "Recommendation": REC_LABEL.get(rev.recommendation, "") if rev else "No recommendation",
        "Primary exception": (rev.primary_exception if rev else r.error_kind) or "",
        "Attempt": ATTEMPT_LABEL.get(r.attempt_kind or "", ""),
        "Snapshot": d.get("snapshot_id", ""),
        "Run": r.id,
    })

event = st.dataframe(rows, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row")
st.caption("Select a row to open its full result. Identical files reuse the earlier result; re-rendered copies of a recorded "
           "bill are kept as duplicate attempts and never create a second bill.")
sel = event.selection.rows if event and event.selection else []
if sel:
    open_review(rows[sel[0]]["Run"])
