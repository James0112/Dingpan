from __future__ import annotations

import time

from google import genai
from google.genai import types

from src.providers.base import GenerateConfig, ProviderResult


class GeminiProvider:
    def __init__(self, api_key: str, model_name: str, fallback_model_name: str | None = None):
        self._api_key = api_key
        self._model_name = model_name
        self._fallback_model_name = fallback_model_name

    def generate(self, prompt: str, config: GenerateConfig) -> ProviderResult:
        client = genai.Client(api_key=self._api_key)
        generate_config = types.GenerateContentConfig(
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            response_mime_type="application/json",
        )

        models = [self._model_name]
        if self._fallback_model_name and self._fallback_model_name != self._model_name:
            models.append(self._fallback_model_name)

        attempts = 3
        last_error: Exception | None = None
        errors: list[str] = []
        for model in models:
            for attempt in range(attempts):
                try:
                    response = client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=generate_config,
                    )
                    return ProviderResult(
                        text=response.text,
                        actual_provider="gemini",
                        actual_model_name=model,
                        provider_response_id="",
                    )
                except Exception as exc:  # pragma: no cover - SDK/runtime dependent
                    last_error = exc
                    errors.append(f"{model} attempt {attempt + 1}: {exc}")
                    if attempt < attempts - 1:
                        time.sleep(2**attempt)
        if last_error is not None:
            raise RuntimeError(" | ".join(errors)) from last_error
        raise RuntimeError("Gemini provider returned without result")
