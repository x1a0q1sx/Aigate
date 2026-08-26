"""
/v1/* OpenAI 兼容端点
"""
import asyncio
import json
import time
from typing import Optional
import urllib.parse
from fastapi import APIRouter, Depends, Request, Response, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from server.schemas.chat import ChatCompletionRequest, ChatCompletionResponse
from server.models.model import Model
from server.models.provider import Provider
from server.models.api_key import ApiKey
from server.db import AsyncSessionLocal
from server.core.auto_router import AutoRouter
from server.core.request_logger import write_log, dedup_log_row  # v3.6 消息级去重写入
from server.core.model_catalog import ModelCatalog
from server.core.auto_router import RouteResult
from server.core.route_decision import (
    add_attempt as _decision_attempt,
    begin_decision as _decision_begin,
    capture_candidates as _decision_candidates,
    configure_decision as _decision_configure,
    finish_decision as _decision_finish,
    ingest_attempt_errors as _decision_ingest_attempts,
    mark_candidate_skipped as _decision_skip,
    mark_selected as _decision_select,
)
from server.core.context_guard import (
    estimate_request_tokens,
    is_context_error,
    context_overflows,
)
from server.config import get_config, save_config


def _openai_completion_is_empty(result) -> bool:
    """
    判断一次『非流式 OpenAI 格式响应』是否为空输出：
    - 没有 choices；或
    - 所有 choices 的 content 为空/None 且没有 tool_calls；且
    - usage 的 completion_tokens 为 0（或缺省）。

    用于 combo 把「HTTP 成功但答空」的候选判为失败，从而继续 fallback 到下一个候选。
    注意：仅对 combo 路由启用，直连/auto 的合法空回复不受影响。
    """
    if not isinstance(result, dict):
        return False
    choices = result.get("choices") or []
    if not choices:
        return True
    total_content = 0
    has_tool_calls = False
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        msg = ch.get("message") or ch.get("delta") or {}
        c = msg.get("content")
        if isinstance(c, str):
            total_content += len(c.strip())
        elif isinstance(c, list):
            total_content += len(str(c))  # 多模态 content 数组
        if msg.get("tool_calls"):
            has_tool_calls = True
    if has_tool_calls:
        return False
    if total_content > 0:
        return False
    usage = result.get("usage") or {}
    ct = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    if ct and int(ct) > 0:
        return False
    return True


def _estimate_prompt_tokens_from_request(request) -> int:
    """按请求内容粗估 prompt token 数（兜底用，不追求精确）。"""
    if not request or not getattr(request, "messages", None):
        return 0
    chars = 0
    for m in request.messages:
        content = getattr(m, "content", None) or ""
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):  # multimodal content parts
            for p in content:
                if isinstance(p, dict):
                    if isinstance(p.get("text"), str):
                        chars += len(p["text"])
                    elif isinstance(p.get("content"), str):
                        chars += len(p["content"])
    # 中文/混合文本粗略按 4 字符≈1 token 估算，最少 1
    return max(1, chars // 4 + 1)


def _sanitize_token_counts(request, pt: int, ct: int, output_text: str = ""):
    """
    修正上游明显瞎报/漏报的 token 数，避免污染用量/成本统计。
    典型场景：agentrouter 等聚合站把 input_tokens 恒报为 1；ModelScope 等流式上游
    根本不返回 usage（completion 恒为 0）。
    - prompt_tokens：上游漏报/明显偏低时按请求内容粗估覆盖
    - completion_tokens：上游漏报（=0）时按实际输出文本粗估（传给 output_text）
    """
    pt = int(pt or 0)
    ct = int(ct or 0)
    est = _estimate_prompt_tokens_from_request(request)
    # 上游明显瞎报（如 agentrouter 恒报 1）：当且仅当上游报的 prompt 极少（≤2）
    # 而按请求内容粗估明显更多（>2）时，用粗估覆盖。真实极短 prompt（如 "hi"）粗估也≈1，不会误改。
    if est > 2 and pt <= 2:
        pt = est
    elif pt == 0 and est > 0:
        pt = est
    if ct <= 0 and output_text:
        ct = max(1, len(output_text) // 4 + 1)
    return pt, ct


def _output_text_from_chunks(chunks) -> str:
    """从 OpenAI 流式 chunk 列表提取实际输出文本（正文 + 思考 + 工具调用参数），用于估算 completion tokens。"""
    parts = []
    for c in (chunks or []):
        if not isinstance(c, dict):
            continue
        for ch in (c.get("choices") or []):
            if not isinstance(ch, dict):
                continue
            d = ch.get("delta") or {}
            if isinstance(d.get("content"), str):
                parts.append(d["content"])
            rc = d.get("reasoning_content")
            if isinstance(rc, str):
                parts.append(rc)
            for tc in (d.get("tool_calls") or []):
                a = ((tc or {}).get("function") or {}).get("arguments")
                if isinstance(a, str):
                    parts.append(a)
    return "".join(parts)


def _output_text_from_result(result: dict) -> str:
    """从 OpenAI 非流式响应提取实际输出文本，用于估算 completion tokens。"""
    parts = []
    for ch in ((result or {}).get("choices") or []):
        if not isinstance(ch, dict):
            continue
        m = ch.get("message") or {}
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        rc = m.get("reasoning_content")
        if isinstance(rc, str):
            parts.append(rc)
        for tc in (m.get("tool_calls") or []):
            a = ((tc or {}).get("function") or {}).get("arguments") or ""
            if a:
                parts.append(a)
    return "".join(parts)


def _extract_cache_tokens(usage: dict):
    """从 usage 提取缓存读/写 token 数，兼容多种上游格式。返回 (cache_read, cache_write)。"""
    if not usage:
        return 0, 0
    # OpenAI 格式：usage.prompt_tokens_details.{cached_tokens, cache_creation_tokens}
    ptd = usage.get("prompt_tokens_details") or {}
    if isinstance(ptd, dict):
        crt = int(ptd.get("cached_tokens") or 0)
        cwt = int(ptd.get("cache_creation_tokens") or 0)
        if crt or cwt:
            return crt, cwt
    # Anthropic 格式：usage.cache_read_input_tokens / cache_creation_input_tokens
    crt = int(usage.get("cache_read_input_tokens") or 0)
    cwt = int(usage.get("cache_creation_input_tokens") or 0)
    if crt or cwt:
        return crt, cwt
    # Responses API 格式：usage.input_tokens_details.{cached_tokens, cache_write_tokens, cache_creation_tokens}
    # / usage.cache_creation_details.total_tokens（部分上游把缓存写放在 input_tokens_details.cache_write_tokens）
    itd = usage.get("input_tokens_details") or {}
    if isinstance(itd, dict):
        if not crt:
            crt = int(itd.get("cached_tokens") or 0)
        if not cwt:
            cwt = int(itd.get("cache_write_tokens") or itd.get("cache_creation_tokens") or 0)
    ccd = usage.get("cache_creation_details") or {}
    if isinstance(ccd, dict) and not cwt:
        cwt = int(ccd.get("total_tokens") or 0)
    return crt, cwt


def _segmented_cost(prices: dict, pt: int, ct: int, crt: int, cwt: int) -> float:
    """分段计算成本（美元/请求）。

    普通输入 token 按 input_price；缓存读 token 按 cache_read_input_price（缺价时回退 input_price，
    保守不低估）；缓存写 token 按 cache_write_input_price（缺价时回退 input_price）；输出按 output_price。
    缓存价缺省为 0 视为「未配置」，回退到 input 价，等价于改造前行为。
    """
    ip = float(prices.get("input_price") or 0)
    op = float(prices.get("output_price") or 0)
    crp = float(prices.get("cache_read_input_price") or 0) or ip
    cwp = float(prices.get("cache_write_input_price") or 0) or ip
    pt = int(pt or 0); ct = int(ct or 0); crt = int(crt or 0); cwt = int(cwt or 0)
    # 普通输入 = 总输入 - 缓存读 - 缓存写（防止上游口径不一致导致负数）
    safe = max(0, pt - crt - cwt)
    return round((safe * ip + crt * crp + cwt * cwp + ct * op) / 1_000_000.0, 6)


def _stream_content_is_empty(buf) -> bool:
    """
    判断一段流式 chunk 列表是否『零内容』：所有 delta.content 拼起来为空，且没有 tool_calls。
    用于 combo 流式级联：当且仅当客户端尚未收到任何真实内容时，才能干净地 fallback 到下一个候选。
    """
    if not buf:
        return True
    total = 0
    for ck in buf:
        if not isinstance(ck, dict):
            continue
        ch = ck.get("choices") or []
        if not ch:
            continue
        first = ch[0] if isinstance(ch, list) else ch
        delta = first.get("delta", {}) if isinstance(first, dict) else {}
        c = delta.get("content")
        if isinstance(c, str):
            total += len(c)
        if delta.get("tool_calls"):
            return False
    return total == 0


def _stream_usage_dict(chunk) -> dict:
    """Return usage only when an upstream chunk provides the OpenAI object shape."""
    if not isinstance(chunk, dict):
        return {}
    usage = chunk.get("usage")
    return usage if isinstance(usage, dict) else {}


_STREAM_CONTENT_VALIDATION_MARKERS = (
    "stream content validation failed",
    "stream disconnected before valid content",
    "stream disconnected before completion",
    "idle timeout",
    "waiting for sse",
    "content is insufficient",
    "received 0 chars",
)


def _is_stream_content_validation_error(text) -> bool:
    """识别上游转发网关（new-api / one-api 类）在流式内容校验失败时返回的错误。

    这类错误常出现在思考型模型只产出 reasoning_content、或模型达到 max_tokens 上限后
    没有吐出任何正文的场景。它表示候选上游没有得到可用正文，而不是模型网络/健康故障，
    因此：A）在级联路由中应静默回退到下一个候选；B）不应据此对模型计失败或冷却。
    """
    if not text:
        return False
    lowered = str(text).lower()
    return any(marker in lowered for marker in _STREAM_CONTENT_VALIDATION_MARKERS)


async def _stream_with_first_chunk_timeout(source, timeout_seconds: float):
    """Yield an upstream stream, failing quickly when it never produces a chunk."""
    try:
        first = await asyncio.wait_for(anext(source), timeout=max(1, timeout_seconds))
    except StopAsyncIteration:
        return
    except asyncio.TimeoutError as error:
        raise TimeoutError(
            f"upstream did not produce a first stream chunk within {timeout_seconds:.0f}s"
        ) from error
    yield first
    async for chunk in source:
        yield chunk


def _merge_oauth_headers(provider, base_headers=None):
    """Merge auth and per-provider routing metadata into outbound header hints."""
    eh = dict(base_headers) if base_headers else None
    if provider and getattr(provider, "credential_type", "") == "oauth":
        eh = eh or {}
        eh["__oauth"] = True
    if provider and getattr(provider, "proxy_enabled", False):
        eh = eh or {}
        eh["__proxy_force"] = True
    return eh

config = get_config()
router = APIRouter(prefix="/v1")
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

@router.post("/compress")
async def compress_context(raw_request: Request):
    """Compress messages using AIGate token savers without routing to an upstream model."""
    verify_aigate_api_key(raw_request)
    try:
        body = await raw_request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return JSONResponse(status_code=400, content={"error": "messages must be a list"})
    from server.core.compress_service import compress_messages
    result = compress_messages(
        messages,
        rtk_enabled=body.get("rtk_enabled"),
        caveman_enabled=body.get("caveman_enabled"),
        ponytail_enabled=body.get("ponytail_enabled"),
    )
    return JSONResponse(content={"object": "context.compression", **result})

def verify_aigate_api_key(raw_request: Request):
    expected = getattr(config.security, "aigate_api_key", "") or ""
    if not expected:
        return
    # Bearer（OpenAI/Codex 客户端）与 x-api-key（Anthropic 客户端）都接受
    auth = raw_request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    else:
        token = raw_request.headers.get("x-api-key", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing AIGate API key")
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid AIGate API key")

# 全局单例缓存
from server.core.auto_router import AutoRouter
from server.core.model_catalog import ModelCatalog
from server.core.health_checker import HealthChecker
from server.core.key_manager import KeyManager
from server.core.crypto_service import get_crypto_service
_auto_router: Optional[AutoRouter] = None
_model_catalog: Optional[ModelCatalog] = None

def _safe_header(val: str) -> str:
    """Ensure header value is ASCII-safe (URL-encode non-ASCII)."""
    return urllib.parse.quote(val, safe="")

def _format_sse_chunk(chunk: dict, model_id_full: str) -> bytes:
    """Serialize an OpenAI-compatible SSE chunk."""
    if "model" in chunk:
        chunk = {**chunk, "model": model_id_full}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

_MAX_SYSTEM_CHARS = 8000

# ── 诊断日志：阶段 / 参数 中文映射（让日志一目了然"当前在干什么"）──
_DIAG_STAGE_LABELS = {
    "request_enter": "收到新请求",
    "auth_start": "开始鉴权",
    "auth_done": "鉴权通过",
    "router_get_start": "开始解析路由",
    "router_get_done": "路由解析完成",
    "rtk_applied": "已应用 Token 压缩(RTK)",
    "caveman_applied": "已应用 Caveman 压缩",
    "ponytail_applied": "已应用 Ponytail 压缩",
    "preprocess_done": "请求预处理完成",
    "direct_route_start": "开始直连路由",
    "direct_route_not_found": "未找到对应模型",
    "direct_model_done": "已锁定目标模型",
    "direct_key_skipped_free_tier": "免密钥供应商跳过取 key",
    "direct_key_skipped_oauth": "OAuth 供应商跳过取 key",
    "direct_key_done": "已取得可用密钥",
    "direct_key_missing": "无可用密钥",
    "direct_route_done": "直连路由完成",
    "free_provider_executor_hit": "命中免费供应商执行器",
    "free_provider_executor_miss": "未命中免费供应商执行器",
    "combo_route_start": "开始组合(combo)路由",
    "combo_targets": "已确定候选模型",
    "combo_stream_start": "开始并发流式请求",
    "combo_stream_exhausted": "候选已尝试完",
    "upstream_stream_start": "向上游发起请求",
    "upstream_stream_done": "上游请求成功",
    "upstream_stream_error": "上游请求失败",
    "auto_cascade_start": "开始自动(auto)级联",
    "auto_candidate_start": "开始尝试候选",
    "auto_candidate_done": "候选尝试完成",
    "upstream_start": "向上游发起请求(非流式)",
    "upstream_done": "上游请求成功(非流式)",
    "upstream_error": "上游请求失败(非流式)",
    "fallback_log_start": "开始记录降级日志",
    "fallback_log_done": "降级日志记录完成",
    "fallback_log_error": "降级日志记录失败",
    "stream_log_start": "开始记录上游结果",
    "stream_log_done": "上游结果记录完成",
    "stream_log_error": "上游结果记录失败",
    "combo_hit": "组合命中可用模型",
    "combo_all_failed": "组合全部候选失败",
    "auto_stream_response_created": "已创建自动流式响应",
    "auto_stream_generator_start": "自动流式生成器启动",
    "auto_stream_candidate_start": "开始尝试自动候选",
    "auto_stream_candidate_done": "自动候选尝试完成",
    "final_error_log_start": "开始记录最终错误",
    "final_error_log_done": "最终错误记录完成",
    "final_error_log_error": "最终错误记录失败",
    "direct_stream_response_created": "已创建直连流式响应",
    "direct_stream_generator_start": "直连流式生成器启动",
    "request_log_start": "开始写入请求日志",
    "request_log_done": "请求日志写入完成",
    "request_log_error": "请求日志写入失败",
    "response_ready": "响应已就绪",
}

_DIAG_KW_LABELS = {
    "model": "模型",
    "stream": "流式",
    "client": "客户端",
    "combo": "组合名",
    "count": "数量",
    "attempt": "第N次尝试",
    "provider": "服务商",
    "rules": "规则",
    "saved": "节省字符",
    "compressed": "压缩后字符",
    "orig": "原始字符",
    "preview": "压缩预览",
    "chunks": "数据块数",
    "status": "状态",
    "success": "是否成功",
    "error": "错误类型",
    "target": "目标",
    "attempted": "已尝试数",
    "routed_model": "路由模型",
    "key_id": "密钥ID",
    "code": "免费码",
    "max_retries": "最大重试",
    "http_status": "HTTP状态",
    "response_type": "响应类型",
}

_DIAG_VERBOSE = None
def _diag_verbose() -> bool:
    """按需读取 config.logging.verbose_diag（默认 False=精简模式）"""
    global _DIAG_VERBOSE
    if _DIAG_VERBOSE is None:
        try:
            _DIAG_VERBOSE = bool(get_config().logging.verbose_diag)
        except Exception:
            _DIAG_VERBOSE = False
    return _DIAG_VERBOSE

def get_diag_verbose() -> bool:
    """供管理接口读取当前运行时诊断开关"""
    return _diag_verbose()

def set_diag_verbose(val: bool):
    """切换诊断开关（同时持久化到 config.yaml，重启后仍生效）"""
    global _DIAG_VERBOSE
    _DIAG_VERBOSE = bool(val)
    try:
        cfg = get_config()
        cfg.logging.verbose_diag = bool(val)
        save_config()
    except Exception:
        # 持久化失败不影响本次运行时的开关
        pass

# 精简模式下跳过的「平凡过渡」阶段（调试时把 verbose_diag 设为 true 即全部输出）
_DIAG_SKIP_WHEN_QUIET = {
    # 鉴权 / 路由解析的过渡行
    "auth_start", "auth_done",
    "router_get_start", "router_get_done",
    "preprocess_done",
    "direct_route_start", "combo_route_start",
    # 自动路由级联逐候选（调试用）
    "auto_candidate_start", "auto_candidate_done",
    "auto_stream_response_created", "auto_stream_generator_start",
    "auto_stream_candidate_start", "auto_stream_candidate_done",
    # 非流式上游起止（流式有 upstream_stream_* 覆盖）
    "upstream_start", "upstream_done",
    # 直连 key 选取细节
    "direct_key_done", "direct_model_done",
    "free_provider_executor_hit", "free_provider_executor_miss",
    # 流 / 日志收尾的冗余 bookend
    "stream_log_start", "stream_log_done",
    "fallback_log_start", "fallback_log_done",
    "final_error_log_start", "final_error_log_done",
    "direct_stream_response_created", "direct_stream_generator_start",
    "request_log_start",
}

def _diag(conversation_id: str, stage: str, start_ts: float, **kwargs):
    """轻量并发诊断日志（中文可读）：说明请求当前在哪个阶段、做了什么。
    精简模式（verbose_diag=false，默认）下跳过平凡过渡阶段，只保留关键里程碑。"""
    if not _diag_verbose():
        if stage in _DIAG_SKIP_WHEN_QUIET or stage.startswith("direct_key_skipped_"):
            return
    elapsed_ms = int((time.time() - start_ts) * 1000)
    label = _DIAG_STAGE_LABELS.get(stage, stage)
    parts = [f"[请求诊断] {label}", f"耗时 {elapsed_ms}ms"]
    for k, v in kwargs.items():
        if v is None:
            continue
        if k == "attempt":
            parts.append(f"第{v + 1}次尝试")
            continue
        parts.append(f"{_DIAG_KW_LABELS.get(k, k)}={v}")
    print(" | ".join(parts), flush=True)


def _rtk_preview(msgs, limit: int = 600) -> str:
    """拼接压缩后 system/user 内容做日志预览（截断，避免刷屏/撑爆日志）"""
    parts = []
    for m in msgs:
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
        if role not in ("system", "user"):
            continue
        content = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(f"[{role}] {content}")
    s = "\n".join(parts)
    if not s:
        return ""
    if len(s) > limit:
        s = s[:limit] + f"...(截断, 共 {len(s)} 字符)"
    return s

def _extract_error_body(e: Exception) -> str:
    """从 httpx 异常/str 中提取上游返回的原始响应体"""
    s = str(e)
    # adapter 已把 response body 拼在异常消息里，格式：...\nResponse: {...}
    if '\nResponse: ' in s:
        return s.split('\nResponse: ', 1)[1][:5000]
    # httpx HTTPStatusError 对象
    try:
        t = getattr(getattr(e, 'response', None), 'text', '') or ''
        if t:
            return t[:5000]
    except Exception:
        pass
    return s


def _proxy_log_fields() -> dict:
    """读取本次请求线请求实际使用的代理（adapter 在发请求前写入 ContextVar），供日志落库。

    返回 {used_proxy, proxy_url}。未走代理时 proxy_url=None、used_proxy=False。
    每个请求在独立 asyncio 任务中处理，ContextVar 天然隔离，不会串号。
    """
    from server.core.proxy_pool import CURRENT_PROXY_URL
    u = CURRENT_PROXY_URL.get()
    return {"used_proxy": bool(u), "proxy_url": u}

def _preprocess_request(req):
    """轻量截断超长 system message（保底）+ RTK Token Saver 注入式压缩
    + Caveman / Ponytail（默认关，config.token_saver_extra 开启时生效）"""
    msgs = getattr(req, 'messages', None) or []
    # 1) 超长保底截断（防止某些 upstream 不允许 system 过大）
    for i, m in enumerate(msgs):
        if hasattr(m, 'role') and m.role == 'system' and hasattr(m, 'content') and m.content:
            if isinstance(m.content, str) and len(m.content) > _MAX_SYSTEM_CHARS:
                msgs[i] = m.model_copy(update={"content": m.content[:_MAX_SYSTEM_CHARS] + "\n...(truncated by AIGate)"})
    # 2) RTK Token Saver（默认开启，可在 config.yaml 关闭）
    try:
        from server.core.token_saver import apply_rtk
        ts_cfg = getattr(config, 'token_saver', None)
        ts_enabled = getattr(ts_cfg, 'enabled', True) if ts_cfg else True
        new_msgs, stats = apply_rtk(msgs, enabled=ts_enabled)
        if stats.get("applied"):
            _diag("", "rtk_applied", time.time(),
                  rules=stats["rules_hit"], orig=stats["original_chars"],
                  saved=stats["chars_saved"], compressed=stats.get("compressed_chars"))
        # 用浅拷贝方式替换原对象的 messages（req 是 pydantic Model）
        if hasattr(req, 'messages'):
            try:
                req = req.model_copy(update={"messages": new_msgs})
            except Exception:
                req.messages = new_msgs
    except Exception as _e:
        # 任何异常都不影响业务，安全回退原 request
        import logging
        logging.getLogger(__name__).warning("RTK apply failed: %s", _e)
    # 3) Caveman 压缩（默认关）
    try:
        from server.core.caveman_saver import apply_caveman
        extra = getattr(config, 'token_saver_extra', None)
        if extra and getattr(extra, 'caveman_enabled', False):
            cur_msgs = getattr(req, 'messages', None) or []
            new_msgs_c, stats_c = apply_caveman(cur_msgs, enabled=True)
            if stats_c.get("applied"):
                _diag("", "caveman_applied", time.time(),
                      applied=stats_c["applied"], saved=stats_c["saved_chars"])
            if hasattr(req, 'messages'):
                try:
                    req = req.model_copy(update={"messages": new_msgs_c})
                except Exception:
                    req.messages = new_msgs_c
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning("caveman apply failed: %s", _e)
    # 4) Ponytail 折叠（默认关）
    try:
        from server.core.ponytail_saver import apply_ponytail
        extra = getattr(config, 'token_saver_extra', None)
        if extra and getattr(extra, 'ponytail_enabled', False):
            cur_msgs = getattr(req, 'messages', None) or []
            new_msgs_p, stats_p = apply_ponytail(cur_msgs, enabled=True)
            if stats_p.get("applied"):
                _diag("", "ponytail_applied", time.time(),
                      applied=stats_p["applied"], saved=stats_p["saved_chars"])
            if hasattr(req, 'messages'):
                try:
                    req = req.model_copy(update={"messages": new_msgs_p})
                except Exception:
                    req.messages = new_msgs_p
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning("ponytail apply failed: %s", _e)
    return req

async def _auto_route_with_runtime_fallback(ar, db, request, conversation_id):
    """
    Auto 路由 + 运行时 fallback（仅探测连通性）。
    此函数仅用于快速探测候选模型是否可达（max_tokens=1），
    不做完整的业务请求。真实请求在 chat_completions 中，
    配合 _auto_request_with_cascade_fallback 做级联回退。
    """
    max_retries = max(1, ar.config.max_fallbacks)
    attempt_errors = []
    last_result = None
    tried_ids = set()
    for attempt in range(max_retries + 1):
        candidate = await ar.get_best_candidate(db, conversation_id, exclude_model_ids=tried_ids)
        last_result = candidate
        if not candidate.success:
            attempt_errors.append({"attempt": attempt, "error": candidate.error})
            break
        if candidate.model and candidate.model.id in tried_ids:
            attempt_errors.append({"attempt": attempt, "error": "duplicate candidate, no more options"})
            break
        tried_ids.add(candidate.model.id)
        from server.schemas.chat import ChatCompletionRequest as _CCR
        probe = _CCR(
            model=candidate.model.model_id,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            stream=False,
        )
        try:
            result = await candidate.adapter.chat_completion(
                probe,
                candidate.api_key,
                candidate.provider.base_url,
                _merge_oauth_headers(candidate.provider, candidate.provider.headers),
            )
            choices = result.get("choices", []) if isinstance(result, dict) else []
            usage = result.get("usage", {}) if isinstance(result, dict) else {}
            has_content = any(
                c.get("message", {}).get("content") or c.get("delta", {}).get("content") or c.get("text")
                for c in choices
            ) if choices else False
            if not choices or not has_content:
                raise ValueError(f"empty_response: choices={len(choices)} tokens={usage.get('completion_tokens', 0)}")
            return RouteResult(
                success=True,
                model=candidate.model,
                provider=candidate.provider,
                api_key=candidate.api_key,
                adapter=candidate.adapter,
                fallback_count=attempt,
            ), attempt_errors
        except Exception as e:
            err_short = f"{type(e).__name__}: {str(e)[:120]}"
            attempt_errors.append({
                "attempt": attempt,
                "model": f"{candidate.provider.name}/{candidate.model.model_id}",
                "error": err_short,
            })
            if ar.health_checker and not is_context_error(err_short):
                # 上下文超限不是模型故障，不进冷却（请求体大小问题）
                ar.health_checker.mark_cooling(
                    candidate.model.id,
                    ar.config.cooling_period_seconds,
                )
            # 探测失败也应留痕，否则健康页显示冷却却查不到原因（与非流式候选失败对称）
            try:
                import json as _pj
                from server.db import AsyncSessionLocal as _PLS
                async with _PLS() as _pldb:
                    await write_log(_pldb,
                        conversation_id=conversation_id,
                        requested_model=request.model if request else "unknown",
                        routed_provider=candidate.provider.name,
                        routed_provider_id=candidate.provider.id,
                        routed_model=candidate.model.model_id,
                        status="error",
                        error_type="probe_error",
                        error_msg=err_short,
                        fallback_count=attempt,
                        user_ip=None,
                        request_body=_pj.dumps(request.model_dump(), ensure_ascii=False) if request else None,
                        response_body=None,
                    )
            except Exception:
                pass
            continue
    return last_result, attempt_errors


async def _auto_request_with_cascade_fallback(ar, db, request, conversation_id, diag_start_ts=None, *, combo_targets=None):
    """
    级联回退：对第一个 auto 候选发起完整业务请求，
    若超时/错误/空返回 → 自动尝试第二个、第三个……
    直到成功或全部失败。
    返回 (RouteResult, response_dict, list_of_attempts)
    
    如果指定 combo_targets（list of full_id str），直接用它迭代，不调用 ar.get_best_candidate。
    """
    max_retries = max(1, ar.config.max_fallbacks)
    attempt_errors = []
    tried_ids = set()
    last_result = None
    diag_start_ts = diag_start_ts or time.time()
    est_tokens = estimate_request_tokens(request)
    _diag(conversation_id, "auto_cascade_start", diag_start_ts, max_retries=max_retries, combo=bool(combo_targets), est_tokens=est_tokens)
    
    # 预先解析 combo 候选（如果有），避免在循环内重复查 DB
    combo_candidates = []
    if combo_targets:
        for full_id in combo_targets:
            prov_name, m_id = full_id.split("/", 1) if "/" in full_id else (None, full_id)
            from server.models.provider import Provider as _P
            from server.models.model import Model as _M
            p_r = await db.execute(select(_P).where(_P.name == prov_name).limit(1))
            _prov = p_r.scalar_one_or_none()
            # v4.0: 服务商被禁用 → 跳过该候选（不删除组合配置）
            if _prov is not None and not getattr(_prov, "enabled", True):
                attempt_errors.append({"target": full_id, "error": "provider disabled"})
                continue
            m_r = await db.execute(select(_M).where(_M.provider_id == _prov.id, _M.model_id == m_id, _M.enabled == True).limit(1)) if _prov else None
            _mdl = m_r.scalar_one_or_none() if m_r is not None else None
            if not _prov or not _mdl:
                attempt_errors.append({"target": full_id, "error": "provider or model not found"})
                continue
            from server.core.key_rotator import get_key_rotator as _gkr
            _picked = await _gkr().pick_key_for_model(db, _mdl)
            if not _picked or _picked[0] is None:
                attempt_errors.append({"target": full_id, "error": f"no active key for {_prov.name}"})
                continue
            _ak = _picked[1]
            from server.core.model_catalog import create_adapter_for_provider as _caf
            combo_candidates.append(RouteResult(
                success=True, model=_mdl, provider=_prov,
                api_key=_ak, adapter=_caf(_prov.api_type), fallback_count=0,
            ))
        max_retries = len(combo_candidates)
    
    for attempt in range(max_retries + 1):
        _diag(conversation_id, "auto_candidate_start", diag_start_ts, attempt=attempt)
        if combo_candidates:
            if attempt >= len(combo_candidates):
                break
            candidate = combo_candidates[attempt]
        else:
            candidate = await ar.get_best_candidate(db, conversation_id, exclude_model_ids=tried_ids)
        _diag(conversation_id, "auto_candidate_done", diag_start_ts, attempt=attempt, success=candidate.success if candidate else None)
        last_result = candidate
        if not candidate.success:
            print(f"[CASCADE-NONSTREAM] exhausted at attempt {attempt}: {candidate.error} tried={tried_ids}", flush=True)
            attempt_errors.append({"attempt": attempt, "error": candidate.error})
            break
        if candidate.model and candidate.model.id in tried_ids:
            attempt_errors.append({"attempt": attempt, "error": "duplicate candidate, no more options"})
            break
        if candidate.model:
            tried_ids.add(candidate.model.id)
        # 上下文预检：估算输入装不进窗口的候选直接跳过（不打上游、不进冷却）
        if candidate.model and context_overflows(candidate.model, est_tokens):
            attempt_errors.append({
                "attempt": attempt,
                "model": f"{candidate.provider.name}/{candidate.model.model_id}",
                "error": f"skip: est ~{est_tokens} tokens > context window {candidate.model.context_length}",
            })
            _decision_skip(
                conversation_id,
                model_pk=candidate.model.id,
                provider=candidate.provider.name,
                model=candidate.model.model_id,
                reason="context window too small",
            )
            continue
        # 发起完整业务请求（非探测）
        upstream_request = _without_unsupported_reasoning(
            request.model_copy(update={"model": candidate.model.model_id}), candidate.model
        )
        extra_headers = candidate.provider.headers
        try:
            _attempt_started = time.time()
            _diag(conversation_id, "upstream_start", diag_start_ts, attempt=attempt, provider=candidate.provider.name, model=candidate.model.model_id, stream=False)
            result = await candidate.adapter.chat_completion(
                upstream_request,
                candidate.api_key,
                candidate.provider.base_url,
                extra_headers,
            )
            # 校验返回内容有效性（含 tool_calls）
            if isinstance(result, dict):
                choices = result.get("choices", [])
                usage = result.get("usage", {})
                has_content = any(
                    c.get("message", {}).get("content") or c.get("delta", {}).get("content")
                    or c.get("message", {}).get("tool_calls") or c.get("delta", {}).get("tool_calls")
                    or c.get("text")
                    for c in choices
                ) if choices else False
                if not choices or not has_content:
                    raise ValueError(f"empty_response: choices={len(choices)} tokens={usage.get('completion_tokens', 0)}")
            _diag(conversation_id, "upstream_done", diag_start_ts, attempt=attempt, provider=candidate.provider.name, model=candidate.model.model_id, stream=False)
            # 成功
            route = RouteResult(
                success=True,
                model=candidate.model,
                provider=candidate.provider,
                api_key=candidate.api_key,
                adapter=candidate.adapter,
                fallback_count=attempt,
            )
            # 确保返回结果中的 model 字段已设为完整标识
            if isinstance(result, dict):
                result["model"] = f"{candidate.provider.name}/{candidate.model.model_id}"
            if ar.health_checker:
                ar.health_checker.mark_success(candidate.model.id)
            _decision_attempt(
                conversation_id,
                provider=candidate.provider.name,
                model=candidate.model.model_id,
                status="success",
                attempt=attempt,
                latency_ms=int((time.time() - _attempt_started) * 1000),
            )
            return route, result, attempt_errors
        except Exception as e:
            _diag(conversation_id, "upstream_error", diag_start_ts, attempt=attempt, provider=candidate.provider.name, model=candidate.model.model_id, error=type(e).__name__)
            err_short = f"{type(e).__name__}: {str(e)[:200]}"
            attempt_errors.append({
                "attempt": attempt,
                "model": f"{candidate.provider.name}/{candidate.model.model_id}",
                "error": err_short,
            })
            _decision_attempt(
                conversation_id,
                provider=candidate.provider.name,
                model=candidate.model.model_id,
                status="failed",
                attempt=attempt,
                latency_ms=int((time.time() - _attempt_started) * 1000),
                error=err_short,
            )
            if ar.health_checker and not is_context_error(err_short):
                # 上下文超限不进冷却（漏网的预检由上游 400 兜底识别）
                ar.health_checker.mark_failure(candidate.model.id)
                ar.health_checker.mark_cooling(
                    candidate.model.id,
                    ar.config.cooling_period_seconds,
                )
            # 写一条失败日志，方便在分析页看到每次尝试（含冷却信息）
            cd_seconds = ar.config.cooling_period_seconds
            fc = (ar.health_checker._fail_count.get(candidate.model.id, 0) if ar.health_checker else 0)
            cd_actual = min(cd_seconds * (2 ** max(fc - 1, 0)), 3600) if fc > 1 else cd_seconds
            cooldown_note = f" | cooldown={cd_actual}s fail#{fc}"
            try:
                import json as _j
                from server.db import AsyncSessionLocal as _LS
                from server.models.request_log import RequestLog as _RL
                _diag(conversation_id, "fallback_log_start", diag_start_ts, attempt=attempt)
                async with _LS() as _ldb:
                    await write_log(_ldb,
                        conversation_id=conversation_id,
                        requested_model=request.model if request else "unknown",
                        routed_provider=candidate.provider.name,
                        routed_provider_id=candidate.provider.id,
                        routed_model=candidate.model.model_id,
                        status="error",
                        error_type="upstream_error",
                        error_msg=err_short + cooldown_note,
                        fallback_count=attempt,
                        **_proxy_log_fields(),
                        request_body=_j.dumps(request.model_dump(), ensure_ascii=False) if request else None,
                        response_body=_extract_error_body(e) or err_short,
                    )
                    _diag(conversation_id, "fallback_log_done", diag_start_ts, attempt=attempt)
            except Exception:
                _diag(conversation_id, "fallback_log_error", diag_start_ts, attempt=attempt)
                pass
            print(f"[CASCADE-NONSTREAM] attempt {attempt} failed, trying next (tried={tried_ids})", flush=True)
            continue
    # 全部失败
    print(f"[CASCADE-NONSTREAM] all {max_retries+1} attempts exhausted", flush=True)
    terminal_error = None
    if attempt_errors:
        terminal_error = attempt_errors[-1].get("error")
    if not terminal_error and last_result:
        terminal_error = last_result.error
    failed = RouteResult(success=False, error=terminal_error or "all candidates failed")
    return failed, {"error": failed.error, "attempts": attempt_errors}, attempt_errors


async def _write_stream_log(conversation_id, request, raw_request, status,
                           routed_provider, routed_model, error_msg, fallback_count, attempt_errors,
                           stream_body=None, prompt_tokens=0, completion_tokens=0, latency_ms=None,
                           ttft_ms=None, cache_read_tokens=0, cache_write_tokens=0, diag_start_ts=None):
    """在流式生成器内异步写请求日志。

    方案A：request_logs 作为唯一用量数据源，直接在此写入
    routed_provider_id 与 estimated_cost_usd，不再写入平行的 quota_usage 表。
    """
    try:
        _decision_ingest_attempts(conversation_id, attempt_errors)
        if routed_provider and routed_model:
            _decision_select(
                conversation_id,
                provider=routed_provider,
                model=routed_model,
                reason="completed upstream attempt",
            )
        terminal_decision = status == "success" or attempt_errors is not None or not routed_provider
        if terminal_decision:
            decision_total_ms = int((time.time() - diag_start_ts) * 1000) if diag_start_ts else latency_ms
            decision_fallback_count = int(fallback_count or 0)
            if attempt_errors:
                decision_fallback_count = max(decision_fallback_count, len(attempt_errors) - 1)
            await _decision_finish(
                conversation_id,
                status=status,
                provider=routed_provider,
                model=routed_model,
                fallback_count=decision_fallback_count,
                total_latency_ms=decision_total_ms,
                ttft_ms=ttft_ms,
                failure_reason=error_msg,
                attempts=attempt_errors,
            )
        if diag_start_ts:
            _diag(conversation_id, "stream_log_start", diag_start_ts, provider=routed_provider, model=routed_model, status=status)
        import json as _json_mod
        from sqlalchemy import select as _sa_sel
        from server.db import AsyncSessionLocal as _LogSession
        from server.models.request_log import RequestLog as _RL
        from server.models.provider import Provider as _QP
        from server.models.model import Model as _QM
        req_s = _json_mod.dumps(request.model_dump(), ensure_ascii=False) if request else None
        pt = int(prompt_tokens) if prompt_tokens else 0
        ct = int(completion_tokens) if completion_tokens else 0
        async with _LogSession() as _ldb:
            # 解析服务商/模型 id 与单价，写入成本
            _prov_id = None
            _model_id = None
            _cost = 0.0
            if routed_provider:
                prov_row = (await _ldb.execute(_sa_sel(_QP).where(_QP.name == routed_provider).limit(1))).scalar_one_or_none()
                _prov_id = prov_row.id if prov_row else None
            if _prov_id and routed_model:
                md_row = (await _ldb.execute(_sa_sel(_QM).where(_QM.provider_id == _prov_id, _QM.model_id == routed_model).limit(1))).scalar_one_or_none()
                _model_id = md_row.id if md_row else None
                if md_row is not None and (pt or ct or cache_read_tokens or cache_write_tokens):
                    _md_prices = {
                        "input_price": float(getattr(md_row, "input_price", 0) or 0),
                        "output_price": float(getattr(md_row, "output_price", 0) or 0),
                        "cache_read_input_price": getattr(md_row, "cache_read_input_price", 0) or 0,
                        "cache_write_input_price": getattr(md_row, "cache_write_input_price", 0) or 0,
                    }
                    _cost = _segmented_cost(_md_prices, pt, ct, cache_read_tokens, cache_write_tokens)
            await write_log(_ldb,
                conversation_id=conversation_id,
                requested_model=request.model if request else "unknown",
                routed_provider=routed_provider,
                routed_provider_id=_prov_id,
                routed_model=routed_model,
                status=status,
                prompt_tokens=pt,
                completion_tokens=ct,
                cache_read_tokens=cache_read_tokens or None,
                cache_write_tokens=cache_write_tokens or None,
                estimated_cost_usd=_cost,
                error_type="upstream_error" if error_msg else None,
                error_msg=(error_msg or ""),
                fallback_count=fallback_count or 0,
                latency_ms=latency_ms,
                ttft_ms=ttft_ms,
                user_ip=raw_request.client.host if raw_request.client else None,
                **_proxy_log_fields(),
                request_body=req_s,
                response_body=stream_body or (_json_mod.dumps(attempt_errors, ensure_ascii=False) if attempt_errors else "[stream]"),
            )
            if diag_start_ts:
                _diag(conversation_id, "stream_log_done", diag_start_ts, provider=routed_provider, model=routed_model, status=status)
    except Exception:
        if diag_start_ts:
            _diag(conversation_id, "stream_log_error", diag_start_ts, provider=routed_provider, model=routed_model, status=status)
        pass

def get_auto_router() -> AutoRouter:
    global _auto_router
    if _auto_router is None:
        from server.core.auto_router import AutoRouter
        from server.main import get_health_checker
        # 复用 main 启动初始化的单例，避免另建一个 HealthChecker 实例
        # 导致 mark_cooling 写入的冷却状态与「冷却总览」读取的不是同一个对象。
        _auto_router = AutoRouter(
            model_catalog=ModelCatalog(),
            health_checker=get_health_checker(),
            key_manager=KeyManager(get_crypto_service()),
        )
    return _auto_router


# 思考强度后缀档位（与 codex_responses adapter 的 _EFFORT_LEVELS 约定保持一致，另加 minimal）
_EFFORT_SUFFIX_LEVELS = ("xhigh", "none", "minimal", "high", "medium", "low")


def _split_effort_suffix(model_name: str):
    """模型名尾部思考强度后缀（如 combo:xxx-high）→ (剥后缀基名, 强度档位)。

    无后缀或名字本身就只是后缀时返回 (原名, None)。
    """
    if not model_name:
        return model_name, None
    for level in _EFFORT_SUFFIX_LEVELS:
        suffix = f"-{level}"
        if model_name.endswith(suffix) and len(model_name) > len(suffix):
            return model_name[: -len(suffix)], level
    return model_name, None


def _reasoning_effort_supported(model) -> bool:
    """Explicit admin capability wins; otherwise use a conservative model-name heuristic."""
    explicit = getattr(model, "supports_reasoning_effort", None)
    if explicit is not None:
        return bool(explicit)
    from server.core.model_capabilities import infer_reasoning_effort_support
    return bool(infer_reasoning_effort_support("", getattr(model, "model_id", "")))


def _without_unsupported_reasoning(request, model):
    """Some strict OpenAI-compatible upstreams reject reasoning_effort outright."""
    if _reasoning_effort_supported(model):
        return request
    return request.model_copy(update={"reasoning_effort": None, "reasoning": None})


async def _model_name_resolves(db: AsyncSession, name: str) -> bool:
    """名称能否解析为启用中的组合或启用模型（provider/model_id 或裸 model_id）。

    用于后缀剥离的安全预检：原名能解析就不剥，避免误伤恰好以 -high 结尾的真实模型名。
    """
    try:
        from server.core.combo_router import is_combo_request, find_combo_by_name
        is_combo, combo_name = is_combo_request(name)
        if is_combo:
            return await find_combo_by_name(db, combo_name) is not None
        if "/" in name:
            prov_name, m_id = name.split("/", 1)
            row = (await db.execute(
                select(Model.id).join(Provider, Model.provider_id == Provider.id)
                .where(Provider.name == prov_name, Model.model_id == m_id, Model.enabled == True)
                .limit(1)
            )).first()
            return row is not None
        row = (await db.execute(
            select(Model.id).where(Model.model_id == name, Model.enabled == True).limit(1)
        )).first()
        return row is not None
    except Exception:
        return False


@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OpenAI 兼容聊天补全端点"""
    _diag_start = time.time()
    import uuid
    conversation_id = str(uuid.uuid4())
    _diag(conversation_id, "request_enter", _diag_start, model=getattr(request, "model", None), stream=getattr(request, "stream", None), client=raw_request.client.host if raw_request.client else None)
    try:
        _diag(conversation_id, "auth_start", _diag_start)
        verify_aigate_api_key(raw_request)
        _diag(conversation_id, "auth_done", _diag_start)
    except HTTPException as auth_err:
        # 认证失败也写日志，方便排查
        import json as _json_mod
        try:
            from server.db import AsyncSessionLocal as _LogSession
            from server.models.request_log import RequestLog as _RL
            async with _LogSession() as _ldb:
                await write_log(_ldb,
                    conversation_id=str(uuid.uuid4()),
                    requested_model=request.model if request else "unknown",
                    status="error",
                    error_type="auth_failed",
                    error_msg=str(auth_err.detail),
                    **_proxy_log_fields(),
                    user_ip=raw_request.client.host if raw_request.client else None,
                    request_body=_json_mod.dumps(request.model_dump(), ensure_ascii=False) if request else None,
                )
        except Exception:
            pass
        raise auth_err
    _diag(conversation_id, "router_get_start", _diag_start)
    ar = get_auto_router()
    _diag(conversation_id, "router_get_done", _diag_start)
    request = _preprocess_request(request)
    _diag(conversation_id, "preprocess_done", _diag_start)
    # ─── 思考强度后缀：combo:xxx-high / 模型-high ───
    # 仅当原名解析不到、剥后缀后能解析时才剥离（避免误伤以 -high 结尾的真实模型名）；
    # 显式传入的 reasoning_effort 优先于后缀档位
    _base_name, _suffix_effort = _split_effort_suffix(request.model)
    if _suffix_effort and not await _model_name_resolves(db, request.model):
        if await _model_name_resolves(db, _base_name):
            _upd = {"model": _base_name}
            if not request.reasoning_effort:
                _upd["reasoning_effort"] = _suffix_effort
            request = request.model_copy(update=_upd)
            _diag(conversation_id, "effort_suffix_applied", _diag_start,
                  base=_base_name, effort=_upd.get("reasoning_effort"))
    # 上下文窗口预检的请求体量估算（跳过装不下的候选，避免 400 + 误冷却）
    est_req_tokens = estimate_request_tokens(request)
    _decision_begin(
        conversation_id,
        request.model,
        "auto" if request.is_auto else "direct",
        stream=bool(request.stream),
        estimated_tokens=est_req_tokens,
        strategy="ranked-fallback" if request.is_auto else "direct",
    )
    http_status_code = 200
    _send_time = time.time()  # 提前设，级联路径也需要
    route_result: Optional[RouteResult] = None
    made_by_cascade = False  # 是否已由级联回退发起过实际请求
    is_auto = request.is_auto

    # ─── 直接路由 ───
    if not is_auto:
        _diag(conversation_id, "direct_route_start", _diag_start, model=request.model)
        # v3.0: combo 路由 — 形如 "combo:my-fast"
        from server.core.combo_router import is_combo_request, find_combo_by_name, resolve_combo_targets, pick_next_index
        is_combo, combo_name = is_combo_request(request.model)
        if is_combo:
            _diag(conversation_id, "combo_route_start", _diag_start, combo=combo_name)
            combo = await find_combo_by_name(db, combo_name)
            if not combo:
                await _decision_finish(conversation_id, status="error", failure_reason=f"Combo '{combo_name}' not found")
                return JSONResponse(status_code=404, content={"error": f"Combo '{combo_name}' not found"})
            targets = await resolve_combo_targets(db, combo)
            combo_strategy = getattr(combo, "strategy", None) or "fallback"
            _decision_configure(
                conversation_id,
                route_type="combo",
                strategy=combo_strategy,
            )
            if not targets:
                try:
                    await _write_stream_log(
                        conversation_id, request, raw_request, "error",
                        None, None, f"Combo '{combo_name}' no available targets", 0, None,
                        diag_start_ts=_diag_start,
                    )
                except Exception:
                    pass
                await _decision_finish(
                    conversation_id,
                    status="error",
                    failure_reason=f"Combo '{combo_name}' no available targets",
                )
                return JSONResponse(status_code=503, content={"error": f"Combo '{combo_name}' no available targets"})
            _diag(conversation_id, "combo_targets", _diag_start, count=len(targets))
            _combo_start = pick_next_index(combo.id, len(targets), combo_strategy)
            ordered_targets = targets[_combo_start:] + targets[:_combo_start]
            combo_full_ids = [t["full_id"] for t in ordered_targets]
            _decision_candidates(conversation_id, [
                {
                    "rank": index,
                    "provider": full_id.split("/", 1)[0] if "/" in full_id else None,
                    "model": full_id.split("/", 1)[1] if "/" in full_id else full_id,
                    "eligible": True,
                }
                for index, full_id in enumerate(combo_full_ids, start=1)
            ])
            # ─── 流式 combo：统一级联回退（带冷却），与 auto 流式行为一致 ───
            if request.stream:
                _diag(conversation_id, "combo_stream_start", _diag_start, count=len(combo_full_ids))

                async def _combo_cascade_stream():
                    from server.db import AsyncSessionLocal as _CS
                    cdb = _CS()
                    tried_sids = set()
                    stream_errs = []
                    max_r = max(1, ar.config.max_fallbacks, len(combo_full_ids))
                    yield b": keepalive\n\n"
                    try:
                        for st_attempt in range(max_r):
                            if st_attempt >= len(combo_full_ids):
                                break
                            full_id = combo_full_ids[st_attempt]
                            prov_name, m_id = full_id.split("/", 1) if "/" in full_id else (None, full_id)
                            from server.models.provider import Provider as _P
                            from server.models.model import Model as _M
                            from sqlalchemy import select as _sel
                            p_r = await cdb.execute(_sel(_P).where(_P.name == prov_name).limit(1))
                            _prov = p_r.scalar_one_or_none()
                            # v4.0: 服务商被禁用 → 跳过该候选（不删除组合配置）
                            if _prov is not None and not getattr(_prov, "enabled", True):
                                stream_errs.append({"attempt": st_attempt, "error": f"provider disabled: {full_id}"})
                                _decision_skip(conversation_id, provider=prov_name, model=m_id, reason="provider disabled")
                                continue
                            m_r = await cdb.execute(_sel(_M).where(_M.provider_id == _prov.id, _M.model_id == m_id, _M.enabled == True).limit(1)) if _prov else None
                            _mdl = m_r.scalar_one_or_none() if m_r is not None else None
                            if not _prov or not _mdl:
                                stream_errs.append({"attempt": st_attempt, "error": f"combo target {full_id} not found"})
                                _decision_skip(conversation_id, provider=prov_name, model=m_id, reason="provider or model not found")
                                continue
                            # 上下文预检：装不下的候选直接跳过
                            if context_overflows(_mdl, est_req_tokens):
                                stream_errs.append({"attempt": st_attempt, "error": f"skip (context window {_mdl.context_length} < est ~{est_req_tokens} tokens): {full_id}"})
                                _decision_skip(conversation_id, model_pk=_mdl.id, provider=_prov.name, model=_mdl.model_id, reason="context window too small")
                                continue
                            from server.core.key_rotator import get_key_rotator as _gkr
                            _picked = await _gkr().pick_key_for_model(cdb, _mdl)
                            if not _picked or _picked[0] is None:
                                stream_errs.append({"attempt": st_attempt, "error": f"no key for {_prov.name}"})
                                _decision_skip(conversation_id, model_pk=_mdl.id, provider=_prov.name, model=_mdl.model_id, reason="no active API key")
                                continue
                            _ak = _picked[1]
                            from server.core.model_catalog import create_adapter_for_provider as _caf
                            _adapter = _caf(_prov.api_type)
                            # 跳过处于冷却（被惩罚）中的 target，避免反复打到坏模型
                            if ar.health_checker and ar.health_checker.is_cooling(_mdl.id):
                                stream_errs.append({"attempt": st_attempt, "error": f"skipped (cooling) {full_id}"})
                                _decision_skip(conversation_id, model_pk=_mdl.id, provider=_prov.name, model=_mdl.model_id, reason="model is cooling down")
                                continue
                            mid_full = f"{_prov.name}/{_mdl.model_id}"
                            _decision_select(conversation_id, provider=_prov.name, model=_mdl.model_id, model_pk=_mdl.id, reason="next combo target")
                            up_req = _without_unsupported_reasoning(
                                request.model_copy(update={"model": _mdl.model_id}), _mdl
                            )
                            _start = time.time()
                            try:
                                _diag(conversation_id, "upstream_stream_start", _diag_start, attempt=st_attempt, provider=_prov.name, model=_mdl.model_id)
                                _csc = 0
                                _cbuf = []
                                _cu = {}
                                _cb_ttft_ms = None
                                _cb_attempt_ttft_ms = None
                                _fb_eh = _merge_oauth_headers(_prov, _prov.headers)
                                _fbmov = getattr(_mdl, "request_overrides", None) or {}
                                if isinstance(_fbmov, dict) and _fbmov.get("headers"):
                                    _fb_eh = {**(_fb_eh or {}), **_fbmov["headers"]}
                                async for ck in _adapter.stream_chat_completion(up_req, _ak, _prov.base_url, _fb_eh):
                                    # 首字延迟：首个 chunk 距请求开始的时间
                                    if _cb_ttft_ms is None:
                                        _cb_ttft_ms = int((time.time() - _send_time) * 1000)
                                        _cb_attempt_ttft_ms = int((time.time() - _start) * 1000)
                                    if isinstance(ck, dict) and "error" in ck:
                                        raise RuntimeError(f"upstream_stream_error: {ck.get('error')}")
                                    _csc += 1
                                    _cbuf.append(ck)
                                    u = ck.get("usage", {}) if isinstance(ck, dict) else {}
                                    if u:
                                        _cu = u
                                    yield _format_sse_chunk(ck, mid_full)
                                _diag(conversation_id, "upstream_stream_done", _diag_start, attempt=st_attempt, provider=_prov.name, model=_mdl.model_id, chunks=_csc)
                                # ── 空输出判定（combo 专用）：HTTP 成功但零内容 → 视为候选失败，继续 fallback ──
                                # 只要整个流累计的 delta.content 全为空（无论上游发了多少空 chunk），
                                # 就判为空输出。空 content 的 chunk 客户端渲染不出任何东西，回退对客户端无感、安全。
                                if _stream_content_is_empty(_cbuf):
                                    err_s = "empty_stream_output: 上游返回成功但无内容"
                                    stream_errs.append({"attempt": st_attempt, "model": mid_full, "error": err_s})
                                    _decision_attempt(conversation_id, provider=_prov.name, model=_mdl.model_id, status="failed", attempt=st_attempt, latency_ms=int((time.time() - _start) * 1000), ttft_ms=_cb_attempt_ttft_ms, error=err_s)
                                    if ar.health_checker:
                                        ar.health_checker.mark_failure(_mdl.id)
                                        ar.health_checker.mark_cooling(_mdl.id, ar.config.cooling_period_seconds)
                                    print(f"[组合流式] 第{st_attempt + 1}次尝试 服务商={_prov.name} 模型={_mdl.model_id} 空输出，正在尝试下一个候选", flush=True)
                                    import json as _cj
                                    await _write_stream_log(conversation_id, request, raw_request, "error",
                                        _prov.name, _mdl.model_id, err_s, st_attempt, None,
                                        stream_body=_cj.dumps(_cbuf, ensure_ascii=False) if _cbuf else None, diag_start_ts=_diag_start)
                                    continue
                                # 注意：成功日志必须在 yield [DONE] 之前写。
                                # 客户端收到 [DONE] 会立即断开连接，生成器被取消，
                                # 若日志写在 DONE 之后，await commit 会被取消导致成功日志丢失。
                                import json as _cj
                                _combo_latency = int((time.time() - _start) * 1000)
                                _combo_body = _cj.dumps(_cbuf, ensure_ascii=False) if _cbuf else None
                                _pt = int(_cu.get("prompt_tokens") or _cu.get("input_tokens") or 0)
                                _ct = int(_cu.get("completion_tokens") or _cu.get("output_tokens") or 0)
                                _crd, _cwt = _extract_cache_tokens(_cu)
                                _pt, _ct = _sanitize_token_counts(request, _pt, _ct, _output_text_from_chunks(_cbuf))
                                _decision_attempt(conversation_id, provider=_prov.name, model=_mdl.model_id, status="success", attempt=st_attempt, latency_ms=_combo_latency, ttft_ms=_cb_attempt_ttft_ms)
                                await _write_stream_log(conversation_id, request, raw_request, "success",
                                    _prov.name, _mdl.model_id, None, st_attempt, None,
                                    stream_body=_combo_body, prompt_tokens=_pt, completion_tokens=_ct,
                                    cache_read_tokens=_crd, cache_write_tokens=_cwt,
                                    latency_ms=_combo_latency, ttft_ms=_cb_ttft_ms, diag_start_ts=_diag_start)
                                if ar.health_checker:
                                    ar.health_checker.mark_success(_mdl.id)
                                yield b"data: [DONE]\n\n"
                                return
                            except Exception as se:
                                err_s = f"{type(se).__name__}: {str(se)[:200]}"
                                stream_errs.append({"attempt": st_attempt, "model": mid_full, "error": err_s})
                                _decision_attempt(conversation_id, provider=_prov.name, model=_mdl.model_id, status="failed", attempt=st_attempt, latency_ms=int((time.time() - _start) * 1000), ttft_ms=_cb_attempt_ttft_ms, error=err_s)
                                # 失败惩罚：与 auto 路由一致 —— 计入失败并进入冷却（指数退避 30×2^n 秒）
                                # 上下文超限 / 上游内容校验失败 均不是模型自身故障，不进冷却
                                if ar.health_checker and not is_context_error(err_s) and not _is_stream_content_validation_error(err_s):
                                    ar.health_checker.mark_failure(_mdl.id)
                                    ar.health_checker.mark_cooling(_mdl.id, ar.config.cooling_period_seconds)
                                raw_err = _extract_error_body(se) or err_s
                                await _write_stream_log(conversation_id, request, raw_request, "error",
                                    _prov.name, _mdl.model_id, err_s, st_attempt, None,
                                    stream_body=raw_err, diag_start_ts=_diag_start)
                                print(f"[组合流式] 第{st_attempt + 1}次尝试 服务商={_prov.name} 模型={_mdl.model_id} 失败：{err_s}，正在尝试下一个候选", flush=True)
                                continue
                        # 全部候选失败
                        yield _format_sse_chunk({"error": "combo all targets failed", "attempts": stream_errs}, "unknown")
                        yield b"data: [DONE]\n\n"
                        await _write_stream_log(conversation_id, request, raw_request, "error",
                            None, None, "combo all targets failed", 0, stream_errs, diag_start_ts=_diag_start)
                    finally:
                        await cdb.close()
                        try:
                            await asyncio.shield(_decision_finish(
                                conversation_id,
                                status="error",
                                fallback_count=max(0, len(stream_errs) - 1),
                                failure_reason="stream ended before a terminal routing result",
                                attempts=stream_errs,
                            ))
                        except Exception:
                            pass

                return StreamingResponse(_combo_cascade_stream(), media_type="text/event-stream")
            # ─── 非流式 combo：循环尝试（含冷却），不再走 is_auto 级联路径 ───
            from server.core.model_catalog import create_adapter_for_provider as _cafp
            combo_attempts = []
            last_error = None
            for t_idx, t in enumerate(ordered_targets):
                full_id = t["full_id"]
                prov_name, m_id = full_id.split("/", 1) if "/" in full_id else (None, full_id)
                # 查找 provider + model
                provider = (await db.execute(
                    select(Provider).where(Provider.name == prov_name).limit(1)
                )).scalar_one_or_none()
                model = (await db.execute(
                    select(Model).where(Model.provider_id == provider.id, Model.model_id == m_id, Model.enabled == True).limit(1)
                )).scalar_one_or_none() if provider else None
                if not provider or not model:
                    combo_attempts.append({"target": full_id, "error": "provider or model not found"})
                    _decision_skip(conversation_id, provider=prov_name, model=m_id, reason="provider or model not found")
                    continue
                # 上下文预检：装不下的候选直接跳过
                if context_overflows(model, est_req_tokens):
                    combo_attempts.append({"target": full_id, "error": f"skip: est ~{est_req_tokens} tokens > context window {model.context_length}"})
                    _decision_skip(conversation_id, model_pk=model.id, provider=provider.name, model=model.model_id, reason="context window too small")
                    continue
                # 跳过处于冷却（被惩罚）中的 target，让后续健康候选顶上
                if ar.health_checker and ar.health_checker.is_cooling(model.id):
                    combo_attempts.append({"target": full_id, "error": "skipped (cooling)"})
                    _decision_skip(conversation_id, model_pk=model.id, provider=provider.name, model=model.model_id, reason="model is cooling down")
                    continue
                # 取 key（v3.5：按模型归属集合选）
                try:
                    from server.core.key_rotator import get_key_rotator
                    _picked = await get_key_rotator().pick_key_for_model(db, model)
                except Exception:
                    _picked = None
                if not _picked or _picked[0] is None:
                    combo_attempts.append({"target": full_id, "error": f"no active key for {provider.name}"})
                    _decision_skip(conversation_id, model_pk=model.id, provider=provider.name, model=model.model_id, reason="no active API key")
                    continue
                api_key = _picked[1]
                adapter = _cafp(provider.api_type)
                upstream_req = _without_unsupported_reasoning(
                    request.model_copy(update={"model": model.model_id}), model
                )
                extra_hdr = _merge_oauth_headers(provider, provider.headers if provider.headers else None)
                model_overrides = getattr(model, "request_overrides", None) or {}
                if isinstance(model_overrides, dict):
                    ov_headers = model_overrides.get("headers") or {}
                    ov_body = model_overrides.get("body_patch") or {}
                    ov_model = model_overrides.get("model_alias")
                    if ov_model:
                        upstream_req = upstream_req.model_copy(update={"model": ov_model})
                    if ov_headers and isinstance(ov_headers, dict):
                        extra_hdr = {**(extra_hdr or {}), **ov_headers}
                    if ov_body and isinstance(ov_body, dict):
                        try:
                            upstream_req = upstream_req.model_copy(update=ov_body)
                        except Exception:
                            pass
                try:
                    _combo_send_time = time.time()
                    _decision_select(conversation_id, provider=provider.name, model=model.model_id, model_pk=model.id, reason="next combo target")
                    result = await adapter.chat_completion(
                        upstream_req, api_key, provider.base_url, extra_hdr
                    )
                    if isinstance(result, dict):
                        result["model"] = f"{provider.name}/{model.model_id}"
                    # ── 空输出判定：HTTP 成功但无 content/无 tool_calls/无 completion token
                    #    → 视为候选失败，继续 fallback 到下一个候选（combo 专用，不影响直连）──
                    if _openai_completion_is_empty(result):
                        err_str = "empty_output: upstream 返回 200 但无内容/tool_calls/usage"
                        combo_attempts.append({"target": full_id, "error": err_str})
                        last_error = err_str
                        _decision_attempt(conversation_id, provider=provider.name, model=model.model_id, status="failed", attempt=t_idx, latency_ms=int((time.time() - _combo_send_time) * 1000), error=err_str)
                        if ar.health_checker:
                            ar.health_checker.mark_failure(model.id)
                            ar.health_checker.mark_cooling(model.id, ar.config.cooling_period_seconds)
                        print(f"[组合路由] 目标 {full_id} 空输出，判定为失败，正在尝试下一个候选", flush=True)
                        continue
                    if ar.health_checker:
                        ar.health_checker.mark_success(model.id)
                    _decision_attempt(conversation_id, provider=provider.name, model=model.model_id, status="success", attempt=t_idx, latency_ms=int((time.time() - _combo_send_time) * 1000))
                    # combo 分支独立于集中日志（964 行），需自行写请求日志
                    try:
                        import json as _j
                        _usage = result.get("usage", {}) if isinstance(result, dict) else {}
                        _pt = int(_usage.get("prompt_tokens") or _usage.get("input_tokens") or 0)
                        _ct = int(_usage.get("completion_tokens") or _usage.get("output_tokens") or 0)
                        _crd, _cwt = _extract_cache_tokens(_usage)
                        _pt, _ct = _sanitize_token_counts(request, _pt, _ct, _output_text_from_result(result))
                        await _write_stream_log(
                            conversation_id, request, raw_request, "success",
                            provider.name, model.model_id, None, 0, None,
                            stream_body=_j.dumps(result, ensure_ascii=False),
                            prompt_tokens=_pt,
                            completion_tokens=_ct,
                            cache_read_tokens=_crd, cache_write_tokens=_cwt,
                            latency_ms=int((time.time() - _combo_send_time) * 1000),
                            diag_start_ts=_diag_start,
                        )
                    except Exception:
                        pass
                    _diag(conversation_id, "combo_hit", _diag_start, target=full_id)
                    return JSONResponse(content=result)
                except Exception as e:
                    err_str = f"{type(e).__name__}: {str(e)[:200]}"
                    combo_attempts.append({"target": full_id, "error": err_str})
                    last_error = err_str
                    _decision_attempt(conversation_id, provider=provider.name, model=model.model_id, status="failed", attempt=t_idx, latency_ms=int((time.time() - _combo_send_time) * 1000), error=err_str)
                    # 失败惩罚：与 auto 路由一致 —— 计入失败并进入冷却（指数退避 30×2^n 秒）
                    if ar.health_checker and model and not is_context_error(err_str):
                        # 上下文超限不进冷却
                        ar.health_checker.mark_failure(model.id)
                        ar.health_checker.mark_cooling(model.id, ar.config.cooling_period_seconds)
                    print(f"[组合路由] 目标 {full_id} 失败：{err_str}，正在尝试下一个候选", flush=True)
                    continue
            # 全部 target 失败
            _diag(conversation_id, "combo_all_failed", _diag_start, attempts=combo_attempts)
            try:
                import json as _j
                await _write_stream_log(
                    conversation_id, request, raw_request, "error",
                    None, None, f"Combo '{combo_name}' all targets failed", 0, combo_attempts,
                    stream_body=_j.dumps({"attempts": combo_attempts}, ensure_ascii=False),
                    diag_start_ts=_diag_start,
                )
            except Exception:
                pass
            return JSONResponse(
                status_code=503,
                content={"error": f"Combo '{combo_name}' all targets failed", "attempts": combo_attempts},
            )
        else:
            # 直接路由：解析 model + provider
            if "/" in request.model:
                provider_name, model_id = request.model.split("/", 1)
                model = await ar.model_catalog.get_by_full_id(db, provider_name, model_id)
            else:
                mc = ModelCatalog()
                models = await mc.list_models(db, enabled_only=True)
                model = next((m for m in models if m.model_id == request.model), None)
            if not model:
                _diag(conversation_id, "direct_route_not_found", _diag_start, model=request.model)
                await _decision_finish(conversation_id, status="error", failure_reason=f"Model {request.model} not found")
                return JSONResponse(status_code=404, content={"error": f"Model {request.model} not found"})
            _diag(conversation_id, "direct_model_done", _diag_start, routed_model=model.model_id)
            provider = await db.get(Provider, model.provider_id)
            # v4.0: 服务商被禁用 → 直连同样不可用
            if provider is None or not getattr(provider, "enabled", True):
                await _decision_finish(conversation_id, status="error", failure_reason=f"Provider for model {request.model} is disabled")
                return JSONResponse(status_code=404, content={"error": f"Provider for model {request.model} is disabled"})
            # Free Tier / OAuth providers — key 可空（无需密钥直发 / OAuth token 走 OAuth client）
            api_key = None
            _kid = None
            if getattr(provider, "credential_type", "api_key") in ("free_tier", "oauth") or provider.api_type == "atomcode":
                _diag(conversation_id, "direct_key_skipped_" + provider.credential_type, _diag_start, provider=provider.name)
                if provider.credential_type == "oauth":
                    # 通过 OAuth client pick_access_token（自动刷新）
                    from server.core.oauth_client import get_oauth_client
                    from server.core.oauth_registry import get_oauth_provider as _get_oauth_p
                    # v3.1：优先用 provider.oauth_code 字段，显式指向 OAuthRegistry code
                    # 兼容老数据：若 oauth_code 为空，回退尝试 provider.name
                    oauth_code = getattr(provider, "oauth_code", None) or provider.name
                    oauth_p = _get_oauth_p(oauth_code)
                    if oauth_p:
                        api_key = await get_oauth_client().pick_access_token(oauth_code, db)
                    if not api_key:
                        await _decision_finish(conversation_id, status="error", failure_reason=f"OAuth provider '{oauth_code}' not connected")
                        return JSONResponse(status_code=503, content={"error": f"OAuth provider '{oauth_code}' not connected (set provider.oauth_code or import token)"})
                else:
                    api_key = ""   # free_tier：decoder 时 adapter 用空字符串鉴权头
            else:
                # v3.5 标准路径：按模型归属 key 集合选（多则轮询、单则用一、无归属 fallback 第一把）
                try:
                    from server.core.key_rotator import get_key_rotator
                    picked = await get_key_rotator().pick_key_for_model(db, model)
                except Exception:
                    picked = None
                if picked and picked[0] is not None:
                    _kid, api_key = picked
                    _diag(conversation_id, "direct_key_done", _diag_start, provider=provider.name, key_id=_kid)
                else:
                    # 兜底：provider 第一把 active key
                    result = await db.execute(
                        sa_select(ApiKey).where(ApiKey.provider_id == provider.id, ApiKey.is_active == True).limit(1)
                    )
                    key = result.scalar_one_or_none()
                    if not key:
                        _diag(conversation_id, "direct_key_missing", _diag_start, provider=provider.name)
                        await _decision_finish(conversation_id, status="error", failure_reason=f"No active API key for provider {provider.name}")
                        return JSONResponse(status_code=503, content={"error": f"No active API key for provider {provider.name}"})
                    _diag(conversation_id, "direct_key_done", _diag_start, provider=provider.name)
                    _kid, api_key = key.id, get_crypto_service().decrypt(key.key_encrypted)
            from server.core.model_catalog import create_adapter_for_provider
            adapter = create_adapter_for_provider(provider.api_type)
            route_result = RouteResult(
                success=True, model=model, provider=provider, api_key=api_key, key_id=_kid,
                adapter=adapter, fallback_count=0
            )
            _decision_candidates(conversation_id, [{
                "rank": 1,
                "model_pk": model.id,
                "provider": provider.name,
                "model": model.model_id,
                "eligible": True,
                "selected": True,
                "selection_reason": "explicit model request",
            }])
            _decision_select(conversation_id, provider=provider.name, model=model.model_id, model_pk=model.id, reason="explicit model request")
            _diag(conversation_id, "direct_route_done", _diag_start, provider=provider.name, model=model.model_id)

            # ─── v3.2 free_tier 专用 provider (opencode / mimo-free) 直走 free executor ───
            # 这些 9Router 来源的 provider 有 bootstrap / 自定义鉴权协议，不能走 OpenAICompatAdapter（之前 api_key="" 会报 LocalProtocolError）
            if provider.credential_type == "free_tier":
                from server.core.free_providers import get_free_executor, resolve_free_code, _FREE_PROVIDERS_META
                free_code = resolve_free_code(provider.name, getattr(provider, "oauth_code", None))
                free_exec = get_free_executor(free_code) if free_code else None
                if free_exec:
                    _diag(conversation_id, "free_provider_executor_hit", _diag_start, code=free_code)
                    from server.core.proxy_pool import FORCE_PROXY as _FORCE_PROXY
                    # free executor 直接用裸 model_id 调上游，不带 provider 前缀
                    free_req = _without_unsupported_reasoning(
                        request.model_copy(update={"model": model.model_id}), model
                    )
                    if request.stream:
                        async def _free_stream():
                            _free_stream_start = time.time()
                            _free_ttft_ms = None
                            _free_err = None
                            _proxy_token = _FORCE_PROXY.set(bool(getattr(provider, "proxy_enabled", False)))
                            try:
                                async for ck in free_exec.execute_stream(free_req):
                                    if _free_ttft_ms is None:
                                        _free_ttft_ms = int((time.time() - _free_stream_start) * 1000)
                                    yield _format_sse_chunk(ck, model.full_id)
                                yield b"data: [DONE]\n\n"
                            except Exception as e:
                                _free_err = f"{type(e).__name__}: {str(e)[:200]}"
                                err_data = {"error": f"free_provider_stream_failed: {e}"}
                                yield _format_sse_chunk(err_data, model.full_id)
                                yield b"data: [DONE]\n\n"
                            finally:
                                _FORCE_PROXY.reset(_proxy_token)
                                try:
                                    from server.db import AsyncSessionLocal as _AS
                                    async with _AS() as sdb:
                                        _decision_attempt(
                                            conversation_id,
                                            provider=provider.name,
                                            model=model.model_id,
                                            status="failed" if _free_err else "success",
                                            attempt=0,
                                            latency_ms=int((time.time() - _free_stream_start) * 1000),
                                            ttft_ms=_free_ttft_ms,
                                            error=_free_err,
                                        )
                                        await _write_stream_log(conversation_id, request, raw_request,
                                            "error" if _free_err else "success", provider.name, model.model_id, _free_err, 0,
                                            [] if _free_err else None,
                                            latency_ms=int((time.time() - _free_stream_start) * 1000),
                                            ttft_ms=_free_ttft_ms)
                                except Exception:
                                    pass
                        return StreamingResponse(_free_stream(), media_type="text/event-stream")
                    else:
                        try:
                            _free_started = time.time()
                            _proxy_token = _FORCE_PROXY.set(bool(getattr(provider, "proxy_enabled", False)))
                            data = await free_exec.execute_non_stream(free_req)
                            _free_latency = int((time.time() - _free_started) * 1000)
                            _decision_attempt(conversation_id, provider=provider.name, model=model.model_id, status="success", attempt=0, latency_ms=_free_latency)
                            await _decision_finish(conversation_id, status="success", provider=provider.name, model=model.model_id, fallback_count=0, total_latency_ms=_free_latency)
                            return JSONResponse(content=data)
                        except Exception as e:
                            _free_latency = int((time.time() - _free_started) * 1000)
                            _free_err = f"{type(e).__name__}: {str(e)[:200]}"
                            _decision_attempt(conversation_id, provider=provider.name, model=model.model_id, status="failed", attempt=0, latency_ms=_free_latency, error=_free_err)
                            await _decision_finish(conversation_id, status="error", provider=provider.name, model=model.model_id, fallback_count=0, total_latency_ms=_free_latency, failure_reason=_free_err)
                            return JSONResponse(status_code=502, content={"error": f"free_provider_failed: {e}"})
                        finally:
                            _FORCE_PROXY.reset(_proxy_token)
                else:
                    # free_tier provider 找不到对应 executor — 不回退 adapter（避免 URL 被错误二次追加）
                    _diag(conversation_id, "free_provider_executor_miss", _diag_start,
                          name=provider.name, oauth_code=getattr(provider, "oauth_code", None))
                    known_codes = ", ".join(f"'{c}' ({_FREE_PROVIDERS_META[c]['name']})" for c in _FREE_PROVIDERS_META)
                    await _decision_finish(conversation_id, status="error", failure_reason=f"free_tier provider '{provider.name}' has no matching executor")
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": f"free_tier provider '{provider.name}' has no matching executor. "
                                     f"请编辑该服务商，将 oauth_code 填为 {known_codes} 之一。"
                        },
                    )

    # ─── auto 路由 ───
    elif is_auto and request.stream:
        _diag(conversation_id, "auto_stream_response_created", _diag_start)
        # 流式级联回退：直接拉流，连接失败自动换下一个候选
        async def cascade_stream():
            _diag(conversation_id, "auto_stream_generator_start", _diag_start)
            from server.db import AsyncSessionLocal as _CS
            cascade_db = _CS()
            max_r = max(1, ar.config.max_fallbacks)
            first_chunk_timeout = max(5, int(getattr(ar.config, "stream_first_chunk_timeout_seconds", 20)))
            first_response_budget = max(first_chunk_timeout, int(getattr(ar.config, "stream_first_response_budget_seconds", 75)))
            first_response_deadline = time.monotonic() + first_response_budget
            tried_sids = set()
            stream_errs = []
            # v3.0: combo 路由会用自定义候选池替代 ar.get_best_candidate
            import server.api.v1_router as _vr_mod
            combo_pool = getattr(_vr_mod, '_combo_targets_map', {}).get(conversation_id, [])
            # 立即发一个 SSE 注释块，防客户端超时
            yield b": keepalive\n\n"
            try:
                for st_attempt in range(max_r + 1):
                    remaining_first_response = first_response_deadline - time.monotonic()
                    if remaining_first_response <= 0:
                        stream_errs.append({
                            "attempt": st_attempt,
                            "error": f"first response budget exceeded ({first_response_budget}s)"
                        })
                        break
                    _diag(conversation_id, "auto_stream_candidate_start", _diag_start, attempt=st_attempt)
                    if combo_pool:
                        # combo 路径：按池子顺序取下一个未试目标
                        if st_attempt >= len(combo_pool):
                            _diag(conversation_id, "combo_stream_exhausted", _diag_start, attempted=st_attempt)
                            err_data = {"error": "combo pool exhausted", "attempts": stream_errs}
                            yield _format_sse_chunk(err_data, "unknown")
                            yield b"data: [DONE]\n\n"
                            await _write_stream_log(conversation_id, request, raw_request, "error",
                                None, None, "combo pool exhausted", st_attempt, stream_errs,
                                diag_start_ts=_diag_start)
                            return
                        full_id = combo_pool[st_attempt]
                        # 解析 provider/model
                        prov_name, m_id = full_id.split("/", 1) if "/" in full_id else (None, full_id)
                        from server.models.provider import Provider as _P
                        from sqlalchemy import select as _sel
                        p_r = await cascade_db.execute(_sel(_P).where(_P.name == prov_name).limit(1))
                        _prov = p_r.scalar_one_or_none()
                        from server.models.model import Model as _M
                        m_r = await cascade_db.execute(_sel(_M).where(_M.provider_id == _prov.id, _M.model_id == m_id, _M.enabled == True).limit(1)) if _prov else None
                        _mdl = m_r.scalar_one_or_none() if m_r is not None else None
                        if not _prov or not _mdl:
                            stream_errs.append({"attempt": st_attempt, "error": f"combo target {full_id} not found"})
                            continue
                        from server.core.model_catalog import create_adapter_for_provider as _caf
                        from server.core.key_rotator import get_key_rotator as _gkr
                        _picked = await _gkr().pick_key_for_model(cascade_db, _mdl)
                        if not _picked or _picked[0] is None:
                            stream_errs.append({"attempt": st_attempt, "error": f"no key for {_prov.name}"})
                            continue
                        _ak = _picked[1]
                        from server.core.auto_router import RouteResult as _RR
                        cand = _RR(success=True, model=_mdl, provider=_prov, api_key=_ak,
                                   adapter=_caf(_prov.api_type), fallback_count=st_attempt)
                    else:
                        cand = await ar.get_best_candidate(cascade_db, conversation_id, exclude_model_ids=tried_sids)
                    _diag(conversation_id, "auto_stream_candidate_done", _diag_start, attempt=st_attempt, success=cand.success if cand else None)
                    if not cand.success or (cand.model and cand.model.id in tried_sids):
                        print(f"[CASCADE] exhausted at attempt {st_attempt}: {cand.error if cand else 'no cand'} tried={tried_sids}", flush=True)
                        err_data = {"error": cand.error or "no more candidates", "attempts": stream_errs}
                        yield _format_sse_chunk(err_data, "unknown")
                        yield b"data: [DONE]\n\n"
                        await _write_stream_log(conversation_id, request, raw_request, "error",
                            None, None, str(cand.error if cand else "no candidates"), st_attempt, stream_errs,
                            diag_start_ts=_diag_start)
                        return
                    tried_sids.add(cand.model.id)
                    # 防 MissingGreenlet：流式回退过程中 session 可能因限流/日志写入发生 commit/rollback，
                    # ORM 对象属性可能过期。这里先显式刷新并把后续要用的字段拷贝成普通 Python 值。
                    try:
                        await cascade_db.refresh(cand.provider, attribute_names=["name", "base_url", "headers", "credential_type", "oauth_code", "proxy_enabled"])
                        await cascade_db.refresh(cand.model, attribute_names=["id", "model_id", "request_overrides", "context_length", "supports_reasoning_effort"])
                    except Exception:
                        pass
                    cand_model_pk = cand.model.id
                    cand_model_id = cand.model.model_id
                    cand_provider_name = cand.provider.name
                    cand_provider_base_url = cand.provider.base_url
                    cand_provider_headers = cand.provider.headers
                    mid_full = f"{cand_provider_name}/{cand_model_id}"
                    # 上下文预检：装不下的候选直接跳过（不打上游、不进冷却）
                    _cand_ctx_len = int(getattr(cand.model, "context_length", 0) or 0)
                    if _cand_ctx_len > 0 and est_req_tokens + 1024 > _cand_ctx_len:
                        stream_errs.append({"attempt": st_attempt, "error": f"skip (context window {_cand_ctx_len} < est ~{est_req_tokens} tokens)"})
                        _decision_skip(conversation_id, model_pk=cand_model_pk, provider=cand_provider_name, model=cand_model_id, reason="context window too small")
                        continue
                    up_req = _without_unsupported_reasoning(
                        request.model_copy(update={"model": cand_model_id}), cand.model
                    )
                    _decision_select(conversation_id, provider=cand_provider_name, model=cand_model_id, model_pk=cand_model_pk, reason=getattr(cand, "selection_reason", None) or "next ranked candidate")
                    gen = None
                    sc = 0
                    stream_buf = []
                    _candidate_started = time.time()
                    try:
                        _diag(conversation_id, "upstream_stream_start", _diag_start, attempt=st_attempt, provider=cand_provider_name, model=cand_model_id)
                        _cascade_eh = _merge_oauth_headers(cand.provider, cand_provider_headers)
                        _cmov = getattr(cand.model, "request_overrides", None) or {}
                        if isinstance(_cmov, dict) and _cmov.get("headers"):
                            _cascade_eh = {**(_cascade_eh or {}), **_cmov["headers"]}
                        gen = cand.adapter.stream_chat_completion(
                            up_req, cand.api_key, cand_provider_base_url, _cascade_eh
                        )
                        stream_buf = []
                        last_usage = {}
                        stream_has_error = False
                        stream_err_detail = ""
                        _cascade_ttft_ms = None
                        _candidate_ttft_ms = None
                        candidate_first_chunk_timeout = min(first_chunk_timeout, remaining_first_response)
                        async for ck in _stream_with_first_chunk_timeout(gen, candidate_first_chunk_timeout):
                            # 首字延迟：首个 chunk 距请求开始的时间
                            if _cascade_ttft_ms is None:
                                _cascade_ttft_ms = int((time.time() - _send_time) * 1000)
                                _candidate_ttft_ms = int((time.time() - _candidate_started) * 1000)
                            sc += 1
                            if isinstance(ck, dict) and "error" in ck:
                                stream_has_error = True
                                stream_err_detail = str(ck.get("error", "unknown"))[:200]
                                break
                            yield _format_sse_chunk(ck, mid_full)
                            stream_buf.append(ck)
                            u = _stream_usage_dict(ck)
                            if u:
                                last_usage = u
                        if stream_has_error:
                            raise RuntimeError(f"upstream_stream_error: {stream_err_detail}")
                        # ── 空输出判定（combo 专用）：HTTP 成功但零内容 → 视为候选失败，继续 fallback ──
                        # 只要整个流累计的 delta.content 全为空（无论上游发了多少空 chunk），就判为空输出。
                        # 空 content 的 chunk 客户端渲染不出任何东西，回退对客户端无感、安全。
                        if _stream_content_is_empty(stream_buf):
                            err_s = "empty_stream_output: 上游返回成功但无内容"
                            stream_errs.append({"attempt": st_attempt, "model": mid_full, "error": err_s})
                            _decision_attempt(conversation_id, provider=cand_provider_name, model=cand_model_id, status="failed", attempt=st_attempt, latency_ms=int((time.time() - _candidate_started) * 1000), ttft_ms=_candidate_ttft_ms, error=err_s)
                            if ar.health_checker:
                                ar.health_checker.mark_failure(cand_model_pk)
                                ar.health_checker.mark_cooling(cand_model_pk, ar.config.cooling_period_seconds)
                            print(f"[CASCADE] 第{st_attempt + 1}次尝试 服务商={cand_provider_name} 模型={cand_model_id} 空输出，正在尝试下一个候选", flush=True)
                            import json as _cjs
                            await _write_stream_log(conversation_id, request, raw_request, "error",
                                cand_provider_name, cand_model_id, err_s, st_attempt, None,
                                stream_body=_cjs.dumps(stream_buf, ensure_ascii=False) if stream_buf else None, diag_start_ts=_diag_start)
                            continue
                        _diag(conversation_id, "upstream_stream_done", _diag_start, attempt=st_attempt, provider=cand_provider_name, model=cand_model_id, chunks=sc)
                        resp_snapshot = None
                        if stream_buf:
                            import json as _json_mod
                            resp_snapshot = _json_mod.dumps(stream_buf, ensure_ascii=False)
                        pt = int(last_usage.get("prompt_tokens") or last_usage.get("input_tokens") or 0)
                        ct = int(last_usage.get("completion_tokens") or last_usage.get("output_tokens") or 0)
                        _crd, _cwt = _extract_cache_tokens(last_usage)
                        # 上游漏报 usage 时按实际输出文本粗估 completion（prompt 由 sanitize 兜底）
                        _pt, _ct = _sanitize_token_counts(request, pt, ct, _output_text_from_chunks(stream_buf))
                        _decision_attempt(conversation_id, provider=cand_provider_name, model=cand_model_id, status="success", attempt=st_attempt, latency_ms=int((time.time() - _candidate_started) * 1000), ttft_ms=_candidate_ttft_ms)
                        await _write_stream_log(conversation_id, request, raw_request, "success",
                            cand_provider_name, cand_model_id, None, st_attempt, None,
                            stream_body=resp_snapshot, prompt_tokens=_pt, completion_tokens=_ct,
                            cache_read_tokens=_crd, cache_write_tokens=_cwt,
                            latency_ms=int((time.time() - _send_time) * 1000),
                            ttft_ms=_cascade_ttft_ms, diag_start_ts=_diag_start)
                        if ar.health_checker:
                            ar.health_checker.mark_success(cand_model_pk)
                        yield b"data: [DONE]\n\n"
                        return
                    except Exception as se:
                        _diag(conversation_id, "upstream_stream_error", _diag_start, attempt=st_attempt, provider=cand_provider_name, model=cand_model_id, error=type(se).__name__)
                        err_s = f"{type(se).__name__}: {str(se)[:200]}"
                        stream_errs.append({"attempt": st_attempt, "model": mid_full, "error": err_s})
                        _decision_attempt(conversation_id, provider=cand_provider_name, model=cand_model_id, status="failed", attempt=st_attempt, latency_ms=int((time.time() - _candidate_started) * 1000), ttft_ms=_candidate_ttft_ms, error=err_s)
                        if ar.health_checker and not is_context_error(err_s):
                            # 上下文超限不进冷却（请求体过大不是候选的错）
                            # 上游内容校验失败（如思考模型零正文）同样不代表模型故障，不冷却
                            if not _is_stream_content_validation_error(err_s):
                                ar.health_checker.mark_failure(cand_model_pk)
                                ar.health_checker.mark_cooling(cand_model_pk, ar.config.cooling_period_seconds)
                        cd_seconds = ar.config.cooling_period_seconds
                        fc = (ar.health_checker._fail_count.get(cand_model_pk, 0) if ar.health_checker else 0)
                        cd_actual = min(cd_seconds * (2 ** max(fc - 1, 0)), 3600) if fc > 1 else cd_seconds
                        err_s_annotated = f"{err_s} | cooldown={cd_actual}s fail#{fc}"
                        raw_err = _extract_error_body(se) or err_s
                        # 只有已经向客户端吐出过真实内容（正文/tool_calls）的中途失败才应截断；
                        # 如果错误发生在任何实质内容之前（典型：上游内容校验失败），应回退下一候选
                        if not _stream_content_is_empty(stream_buf):
                            yield _format_sse_chunk({"error": f"stream_mid_failure: {err_s}"}, mid_full)
                            yield b"data: [DONE]\n\n"
                            await _write_stream_log(conversation_id, request, raw_request, "error",
                                cand_provider_name, cand_model_id, err_s_annotated, st_attempt, stream_errs,
                                stream_body=raw_err, diag_start_ts=_diag_start)
                            return
                        print(f"[CASCADE] attempt {st_attempt} failed ({err_s[:60]}), trying next (tried={tried_sids})", flush=True)
                        await _write_stream_log(conversation_id, request, raw_request, "error",
                            cand_provider_name, cand_model_id, err_s_annotated, st_attempt, None,
                            stream_body=raw_err, diag_start_ts=_diag_start)
                        continue
                    finally:
                        # 显式关闭上游 async generator，防止 GC 时 aclose() 与正在运行的 generator 竞态
                        if gen is not None:
                            try:
                                await gen.aclose()
                            except Exception:
                                pass
                yield _format_sse_chunk({"error": "all cascade fallbacks exhausted", "attempts": stream_errs}, "unknown")
                yield b"data: [DONE]\n\n"
                await _write_stream_log(conversation_id, request, raw_request, "error",
                    None, None, "all cascade fallbacks exhausted", 0, stream_errs,
                    diag_start_ts=_diag_start)
            finally:
                await cascade_db.close()
                try:
                    await asyncio.shield(_decision_finish(
                        conversation_id,
                        status="error",
                        fallback_count=max(0, len(stream_errs) - 1),
                        failure_reason="stream ended before a terminal routing result",
                        attempts=stream_errs,
                    ))
                except Exception:
                    pass

        return StreamingResponse(cascade_stream(), media_type="text/event-stream")

    else:
        # 非流式级联回退：完整业务请求内置于回退循环
        route_result, response, _attempt_errors = await _auto_request_with_cascade_fallback(
            ar, db, request, conversation_id, diag_start_ts=_diag_start
        )
        if not route_result.success:
            # 写一条失败日志
            try:
                _diag(conversation_id, "final_error_log_start", _diag_start)
                from server.db import AsyncSessionLocal as _LS
                from server.models.request_log import RequestLog as _RL
                import json as _j
                async with _LS() as _ldb:
                    await write_log(_ldb,
                        conversation_id=conversation_id,
                        requested_model=request.model,
                        status="error",
                        error_type="upstream_error",
                        error_msg=response.get("error", "all_candidates_failed"),
                        fallback_count=len(_attempt_errors),
                        user_ip=raw_request.client.host if raw_request.client else None,
                        request_body=_j.dumps(request.model_dump(), ensure_ascii=False),
                        response_body=_j.dumps(response, ensure_ascii=False),
                        **_proxy_log_fields(),
                    )
                    _diag(conversation_id, "final_error_log_done", _diag_start)
            except Exception:
                _diag(conversation_id, "final_error_log_error", _diag_start)
                pass
            await _decision_finish(
                conversation_id,
                status="error",
                fallback_count=max(0, len(_attempt_errors) - 1),
                total_latency_ms=int((time.time() - _send_time) * 1000),
                failure_reason=response.get("error", "all_candidates_failed"),
                attempts=_attempt_errors,
            )
            return JSONResponse(
                status_code=503,
                content={"error": response.get("error", "all_candidates_failed"), "attempts": _attempt_errors},
            )
        made_by_cascade = True
        # response 已经是上游返回的完整 dict，携带 usage/choices/model

    # ─── 直接路由的实际调用 ───
    if not made_by_cascade:
        # 只有直接路由才需要在此发起实际 API 调用
        model_id_full = f"{route_result.provider.name}/{route_result.model.model_id}"
        upstream_request = _without_unsupported_reasoning(
            request.model_copy(update={"model": route_result.model.model_id}), route_result.model
        )
        extra_headers = route_result.provider.headers if route_result.provider.headers else None
        if getattr(route_result, "extra_headers", None):
            extra_headers = {**(extra_headers or {}), **route_result.extra_headers}
        if not extra_headers:
            extra_headers = None
        # ??? per-model request overrides (v3.4) ???
        model_overrides_direct = getattr(route_result.model, "request_overrides", None) or {}
        if isinstance(model_overrides_direct, dict):
            ov_headers = model_overrides_direct.get("headers") or {}
            ov_body = model_overrides_direct.get("body_patch") or {}
            ov_model = model_overrides_direct.get("model_alias")
            if ov_model:
                upstream_request = upstream_request.model_copy(update={"model": ov_model})
            if ov_headers and isinstance(ov_headers, dict):
                extra_headers = {**(extra_headers or {}), **ov_headers}
            if ov_body and isinstance(ov_body, dict):
                try:
                    upstream_request = upstream_request.model_copy(update=ov_body)
                except Exception:
                    pass
        _send_time = time.time()
        if request.stream:
            _diag(conversation_id, "direct_stream_response_created", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id)
            generator = route_result.adapter.stream_chat_completion(
                upstream_request, route_result.api_key, route_result.provider.base_url, extra_headers
            )
            _stream_usage = {}
            _stream_err = None
            _stream_ttft_ms = None
            _stream_chunks_log = []  # 收集所有 chunk 用于日志
            async def wrap_stream():
                nonlocal _stream_usage, _stream_err, _stream_ttft_ms
                _diag(conversation_id, "direct_stream_generator_start", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id)
                # 直连流对“正文出现前断流”（内容校验失败 / 空闲超时等）最多重试一次：
                # 只有尚未向客户端吐出任何实质 chunk 时才重试，避免重复输出对客户端可见。
                try:
                    for _attempt_no in range(2):
                        _attempt_gen = generator if _attempt_no == 0 else route_result.adapter.stream_chat_completion(
                            upstream_request, route_result.api_key, route_result.provider.base_url, extra_headers
                        )
                        if _attempt_no > 0:
                            # 上一轮可能只收到 role/reasoning/usage 元数据；重试前清空，避免污染日志和 usage。
                            _stream_chunks_log.clear()
                            _stream_usage = {}
                            _stream_ttft_ms = None
                        stream_has_error = False
                        stream_err_detail = ""
                        try:
                            _diag(conversation_id, "upstream_stream_start", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id)
                            async for chunk in _attempt_gen:
                                # 首字延迟：首个到达 chunk 距请求开始的时间
                                if _stream_ttft_ms is None:
                                    _stream_ttft_ms = int((time.time() - _send_time) * 1000)
                                if isinstance(chunk, dict):
                                    # 检测上游在 SSE 流中返回的错误
                                    if "error" in chunk and "choices" not in chunk:
                                        stream_has_error = True
                                        stream_err_detail = str(chunk.get("error", "unknown"))[:200]
                                        break  # 跳出循环再抛，避免 aclose() 竞态
                                    u = _stream_usage_dict(chunk)
                                    if u:
                                        _stream_usage = u
                                    _stream_chunks_log.append(chunk)
                                yield _format_sse_chunk(chunk, model_id_full)
                            if stream_has_error:
                                if (
                                    _attempt_no == 0
                                    and _stream_content_is_empty(_stream_chunks_log)
                                    and _is_stream_content_validation_error(stream_err_detail)
                                ):
                                    continue
                                raise RuntimeError(f"upstream_stream_error: {stream_err_detail}")
                            _diag(conversation_id, "upstream_stream_done", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, chunks=len(_stream_chunks_log))
                            yield b"data: [DONE]\n\n"
                            return
                        except Exception as e:
                            _diag(conversation_id, "upstream_stream_error", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, error=type(e).__name__)
                            _err_text = f"{type(e).__name__}: {str(e)[:200]}"
                            if (
                                _attempt_no == 0
                                and _stream_content_is_empty(_stream_chunks_log)
                                and _is_stream_content_validation_error(_err_text)
                            ):
                                continue
                            _stream_err = _err_text
                            error_chunk = {"error": f"upstream_stream_failed: {_stream_err}"}
                            yield _format_sse_chunk(error_chunk, model_id_full)
                            yield b"data: [DONE]\n\n"
                            return
                        finally:
                            try:
                                await _attempt_gen.aclose()
                            except Exception:
                                pass
                finally:
                    # 显式关闭上游 async generator，防止 GC 时 aclose() 竞态
                    try:
                        await generator.aclose()
                    except Exception:
                        pass
                    # 流完成后异步写日志
                    resp_snapshot = None
                    if _stream_chunks_log:
                        import json as _j
                        resp_snapshot = _j.dumps(_stream_chunks_log, ensure_ascii=False)
                    pt = int(_stream_usage.get("prompt_tokens") or _stream_usage.get("input_tokens") or 0)
                    ct = int(_stream_usage.get("completion_tokens") or _stream_usage.get("output_tokens") or 0)
                    _crd, _cwt = _extract_cache_tokens(_stream_usage)
                    # 上游漏报 usage 时按实际输出文本粗估 completion（prompt 由 sanitize 兜底）
                    pt, ct = _sanitize_token_counts(request, pt, ct, _output_text_from_chunks(_stream_chunks_log))
                    try:
                        _decision_attempt(
                            conversation_id,
                            provider=route_result.provider.name,
                            model=route_result.model.model_id,
                            status="failed" if _stream_err else "success",
                            attempt=0,
                            latency_ms=int((time.time() - _send_time) * 1000),
                            ttft_ms=_stream_ttft_ms,
                            error=_stream_err,
                        )
                        _diag(conversation_id, "stream_log_start", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, status="error" if _stream_err else "success")
                        from server.db import AsyncSessionLocal as _LS
                        from server.models.request_log import RequestLog as _RL
                        async with _LS() as _ldb:
                            await write_log(_ldb,
                                conversation_id=conversation_id,
                                requested_model=request.model,
                                routed_provider=route_result.provider.name,
                                routed_model=route_result.model.model_id,
                                status="error" if _stream_err else "success",
                                prompt_tokens=pt,
                                completion_tokens=ct,
                cache_read_tokens=_crd or None,
                cache_write_tokens=_cwt or None,
                estimated_cost_usd=(
                    _segmented_cost({
                        "input_price": float(route_result.model.input_price or 0),
                        "output_price": float(route_result.model.output_price or 0),
                        "cache_read_input_price": float(getattr(route_result.model, "cache_read_input_price", 0) or 0),
                        "cache_write_input_price": float(getattr(route_result.model, "cache_write_input_price", 0) or 0),
                    }, pt, ct, _crd, _cwt)
                    if (not _stream_err and route_result and route_result.success and (pt or ct or _crd or _cwt)) else 0.0
                ),
                fallback_count=route_result.fallback_count if route_result else 0,
                latency_ms=int((time.time() - _send_time) * 1000),
                ttft_ms=_stream_ttft_ms,
                error_type="upstream_error" if _stream_err else None,
                error_msg=(_stream_err or ""),
                request_body=_j.dumps(upstream_request.model_dump(), ensure_ascii=False) if upstream_request else None,
                response_body=resp_snapshot,
                **_proxy_log_fields(),
                            )
                            _diag(conversation_id, "stream_log_done", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, status="error" if _stream_err else "success")
                        await _decision_finish(
                            conversation_id,
                            status="error" if _stream_err else "success",
                            provider=route_result.provider.name,
                            model=route_result.model.model_id,
                            fallback_count=route_result.fallback_count if route_result else 0,
                            total_latency_ms=int((time.time() - _send_time) * 1000),
                            ttft_ms=_stream_ttft_ms,
                            failure_reason=_stream_err,
                        )
                    except Exception:
                        _diag(conversation_id, "stream_log_error", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, status="error" if _stream_err else "success")
                        pass
                    try:
                        await asyncio.shield(_decision_finish(
                            conversation_id,
                            status="error" if _stream_err else "success",
                            provider=route_result.provider.name,
                            model=route_result.model.model_id,
                            fallback_count=route_result.fallback_count if route_result else 0,
                            total_latency_ms=int((time.time() - _send_time) * 1000),
                            ttft_ms=_stream_ttft_ms,
                            failure_reason=_stream_err,
                        ))
                    except Exception:
                        pass
            response = StreamingResponse(wrap_stream(), media_type="text/event-stream")
        else:
            try:
                # v3.5 同模型 key 内切换：首把失败则在同一模型归属 key 集合内换下一把重试
                from server.core.key_rotator import get_key_rotator as _gkr
                _rot = _gkr()
                _tried = set()
                if route_result.key_id is not None:
                    _tried.add(route_result.key_id)
                _done = False
                _last_err = None
                result = None
                _cur_kid, _cur_key = route_result.key_id, route_result.api_key
                for _attempt in range(8):
                    _direct_attempt_started = time.time()
                    if _attempt > 0:
                        _nk = await _rot.next_key_for_model(db, route_result.model, _tried)
                        if not _nk or _nk[0] is None:
                            break  # 同模型 key 集合内已无可用 key
                        _cur_kid, _cur_key = _nk
                        _tried.add(_cur_kid)
                    _diag(conversation_id, "upstream_start", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, stream=False)
                    try:
                        result = await route_result.adapter.chat_completion(
                            upstream_request, _cur_key, route_result.provider.base_url, extra_headers
                        )
                        route_result.api_key = _cur_key
                        route_result.key_id = _cur_kid
                        _rot.mark_success(_cur_kid)
                        _decision_attempt(
                            conversation_id,
                            provider=route_result.provider.name,
                            model=route_result.model.model_id,
                            status="success",
                            attempt=_attempt,
                            latency_ms=int((time.time() - _direct_attempt_started) * 1000),
                            reason="key rotation attempt" if _attempt else None,
                        )
                        _diag(conversation_id, "upstream_done", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, stream=False)
                        _done = True
                        break
                    except Exception as e:
                        _last_err = e
                        _decision_attempt(
                            conversation_id,
                            provider=route_result.provider.name,
                            model=route_result.model.model_id,
                            status="failed",
                            attempt=_attempt,
                            latency_ms=int((time.time() - _direct_attempt_started) * 1000),
                            error=f"{type(e).__name__}: {str(e)[:200]}",
                            reason="retrying another key",
                        )
                        _rot.mark_failure(_cur_kid, getattr(e, "status_code", None))
                        _diag(conversation_id, "upstream_error", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, stream=False, error=type(e).__name__)
                        continue
                if not _done:
                    if _last_err:
                        raise _last_err
                if isinstance(result, dict) and "model" in result:
                    result["model"] = model_id_full
                response = result
            except Exception as e:
                _diag(conversation_id, "upstream_error", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, stream=False, error=type(e).__name__)
                http_status_code = 503
                raw_err = _extract_error_body(e) or f"{type(e).__name__}: {str(e)[:200]}"
                response = {"error": f"upstream_call_failed: {type(e).__name__}: {str(e)[:200]}", "_raw_response": raw_err}
    else:
        # 级联回退已完成实际调用，用返回的 model 信息构建标识
        if route_result and route_result.success:
            model_id_full = f"{route_result.provider.name}/{route_result.model.model_id}"

    # ─── 响应头（ASCII-safe） ───
    if isinstance(response, Response) and route_result:
        response.headers["x-routed-via"] = _safe_header(model_id_full)
        response.headers["x-routing-strategy"] = "auto" if is_auto else "direct"
        response.headers["x-fallback-count"] = str(route_result.fallback_count)

    # ─── 异步写请求日志（流式已在 wrap_stream 内完成，跳过） ───
    if not isinstance(response, Response):
        try:
            _diag(conversation_id, "request_log_start", _diag_start)
            from server.db import AsyncSessionLocal as _LogSession
            from server.models.request_log import RequestLog as _RL
            import json as _json_mod
            is_err = isinstance(response, dict) and "error" in response
            resp_dict = response if isinstance(response, dict) else None
            usage = resp_dict.get("usage", {}) if resp_dict else {}
            pt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            ct = usage.get("completion_tokens") or usage.get("output_tokens") or 0
            pt, ct = _sanitize_token_counts(request, pt, ct, _output_text_from_result(resp_dict) if resp_dict else "")
            _crd, _cwt = _extract_cache_tokens(usage)
            _latency = int((time.time() - _send_time) * 1000) if _send_time else None
            try:
                _log_req = upstream_request
            except NameError:
                _log_req = request
            req_body_str = _json_mod.dumps(_log_req.model_dump(), ensure_ascii=False) if _log_req else None
            resp_body_str = _json_mod.dumps(resp_dict, ensure_ascii=False) if resp_dict else None
            if not resp_body_str and is_err and isinstance(response, dict):
                raw = response.get("_raw_response", "")
                if raw:
                    resp_body_str = raw[:5000]
            async with _LogSession() as _ldb:
                _rlog = _RL(
                    conversation_id=conversation_id,
                    requested_model=request.model,
                    routed_provider=route_result.provider.name if (route_result and route_result.success) else None,
                    routed_provider_id=route_result.provider.id if (route_result and route_result.success) else None,
                    routed_model=route_result.model.model_id if (route_result and route_result.success) else None,
                    status="error" if is_err else "success",
                    latency_ms=_latency,
                    prompt_tokens=int(pt) if pt else 0,
                    completion_tokens=int(ct) if ct else 0,
                    cache_read_tokens=_crd or None,
                    cache_write_tokens=_cwt or None,
                    estimated_cost_usd=(
                        _segmented_cost({
                            "input_price": float(route_result.model.input_price or 0),
                            "output_price": float(route_result.model.output_price or 0),
                            "cache_read_input_price": float(getattr(route_result.model, "cache_read_input_price", 0) or 0),
                            "cache_write_input_price": float(getattr(route_result.model, "cache_write_input_price", 0) or 0),
                        }, pt, ct, _crd, _cwt)
                        if (not is_err and route_result and route_result.success and (pt or ct or _crd or _cwt)) else 0.0
                    ),
                    fallback_count=route_result.fallback_count if route_result else 0,
                    user_ip=raw_request.client.host if raw_request.client else None,
                    error_type="upstream_error" if is_err else None,
                    error_msg=str(response.get("error", "")) if is_err else None,
                    request_body=req_body_str,
                    response_body=resp_body_str,
                    **_proxy_log_fields(),
                )
                await dedup_log_row(_ldb, _rlog)
                _ldb.add(_rlog)
                await _ldb.commit()
                _diag(conversation_id, "request_log_done", _diag_start, status="error" if is_err else "success")
            if not made_by_cascade:
                _decision_attempt(
                    conversation_id,
                    provider=route_result.provider.name if (route_result and route_result.success) else None,
                    model=route_result.model.model_id if (route_result and route_result.success) else None,
                    status="failed" if is_err else "success",
                    attempt=route_result.fallback_count if route_result else 0,
                    latency_ms=_latency,
                    error=str(response.get("error", "")) if is_err else None,
                )
            await _decision_finish(
                conversation_id,
                status="error" if is_err else "success",
                provider=route_result.provider.name if (route_result and route_result.success) else None,
                model=route_result.model.model_id if (route_result and route_result.success) else None,
                fallback_count=route_result.fallback_count if route_result else 0,
                total_latency_ms=_latency,
                failure_reason=str(response.get("error", "")) if is_err else None,
            )
        except Exception as _e:
            _diag(conversation_id, "request_log_error", _diag_start, error=type(_e).__name__)
            print(f"[WARN] request log write failed: {_e}")

    # ─── 返回 ───
    _diag(conversation_id, "response_ready", _diag_start, http_status=http_status_code, response_type=type(response).__name__)
    if isinstance(response, dict) and http_status_code != 200:
        json_response = JSONResponse(status_code=http_status_code, content=response)
        json_response.headers["x-routed-via"] = _safe_header(model_id_full)
        json_response.headers["x-routing-strategy"] = "auto" if is_auto else "direct"
        json_response.headers["x-fallback-count"] = str(route_result.fallback_count) if route_result else "0"
        return json_response
    return response
@router.get("/models")
async def list_models(
    db: AsyncSession = Depends(get_db)
):
    """OpenAI 兼容 models 端点"""
    mc = ModelCatalog()
    models = await mc.list_models(db, enabled_only=True)
    data = []
    for model in models:
        provider = await db.get(Provider, model.provider_id)
        if not provider:
            continue  # 跳过已删除服务商的孤立模型
        data.append({
            "id": f"{provider.name}/{model.model_id}",
            "object": "model",
            "created": int(model.created_at.timestamp()) if model.created_at else int(time.time()),
            "owned_by": provider.name,
            "pricing": {
                "input": model.input_price,
                "output": model.output_price,
                "cache_read_input": getattr(model, "cache_read_input_price", 0) or 0,
                "cache_write_input": getattr(model, "cache_write_input_price", 0) or 0,
                "unit": "per_1M_tokens",
                "currency": "USD"
            },
            "is_free": model.is_free,
            "auto_enabled": model.auto_enabled,
            "capabilities": {
                "streaming": model.supports_streaming,
                "vision": model.supports_vision,
                "reasoning_effort": getattr(model, "supports_reasoning_effort", None),
                "context_length": model.context_length
            }
        })
    return {
        "object": "list",
        "data": data
    }
