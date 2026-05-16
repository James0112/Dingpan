from __future__ import annotations

import json
from urllib import error, request

from src.providers.base import GenerateConfig, ProviderResult


class OpenAIProvider:
    def __init__(self, api_key: str, base_url: str, model_name: str, reasoning_effort: str = "xhigh"):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._reasoning_effort = reasoning_effort

    def generate(self, prompt: str, config: GenerateConfig) -> ProviderResult:
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY is required")

        payload = {
            "model": self._model_name,
            "input": prompt,
            "max_output_tokens": config.max_output_tokens,
            "reasoning": {"effort": self._reasoning_effort},
            "text": {"format": {"type": "json_object"}},
        }
        raw_body = json.dumps(payload).encode("utf-8")
        http_request = request.Request(
            url=f"{self._base_url}/responses",
            data=raw_body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=120) as response:
                raw_response = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI responses request failed: HTTP {exc.code} {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"OpenAI responses request failed: {exc.reason}") from exc

        data = json.loads(raw_response)
        text = _extract_response_text(data)
        if not text:
            raise RuntimeError("OpenAI responses request returned no output_text")
        return ProviderResult(
            text=text,
            actual_provider="openai",
            actual_model_name=self._model_name,
            provider_response_id=str(data.get("id") or ""),
        )


def _extract_response_text(payload: dict[str, object]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output_items = payload.get("output")
    if not isinstance(output_items, list):
        return ""
    chunks: list[str] = []
    for item in output_items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content_items = item.get("content")
        if not isinstance(content_items, list):
            continue
        for content in content_items:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    return "".join(chunks).strip()
