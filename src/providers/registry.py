from __future__ import annotations

from src.providers.base import ModelProvider
from src.providers.gemini import GeminiProvider


def get_provider(
    model_id: str,
    *,
    api_key: str,
    model_name: str,
    fallback_model_name: str | None = None,
) -> ModelProvider:
    if model_id == "gemini":
        return GeminiProvider(
            api_key=api_key,
            model_name=model_name,
            fallback_model_name=fallback_model_name,
        )
    raise ValueError(f"Unsupported model_id: {model_id}")
