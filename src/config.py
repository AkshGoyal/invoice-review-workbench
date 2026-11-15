"""Runtime settings from the environment (or a git-ignored .env). Never logs secret values."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    model_provider: str
    openrouter_api_key: str | None
    openrouter_base_url: str
    extract_model: str
    ocr_engine: str
    ocr_carrier_model: str
    data_dir: Path
    ocr_timeout_s: float = 45.0
    extract_timeout_s: float = 30.0
    run_deadline_s: float = 180.0
    max_attempts_per_stage: int = 2
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pages: int = 10
    max_lines: int = 50
    queue_limit: int = 5
    provider_call_cap_per_workspace: int = 50

    @property
    def has_model_credentials(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "workbench.db"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("WORKBENCH_DATA_DIR", "./var"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    return Settings(
        model_provider=os.environ.get("MODEL_PROVIDER", "openrouter"),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
        openrouter_base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        extract_model=os.environ.get("EXTRACT_MODEL", "mistralai/mistral-small-2603"),
        ocr_engine=os.environ.get("OCR_ENGINE", "mistral-ocr"),
        ocr_carrier_model=os.environ.get("OCR_CARRIER_MODEL", "mistralai/mistral-small-2603"),
        data_dir=data_dir,
    )
