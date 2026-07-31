"""
Generic New API pricing and performance source.

Many OpenAI-compatible gateways expose model metadata from `/v1/models`, but
publish billing and runtime quality data through their web UI APIs instead.
New API based sites usually provide:
- /api/pricing
- /api/perf-metrics/summary

This module fetches those public endpoints from the provider host and normalizes
model price plus success-rate data into AIGate's model metadata shape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse

import httpx

USER_AGENT = "Mozilla/5.0 AIGate Pricing Sync"


@dataclass
class PricingSyncResult:
    pricing: Dict[str, dict]
    source_url: str
    error: Optional[str] = None


def _normalize_model_name(value: str) -> str:
    return re.sub(r"[^a-z0-9._:/+-]+", "", (value or "").lower())


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _walk_records(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        if any(key in value for key in (
            "model", "model_name", "modelName", "model_id", "modelId", "name", "id",
            "inputPrice", "input_price", "prompt_price", "input_unit_cost",
            "outputPrice", "output_price", "completion_price", "output_unit_cost",
            "model_ratio", "completion_ratio", "success_rate", "successRate",
            "avg_latency_ms", "avg_ttft_ms", "avg_tps", "request_count",
        )):
            yield value
        for child in value.values():
            yield from _walk_records(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_records(item)


def _first(record: dict, *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


# 缓存价常见字段名（New API / one-api 各 fork 命名不一，尽量兼容）
_CACHE_READ_KEYS = (
    "cache_read_input_price", "cacheReadInputPrice", "cache_read_input_token_price",
    "inputCachedPrice", "cache_read_price", "cacheReadPrice", "cache_read_input_cost",
)
_CACHE_WRITE_KEYS = (
    "cache_write_input_price", "cacheWriteInputPrice", "cache_write_input_token_price",
    "cache_write_price", "cacheWritePrice", "cache_creation_input_price",
    "cacheCreationInputPrice", "cache_write_input_cost",
)
_CACHE_READ_RATIO_KEYS = ("cache_read_ratio", "cacheReadRatio", "cache_read_input_ratio", "cache_ratio")
_CACHE_WRITE_RATIO_KEYS = ("cache_write_ratio", "cacheWriteRatio", "cache_creation_ratio", "cacheCreationRatio", "create_cache_ratio")


def _extract_cache_prices(record: dict, in_price: Optional[float]) -> tuple:
    """从定价记录抽取缓存读/写价（每百万 token 美元）。

    优先取绝对价格字段；缺失时退化为 ratio（相对 input 价的乘数，New API 语义）
    乘 input 价。返回 (cache_read, cache_write)，任一为 None 表示未提供。
    """
    read = _to_float(_first(record, *_CACHE_READ_KEYS))
    write = _to_float(_first(record, *_CACHE_WRITE_KEYS))
    if read is None:
        r = _to_float(_first(record, *_CACHE_READ_RATIO_KEYS))
        if r is not None and in_price:
            read = round(in_price * r, 6)
    if write is None:
        w = _to_float(_first(record, *_CACHE_WRITE_RATIO_KEYS))
        if w is not None and in_price:
            write = round(in_price * w, 6)
    return read, write


def _merge_record(target: Dict[str, dict], model_key: str, values: dict) -> None:
    current = target.setdefault(model_key, {})
    for key, value in values.items():
        if value is not None:
            current[key] = value


def _extract_pricing_from_json(data: Any) -> Dict[str, dict]:
    pricing: Dict[str, dict] = {}
    for record in _walk_records(data):
        model = _first(
            record,
            "model", "model_name", "modelName", "model_id", "modelId",
            "name", "id", "key",
        )
        input_price = _first(
            record,
            "inputPrice", "input_price", "prompt_price", "promptPrice",
            "input_unit_cost", "inputUnitCost", "input", "p", "model_ratio",
        )
        output_price = _first(
            record,
            "outputPrice", "output_price", "completion_price", "completionPrice",
            "output_unit_cost", "outputUnitCost", "output", "c",
        )
        completion_ratio = _first(record, "completion_ratio", "completionRatio")
        model_key = _normalize_model_name(str(model or ""))
        in_price = _to_float(input_price)
        out_price = _to_float(output_price)
        completion_multiplier = _to_float(completion_ratio)
        if out_price is None and in_price is not None and completion_multiplier is not None:
            out_price = in_price * completion_multiplier
        if not model_key or in_price is None or out_price is None:
            continue
        cache_read, cache_write = _extract_cache_prices(record, in_price)
        _merge_record(pricing, model_key, {
            "input": in_price,
            "output": out_price,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "is_free": in_price == 0 and out_price == 0,
        })
    return pricing


def _extract_metrics_from_json(data: Any) -> Dict[str, dict]:
    metrics: Dict[str, dict] = {}
    for record in _walk_records(data):
        model = _first(record, "model", "model_name", "modelName", "model_id", "modelId", "name", "id")
        model_key = _normalize_model_name(str(model or ""))
        if not model_key:
            continue
        success_rate = _to_float(_first(record, "success_rate", "successRate", "success", "availability"))
        avg_latency_ms = _to_float(_first(record, "avg_latency_ms", "avgLatencyMs", "latency_ms", "latency"))
        avg_ttft_ms = _to_float(_first(record, "avg_ttft_ms", "avgTtftMs", "ttft_ms", "ttft"))
        avg_tps = _to_float(_first(record, "avg_tps", "avgTps", "tps"))
        request_count = _to_float(_first(record, "request_count", "requestCount", "requests"))
        values = {
            "success_rate": success_rate,
            "avg_latency_ms": avg_latency_ms,
            "avg_ttft_ms": avg_ttft_ms,
            "avg_tps": avg_tps,
            "request_count": int(request_count) if request_count is not None else None,
        }
        if any(value is not None for value in values.values()):
            _merge_record(metrics, model_key, values)
    return metrics


def _extract_pricing_from_text(text: str) -> Dict[str, dict]:
    pricing: Dict[str, dict] = {}
    compact = re.sub(r"<[^>]+>", " ", text)
    compact = re.sub(r"\s+", " ", compact)
    model_pattern = re.compile(
        r"([a-zA-Z0-9][a-zA-Z0-9._:/+-]{2,100})\s+"
        r"(?:¥|￥|\$)?\s*(\d+(?:\.\d+)?)\s+"
        r"(?:¥|￥|\$)?\s*(\d+(?:\.\d+)?)"
    )
    for model, input_price, output_price in model_pattern.findall(compact):
        model_key = _normalize_model_name(model)
        in_price = _to_float(input_price)
        out_price = _to_float(output_price)
        if not model_key or in_price is None or out_price is None:
            continue
        if model_key in {"http", "https", "pricing", "api/pricing"}:
            continue
        pricing[model_key] = {
            "input": in_price,
            "output": out_price,
            "is_free": in_price == 0 and out_price == 0,
        }
    return pricing


def _provider_origin(provider_base_url: str) -> Optional[str]:
    parsed = urlparse(provider_base_url or "")
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _merge_metrics(pricing: Dict[str, dict], metrics: Dict[str, dict]) -> Dict[str, dict]:
    merged = {key: value.copy() for key, value in pricing.items()}
    for model_key, values in metrics.items():
        _merge_record(merged, model_key, values)
    return merged


async def fetch_provider_pricing(provider_base_url: str, timeout: int = 15) -> PricingSyncResult:
    origin = _provider_origin(provider_base_url)
    if not origin:
        return PricingSyncResult({}, "", "invalid provider base_url")
    headers = {"User-Agent": USER_AGENT}
    pricing: Dict[str, dict] = {}
    metrics: Dict[str, dict] = {}
    errors = []
    from server.core.proxy_pool import get_proxy_pool
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, **get_proxy_pool().proxied_kwargs()) as client:
        for url in (
            f"{origin}/api/pricing",
            f"{origin}/api/pricing?page=1&page_size=2000",
            f"{origin}/api/pricing?type=all",
        ):
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    continue
                try:
                    pricing = _extract_pricing_from_json(resp.json())
                except Exception:
                    pricing = _extract_pricing_from_text(resp.text)
                if pricing:
                    break
            except Exception as exc:
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
        for url in (
            f"{origin}/api/perf-metrics/summary",
            f"{origin}/api/perf-metrics/summary?period=24h",
        ):
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    continue
                metrics = _extract_metrics_from_json(resp.json())
                if metrics:
                    break
            except Exception as exc:
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
        if not pricing:
            try:
                resp = await client.get(f"{origin}/pricing", headers=headers)
                if resp.status_code == 200:
                    pricing = _extract_pricing_from_text(resp.text)
            except Exception as exc:
                errors.append(f"{origin}/pricing: {type(exc).__name__}: {exc}")
    merged = _merge_metrics(pricing, metrics)
    error = "; ".join(errors[-3:]) if errors and not merged else None
    return PricingSyncResult(merged, origin, error)


async def fetch_xyusec_pricing(timeout: int = 15) -> Dict[str, dict]:
    return (await fetch_provider_pricing("https://www.xyusec.com/v1", timeout=timeout)).pricing


def match_model_metadata(model_id: str, pricing: Dict[str, dict]) -> Optional[dict]:
    key = _normalize_model_name(model_id)
    if key in pricing:
        return pricing[key]
    for price_key, value in pricing.items():
        if key.endswith(price_key) or price_key.endswith(key):
            return value
    tail = key.split("/")[-1]
    if tail in pricing:
        return pricing[tail]
    for price_key, value in pricing.items():
        price_tail = price_key.split("/")[-1]
        if tail == price_tail or tail.endswith(price_tail) or price_tail.endswith(tail):
            return value
    return None


def match_xyusec_pricing(model_id: str, pricing: Dict[str, dict]) -> Optional[dict]:
    return match_model_metadata(model_id, pricing)
