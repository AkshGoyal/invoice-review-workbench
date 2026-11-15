"""Shared UI helpers: the cached Workbench, workspace selection and plain-language labels."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import streamlit as st

from src.config import load_settings
from src.domain.money import fmt_inr
from src.services.loaders import load_policy
from src.services.workflow import STAGE_LABEL, STAGES, Workbench

REC_LABEL = {
    "READY_FOR_FINANCE_REVIEW": "Ready for finance review",
    "NEEDS_REVIEW": "Needs review",
    "BLOCKED": "Blocked",
}
REC_COLOR = {"READY_FOR_FINANCE_REVIEW": "green", "NEEDS_REVIEW": "orange", "BLOCKED": "red"}
STATUS_LABEL = {"MATCHED": "Matched", "NEEDS_REVIEW": "Needs review", "BLOCKED": "Blocked", "NOT_REQUIRED": "Not required"}
OUTCOME_ICON = {"PASS": "✅ Pass", "REVIEW": "⚠️ Review", "BLOCK": "⛔ Block", "NOT_RUN": "⏸ Not run", "NOT_REQUIRED": "➖ Not required"}
RUN_STATE_LABEL = {"QUEUED": "Queued", "RUNNING": "Running", "COMPLETED": "Completed", "FAILED": "Failed",
                   "INTERRUPTED": "Interrupted"}
ATTEMPT_LABEL = {"NEW_BILL": "New bill", "DUPLICATE_ATTEMPT": "Duplicate attempt", "EXACT_REPEAT": "Identical file (reused)",
                 "UNRESOLVED": "Bill identity unresolved"}
CHECK_LABEL = {
    "SCOPE": "Supported scope", "REQUIRED_EVIDENCE": "Mandatory fields and evidence", "DOCUMENT_WARNINGS": "Document anomalies",
    "BUYER_IDENTITY": "Buyer (bill-to)", "SUPPLIER_IDENTITY": "Supplier", "DUPLICATE": "Duplicate bill",
    "PO_RESOLUTION": "Purchase order", "PO_STATUS": "PO status", "LINE_MATCH": "Line match", "UNIT_PRICE": "Unit price",
    "LINE_ARITHMETIC": "Line arithmetic", "INVOICE_ARITHMETIC": "Invoice totals", "PO_LINE_QUANTITY": "Ordered quantity",
    "PO_AMOUNT": "PO amount cap", "RECEIPT": "Receipt evidence", "CHECK_COVERAGE": "Check coverage",
}


@st.cache_resource
def get_workbench() -> Workbench:
    bench = Workbench(load_settings(), load_policy())
    bench.ensure_snapshot("S1")
    return bench


def current_workspace(bench: Workbench) -> str:
    ws = st.query_params.get("ws")
    if ws and bench.workspace(ws):
        return ws
    ws = bench.create_workspace(f"Demo workspace {datetime.now():%d %b %H:%M}")
    st.query_params["ws"] = ws
    return ws


def sidebar(bench: Workbench, ws_id: str) -> None:
    ws = bench.workspace(ws_id)
    snap = bench.snapshot(ws.active_snapshot_id)
    with st.sidebar:
        st.caption("SYNTHETIC DEMO: fictional companies, invoices and procurement records.")
        st.markdown(f"**Workspace** `{ws.label}`")
        st.markdown(f"**Procurement snapshot** {snap.snapshot_id} v{snap.version}  \nas of {snap.as_of:%d %b %Y %H:%M %Z}")
        st.caption(snap.label)
        settings = bench.settings
        st.markdown(f"**Models** via OpenRouter  \nOCR: `{settings.ocr_engine}`  \nExtraction: `{settings.extract_model}`")
        if not settings.has_model_credentials:
            st.error("OPENROUTER_API_KEY is not set. Uploads will fail at the read stage.")
        st.caption(f"Provider calls used in this workspace: {ws.provider_calls_used} / {settings.provider_call_cap_per_workspace}")
        if st.button("Start a fresh workspace", help="Independent history and duplicate scope; snapshot S1."):
            new = bench.create_workspace(f"Demo workspace {datetime.now():%d %b %H:%M}")
            st.query_params.clear()
            st.query_params["ws"] = new
            st.rerun()
        if bench.interrupted_on_start:
            st.warning(f"{bench.interrupted_on_start} unfinished run(s) were marked Interrupted after a restart.")


def money(v) -> str:
    return fmt_inr(Decimal(v)) if v not in (None, "") else "unknown"


def elapsed(start, end) -> str:
    if not start or not end:
        return ""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    secs = (end - start).total_seconds()
    return f"{secs * 1000:.0f} ms" if secs < 1 else f"{secs:.1f} s"


def stage_rows(events) -> list[dict]:
    """One row per stage from actual events; stages without events are shown as not started."""
    by_stage: dict[str, dict] = {}
    for e in events:
        row = by_stage.setdefault(e.stage, {"start": None, "end": None, "status": None, "notes": []})
        if e.status == "STARTED":
            row["start"] = e.at
            row["status"] = "RUNNING"
        elif e.status in ("COMPLETED", "FAILED", "SKIPPED"):
            row["end"] = e.at
            row["status"] = e.status
            if row["start"] is None:
                row["start"] = e.at
            if e.message:
                row["notes"].append(e.message)
        elif e.status == "INFO" and e.message:
            row["notes"].append(e.message)
            row["status"] = row["status"] or "INFO"
    icon = {"COMPLETED": "✅", "FAILED": "❌", "SKIPPED": "⏭", "RUNNING": "⏳", "INFO": "ℹ️", None: "○"}
    out = []
    for code, label in STAGES:
        r = by_stage.get(code)
        if r is None:
            out.append({"Stage": f"○ {label}", "Time": "", "Detail": ""})
        else:
            out.append({"Stage": f"{icon[r['status']]} {label}", "Time": elapsed(r["start"], r["end"]),
                        "Detail": " · ".join(r["notes"])})
    return out


def open_review(run_id: str) -> None:
    st.query_params["run"] = run_id
    st.switch_page("src/ui/review.py")


__all__ = ["STAGE_LABEL"]
