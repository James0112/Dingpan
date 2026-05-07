from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GenerateConfig:
    temperature: float = 0.4
    max_output_tokens: int = 2500


class ModelProvider(Protocol):
    def generate(self, prompt: str, config: GenerateConfig) -> str:
        """Send a prompt to the model provider and return raw JSON text."""

