"""LitellM 社区模型价格与上下文库（兜底数据源）。

数据源：https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
社区维护、覆盖主流模型，含每 token 价格与上下文窗口。

定位：站点自身 /api/pricing 的价格经常缺失或不准；本模块作为**兜底**——
仅在站点没给价（或为 0）时填充，且永不覆盖已确认的站点价；context_length
仅在模型还是默认 4096 时补齐。手动维护的价格（pricing_source == "manual"）
在调用方跳过，本模块不感知。

磁盘缓存 data/litellm_pricing.json，TTL 7 天；下载走代理池（可直连则直连）。
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
_CACHE_FILE = Path("data/litellm_pricing.json")
_TTL_SECONDS = 7 * 24 * 3600
_LOCK: Optional[asyncio.Lock] = None

# 模型名归一化时剥离的装饰：厂商路由前缀、日期版本号、免费/ Beta 后缀等
_STRIP_PREFIX = re.compile(r"^(openai/|anthropic/|google/|gemini/|meta-llama/|mistralai/|deepseek/|qwen/|azure/|bedrock/|vertex_ai/|groq/|together_ai/|openrouter/|github/)")
_STRIP_SUFFIX = re.compile(r"(-\d{4}-\d{2}-\d{2}|:free|:beta|:extended|:thinking|:nitro|-\d+k)$", re.IGNORECASE)


def _normalize_name(model_id: str) -> str:
    s = model_id.strip().lower()
    s = _STRIP_PREFIX.sub("", s)
    s = _STRIP_SUFFIX.sub("", s)
    return s


def _to_per_million(value: Any) -> Optional[float]:
    """litellm 按 per-token 计价 → aigate 按 per-million。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return round(v * 1_000_000, 6)


def _safe_int(value: Any) -> int:
    """litellm 库里个别条目把字段写成文档说明字符串，非数字一律按 0 处理。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _extract_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """从 litellm 条目提取 aigate 需要的字段（缺失的键不出现）。"""
    out: Dict[str, Any] = {}
    inp = _to_per_million(entry.get("input_cost_per_token"))
    outp = _to_per_million(entry.get("output_cost_per_token"))
    if inp is not None:
        out["input"] = inp
    if outp is not None:
        out["output"] = outp
    cr = _to_per_million(entry.get("cache_read_input_token_cost"))
    cw = _to_per_million(entry.get("cache_creation_input_token_cost"))
    if cr:
        out["cache_read"] = cr
    if cw:
        out["cache_write"] = cw
    # 上下文窗口：优先 max_input_tokens，退回 max_tokens（旧字段，输入+输出共享上限）
    max_in = _safe_int(entry.get("max_input_tokens"))
    max_total = _safe_int(entry.get("max_tokens"))
    window = max_in or max_total
    if window > 0:
        out["context_length"] = window
    mo = _safe_int(entry.get("max_output_tokens"))
    if mo > 0:
        out["max_output_tokens"] = mo
    if "input" in out or "output" in out:
        out["is_free"] = (out.get("input", 0) == 0 and out.get("output", 0) == 0)
    return out


def _load_cache() -> Dict[str, Any]:
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(db: Dict[str, Any]) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("save litellm cache failed: %s", e)


async def fetch_litellm_db(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """获取 litellm 价格库（模型名 → {input, output, cache_*, context_length...}）。

    带 TTL 的磁盘缓存；下载失败时退回过期缓存；全都失败返回空 dict。
    """
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    async with _LOCK:
        cache = _load_cache()
        now = time.time()
        fresh = cache.get("fetched_at", 0) + _TTL_SECONDS > now and cache.get("models")
        if fresh and not force:
            return cache["models"]
        raw: Optional[dict] = None
        try:
            import httpx
            from server.core.proxy_pool import get_proxy_pool
            proxy_kwargs = get_proxy_pool().proxied_kwargs()
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, **proxy_kwargs) as client:
                resp = await client.get(LITELLM_URL)
                resp.raise_for_status()
                raw = resp.json()
        except Exception as e:
            logger.warning("fetch litellm pricing failed: %s", e)
        if not isinstance(raw, dict):
            # 下载失败：有过期缓存就先用着
            return cache.get("models") or {}
        models: Dict[str, Dict[str, Any]] = {}
        for name, entry in raw.items():
            if not isinstance(entry, dict) or entry.get("litellm_provider") in (None, "unknown"):
                continue
            extracted = _extract_entry(entry)
            if extracted:
                models[_normalize_name(name)] = extracted
        _save_cache({"fetched_at": now, "count": len(models), "models": models})
        logger.info("litellm pricing loaded: %s models", len(models))
        return models


def match_litellm(model_id: str, db: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按模型名在 litellm 库里找价格：精确归一化匹配 → 最长子串匹配。"""
    if not model_id or not db:
        return None
    key = _normalize_name(model_id)
    if key in db:
        return db[key]
    # 子串匹配取最长键（如 "gpt-5.2-codex" 命中 "gpt-5.2"），避免短键误抢
    best_key, best_len = None, 0
    for k in db:
        if (k in key or key in k) and len(k) > best_len:
            best_key, best_len = k, len(k)
    return db.get(best_key) if best_key else None
