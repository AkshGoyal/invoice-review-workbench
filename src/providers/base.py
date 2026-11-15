"""Provider contracts: OCR result, extraction output, typed errors and a shared call budget."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class Block(BaseModel):
    model_config = ConfigDict(frozen=True)
    block_id: str
    text: str


class OcrPage(BaseModel):
    model_config = ConfigDict(frozen=True)
    page: int
    text: str
    blocks: list[Block]


class OcrResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider: str
    engine: str
    carrier_model: str
    pages: list[OcrPage]
    calls: int
    reused: bool = False

    def block(self, block_id: str) -> Block | None:
        for p in self.pages:
            for b in p.blocks:
                if b.block_id == block_id:
                    return b
        return None


class ProviderErrorKind(StrEnum):
    CONFIG = "CONFIG"  # missing credentials or bad settings; not retryable
    AUTH = "AUTH"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    UPSTREAM = "UPSTREAM"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    BUDGET = "BUDGET"


RETRYABLE = {ProviderErrorKind.TIMEOUT, ProviderErrorKind.RATE_LIMIT, ProviderErrorKind.UPSTREAM}


class ProviderError(Exception):
    def __init__(self, kind: ProviderErrorKind, message: str, *, retry_after: float | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


@dataclass
class Budget:
    """Overall run deadline plus a per-workspace provider call cap (SPEC.md §15)."""

    deadline_s: float
    call_cap: int
    calls_used: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def remaining_s(self) -> float:
        return self.deadline_s - (time.monotonic() - self.started)

    def take_call(self) -> None:
        if self.calls_used >= self.call_cap:
            raise ProviderError(ProviderErrorKind.BUDGET, f"provider call cap of {self.call_cap} reached")
        if self.remaining_s <= 1:
            raise ProviderError(ProviderErrorKind.BUDGET, "run deadline reached")
        self.calls_used += 1


class DocumentReader(Protocol):
    def read_document(self, pdf_bytes: bytes, filename: str, budget: Budget) -> OcrResult: ...


class InvoiceExtractor(Protocol):
    model_id: str
    prompt_version: str

    def extract_invoice(self, ocr: OcrResult, budget: Budget) -> dict: ...
