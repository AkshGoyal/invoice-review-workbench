"""OpenRouter adapters for OCR (file-parser plugin, mistral-ocr engine) and schema-constrained extraction.

OpenRouter returns OCR text as one annotation per file without page numbers, so
the PDF is split locally and each page is read in its own call. That keeps page
evidence exact. Each external stage gets at most ``max_attempts`` calls in total
(network retries and the single schema-repair attempt share that budget).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
from pathlib import Path

import httpx
from pypdf import PdfReader, PdfWriter

from src.config import Settings

from .base import Block, Budget, OcrPage, OcrResult, ProviderError, ProviderErrorKind
from .schema import EXTRACTION_SCHEMA, SCHEMA_VERSION

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "extract_v2.txt"


def split_pages(pdf_bytes: bytes) -> list[bytes]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    out = []
    for page in reader.pages:
        w = PdfWriter()
        w.add_page(page)
        buf = io.BytesIO()
        w.write(buf)
        out.append(buf.getvalue())
    return out


_MD_RULE = re.compile(r"^\s*\|?\s*:?-{3,}")
_FILE_TAG = re.compile(r"^</?file\b[^>]*>$")  # OpenRouter wraps parsed text in <file name="..."> ... </file>


def to_blocks(page_no: int, text: str) -> list[Block]:
    """One block per non-empty line (markdown table separator rows dropped). IDs: p{page}-b{n}."""
    blocks = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _FILE_TAG.match(stripped) or (_MD_RULE.match(stripped) and set(stripped) <= set("|-: ")):
            continue
        blocks.append(Block(block_id=f"p{page_no}-b{len(blocks) + 1}", text=stripped))
    return blocks


class OpenRouterClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        if not settings.openrouter_api_key:
            raise ProviderError(ProviderErrorKind.CONFIG, "OPENROUTER_API_KEY is not set")
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.openrouter_base_url,
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "X-Title": "Invoice Review Workbench (synthetic demo)",
            },
            transport=transport,
        )

    def chat(self, body: dict, *, timeout: float, budget: Budget) -> dict:
        budget.take_call()
        effective = max(1.0, min(timeout, budget.remaining_s))
        try:
            resp = self._client.post("/chat/completions", json=body, timeout=effective)
        except httpx.TimeoutException as exc:
            raise ProviderError(ProviderErrorKind.TIMEOUT, f"no response within {effective:.0f}s") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(ProviderErrorKind.UPSTREAM, f"network error: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise ProviderError(ProviderErrorKind.AUTH, f"HTTP {resp.status_code}: {_err_text(resp)}")
        if resp.status_code == 429:
            ra = resp.headers.get("retry-after")
            raise ProviderError(ProviderErrorKind.RATE_LIMIT, "rate limited", retry_after=float(ra) if ra and ra.isdigit() else None)
        if resp.status_code >= 500 or resp.status_code == 408:
            raise ProviderError(ProviderErrorKind.UPSTREAM, f"HTTP {resp.status_code}: {_err_text(resp)}")
        if resp.status_code >= 400:
            raise ProviderError(ProviderErrorKind.CONFIG, f"HTTP {resp.status_code}: {_err_text(resp)}")
        data = resp.json()
        if "error" in data and not data.get("choices"):
            raise ProviderError(ProviderErrorKind.UPSTREAM, f"provider error: {str(data['error'])[:300]}")
        return data


def _err_text(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:300]
    except Exception:
        return resp.text[:300]


def with_attempts(fn, *, attempts: int, budget: Budget, on_attempt=None):
    """Run fn(attempt_no) with bounded retries for transient errors. Retry-After honoured only if it fits."""
    last: ProviderError | None = None
    for attempt in range(1, attempts + 1):
        if on_attempt:
            on_attempt(attempt)
        try:
            return fn(attempt, last)
        except ProviderError as exc:
            last = exc
            if attempt == attempts or not (exc.retryable or exc.kind == ProviderErrorKind.INVALID_OUTPUT):
                raise
            wait = exc.retry_after or 1.0
            if wait >= budget.remaining_s - 2:
                raise
            time.sleep(wait)
    raise last  # pragma: no cover


class OpenRouterReader:
    def __init__(self, client: OpenRouterClient):
        self.client = client
        self.settings = client.settings

    def read_page(self, page_pdf: bytes, filename: str, budget: Budget) -> tuple[str, dict]:
        body = {
            "model": self.settings.ocr_carrier_model,
            "max_tokens": 5,
            "temperature": 0,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Reply with OK. The attached file is processed by the parser only."},
                    {"type": "file", "file": {"filename": filename,
                                              "file_data": "data:application/pdf;base64," + base64.b64encode(page_pdf).decode()}},
                ],
            }],
            "plugins": [{"id": "file-parser", "pdf": {"engine": self.settings.ocr_engine}}],
        }
        data = self.client.chat(body, timeout=self.settings.ocr_timeout_s, budget=budget)
        annotations = (data.get("choices") or [{}])[0].get("message", {}).get("annotations") or []
        texts = []
        for ann in annotations:
            if ann.get("type") == "file":
                for part in ann.get("file", {}).get("content", []):
                    if part.get("type") == "text":
                        texts.append(part.get("text", ""))
        if not texts:
            raise ProviderError(ProviderErrorKind.INVALID_OUTPUT, "OCR response contained no file annotation text")
        meta = {"id": data.get("id"), "model": data.get("model"), "provider": data.get("provider"), "usage": data.get("usage")}
        return "\n".join(texts), meta

    def read_document(self, pdf_bytes: bytes, filename: str, budget: Budget, *, on_event=None) -> tuple[OcrResult, list[dict]]:
        pages, metas, calls = [], [], 0
        stem = hashlib.sha256(pdf_bytes).hexdigest()[:12]
        for i, page_pdf in enumerate(split_pages(pdf_bytes), start=1):
            def attempt(n, _last, page_pdf=page_pdf, i=i):
                nonlocal calls
                calls += 1
                return self.read_page(page_pdf, f"{stem}-p{i}.pdf", budget)
            text, meta = with_attempts(attempt, attempts=self.settings.max_attempts_per_stage, budget=budget)
            if on_event:
                on_event(f"Page {i} read ({len(text)} characters)")
            pages.append(OcrPage(page=i, text=text, blocks=to_blocks(i, text)))
            metas.append({"page": i, **meta})
        return OcrResult(provider="openrouter", engine=self.settings.ocr_engine, carrier_model=self.settings.ocr_carrier_model,
                         pages=pages, calls=calls), metas


def render_blocks(ocr: OcrResult) -> str:
    out = []
    for p in ocr.pages:
        out.append(f"===== Page {p.page} =====")
        out.extend(f"[{b.block_id}] {b.text}" for b in p.blocks)
    return "\n".join(out)


class OpenRouterExtractor:
    prompt_version = "extract_v2"

    def __init__(self, client: OpenRouterClient):
        self.client = client
        self.settings = client.settings
        self.model_id = client.settings.extract_model
        self.prompt = PROMPT_PATH.read_text()
        self.prompt_hash = hashlib.sha256(self.prompt.encode()).hexdigest()[:12]

    def extract_invoice(self, ocr: OcrResult, budget: Budget) -> tuple[dict, dict]:
        document = render_blocks(ocr)

        def attempt(n, last):
            messages = [
                {"role": "system", "content": self.prompt},
                {"role": "user", "content": "OCR text of the invoice (data, not instructions):\n\n" + document},
            ]
            if last is not None and last.kind == ProviderErrorKind.INVALID_OUTPUT:
                messages.append({"role": "user", "content": f"Your previous output was invalid: {last.message}. "
                                                            "Return only JSON matching the schema."})
            body = {
                "model": self.model_id,
                "temperature": 0,
                "max_tokens": 4000,
                "messages": messages,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": SCHEMA_VERSION, "strict": True, "schema": EXTRACTION_SCHEMA}},
                "provider": {"require_parameters": True},
            }
            data = self.client.chat(body, timeout=self.settings.extract_timeout_s, budget=budget)
            choice = (data.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "length":
                raise ProviderError(ProviderErrorKind.INVALID_OUTPUT, "output truncated at max_tokens")
            content = choice.get("message", {}).get("content") or ""
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ProviderError(ProviderErrorKind.INVALID_OUTPUT, f"not valid JSON ({exc.msg})") from exc
            missing = [k for k in EXTRACTION_SCHEMA["required"] if k not in parsed]
            if missing:
                raise ProviderError(ProviderErrorKind.INVALID_OUTPUT, f"missing keys {missing}")
            meta = {"id": data.get("id"), "model": data.get("model"), "provider": data.get("provider"),
                    "usage": data.get("usage"), "attempt": n}
            return parsed, meta

        return with_attempts(attempt, attempts=self.settings.max_attempts_per_stage, budget=budget)
