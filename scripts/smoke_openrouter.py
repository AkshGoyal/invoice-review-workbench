"""Authenticated smoke calls: OCR (file-parser/mistral-ocr) and extraction on a synthetic PDF.

Usage: uv run python -m scripts.smoke_openrouter [data/fixtures/pdf/H1_digital.pdf]
Prints call outcomes and writes raw OCR text + extraction JSON to var/smoke/. Never prints the key.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from src.config import load_settings
from src.providers.base import Budget, ProviderError
from src.providers.openrouter import OpenRouterClient, OpenRouterExtractor, OpenRouterReader


def main(argv: list[str]) -> int:
    settings = load_settings()
    print(f"provider={settings.model_provider} key_present={settings.has_model_credentials} "
          f"ocr_engine={settings.ocr_engine} carrier={settings.ocr_carrier_model} extract={settings.extract_model}")
    if not settings.has_model_credentials:
        print("BLOCKED: OPENROUTER_API_KEY is not set in .env")
        return 2
    pdf = Path(argv[0] if argv else "data/fixtures/pdf/H1_digital.pdf")
    out = settings.data_dir / "smoke"
    out.mkdir(parents=True, exist_ok=True)
    client = OpenRouterClient(settings)
    budget = Budget(deadline_s=settings.run_deadline_s, call_cap=10)

    t0 = time.monotonic()
    try:
        ocr, metas = OpenRouterReader(client).read_document(pdf.read_bytes(), pdf.name, budget)
    except ProviderError as exc:
        print(f"OCR FAILED: {exc}")
        return 1
    print(f"OCR OK in {time.monotonic() - t0:.1f}s, {ocr.calls} call(s), pages={len(ocr.pages)}")
    for m in metas:
        print(f"  page {m['page']}: served by model={m.get('model')} provider={m.get('provider')} usage={m.get('usage')}")
    for p in ocr.pages:
        print(f"  --- page {p.page}: {len(p.blocks)} blocks ---")
        for b in p.blocks[:40]:
            print(f"    [{b.block_id}] {b.text}")
    (out / f"{pdf.stem}.ocr.json").write_text(ocr.model_dump_json(indent=2))

    t1 = time.monotonic()
    try:
        extracted, meta = OpenRouterExtractor(client).extract_invoice(ocr, budget)
    except ProviderError as exc:
        print(f"EXTRACTION FAILED: {exc}")
        return 1
    print(f"EXTRACTION OK in {time.monotonic() - t1:.1f}s model={meta.get('model')} provider={meta.get('provider')} "
          f"attempt={meta.get('attempt')} usage={meta.get('usage')}")
    (out / f"{pdf.stem}.extract.json").write_text(json.dumps(extracted, indent=2))
    print(json.dumps({k: extracted[k] for k in ("invoice_number", "invoice_date", "po_reference", "total")}, indent=1))
    print(f"Lines: {len(extracted['lines'])}; warnings: {extracted['warnings']}")
    print(f"Calls used: {budget.calls_used}. Raw outputs in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
