# Invoice Review Workbench

**Upload a supplier invoice PDF, get Ready for finance review, Needs review or Blocked, with the
evidence behind it.**

## The problem I saw

Accounts payable teams check every supplier invoice against the purchase order and what was
actually received before finance pays. The PO number is often missing or wrong, item names don't
match the catalogue, and quantities have already been billed on an earlier invoice. Doing this by
hand is slow. Handing it to an LLM is risky: a model that "approves" an invoice it misread
costs real money.

## What I built

A review workbench where **the model only reads the document. Trusted procurement records and
plain Python make the decision.**

- OCR and schema-constrained extraction turn the PDF into fields. Every value cites the page,
  block and quote it came from, and a value without evidence becomes *unknown*, never a guess.
- Deterministic checks then run: buyer and supplier identity, duplicates, PO resolution,
  line matching, unit price, quantity against the PO, receipts minus prior billing, and
  line/invoice arithmetic.
- The result is one of three outcomes. Each comes with the primary exception, who owns it
  (finance, procurement or stores) and the next action. Payment approval stays with finance.

**Verified live on synthetic invoices** (digital, scanned and two-page): 23 of 23 critical
fields correct on each, about 5–11 s per invoice, roughly $0.003 in provider cost.

All invoices, companies and procurement records in this repository are **synthetic and fictional**.

**Status:** the core path from PDF to decision is built and verified, with 53 tests. Exception
workflows (missing PO, duplicate, ambiguous party or item, receipt shortfall) are designed and
partly built. See [`docs/BUILD_STATUS.md`](docs/BUILD_STATUS.md) for the details and
[`docs/SPEC.md`](docs/SPEC.md) for the product and engineering specification.

## Setup

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 if needed.

```bash
uv sync
cp .env.example .env   # then set OPENROUTER_API_KEY locally; never commit it
```

## Run the app

```bash
uv run python -m scripts.smoke_openrouter   # one OCR + one extraction call on a synthetic PDF
uv run streamlit run app.py                 # Process, Review, History at http://localhost:8501
```

Synthetic test PDFs are in `data/fixtures/pdf/` (`uv run python -m scripts.make_fixtures` regenerates them).

## Run the local domain check

This runs the deterministic matching and decision path on **structured input** (known invoice fields, no PDF, no model call) against synthetic snapshot S1:

```bash
uv run python -m scripts.run_structured H1
uv run python -m scripts.run_structured H1 --json var/exports/H1.json   # optional evidence export
uv run python -m scripts.validate_snapshot S1
uv run pytest
```

## Layout

| Path | Purpose |
|---|---|
| `src/domain/` | Pure typed contracts and rules: snapshot, invoice, matching, checks, decision. No Streamlit, provider or database imports. |
| `src/providers/` | Mistral OCR and extraction adapters (Phase 2). |
| `src/services/`, `src/persistence/`, `src/ui/` | Workflow, SQLite repositories and Streamlit screens (Phase 2+). |
| `config/policy_v2.json` | Explicit demo tolerances and limits. |
| `data/seeds/` | Synthetic procurement snapshots (S1). |
| `data/fixtures/structured/` | Known structured invoice inputs for domain checks. |
| `scripts/` | CLI entry points. |
| `tests/` | pytest suite. Expected values are computed in the tests, not read from fixtures. |
