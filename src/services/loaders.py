"""File I/O for policy, seed snapshots and structured fixtures. Kept out of src/domain."""

from __future__ import annotations

import json
from pathlib import Path

from src.domain.invoice import NormalizedInvoice
from src.domain.policy import Policy
from src.domain.snapshot import Snapshot, validate_snapshot

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config" / "policy_v2.json"
SEEDS_DIR = ROOT / "data" / "seeds"
STRUCTURED_DIR = ROOT / "data" / "fixtures" / "structured"


def load_policy(path: Path = POLICY_PATH) -> Policy:
    return Policy.model_validate(json.loads(path.read_text()))


def load_snapshot_raw(snapshot_id: str) -> dict:
    return json.loads((SEEDS_DIR / f"{snapshot_id}.json").read_text())


def load_snapshot(snapshot_id: str, policy: Policy | None = None) -> Snapshot:
    currency = (policy or load_policy()).supported_currency
    return validate_snapshot(load_snapshot_raw(snapshot_id), supported_currency=currency)


def load_structured_raw(case_id: str) -> dict:
    return json.loads((STRUCTURED_DIR / f"{case_id}.json").read_text())


def load_structured(case_id: str) -> NormalizedInvoice:
    return NormalizedInvoice.model_validate(load_structured_raw(case_id))
