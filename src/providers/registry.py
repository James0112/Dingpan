from __future__ import annotations

import sqlite3

from src.config import Settings
from src.providers.base import ModelProvider
from src.providers.gemini import GeminiProvider
from src.providers.openai import OpenAIProvider


def get_provider(db_path: str, settings: Settings, model_id: str) -> ModelProvider:
    row = _load_model_row(db_path, model_id)
    provider_name = str(row["provider"])
    upstream_model_name = str(row["upstream_model_name"] or "").strip()
    if not int(row["is_runnable"]):
        raise ValueError(f"Model is not runnable: {model_id}")
    if not upstream_model_name:
        raise ValueError(f"Model {model_id} is missing upstream_model_name")

    if provider_name == "gemini":
        return GeminiProvider(
            api_key=settings.gemini_api_key or "",
            model_name=upstream_model_name,
            fallback_model_name=settings.fallback_model_name,
        )
    if provider_name == "openai":
        return OpenAIProvider(
            api_key=settings.openai_api_key or "",
            base_url=settings.openai_base_url,
            model_name=upstream_model_name,
            reasoning_effort=settings.openai_reasoning_effort,
        )
    raise ValueError(f"Provider not implemented for model_id={model_id}: {provider_name}")


def _load_model_row(db_path: str, model_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT model_id, provider, upstream_model_name, is_runnable
            FROM model_pricing
            WHERE model_id = ?
            """,
            (model_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"Unsupported or unknown model_id: {model_id}")
    return row
