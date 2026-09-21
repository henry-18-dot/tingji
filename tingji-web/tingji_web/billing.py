from __future__ import annotations

import json
import math
from decimal import Decimal, ROUND_CEILING

from .config import get_settings


MILLION = Decimal(1_000_000)


def estimate_asr(seconds: float) -> Decimal:
    value = max(1, math.ceil(float(seconds)))
    return (Decimal(value) * get_settings().asr_yuan_per_hour /
            Decimal(3600)).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


def estimate_deepseek(messages: list[dict], model: str, max_tokens: int) -> Decimal:
    rates = get_settings()
    input_bound = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 512 + 64 * len(messages)
    return ((Decimal(input_bound) * rates.deepseek_cache_miss_yuan_per_million +
             Decimal(max_tokens) * rates.deepseek_output_yuan_per_million) /
            MILLION).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


def actual_deepseek(usage: dict, model: str) -> Decimal | None:
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    hit = usage.get("prompt_cache_hit_tokens", 0)
    miss = usage.get("prompt_cache_miss_tokens")
    hit = hit if isinstance(hit, int) and hit >= 0 else 0
    miss = miss if isinstance(miss, int) and miss >= 0 else max(0, prompt - hit)
    rates = get_settings()
    return ((Decimal(hit) * rates.deepseek_cache_hit_yuan_per_million +
             Decimal(miss) * rates.deepseek_cache_miss_yuan_per_million +
             Decimal(completion) * rates.deepseek_output_yuan_per_million) /
            MILLION).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
