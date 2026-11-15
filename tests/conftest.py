"""Shared fixtures. Fixture *inputs* live here and in data/; expected values live in the tests."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.domain.invoice import NormalizedInvoice
from src.domain.pipeline import evaluate
from src.services.loaders import load_policy, load_snapshot, load_snapshot_raw, load_structured_raw

EVAL_AT = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def policy():
    return load_policy()


@pytest.fixture(scope="session")
def s1(policy):
    return load_snapshot("S1", policy)


@pytest.fixture
def s1_raw():
    return copy.deepcopy(load_snapshot_raw("S1"))


@pytest.fixture
def h1_raw():
    return copy.deepcopy(load_structured_raw("H1"))


def _ev(value, quote=None):
    text = str(value)
    return {"raw": text, "value": text, "evidence": {"page": 1, "block_ids": ["p1-test"], "quote": quote or text}}


def build_invoice(base: dict, rows: list[str], *, price: str = "200.00", number: str | None = None,
                  tax_rate: str = "18", total_override: str | None = None) -> NormalizedInvoice:
    """Variant of H1 with the given row quantities. Amounts are derived consistently from qty x price."""
    raw = copy.deepcopy(base)
    template = raw["lines"][0]
    lines, net_sum = [], Decimal(0)
    for i, qty in enumerate(rows, start=1):
        line = copy.deepcopy(template)
        net = (Decimal(qty) * Decimal(price)).quantize(Decimal("0.01"))
        net_sum += net
        line["source_line_id"] = f"L{i}"
        line["quantity"] = _ev(qty)
        line["unit_price"] = _ev(price)
        line["net_amount"] = _ev(f"{net:.2f}")
        lines.append(line)
    tax = (net_sum * Decimal(tax_rate) / 100).quantize(Decimal("0.01"))
    raw["lines"] = lines
    raw["subtotal"] = _ev(f"{net_sum:.2f}")
    raw["tax"] = _ev(f"{tax:.2f}")
    raw["total"] = _ev(total_override or f"{net_sum + tax:.2f}")
    if number:
        raw["invoice_number"] = _ev(number)
    return NormalizedInvoice.model_validate(raw)


def run(inv: NormalizedInvoice, snap, policy):
    return evaluate(inv, snap, policy, evaluated_at=EVAL_AT)


def checks_by_code(result, code):
    return [c for c in result.checks if c.code == code]
