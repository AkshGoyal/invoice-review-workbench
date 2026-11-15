"""Process screen: upload one PDF, watch actual stage events, see the outcome."""

from __future__ import annotations

import hashlib
import uuid

import streamlit as st

from src.services.workflow import QueueBusy
from src.ui.common import REC_COLOR, REC_LABEL, RUN_STATE_LABEL, current_workspace, get_workbench, open_review, sidebar, stage_rows

bench = get_workbench()
ws = current_workspace(bench)
sidebar(bench, ws)

st.title("Process an invoice")
st.markdown(
    "Upload one supplier invoice PDF. The workbench reads it, checks it against the procurement snapshot and recommends "
    "**Ready for finance review**, **Needs review** or **Blocked**. Finance keeps approval and payment."
)
with st.expander("Supported scope"):
    st.markdown(
        "- One buyer (Northstar Manufacturing, fictional), INR, English\n"
        "- One invoice and one purchase order per PDF; up to 10 pages, 10 MiB and 50 lines\n"
        "- Digital or readable scanned PDFs. Credit notes, foreign currency and multi-PO invoices are routed to review.\n"
        "- Checks run against a read-only snapshot of POs, receipts and prior bills; nothing is posted or approved here."
    )

upload = st.file_uploader("Invoice PDF", type=["pdf"], accept_multiple_files=False)
if upload is not None:
    data = upload.getvalue()
    # One token per (file, upload); repeated clicks reuse it, so double-clicks cannot create extra work.
    fkey = hashlib.sha256(data).hexdigest() + str(upload.file_id)
    if st.session_state.get("upload_key") != fkey:
        st.session_state["upload_key"] = fkey
        st.session_state["upload_token"] = uuid.uuid4().hex
    if st.button("Process", type="primary"):
        try:
            run_id = bench.submit(ws, upload.name, data, st.session_state["upload_token"])
            st.query_params["run"] = run_id
        except QueueBusy as exc:
            st.error(f"The workbench is busy: {exc}")

run_id = st.query_params.get("run")


@st.fragment(run_every=1)
def live(run_id: str) -> None:
    view = bench.run_view(run_id)
    if view is None or view.run.workspace_id != ws:
        st.info("That run is not in this workspace.")
        return
    run = view.run
    st.subheader(f"Run `{run.id}` · {run.filename}")
    st.caption(f"Submitted {run.created_at:%d %b %Y %H:%M:%S} UTC · state: **{RUN_STATE_LABEL.get(run.state, run.state)}**"
               + (f" · reused result of `{run.reused_from_run_id}`" if run.reused_from_run_id else ""))
    st.dataframe(stage_rows(view.events), hide_index=True, width="stretch")
    if run.state in ("QUEUED", "RUNNING"):
        st.caption("Updating from stored stage events every second.")
        return
    if run.state in ("FAILED", "INTERRUPTED"):
        st.error(f"Execution {run.state.lower()} ({run.error_kind}): {run.error_message}  \nNo business recommendation was produced.")
        if run.document_id and st.button("Retry this file"):
            new_id = bench.retry(run.id, token=uuid.uuid4().hex)
            st.query_params["run"] = new_id
            st.rerun()
        return
    rev = view.revision
    if rev is not None:
        d = rev.decision
        color = REC_COLOR[rev.recommendation]
        st.markdown(f"### :{color}[{REC_LABEL[rev.recommendation]}]")
        st.markdown(d["headline"])
        if d.get("next_action"):
            st.markdown(f"**Next action{' (' + d['next_owner'] + ')' if d.get('next_owner') else ''}:** {d['next_action']}")
        if st.button("Open full review", type="primary"):
            open_review(run.id)


if run_id:
    live(run_id)
