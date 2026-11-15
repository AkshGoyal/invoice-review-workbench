"""Headless Streamlit checks of the three screens over fake-provider data in a temp directory."""

import dataclasses
import uuid
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from src.config import load_settings
from src.services.loaders import load_policy
from src.services.workflow import Workbench

from .fakes import factory

ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "data" / "fixtures" / "pdf"


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    st.cache_resource.clear()
    fac, *_ = factory()
    bench = Workbench(dataclasses.replace(load_settings(), data_dir=tmp_path), load_policy(), provider_factory=fac)
    ws = bench.create_workspace("ui-test")
    runs = {}
    for name in ("H1_digital.pdf", "H1_two_page.pdf"):
        runs[name] = bench.submit(ws, name, (PDF_DIR / name).read_bytes(), uuid.uuid4().hex)
        bench.wait_idle()
    bench.executor.shutdown()
    bench.engine.dispose()
    yield ws, runs
    st.cache_resource.clear()


def page(path, ws, run=None):
    at = AppTest.from_file(str(ROOT / path), default_timeout=30)
    at.query_params["ws"] = ws
    if run:
        at.query_params["run"] = run
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_review_shows_decision_first_with_balances(seeded):
    ws, runs = seeded
    at = page("src/ui/review.py", ws, runs["H1_digital.pdf"])
    md = " ".join(m.value for m in at.markdown)
    assert "Ready for finance review" in md
    assert "Checks passed against snapshot S1. Ready for finance review." in md
    assert "₹1,41,600.00 remaining" in md
    po_table = at.dataframe[1].value
    assert po_table.iloc[0]["Received, unbilled"] == "200"


def test_review_of_duplicate_attempt_says_blocked_and_who_acts(seeded):
    ws, runs = seeded
    at = page("src/ui/review.py", ws, runs["H1_two_page.pdf"])
    md = " ".join(m.value for m in at.markdown)
    assert "Blocked" in md and "DUPLICATE_BILL" in md and "do not create a new payable" in md


def test_history_counts_bills_separately_from_attempts(seeded):
    ws, _ = seeded
    at = page("src/ui/history.py", ws)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Unique bills"] == "1" and metrics["Processing attempts"] == "2"
    assert list(at.dataframe[0].value["Recommendation"]) == ["Blocked", "Ready for finance review"]


def test_process_page_reopens_persisted_run(seeded):
    ws, runs = seeded
    at = page("src/ui/process.py", ws, runs["H1_digital.pdf"])
    md = " ".join(m.value for m in at.markdown)
    assert "Ready for finance review" in md
    stages = at.dataframe[0].value["Stage"].tolist()
    assert all(s.startswith("✅") for s in stages), stages
