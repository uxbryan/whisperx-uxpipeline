"""
core/llm.py — Anthropic Claude API thin wrapper.

Single dependency: official `anthropic` SDK. Provides cost estimation
in NTD (approximate, for budgeting; replace with real usage tokens if needed).

Required env var:
    ANTHROPIC_API_KEY
"""

from __future__ import annotations

import os
from anthropic import Anthropic


# Approximate prices (NTD per 1M tokens), assuming USD-TWD ~ 32.
# Haiku 4.5: input ~$1, output ~$5 / 1M tokens.
_HAIKU_INPUT_NTD_PER_M = 32.0
_HAIKU_OUTPUT_NTD_PER_M = 160.0

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in env")
        _client = Anthropic(api_key=api_key)
    return _client


def estimate_tokens(text: str) -> int:
    """Rough token count. Chinese ~1.3 tokens/char, others ~1 token/4 chars."""
    n_zh = sum(1 for c in text if "一" <= c <= "鿿")
    n_other = len(text) - n_zh
    return int(n_zh * 1.3 + n_other / 4)


def estimate_cost_ntd(input_text: str, output_text: str, model: str = "haiku") -> float:
    in_tok = estimate_tokens(input_text)
    out_tok = estimate_tokens(output_text)
    if "haiku" in model.lower():
        return (in_tok * _HAIKU_INPUT_NTD_PER_M
                + out_tok * _HAIKU_OUTPUT_NTD_PER_M) / 1_000_000
    return (in_tok * _HAIKU_INPUT_NTD_PER_M
            + out_tok * _HAIKU_OUTPUT_NTD_PER_M) / 1_000_000


# Map our prefixed model names (compat with config) to bare Anthropic IDs.
def _normalize_model(model: str) -> str:
    if model.startswith("anthropic/"):
        return model.split("/", 1)[1]
    return model


def call(model: str, prompt: str, *, source: str = "whisperx-uxpipeline",
         max_tokens: int = 4096) -> tuple[str, float]:
    """Call Claude with a plain prompt, return (response_text, estimated_cost_ntd)."""
    real_model = _normalize_model(model)
    client = _get_client()
    resp = client.messages.create(
        model=real_model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    cost = estimate_cost_ntd(prompt, text, model)
    return text, cost
