from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GenerateConfig:
    temperature: float = 0.4
    max_output_tokens: int = 2500


@dataclass(frozen=True)
class ProviderResult:
    text: str
    actual_provider: str
    actual_model_name: str
    provider_response_id: str = ""


class ModelProvider(Protocol):
    def generate(self, prompt: str, config: GenerateConfig) -> ProviderResult:
        """Send a prompt to the model provider and return structured generation metadata."""
