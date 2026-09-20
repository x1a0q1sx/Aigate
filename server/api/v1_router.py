"""
/v1/* OpenAI 兼容端点
"""
import asyncio
import json
import re
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
from server.core.usage_normalize import normalize_usage as _normalize_usage
from server.core.client_ip import real_client_ip as _real_client_ip
from server.core.context_guard import (
    estimate_request_tokens,
    is_context_error,
    context_overflows,
    get_estimate_factor,
    record_context_overflow,
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
    """缓存读/写 token 提取（P1-4：统一走 usage_normalize，兼容三种方言）。
    返回 (cache_read, cache_write)。"""
    from server.core.usage_normalize import normalize_usage
    nu = normalize_usage(usage)
    return nu.cache_read_tokens, nu.cache_write_tokens


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


def _chunk_has_substance(ck) -> bool:
    """chunk 是否携带实质输出：正文 / 思考内容 / 工具调用任一非空。

    role-only、usage-only、finish-only 等 chunk 均为元数据。
    用于流式回退的『锁定』语义：实质 chunk 一旦出现即锁定候选，
    此前只缓冲元数据（客户端无感），流结束仍无实质 → 丢弃缓冲无感回退。
    """
    if not isinstance(ck, dict):
        return False
    for ch in (ck.get("choices") or []):
        if not isinstance(ch, dict):
            continue
        d = ch.get("delta") or {}
        if isinstance(d.get("content"), str) and d.get("content"):
            return True
        rc = d.get("reasoning_content")
        if isinstance(rc, str) and rc:
            return True
        if d.get("tool_calls"):
            return True
        m = ch.get("message")
        if isinstance(m, dict) and (m.get("content") or m.get("tool_calls")):
            return True
    return False


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


# 瞬态上游故障：429/5xx、网络超时/断连、站点过载（如 cache-only admission 的 503）。
# 这类故障在尚未吐出实质内容时值得原样重试一次（同会话重试常因缓存变暖而成功）。
_TRANSIENT_UPSTREAM_RE = re.compile(
    r"\b(429|500|502|503|504)\b"
    r"|service unavailable|bad gateway|gateway timeout"
    r"|temporarily unavailable|overloaded|too many requests"
    r"|cache-only|admission rejected"
    r"|(?:connect|read|send)[a-z]*\s*(?:error|timeout|timed out|reset|closed)"
    r"|timed?\s?out",
    re.IGNORECASE,
)


def _is_transient_upstream_error(text) -> bool:
    """识别可自动重试的瞬态上游故障（区别于 4xx 参数错误 / 鉴权失败等不可重试故障）。"""
    if not text:
        return False
    return bool(_TRANSIENT_UPSTREAM_RE.search(str(text)))


def _api_error(message, *, status=None, type_=None, **extra) -> dict:
    """OpenAI 规范的 error 对象。

    error 必须是对象（{message,type,code}）而非字符串 —— Codex 等客户端按
    schema 校验响应，字符串会触发 'Invalid input: expected object' 的类型
    校验错误，把真实原因埋掉。status 用于推断默认 type。"""
    if type_ is None:
        t = status or 0
        type_ = ("rate_limit_error" if t == 429
                 else "server_error" if t >= 500 else "invalid_request_error")
    err = {"message": str(message)[:800], "type": type_, "code": None}
    if extra:
        err.update(extra)
    return {"error": err}


async def _early_error_log(conversation_id, request, raw_request, message, *, err_type="route_error"):
    """路由阶段的提前失败（404/503 等）：落一条终态错误日志，让预落的
    待响应行原位收尾，而不是挂 30 分钟等清扫。"""
    try:
        from server.core.log_queue import enqueue_log as _el
        await _el(
            conversation_id=conversation_id,
            requested_model=request.model,
            status="error",
            error_type=err_type,
            error_msg=str(message)[:500],
            user_ip=_real_client_ip(raw_request, getattr(config.security, 'trust_proxy_headers', False)),
            is_health_check=conversation_id.startswith("hc-"),
        )
    except Exception:
        pass


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
    await verify_aigate_api_key(raw_request)
    try:
        body = await raw_request.json()
    except Exception:
        return JSONResponse(status_code=400, content=_api_error("invalid_json", status=400))
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return JSONResponse(status_code=400, content=_api_error("messages must be a list", status=400))
    from server.core.compress_service import compress_messages
    result = compress_messages(
        messages,
        rtk_enabled=body.get("rtk_enabled"),
        caveman_enabled=body.get("caveman_enabled"),
        ponytail_enabled=body.get("ponytail_enabled"),
    )
    return JSONResponse(content={"object": "context.compression", **result})

async def verify_aigate_api_key(raw_request: Request):
    """鉴权：主密钥（config.security.aigate_api_key）或下游网关密钥（D1）。

    Bearer（OpenAI/Codex 客户端）、x-api-key（Anthropic 客户端）、
    x-goog-api-key（Gemini 客户端）都接受。
    主密钥命中 → 放行（无预算约束）；未命中 → 查网关密钥（含 RPM/预算检查），
    并把 key id 写入请求上下文（日志自动落 downstream_key_id）。
    未配置任何密钥时保持开放（历史行为）。
    """
    from server.core.request_logger import set_downstream_key_id
    from server.core.gateway_keys import check_gateway_key
    expected = getattr(config.security, "aigate_api_key", "") or ""
    auth = raw_request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    else:
        token = raw_request.headers.get("x-api-key", "").strip()
    if not token:
        token = raw_request.headers.get("x-goog-api-key", "").strip()
    if not token:
        token = (raw_request.query_params.get("key") or "").strip()  # Gemini SDK ?key= 形态
    if not token:
        if not expected:
            return  # 网关未启用鉴权
        raise HTTPException(status_code=401, detail="Missing AIGate API key")
    if expected and token == expected:
        set_downstream_key_id(None)  # 主密钥
        return
    info = await check_gateway_key(token)
    if info is None:
        if not expected:
            set_downstream_key_id(None)
            return  # 未启用鉴权且 token 不是任何网关密钥 → 保持开放
        raise HTTPException(status_code=401, detail="Invalid AIGate API key")
    raw_request.state.downstream_key_id = info["id"]
    set_downstream_key_id(info["id"])

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

def _preprocess_request(req, savers_off: bool = False):
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
        ts_enabled = (getattr(ts_cfg, 'enabled', True) if ts_cfg else True) and not savers_off
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
        if extra and getattr(extra, 'caveman_enabled', False) and not savers_off:
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
        if extra and getattr(extra, 'ponytail_enabled', False) and not savers_off:
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
            # free_tier/oauth/atomcode 候选不查 api_keys（调用点经 credential_resolver dispatch）；
            # 此前在此处 pick_key 失败会把它们整条跳过，导致这类候选永远无法在 combo/auto 中被调用
            if getattr(_prov, "credential_type", "api_key") in ("free_tier", "oauth") or getattr(_prov, "api_type", "") == "atomcode":
                from server.core.model_catalog import create_adapter_for_provider as _caf
                combo_candidates.append(RouteResult(
                    success=True, model=_mdl, provider=_prov,
                    api_key="", adapter=_caf(_prov.api_type), fallback_count=0,
                ))
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
    
    # ── Race（config.race.enabled）：候选 N 秒无返回 → 并行打下一候选，先回者用；
    #    被超前的候选判失败+罚冷却。关闭时 max_inflight=1 且无超时 = 旧的顺序回退。──
    from server.core.race import NoMoreCandidates, RaceAllFailed, run_race
    _rc_cfg = getattr(config, "race", None)
    race_on = bool(_rc_cfg and getattr(_rc_cfg, "enabled", False))
    race_secs = max(3, int(getattr(_rc_cfg, "no_content_seconds", 15) or 15))
    _db_lock = asyncio.Lock()  # AsyncSession 不可并发使用：DB 短操作串行化，HTTP 在锁外

    class _ASkip(Exception):
        """预检跳过（上下文装不下等）：记录已在 launch 内完成。"""

    class _AFail(Exception):
        def __init__(self, msg, cand, raw=None):
            super().__init__(msg)
            self.cand = cand
            self.raw = raw

    ctx_by_attempt = {}

    def _cand_full(candidate):
        if candidate is not None and candidate.provider and candidate.model:
            return f"{candidate.provider.name}/{candidate.model.model_id}"
        return "?"

    async def _record_fail(attempt, candidate, err_short, raw_body=None, cooling=True):
        attempt_errors.append({
            "attempt": attempt,
            "model": _cand_full(candidate),
            "error": err_short,
        })
        _decision_attempt(
            conversation_id,
            provider=candidate.provider.name,
            model=candidate.model.model_id,
            status="failed",
            attempt=attempt,
            latency_ms=int((time.time() - (ctx_by_attempt.get(attempt) or {}).get("start", time.time())) * 1000),
            error=err_short,
        )
        if is_context_error(err_short) and candidate.model:
            await record_context_overflow(candidate.model.id, est_tokens)
        elif cooling and ar.health_checker and candidate.model:
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
                    error_msg=err_short[:300] + cooldown_note,
                    fallback_count=attempt,
                    **_proxy_log_fields(),
                    request_body=_j.dumps(request.model_dump(), ensure_ascii=False) if request else None,
                    response_body=raw_body or err_short,
                )
                _diag(conversation_id, "fallback_log_done", diag_start_ts, attempt=attempt)
        except Exception:
            _diag(conversation_id, "fallback_log_error", diag_start_ts, attempt=attempt)
        print(f"[CASCADE-NONSTREAM] attempt {attempt} failed, trying next (tried={tried_ids})", flush=True)

    async def _launch(attempt: int):
        nonlocal last_result
        if attempt > max_retries:
            raise NoMoreCandidates()
        _diag(conversation_id, "auto_candidate_start", diag_start_ts, attempt=attempt)
        async with _db_lock:
            if combo_targets:
                if attempt >= len(combo_candidates):
                    raise NoMoreCandidates()
                candidate = combo_candidates[attempt]
            else:
                candidate = await ar.get_best_candidate(db, conversation_id, exclude_model_ids=tried_ids)
            _diag(conversation_id, "auto_candidate_done", diag_start_ts, attempt=attempt,
                  success=candidate.success if candidate else None)
            last_result = candidate
            if not candidate.success:
                print(f"[CASCADE-NONSTREAM] exhausted at attempt {attempt}: {candidate.error} tried={tried_ids}", flush=True)
                attempt_errors.append({"attempt": attempt, "error": candidate.error})
                raise NoMoreCandidates()
            if candidate.model and candidate.model.id in tried_ids:
                attempt_errors.append({"attempt": attempt, "error": "duplicate candidate, no more options"})
                raise NoMoreCandidates()
            if candidate.model:
                tried_ids.add(candidate.model.id)
            # 上下文预检：估算输入装不进窗口的候选直接跳过（不打上游、不进冷却）
            # P1-5: 按该服务商+模型的历史估算系数校准；observed_context_limit 收紧标称窗口
            if candidate.model:
                _pf = await get_estimate_factor(db, candidate.provider.id, candidate.model.model_id)
                _est_adj = int(est_tokens * _pf)
                _obs = int(getattr(candidate.model, "observed_context_limit", 0) or 0)
                if context_overflows(candidate.model, _est_adj, observed_limit=_obs):
                    attempt_errors.append({
                        "attempt": attempt,
                        "model": _cand_full(candidate),
                        "error": f"skip: est ~{_est_adj} tokens (x{_pf:.2f}) > context window {candidate.model.context_length}",
                    })
                    _decision_skip(
                        conversation_id,
                        model_pk=candidate.model.id,
                        provider=candidate.provider.name,
                        model=candidate.model.model_id,
                        reason="context window too small",
                    )
                    raise _ASkip("context window too small")
            # free_tier/oauth/atomcode 候选走统一凭证解析（凭证解析在锁内完成）
            _cred = None
            if (getattr(candidate.provider, "credential_type", "api_key") in ("free_tier", "oauth")
                    or getattr(candidate.provider, "api_type", "") == "atomcode"):
                from server.core.credential_resolver import resolve_credential_async
                _rc = await resolve_credential_async(candidate.provider, candidate.model, db)
                if not _rc.ok:
                    raise _AFail(_rc.error, candidate)
                _cred = _rc
        # 发起完整业务请求（非探测）——HTTP 在锁外
        upstream_request = _without_unsupported_reasoning(
            request.model_copy(update={"model": candidate.model.model_id}), candidate.model
        )
        extra_headers = candidate.provider.headers
        ctx_by_attempt[attempt] = {"candidate": candidate, "start": time.time()}
        _diag(conversation_id, "upstream_start", diag_start_ts, attempt=attempt,
              provider=candidate.provider.name, model=candidate.model.model_id, stream=False)
        try:
            from server.core.credential_resolver import call_via
            if _cred is not None:
                result = await call_via(_cred, upstream_request, candidate.provider, candidate.model)
            else:
                result = await candidate.adapter.chat_completion(
                    upstream_request,
                    candidate.api_key,
                    candidate.provider.base_url,
                    extra_headers,
                )
        except asyncio.CancelledError:
            ctx_by_attempt[attempt]["cancelled"] = True
            raise
        except Exception as e:
            raise _AFail(f"{type(e).__name__}: {str(e)[:200]}", candidate,
                         raw=_extract_error_body(e)) from e
        ctx_by_attempt[attempt]["send_end"] = time.time()
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
                raise _AFail(f"empty_response: choices={len(choices)} tokens={usage.get('completion_tokens', 0)}",
                             candidate)
        _diag(conversation_id, "upstream_done", diag_start_ts, attempt=attempt,
              provider=candidate.provider.name, model=candidate.model.model_id, stream=False)
        return {"attempt": attempt, "candidate": candidate, "result": result}

    async def _on_fail(attempt, et, exc):
        if isinstance(exc, _ASkip):
            return  # 预检跳过的记录已在 launch 完成
        cand = getattr(exc, "cand", None) or (ctx_by_attempt.get(attempt) or {}).get("candidate")
        if cand is None:
            attempt_errors.append({"attempt": attempt, "error": et})
            return
        await _record_fail(attempt, cand, et, raw_body=getattr(exc, "raw", None))

    async def _on_loser(attempt):
        ctx = ctx_by_attempt.get(attempt)
        if ctx is None or ctx.get("send_end"):
            return  # 其实已返回（只是没赢），不按失败处理
        cand = ctx["candidate"]
        et = (f"race_overtaken: {race_secs}s 内无返回，被更快候选取代（自动罚时冷却）")
        await _record_fail(attempt, cand, et, raw_body=et)

    try:
        _, win = await run_race(
            _launch,
            no_content_seconds=(race_secs if race_on else None),
            max_inflight=(2 if race_on else 1),
            on_failure=_on_fail, on_loser=_on_loser)
    except RaceAllFailed:
        # 全部失败
        print(f"[CASCADE-NONSTREAM] all attempts exhausted ({len(ctx_by_attempt)} candidate attempts)", flush=True)
        try:
            from server.core.notifier import notify_event as _notify_event
            _notify_event("all_failed",
                          f"auto 级联全部候选失败（非流式，{len(ctx_by_attempt)} 次尝试，"
                          f"末次错误：{str(attempt_errors[-1].get('error') if attempt_errors else '未知')[:120]}）")
        except Exception:
            pass
        terminal_error = None
        if attempt_errors:
            terminal_error = attempt_errors[-1].get("error")
        if not terminal_error and last_result:
            terminal_error = last_result.error
        failed = RouteResult(success=False, error=terminal_error or "all candidates failed")
        return failed, {"error": failed.error, "attempts": attempt_errors}, attempt_errors

    # ── 胜出候选 ──
    attempt = win["attempt"]
    candidate = win["candidate"]
    result = win["result"]
    _attempt_started = (ctx_by_attempt.get(attempt) or {}).get("start", time.time())
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
    if ar.health_checker and candidate.model:
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


async def _write_stream_log(conversation_id, request, raw_request, status,
                           routed_provider, routed_model, error_msg, fallback_count, attempt_errors,
                           stream_body=None, prompt_tokens=0, completion_tokens=0, latency_ms=None,
                           ttft_ms=None, cache_read_tokens=0, cache_write_tokens=0, diag_start_ts=None,
                           est_prompt_tokens=None):
    """在流式生成器内异步写请求日志。

    方案A：request_logs 作为唯一用量数据源，直接在此写入
    routed_provider_id 与 estimated_cost_usd，不再写入平行的 quota_usage 表。

    2026-09 加固：
    - 决策收尾与日志写入拆成两个独立守卫阶段，决策失败不再连累日志落库
      （此前二者同 try，决策一挂待响应行就永远无法收尾）
    - 异常不再静默吞掉，失败原因必须出现在服务端日志
    - 整体 shield：客户端断开触发任务取消时，收尾日志仍会补完
      （实测 254s 长流式失败后日志写入被吞、行卡 pending 的根因）
    """
    async def _impl():
        # ── 阶段一：路由决策收尾（失败仅告警，不连累日志） ──
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
        except Exception as e:
            print(f"⚠️ 路由决策收尾失败 conv={str(conversation_id)[:8]}: {type(e).__name__}: {str(e)[:200]}", flush=True)
        # ── 阶段二：请求日志写入 ──
        try:
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
                    est_prompt_tokens=est_prompt_tokens,
                    user_ip=_real_client_ip(raw_request, getattr(config.security, 'trust_proxy_headers', False)),
                    **_proxy_log_fields(),
                    request_body=req_s,
                    response_body=stream_body or (_json_mod.dumps(attempt_errors, ensure_ascii=False) if attempt_errors else "[stream]"),
                )
                if diag_start_ts:
                    _diag(conversation_id, "stream_log_done", diag_start_ts, provider=routed_provider, model=routed_model, status=status)
        except Exception as e:
            # 不再静默：日志写入失败的根因必须可见（否则待响应行永远无法收尾）
            import traceback as _tb
            print(f"⚠️ 请求日志写入失败 conv={str(conversation_id)[:8]}: {type(e).__name__}: {str(e)[:300]}", flush=True)
            _tb.print_exc()
            if diag_start_ts:
                _diag(conversation_id, "stream_log_error", diag_start_ts, provider=routed_provider, model=routed_model, status=status)
    try:
        await asyncio.shield(_impl())
    except asyncio.CancelledError:
        pass  # 外层任务被取消（客户端断开）：_impl 已脱离取消继续执行，日志不丢
    except Exception as e:
        print(f"⚠️ 请求日志收尾异常 conv={str(conversation_id)[:8]}: {type(e).__name__}: {str(e)[:200]}", flush=True)


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


async def _apply_combo_scope(db: AsyncSession, raw_request: Request,
                             request: ChatCompletionRequest):
    """combo 前缀路由注入：/combo:<ref>/... 下锁定组合，但**具体模型优先**。

    语义分层（ref=路径前缀，记入 scope）：
      - 请求体 model 为空 / "*" / 是另一个 combo（combo:xxx）
          → 前缀组合为访问边界，改写为 "combo:<前缀组合名>"，走整套回退。
      - 请求体 model 是**可解析的具体模型**（provider/model_id 或裸 model_id）
        → 视为"指定模型直达"，不改写，走直连路由命中该模型。
    ref 支持组合名称或数字 id；原模型名尾部思考深度(-high 等)在改写路径里
    保留拼到组合后，由下游既有后缀解析逻辑处理。

    返回 (改写后的 request, 错误响应|None)。无 combo_scope 时原样返回。
    """
    ref = getattr(getattr(raw_request, "state", None), "combo_scope", None)
    if not ref:
        return request, None
    from server.core.combo_router import find_combo_by_ref, is_combo_request
    combo = await find_combo_by_ref(db, ref)
    if combo is None:
        return request, JSONResponse(
            status_code=404,
            content=_api_error(f"Combo '{ref}' not found", status=404))
    _raw = (request.model or "").strip()
    _base, _sfx = _split_effort_suffix(_raw)
    # 具体模型（非 combo、非空、可解析）→ 直接命中该模型，不做组合改写。
    _is_combo, _ = is_combo_request(_raw)
    if not _is_combo and _base and _base != "*" and await _model_name_resolves(db, _base):
        return request, None
    new_model = f"combo:{combo.name}" + (f"-{_sfx}" if _sfx else "")
    return request.model_copy(update={"model": new_model}), None


@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OpenAI 兼容聊天补全端点。

    A4: config.response_cache.enabled 开启时，相同请求指纹（模型+参数）的
    非流式成功响应在 TTL 内直接复用，不走路由与上游。"""
    # combo 前缀路由：改写先于缓存指纹，避免不同组合与直连互相污染缓存
    request, _combo_err = await _apply_combo_scope(db, raw_request, request)
    if _combo_err is not None:
        return _combo_err
    # P0-3: 缓存查询必须在网关鉴权之后 → 移到 _chat_completions_impl（verify 通过后）
    from server.core.response_cache import response_cache
    resp = await _chat_completions_impl(request, raw_request, db)
    if isinstance(resp, JSONResponse) and resp.status_code == 200:
        try:
            _body = json.loads(resp.body)
            # 错误体不缓存（上游/网关以 200 返回的 {"error":...} 伪装成功响应）
            if isinstance(_body, dict) and "error" not in _body:
                response_cache.put(request, _body)
        except Exception:
            pass
    return resp

async def _chat_completions_impl(
    request: ChatCompletionRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OpenAI 兼容聊天补全端点（实现体）"""
    _diag_start = time.time()
    import uuid
    conversation_id = str(uuid.uuid4())
    _diag(conversation_id, "request_enter", _diag_start, model=getattr(request, "model", None), stream=getattr(request, "stream", None), client=_real_client_ip(raw_request, getattr(config.security, 'trust_proxy_headers', False)))
    try:
        _diag(conversation_id, "auth_start", _diag_start)
        await verify_aigate_api_key(raw_request)
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
                    user_ip=_real_client_ip(raw_request, getattr(config.security, 'trust_proxy_headers', False)),
                    request_body=_json_mod.dumps(request.model_dump(), ensure_ascii=False) if request else None,
                )
        except Exception:
            pass
        raise auth_err
    # P0-3: 响应缓存查询放在鉴权之后——未授权客户端无法再靠重放请求体白取他人缓存。
    # 命中记一条 cache-hit 日志（不走上游，但审计可见）。
    try:
        from server.core.response_cache import response_cache as _rcache
        _cached = _rcache.get(request)
    except Exception:
        _cached = None
    if _cached is not None:
        try:
            from server.core.log_queue import enqueue_log as _enq_hit
            await _enq_hit(
                conversation_id=conversation_id,
                requested_model=request.model,
                status="success",
                http_status=200,
                latency_ms=0,
                user_ip=_real_client_ip(raw_request, getattr(config.security, 'trust_proxy_headers', False)),
                is_health_check=False,
            )
        except Exception:
            pass
        return JSONResponse(content=_cached)
    # 请求开始即预落一条「待响应」日志：等上游响应期间日志页即可见（500ms 内出现），
    # 完成时日志队列按 conversation_id 原位更新为最终状态（success/error，单行不补插）。
    try:
        from server.core.log_queue import enqueue_log as _enqueue_pending
        await _enqueue_pending(
            conversation_id=conversation_id,
            requested_model=request.model,
            status="pending",
            user_ip=_real_client_ip(raw_request, getattr(config.security, 'trust_proxy_headers', False)),
            is_health_check=conversation_id.startswith("hc-"),
        )
    except Exception:
        pass
    _diag(conversation_id, "router_get_start", _diag_start)
    ar = get_auto_router()
    _diag(conversation_id, "router_get_done", _diag_start)
    # X-AIGate-Token-Saver: off —— 单请求旁路所有 token saver
    # （长上下文客户端如 Codex 偶尔不希望历史被压缩改写）
    _savers_off = (raw_request.headers.get("x-aigate-token-saver", "").strip().lower()
                    in ("off", "0", "none", "skip"))
    request = _preprocess_request(request, savers_off=_savers_off)
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
    # ─── E1: 模型别名（请求名 → 目标模型/组合）───
    # 与 effort 后缀互不冲突：别名命中后按目标名重新解析一次后缀
    if request.model and not request.is_auto:
        from server.core.alias_service import resolve_alias
        _alias_target = await resolve_alias(request.model)
        if _alias_target:
            _alias_name = request.model
            request = request.model_copy(update={"model": _alias_target})
            _diag(conversation_id, "alias_applied", _diag_start,
                  alias=_alias_name, target=_alias_target)

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
        from server.core.combo_router import is_combo_request, find_combo_by_name, resolve_combo_targets, pick_next_index, pick_start_index
        is_combo, combo_name = is_combo_request(request.model)
        if is_combo:
            _diag(conversation_id, "combo_route_start", _diag_start, combo=combo_name)
            combo = await find_combo_by_name(db, combo_name)
            if not combo:
                await _decision_finish(conversation_id, status="error", failure_reason=f"Combo '{combo_name}' not found")
                await _early_error_log(conversation_id, request, raw_request, f"Combo '{combo_name}' not found")
                return JSONResponse(status_code=404, content=_api_error(f"Combo '{combo_name}' not found", status=404))
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
                await _early_error_log(conversation_id, request, raw_request, f"Combo '{combo_name}' no available targets")
                return JSONResponse(status_code=503, content=_api_error(f"Combo '{combo_name}' no available targets", status=503))
            _diag(conversation_id, "combo_targets", _diag_start, count=len(targets))
            _combo_start = pick_start_index(targets, combo.id, combo_strategy)  # E2: weighted 按权重抽签
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
            # ─── Fusion 策略：并行 fan-out + judge 合成 ───
            # 自闭环：本分支自行落日志/决策并 return，不走下方共享级联收尾。
            # 设计：docs/superpowers/specs/2026-09-11-fusion-strategy-design.md
            if combo_strategy == "fusion":
                from server.core.fusion import run_fusion, FusionAllFailed
                import json as _fj

                async def _f_precheck(_p, _m):
                    _pf = await get_estimate_factor(db, _p.id, _m.model_id)
                    _obs = int(getattr(_m, "observed_context_limit", 0) or 0)
                    if context_overflows(_m, int(est_req_tokens * _pf), observed_limit=_obs):
                        return f"context window {_m.context_length} < est ~{int(est_req_tokens * _pf)}"
                    if ar.health_checker and ar.health_checker.is_cooling(_m.id):
                        return "model is cooling down"
                    return None

                def _f_attempt(_t, _ok, _err):
                    try:
                        _decision_attempt(
                            conversation_id,
                            provider=_t["provider"].name, model=_t["model"].model_id,
                            status="success" if _ok else "failed", attempt=0, error=_err,
                        )
                    except Exception:
                        pass

                async def _fusion_job():
                    return await run_fusion(
                        db, ordered_targets, request, combo=combo,
                        precheck=_f_precheck, on_attempt=_f_attempt,
                    )

                async def _fusion_log_done(fresp, fmeta):
                    jp, jm = (fmeta.get("judge") or (None, None))
                    fb = max(0, len(fmeta.get("attempts") or []) - 1)
                    _fl = int((time.time() - _send_time) * 1000)
                    try:
                        await _write_stream_log(
                            conversation_id, request, raw_request, "success",
                            jp, jm, None, fb, fmeta.get("attempts"),
                            stream_body=_fj.dumps(fresp, ensure_ascii=False),
                            latency_ms=_fl, ttft_ms=_fl,
                            diag_start_ts=_diag_start,
                        )
                    except Exception:
                        pass
                    try:
                        await _decision_finish(
                            conversation_id, status="success", provider=jp, model=jm,
                            total_latency_ms=int((time.time() - _send_time) * 1000),
                        )
                    except Exception:
                        pass

                async def _fusion_log_fail(attempts):
                    try:
                        await _write_stream_log(
                            conversation_id, request, raw_request, "error",
                            None, None, "fusion all candidates failed",
                            max(0, len(attempts) - 1), attempts,
                            latency_ms=int((time.time() - _send_time) * 1000),
                            diag_start_ts=_diag_start,
                        )
                    except Exception:
                        pass
                    try:
                        await _decision_finish(
                            conversation_id, status="error",
                            failure_reason="fusion all candidates failed", attempts=attempts,
                        )
                    except Exception:
                        pass

                if request.stream:
                    _fusion_task = asyncio.create_task(_fusion_job())

                    async def _fusion_sse():
                        yield b": keepalive\n\n"
                        while not _fusion_task.done():
                            try:
                                await asyncio.wait_for(asyncio.shield(_fusion_task), 5)
                            except asyncio.TimeoutError:
                                yield b": fusion-collecting\n\n"
                        try:
                            fresp, fmeta = _fusion_task.result()
                        except FusionAllFailed as fe:
                            yield _format_sse_chunk(
                                _api_error("fusion: all candidates failed", status=502,
                                           attempts=fe.attempts), request.model)
                            yield b"data: [DONE]\n\n"
                            await _fusion_log_fail(fe.attempts)
                            return
                        except Exception as fe:
                            yield _format_sse_chunk(
                                _api_error(f"fusion failed: {type(fe).__name__}: {str(fe)[:200]}",
                                           status=502), request.model)
                            yield b"data: [DONE]\n\n"
                            await _fusion_log_fail([{"attempt": 0, "error": str(fe)[:200]}])
                            return
                        _ctext = ((fresp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                        _chunk = {
                            "id": f"chatcmpl-{conversation_id[:12]}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": request.model,
                            "choices": [{"index": 0,
                                         "delta": {"role": "assistant", "content": _ctext},
                                         "finish_reason": "stop"}],
                        }
                        yield _format_sse_chunk(_chunk, request.model)
                        yield b"data: [DONE]\n\n"
                        await _fusion_log_done(fresp, fmeta)

                    return StreamingResponse(_fusion_sse(), media_type="text/event-stream",
                                             headers={"Cache-Control": "no-cache",
                                                      "X-Accel-Buffering": "no"})
                try:
                    fresp, fmeta = await _fusion_job()
                except FusionAllFailed as fe:
                    await _fusion_log_fail(fe.attempts)
                    return JSONResponse(status_code=502, content=_api_error(
                        "fusion: all candidates failed", status=502, attempts=fe.attempts))
                await _fusion_log_done(fresp, fmeta)
                return JSONResponse(fresp)

            # ─── 流式 combo：统一级联回退（带冷却），与 auto 流式行为一致 ───
            # Race（config.race.enabled）：当前候选 N 秒没有返回实质内容 → 不杀它，
            # 并行再打下一候选；谁先出实质内容用谁，落败候选自动罚冷却。
            # 关闭时 max_inflight=1、无超时 → 完全退化为旧的顺序回退。
            if request.stream:
                _diag(conversation_id, "combo_stream_start", _diag_start, count=len(combo_full_ids))
                from server.core.race import NoMoreCandidates as _NoMore, RaceAllFailed as _RaceAllFailed, run_race as _run_race

                _rc_cfg = getattr(config, "race", None)
                _race_on = bool(_rc_cfg and getattr(_rc_cfg, "enabled", False))
                _race_secs = max(3, int(getattr(_rc_cfg, "no_content_seconds", 15) or 15))

                class _SkipAttempt(Exception):
                    def __init__(self, msg, *, full_id, prov=None, model=None, pk=None):
                        super().__init__(msg)
                        self.info = {"full_id": full_id, "prov": prov, "model": model, "pk": pk}

                class _FailAttempt(Exception):
                    def __init__(self, msg, ctx, *, raw_err=None, stream_body=None):
                        super().__init__(msg)
                        self.ctx = ctx
                        self.raw_err = raw_err
                        self.stream_body = stream_body

                async def _combo_cascade_stream():
                    from server.db import AsyncSessionLocal as _CS
                    cdb = _CS()
                    stream_errs = []
                    ctx_by_idx = {}
                    # max_fallbacks 契约与 auto 对齐：总尝试 = min(候选数, max_fallbacks + 1)
                    max_r = min(len(combo_full_ids), max(1, ar.config.max_fallbacks + 1))
                    yield b": keepalive\n\n"

                    async def _launch(st_attempt: int):
                        if st_attempt >= max_r:
                            raise _NoMore()
                        full_id = combo_full_ids[st_attempt]
                        prov_name, m_id = full_id.split("/", 1) if "/" in full_id else (None, full_id)
                        from server.models.provider import Provider as _P
                        from server.models.model import Model as _M
                        from sqlalchemy import select as _sel
                        p_r = await cdb.execute(_sel(_P).where(_P.name == prov_name).limit(1))
                        _prov = p_r.scalar_one_or_none()
                        # v4.0: 服务商被禁用 → 跳过该候选（不删除组合配置）
                        if _prov is not None and not getattr(_prov, "enabled", True):
                            raise _SkipAttempt(f"provider disabled: {full_id}",
                                               full_id=full_id, prov=prov_name, model=m_id)
                        m_r = await cdb.execute(_sel(_M).where(_M.provider_id == _prov.id, _M.model_id == m_id, _M.enabled == True).limit(1)) if _prov else None
                        _mdl = m_r.scalar_one_or_none() if m_r is not None else None
                        if not _prov or not _mdl:
                            raise _SkipAttempt(f"combo target {full_id} not found",
                                               full_id=full_id, prov=prov_name, model=m_id)
                        # 上下文预检：装不下的候选直接跳过（动态因子 + observed 窗口）
                        _pf = await get_estimate_factor(cdb, _prov.id, _mdl.model_id)
                        _est_adj = int(est_req_tokens * _pf)
                        _obs = int(getattr(_mdl, "observed_context_limit", 0) or 0)
                        if context_overflows(_mdl, _est_adj, observed_limit=_obs):
                            raise _SkipAttempt(
                                f"skip (context window {_mdl.context_length} < est ~{_est_adj} tokens x{_pf:.2f}): {full_id}",
                                full_id=full_id, prov=_prov.name, model=_mdl.model_id, pk=_mdl.id)
                        # 统一凭证解析：free_tier/oauth/atomcode/标准密钥一个入口
                        from server.core.credential_resolver import resolve_credential_async, stream_via
                        _rc = await resolve_credential_async(_prov, _mdl, cdb)
                        if not _rc.ok:
                            raise _SkipAttempt(_rc.error, full_id=full_id,
                                               prov=_prov.name, model=_mdl.model_id, pk=_mdl.id)
                        # 跳过处于冷却（被惩罚）中的 target，避免反复打到坏模型
                        if ar.health_checker and ar.health_checker.is_cooling(_mdl.id):
                            raise _SkipAttempt(f"skipped (cooling) {full_id}", full_id=full_id,
                                               prov=_prov.name, model=_mdl.model_id, pk=_mdl.id)
                        mid_full = f"{_prov.name}/{_mdl.model_id}"
                        _decision_select(conversation_id, provider=_prov.name, model=_mdl.model_id,
                                         model_pk=_mdl.id, reason="next combo target")
                        up_req = _without_unsupported_reasoning(
                            request.model_copy(update={"model": _mdl.model_id}), _mdl)
                        _start = time.time()
                        _diag(conversation_id, "upstream_stream_start", _diag_start,
                              attempt=st_attempt, provider=_prov.name, model=_mdl.model_id)
                        _fb_eh = _merge_oauth_headers(_prov, _prov.headers)
                        _fbmov = getattr(_mdl, "request_overrides", None) or {}
                        if isinstance(_fbmov, dict) and _fbmov.get("headers"):
                            _fb_eh = {**(_fb_eh or {}), **_fbmov["headers"]}
                        # free_tier 走专用 executor（FORCE_PROXY/裸 model_id 由 resolver 处理）
                        if _rc.kind == "free_tier":
                            # P0-1: stream_via 是协程函数，必须 await 拿到异步生成器
                            gen = await stream_via(_rc, up_req, _prov, _mdl)
                        else:
                            gen = _rc.adapter.stream_chat_completion(
                                up_req, _rc.api_key, _prov.base_url, _fb_eh)
                        # ── 实质 chunk 锁定语义：缓冲直到出现实质（正文/思考/工具调用）；
                        #    出现即返回给主协程接管外发；流结束仍无实质 → 判失败回退 ──
                        ctx = {"gen": gen, "buf": [], "committed": False, "usage": {},
                               "ttft_ms": None, "attempt_ttft_ms": None,
                               "prov": _prov, "mdl": _mdl, "mid_full": mid_full,
                               "start": _start, "attempt": st_attempt}
                        ctx_by_idx[st_attempt] = ctx
                        try:
                            async for ck in gen:
                                if ctx["ttft_ms"] is None:
                                    ctx["ttft_ms"] = int((time.time() - _send_time) * 1000)
                                    ctx["attempt_ttft_ms"] = int((time.time() - _start) * 1000)
                                if isinstance(ck, dict) and "error" in ck:
                                    raise RuntimeError(f"upstream_stream_error: {ck.get('error')}")
                                ctx["buf"].append(ck)
                                u = _stream_usage_dict(ck) if isinstance(ck, dict) else {}
                                if u:
                                    ctx["usage"] = u
                                if _chunk_has_substance(ck):
                                    ctx["committed"] = True
                                    return ctx
                            import json as _cj
                            raise _FailAttempt(
                                "empty_stream_output: 上游返回成功但无实质内容", ctx,
                                stream_body=_cj.dumps(ctx["buf"], ensure_ascii=False) if ctx["buf"] else None)
                        except (_SkipAttempt, _FailAttempt, _NoMore):
                            raise
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            raw = _extract_error_body(e) or f"{type(e).__name__}: {str(e)[:200]}"
                            raise _FailAttempt(f"{type(e).__name__}: {str(e)[:200]}", ctx,
                                               raw_err=raw) from e
                        finally:
                            if not ctx["committed"]:
                                try:
                                    await gen.aclose()
                                except Exception:
                                    pass

                    async def _on_fail(st_attempt, et, exc):
                        if isinstance(exc, _SkipAttempt):
                            stream_errs.append({"attempt": st_attempt, "error": et})
                            info = exc.info
                            _decision_skip(conversation_id, model_pk=info.get("pk"),
                                           provider=info.get("prov"), model=info.get("model"),
                                           reason=str(exc)[:120])
                            return
                        ctx = getattr(exc, "ctx", None)
                        if ctx is None:
                            stream_errs.append({"attempt": st_attempt, "error": et})
                            return
                        stream_errs.append({"attempt": st_attempt, "model": ctx["mid_full"], "error": et})
                        _decision_attempt(conversation_id, provider=ctx["prov"].name,
                                          model=ctx["mdl"].model_id, status="failed",
                                          attempt=st_attempt,
                                          latency_ms=int((time.time() - ctx["start"]) * 1000),
                                          ttft_ms=ctx["attempt_ttft_ms"], error=et)
                        # 失败惩罚：与 auto 路由一致 —— 计入失败并进入冷却（指数退避）
                        # 上下文超限 / 上游内容校验失败 均不是模型自身故障，不进冷却
                        if is_context_error(et):
                            await record_context_overflow(ctx["mdl"].id, est_req_tokens)
                        elif ar.health_checker and not _is_stream_content_validation_error(et):
                            ar.health_checker.mark_failure(ctx["mdl"].id)
                            ar.health_checker.mark_cooling(ctx["mdl"].id, ar.config.cooling_period_seconds)
                        raw_err = getattr(exc, "raw_err", None) or getattr(exc, "stream_body", None) or et
                        await _write_stream_log(conversation_id, request, raw_request, "error",
                            ctx["prov"].name, ctx["mdl"].model_id, et[:500], st_attempt, None,
                            stream_body=raw_err, diag_start_ts=_diag_start)
                        print(f"[组合流式] 第{st_attempt + 1}次尝试 服务商={ctx['prov'].name} "
                              f"模型={ctx['mdl'].model_id} 失败：{et[:120]}，正在尝试下一个候选", flush=True)

                    async def _on_loser(st_attempt):
                        # 被更快候选超前的在途尝试：取消已发起，判失败 + 罚冷却
                        ctx = ctx_by_idx.get(st_attempt)
                        if ctx is None or ctx["committed"]:
                            return
                        et = (f"race_overtaken: {_race_secs}s 内无实质内容，"
                              f"被更快候选取代（自动罚时冷却）")
                        stream_errs.append({"attempt": st_attempt, "model": ctx["mid_full"], "error": et})
                        _decision_attempt(conversation_id, provider=ctx["prov"].name,
                                          model=ctx["mdl"].model_id, status="failed",
                                          attempt=st_attempt,
                                          latency_ms=int((time.time() - ctx["start"]) * 1000),
                                          ttft_ms=ctx["attempt_ttft_ms"], error=et)
                        if ar.health_checker:
                            ar.health_checker.mark_failure(ctx["mdl"].id)
                            ar.health_checker.mark_cooling(ctx["mdl"].id, ar.config.cooling_period_seconds)
                        await _write_stream_log(conversation_id, request, raw_request, "error",
                            ctx["prov"].name, ctx["mdl"].model_id, et, st_attempt, None,
                            diag_start_ts=_diag_start)

                    async def _finish_session():
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

                    try:
                        winner_idx, ctx = await _run_race(
                            _launch,
                            no_content_seconds=(_race_secs if _race_on else None),
                            max_inflight=(2 if _race_on else 1),
                            on_failure=_on_fail, on_loser=_on_loser)
                    except _RaceAllFailed:
                        try:
                            from server.core.notifier import notify_event as _notify_event
                            _notify_event("all_failed",
                                          f"组合 '{combo_name}' 全部候选失败（流式，{len(ctx_by_idx)} 次尝试）")
                        except Exception:
                            pass
                        yield _format_sse_chunk(_api_error("combo all targets failed", status=503,
                                                           attempts=stream_errs), "unknown")
                        yield b"data: [DONE]\n\n"
                        await _write_stream_log(conversation_id, request, raw_request, "error",
                            None, None, "combo all targets failed", 0, stream_errs,
                            diag_start_ts=_diag_start)
                        await _finish_session()
                        return

                    # ── 胜出候选接管外发：重放已缓冲 chunk，继续流式消费 ──
                    _prov, _mdl, mid_full = ctx["prov"], ctx["mdl"], ctx["mid_full"]
                    _start = ctx["start"]
                    st_attempt = winner_idx
                    _cb_ttft_ms = ctx["ttft_ms"]
                    _cb_attempt_ttft_ms = ctx["attempt_ttft_ms"]
                    _cbuf = ctx["buf"]
                    _cu = ctx["usage"]
                    _diag(conversation_id, "combo_stream_commit", _diag_start,
                          attempt=winner_idx, provider=_prov.name, model=_mdl.model_id,
                          raced=_race_on)
                    for _ck in _cbuf:
                        yield _format_sse_chunk(_ck, mid_full)
                    try:
                        async for ck in ctx["gen"]:
                            if isinstance(ck, dict) and "error" in ck:
                                raise RuntimeError(f"upstream_stream_error: {ck.get('error')}")
                            _cbuf.append(ck)
                            u = _stream_usage_dict(ck) if isinstance(ck, dict) else {}
                            if u:
                                _cu = u
                            yield _format_sse_chunk(ck, mid_full)
                        # 注意：成功日志必须在 yield [DONE] 之前写（客户端收到 DONE 即断开，
                        # 生成器被取消会吃掉 DONE 之后的 await）。
                        import json as _cj
                        _combo_latency = int((time.time() - _start) * 1000)
                        _combo_body = _cj.dumps(_cbuf, ensure_ascii=False) if _cbuf else None
                        _nu = _normalize_usage(_cu)
                        _pt, _ct = _nu.prompt_tokens, _nu.completion_tokens
                        _crd, _cwt = _nu.cache_read_tokens, _nu.cache_write_tokens
                        _pt, _ct = _sanitize_token_counts(request, _pt, _ct,
                                                         _output_text_from_chunks(_cbuf))
                        _decision_attempt(conversation_id, provider=_prov.name, model=_mdl.model_id,
                                          status="success", attempt=st_attempt,
                                          latency_ms=_combo_latency, ttft_ms=_cb_attempt_ttft_ms)
                        await _write_stream_log(conversation_id, request, raw_request, "success",
                            _prov.name, _mdl.model_id, None, st_attempt, None,
                            stream_body=_combo_body, prompt_tokens=_pt, completion_tokens=_ct,
                            cache_read_tokens=_crd, cache_write_tokens=_cwt,
                            latency_ms=_combo_latency, ttft_ms=_cb_ttft_ms,
                            diag_start_ts=_diag_start,
                            est_prompt_tokens=est_req_tokens)
                        if ar.health_checker:
                            ar.health_checker.mark_success(_mdl.id)
                        yield b"data: [DONE]\n\n"
                    except Exception as se:
                        # 已外发实质内容后中途失败 → 无法无感回退，截断并告知客户端
                        err_s = f"{type(se).__name__}: {str(se)[:200]}"
                        stream_errs.append({"attempt": st_attempt, "model": mid_full, "error": err_s})
                        _decision_attempt(conversation_id, provider=_prov.name, model=_mdl.model_id,
                                          status="failed", attempt=st_attempt,
                                          latency_ms=int((time.time() - _start) * 1000),
                                          ttft_ms=_cb_attempt_ttft_ms, error=err_s)
                        if is_context_error(err_s):
                            await record_context_overflow(_mdl.id, est_req_tokens)
                        elif ar.health_checker and not _is_stream_content_validation_error(err_s):
                            ar.health_checker.mark_failure(_mdl.id)
                            ar.health_checker.mark_cooling(_mdl.id, ar.config.cooling_period_seconds)
                        raw_err = _extract_error_body(se) or err_s
                        yield _format_sse_chunk({"error": f"stream_mid_failure: {err_s}"}, mid_full)
                        yield b"data: [DONE]\n\n"
                        await _write_stream_log(conversation_id, request, raw_request, "error",
                            _prov.name, _mdl.model_id, err_s, st_attempt, stream_errs,
                            stream_body=raw_err, diag_start_ts=_diag_start)
                    finally:
                        await _finish_session()

                return StreamingResponse(_combo_cascade_stream(), media_type="text/event-stream")
            # ─── 非流式 combo：循环尝试（含冷却），不再走 is_auto 级联路径 ───
            combo_attempts = []
            # max_fallbacks 契约与 auto/流式对齐：总尝试 = min(候选数, max_fallbacks + 1)
            _attempt_limit = min(len(ordered_targets), max(1, ar.config.max_fallbacks + 1))
            # Race（config.race.enabled）：N 秒无返回 → 并行打下一候选，先回者胜；败者罚冷却
            from server.core.race import NoMoreCandidates as _NoMore, RaceAllFailed as _RaceAllFailed, run_race as _run_race
            _rc_cfg = getattr(config, "race", None)
            _race_on = bool(_rc_cfg and getattr(_rc_cfg, "enabled", False))
            _race_secs = max(3, int(getattr(_rc_cfg, "no_content_seconds", 15) or 15))

            class _NSkip(Exception):
                def __init__(self, msg, *, pk=None, prov=None, model=None):
                    super().__init__(msg)
                    self.info = {"pk": pk, "prov": prov, "model": model}

            class _NFail(Exception):
                def __init__(self, msg, ctx):
                    super().__init__(msg)
                    self.ctx = ctx

            _ns_ctx_by_idx = {}

            async def _ns_launch(t_idx: int):
                if t_idx >= _attempt_limit:
                    raise _NoMore()
                t = ordered_targets[t_idx]
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
                    raise _NSkip("provider or model not found", prov=prov_name, model=m_id)
                # 上下文预检：装不下的候选直接跳过（动态因子 + observed 窗口）
                _pf = await get_estimate_factor(db, provider.id, model.model_id)
                _est_adj = int(est_req_tokens * _pf)
                _obs = int(getattr(model, "observed_context_limit", 0) or 0)
                if context_overflows(model, _est_adj, observed_limit=_obs):
                    raise _NSkip(f"est ~{_est_adj} tokens (x{_pf:.2f}) > context window {model.context_length}",
                                 pk=model.id, prov=provider.name, model=model.model_id)
                # 跳过处于冷却（被惩罚）中的 target，让后续健康候选顶上
                if ar.health_checker and ar.health_checker.is_cooling(model.id):
                    raise _NSkip("skipped (cooling)", pk=model.id, prov=provider.name, model=model.model_id)
                # 统一凭证解析：free_tier/oauth/atomcode/标准密钥一个入口
                from server.core.credential_resolver import resolve_credential_async, call_via
                _rc = await resolve_credential_async(provider, model, db)
                if not _rc.ok:
                    raise _NSkip(_rc.error, pk=model.id, prov=provider.name, model=model.model_id)
                extra_hdr = _merge_oauth_headers(provider, provider.headers if provider.headers else None)
                if _rc.extra_headers:
                    extra_hdr = {**(extra_hdr or {}), **_rc.extra_headers}
                upstream_req = _without_unsupported_reasoning(
                    request.model_copy(update={"model": model.model_id}), model
                )
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
                ctx = {"t_idx": t_idx, "full_id": full_id, "provider": provider, "model": model,
                       "start": time.time()}
                _ns_ctx_by_idx[t_idx] = ctx
                _decision_select(conversation_id, provider=provider.name, model=model.model_id,
                                 model_pk=model.id, reason="next combo target")
                try:
                    if _rc.kind == "free_tier":
                        result = await call_via(_rc, upstream_req, provider, model)
                    else:
                        result = await _rc.adapter.chat_completion(
                            upstream_req, _rc.api_key, provider.base_url, extra_hdr
                        )
                except asyncio.CancelledError:
                    ctx["cancelled"] = True
                    raise
                except Exception as e:
                    raise _NFail(f"{type(e).__name__}: {str(e)[:200]}", ctx) from e
                ctx["send_end"] = time.time()
                if isinstance(result, dict):
                    result["model"] = f"{provider.name}/{model.model_id}"
                # ── 空输出判定：HTTP 成功但无 content/无 tool_calls/无 completion token
                #    → 视为候选失败，继续 fallback 到下一个候选（combo 专用，不影响直连）──
                if _openai_completion_is_empty(result):
                    raise _NFail("empty_output: upstream 返回 200 但无内容/tool_calls/usage", ctx)
                return result

            async def _ns_on_fail(t_idx, et, exc):
                if isinstance(exc, _NSkip):
                    combo_attempts.append({"target": et, "error": et})
                    info = exc.info
                    if info.get("pk"):
                        _decision_skip(conversation_id, model_pk=info["pk"], provider=info.get("prov"),
                                       model=info.get("model"), reason=str(exc)[:120])
                    else:
                        _decision_skip(conversation_id, provider=info.get("prov"), model=info.get("model"),
                                       reason=str(exc)[:120])
                    return
                ctx = getattr(exc, "ctx", None)
                if ctx is None:
                    combo_attempts.append({"target": f"idx{t_idx}", "error": et})
                    return
                combo_attempts.append({"target": ctx["full_id"], "error": et})
                _decision_attempt(conversation_id, provider=ctx["provider"].name,
                                  model=ctx["model"].model_id, status="failed", attempt=t_idx,
                                  latency_ms=int((time.time() - ctx["start"]) * 1000), error=et)
                # 失败惩罚：与 auto 路由一致 —— 计入失败并进入冷却（指数退避 30×2^n 秒）
                # 上下文超限不是模型自身故障，不进冷却
                if is_context_error(et):
                    await record_context_overflow(ctx["model"].id, est_req_tokens)
                elif ar.health_checker:
                    ar.health_checker.mark_failure(ctx["model"].id)
                    ar.health_checker.mark_cooling(ctx["model"].id, ar.config.cooling_period_seconds)
                print(f"[组合路由] 目标 {ctx['full_id']} 失败：{et[:120]}，正在尝试下一个候选", flush=True)

            async def _ns_on_loser(t_idx):
                ctx = _ns_ctx_by_idx.get(t_idx)
                if ctx is None or ctx.get("send_end"):
                    return  # 其实已返回（只是没赢），不按失败处理
                et = (f"race_overtaken: {_race_secs}s 内无返回，被更快候选取代（自动罚时冷却）")
                combo_attempts.append({"target": ctx["full_id"], "error": et})
                _decision_attempt(conversation_id, provider=ctx["provider"].name,
                                  model=ctx["model"].model_id, status="failed", attempt=t_idx,
                                  latency_ms=int((time.time() - ctx["start"]) * 1000), error=et)
                if ar.health_checker:
                    ar.health_checker.mark_failure(ctx["model"].id)
                    ar.health_checker.mark_cooling(ctx["model"].id, ar.config.cooling_period_seconds)
                print(f"[组合路由] 目标 {ctx['full_id']} 超时未返回，判失败并罚冷却", flush=True)

            try:
                winner_idx, result = await _run_race(
                    _ns_launch,
                    no_content_seconds=(_race_secs if _race_on else None),
                    max_inflight=(2 if _race_on else 1),
                    on_failure=_ns_on_fail, on_loser=_ns_on_loser)
            except _RaceAllFailed:
                # 全部 target 失败
                try:
                    from server.core.notifier import notify_event as _notify_event
                    _notify_event("all_failed", f"组合 '{combo_name}' 全部候选失败（{len(combo_attempts)} 个候选）")
                except Exception:
                    pass
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
                    content=_api_error(f"Combo '{combo_name}' all targets failed", status=503, attempts=combo_attempts),
                )
            # ── 胜出候选：计量、成功日志、返回 ──
            _wctx = _ns_ctx_by_idx.get(winner_idx)
            _w_provider = _wctx["provider"] if _wctx else None
            _w_model = _wctx["model"] if _wctx else None
            _w_start = _wctx["start"] if _wctx else _diag_start
            if ar.health_checker and _w_model is not None:
                ar.health_checker.mark_success(_w_model.id)
            _decision_attempt(conversation_id, provider=_w_provider.name, model=_w_model.model_id,
                              status="success", attempt=winner_idx,
                              latency_ms=int((time.time() - _w_start) * 1000))
            # combo 分支独立于集中日志（964 行），需自行写请求日志
            try:
                import json as _j
                _usage = result.get("usage", {}) if isinstance(result, dict) else {}
                _nu = _normalize_usage(_usage)
                _pt, _ct = _nu.prompt_tokens, _nu.completion_tokens
                _crd, _cwt = _nu.cache_read_tokens, _nu.cache_write_tokens
                _pt, _ct = _sanitize_token_counts(request, _pt, _ct, _output_text_from_result(result))
                await _write_stream_log(
                    conversation_id, request, raw_request, "success",
                    _w_provider.name, _w_model.model_id, None, winner_idx, None,
                    stream_body=_j.dumps(result, ensure_ascii=False),
                    prompt_tokens=_pt,
                    completion_tokens=_ct,
                    cache_read_tokens=_crd, cache_write_tokens=_cwt,
                    latency_ms=int((time.time() - _w_start) * 1000),
                    diag_start_ts=_diag_start,
                )
            except Exception:
                pass
            _diag(conversation_id, "combo_hit", _diag_start,
                  target=_wctx["full_id"] if _wctx else None, raced=_race_on)
            return JSONResponse(content=result)
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
                await _early_error_log(conversation_id, request, raw_request, f"Model {request.model} not found")
                return JSONResponse(status_code=404, content=_api_error(f"Model {request.model} not found", status=404))
            _diag(conversation_id, "direct_model_done", _diag_start, routed_model=model.model_id)
            provider = await db.get(Provider, model.provider_id)
            # v4.0: 服务商被禁用 → 直连同样不可用
            if provider is None or not getattr(provider, "enabled", True):
                await _decision_finish(conversation_id, status="error", failure_reason=f"Provider for model {request.model} is disabled")
                await _early_error_log(conversation_id, request, raw_request, f"Provider for model {request.model} is disabled")
                return JSONResponse(status_code=404, content=_api_error(f"Provider for model {request.model} is disabled", status=404))
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
                        await _early_error_log(conversation_id, request, raw_request, f"OAuth provider '{oauth_code}' not connected")
                        return JSONResponse(status_code=503, content=_api_error(f"OAuth provider '{oauth_code}' not connected (set provider.oauth_code or import token)", status=503))
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
                        select(ApiKey).where(ApiKey.provider_id == provider.id, ApiKey.is_active == True).limit(1)  # noqa: E712
                    )
                    key = result.scalar_one_or_none()
                    if not key:
                        _diag(conversation_id, "direct_key_missing", _diag_start, provider=provider.name)
                        await _decision_finish(conversation_id, status="error", failure_reason=f"No active API key for provider {provider.name}")
                        await _early_error_log(conversation_id, request, raw_request, f"No active API key for provider {provider.name}")
                        return JSONResponse(status_code=503, content=_api_error(f"No active API key for provider {provider.name}", status=503))
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
                                err_data = _api_error(f"free_provider_stream_failed: {e}")
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
                            await _early_error_log(conversation_id, request, raw_request, f"free_provider_failed: {e}", err_type="upstream_error")
                            return JSONResponse(status_code=502, content=_api_error(f"free_provider_failed: {e}", status=502))
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
        # 流式级联回退：拉流，无内容自动换下一候选。
        # Race（config.race.enabled）：当前候选 N 秒没吐内容 → 不杀它，并行打下一候选，
        # 谁先出实质内容用谁；被超前的候选判失败+罚冷却。关闭时退化为顺序回退（旧语义）。
        from types import SimpleNamespace as _NS
        from server.core.race import NoMoreCandidates as _NoMore, RaceAllFailed as _RaceAllFailed, run_race as _run_race
        _rc_cfg = getattr(config, "race", None)
        _race_on = bool(_rc_cfg and getattr(_rc_cfg, "enabled", False))
        _race_secs = max(3, int(getattr(_rc_cfg, "no_content_seconds", 15) or 15))

        class _ASkip(Exception):
            pass

        class _AFail(Exception):
            def __init__(self, msg, ctx, raw=None):
                super().__init__(msg)
                self.ac = ctx
                self.raw = raw

        async def cascade_stream():
            _diag(conversation_id, "auto_stream_generator_start", _diag_start)
            from server.db import AsyncSessionLocal as _CS
            cascade_db = _CS()
            _db_lock = asyncio.Lock()  # session 不可并发：DB 短操作串行化，HTTP 在锁外
            max_r = max(1, ar.config.max_fallbacks)
            first_chunk_timeout = max(5, int(getattr(ar.config, "stream_first_chunk_timeout_seconds", 20)))
            first_response_budget = max(first_chunk_timeout, int(getattr(ar.config, "stream_first_response_budget_seconds", 75)))
            first_response_deadline = time.monotonic() + first_response_budget
            tried_sids = set()
            stream_errs = []
            ctx_by_idx = {}
            # v3.0: combo 路由会用自定义候选池替代 ar.get_best_candidate
            import server.api.v1_router as _vr_mod
            combo_pool = getattr(_vr_mod, '_combo_targets_map', {}).get(conversation_id, [])
            # 立即发一个 SSE 注释块，防客户端超时
            yield b": keepalive\n\n"

            async def _launch(st_attempt: int):
                remaining_first_response = first_response_deadline - time.monotonic()
                if remaining_first_response <= 0 or st_attempt > max_r:
                    stream_errs.append({
                        "attempt": st_attempt,
                        "error": f"first response budget exceeded ({first_response_budget}s)"
                    })
                    raise _NoMore()
                _diag(conversation_id, "auto_stream_candidate_start", _diag_start, attempt=st_attempt)
                async with _db_lock:
                    if combo_pool:
                        # combo 路径：按池子顺序取下一个未试目标
                        if st_attempt >= len(combo_pool):
                            raise _NoMore()
                        full_id = combo_pool[st_attempt]
                        prov_name, m_id = full_id.split("/", 1) if "/" in full_id else (None, full_id)
                        from server.models.provider import Provider as _P
                        from server.models.model import Model as _M
                        from sqlalchemy import select as _sel
                        p_r = await cascade_db.execute(_sel(_P).where(_P.name == prov_name).limit(1))
                        _prov = p_r.scalar_one_or_none()
                        m_r = await cascade_db.execute(_sel(_M).where(_M.provider_id == _prov.id, _M.model_id == m_id, _M.enabled == True).limit(1)) if _prov else None
                        _mdl = m_r.scalar_one_or_none() if m_r is not None else None
                        if not _prov or not _mdl:
                            stream_errs.append({"attempt": st_attempt, "error": f"combo target {full_id} not found"})
                            _decision_skip(conversation_id, provider=prov_name, model=m_id,
                                           reason="provider or model not found")
                            raise _ASkip("target not found")
                        _prov_obj, _mdl_obj = _prov, _mdl
                        _sel_reason = None
                    else:
                        cand = await ar.get_best_candidate(cascade_db, conversation_id, exclude_model_ids=tried_sids)
                        _diag(conversation_id, "auto_stream_candidate_done", _diag_start,
                              attempt=st_attempt, success=cand.success if cand else None)
                        if not cand.success or (cand.model and cand.model.id in tried_sids):
                            raise _NoMore()
                        _prov_obj, _mdl_obj = cand.provider, cand.model
                        _sel_reason = getattr(cand, "selection_reason", None)
                    try:
                        await cascade_db.refresh(_prov_obj, attribute_names=["name", "base_url", "headers", "credential_type", "oauth_code", "proxy_enabled"])
                        await cascade_db.refresh(_mdl_obj, attribute_names=["id", "model_id", "request_overrides", "context_length", "supports_reasoning_effort", "observed_context_limit"])
                    except Exception:
                        pass
                    _mdl_ns = _NS(model_id=_mdl_obj.model_id, id=_mdl_obj.id,
                                  supports_reasoning_effort=getattr(_mdl_obj, "supports_reasoning_effort", None),
                                  context_length=int(getattr(_mdl_obj, "context_length", 0) or 0),
                                  observed_context_limit=int(getattr(_mdl_obj, "observed_context_limit", 0) or 0),
                                  request_overrides=getattr(_mdl_obj, "request_overrides", None))
                    _prov_ns = _NS(name=_prov_obj.name, base_url=_prov_obj.base_url,
                                   headers=_prov_obj.headers, id=_prov_obj.id,
                                   credential_type=getattr(_prov_obj, "credential_type", "api_key"),
                                   api_type=getattr(_prov_obj, "api_type", ""),
                                   oauth_code=getattr(_prov_obj, "oauth_code", None),
                                   proxy_enabled=getattr(_prov_obj, "proxy_enabled", False))
                    if _mdl_ns.id and _mdl_ns.id in tried_sids:
                        raise _NoMore()
                    if _mdl_ns.id:
                        tried_sids.add(_mdl_ns.id)
                    mid_full = f"{_prov_ns.name}/{_mdl_ns.model_id}"
                    # 上下文预检：装不下的候选直接跳过（不打上游、不进冷却；动态因子 + observed 窗口）
                    _pf = await get_estimate_factor(cascade_db, _prov_ns.id, _mdl_ns.model_id)
                    _est_adj = int(est_req_tokens * _pf)
                    if ((_mdl_ns.context_length > 0 and _est_adj + 1024 > _mdl_ns.context_length)
                            or (_mdl_ns.observed_context_limit > 0 and _est_adj + 1024 > _mdl_ns.observed_context_limit)):
                        stream_errs.append({"attempt": st_attempt,
                                            "error": f"skip (context window {_mdl_ns.context_length} < est ~{_est_adj} tokens x{_pf:.2f})"})
                        _decision_skip(conversation_id, model_pk=_mdl_ns.id, provider=_prov_ns.name,
                                       model=_mdl_ns.model_id, reason="context window too small")
                        raise _ASkip("context window too small")
                    up_req = _without_unsupported_reasoning(
                        request.model_copy(update={"model": _mdl_ns.model_id}), _mdl_ns
                    )
                    _decision_select(conversation_id, provider=_prov_ns.name, model=_mdl_ns.model_id,
                                     model_pk=_mdl_ns.id, reason=_sel_reason or "next ranked candidate")
                    # 统一凭证解析（锁内完成；oauth 刷新等 DB 操作也收敛于此）
                    from server.core.credential_resolver import resolve_credential_async
                    _rc0 = await resolve_credential_async(_prov_ns, _mdl_ns, cascade_db)
                    if not _rc0.ok:
                        stream_errs.append({"attempt": st_attempt, "model": mid_full, "error": _rc0.error})
                        _decision_skip(conversation_id, model_pk=_mdl_ns.id, provider=_prov_ns.name,
                                       model=_mdl_ns.model_id, reason=_rc0.error)
                        raise _ASkip(_rc0.error)
                    ctx = {"attempt": st_attempt, "full_id": mid_full, "prov": _prov_ns, "mdl": _mdl_ns,
                           "rc": _rc0, "up_req": up_req,
                           "is_free_channel": _rc0.kind == "free_tier",
                           "start": time.time(), "gen": None, "buf": [], "committed": False,
                           "usage": {}, "ttft_ms": None, "attempt_ttft_ms": None,
                           "first_chunk_timeout": max(5.0, min(first_chunk_timeout, remaining_first_response))}
                    ctx_by_idx[st_attempt] = ctx
                # HTTP 在锁外
                _diag(conversation_id, "upstream_stream_start", _diag_start, attempt=st_attempt,
                      provider=_prov_ns.name, model=_mdl_ns.model_id)
                _cascade_eh = _merge_oauth_headers(_prov_ns, _prov_ns.headers)
                _cmov = _mdl_ns.request_overrides or {}
                if isinstance(_cmov, dict) and _cmov.get("headers"):
                    _cascade_eh = {**(_cascade_eh or {}), **_cmov["headers"]}
                gen = None
                try:
                    if ctx["is_free_channel"]:
                        from server.core.credential_resolver import stream_via
                        # P0-1: stream_via 是协程函数，必须 await 拿到异步生成器
                        gen = await stream_via(_rc0, up_req, _prov_ns, _mdl_ns)
                    elif ctx["rc"].kind in ("oauth", "atomcode"):
                        gen = ctx["rc"].adapter.stream_chat_completion(
                            up_req, ctx["rc"].api_key, _prov_ns.base_url,
                            {**(_cascade_eh or {}), **(ctx["rc"].extra_headers or {})}
                        )
                    else:
                        gen = ctx["rc"].adapter.stream_chat_completion(
                            up_req, ctx["rc"].api_key, _prov_ns.base_url, _cascade_eh
                        )
                    ctx["gen"] = gen
                    async for ck in _stream_with_first_chunk_timeout(gen, ctx["first_chunk_timeout"]):
                        if ctx["ttft_ms"] is None:
                            ctx["ttft_ms"] = int((time.time() - _send_time) * 1000)
                            ctx["attempt_ttft_ms"] = int((time.time() - ctx["start"]) * 1000)
                        if isinstance(ck, dict) and "error" in ck:
                            raise RuntimeError(f"upstream_stream_error: {str(ck.get('error', 'unknown'))[:200]}")
                        ctx["buf"].append(ck)
                        u = _stream_usage_dict(ck)
                        if u:
                            ctx["usage"] = u
                        if _chunk_has_substance(ck):
                            ctx["committed"] = True
                            return ctx
                    import json as _cjs
                    raise _AFail("empty_stream_output: 上游返回成功但无内容", ctx,
                                 raw=_cjs.dumps(ctx["buf"], ensure_ascii=False) if ctx["buf"] else None)
                except (_ASkip, _AFail, _NoMore):
                    raise
                except asyncio.CancelledError:
                    ctx["cancelled"] = True
                    raise
                except Exception as e:
                    raise _AFail(f"{type(e).__name__}: {str(e)[:200]}", ctx,
                                 raw=_extract_error_body(e)) from e
                finally:
                    if not ctx["committed"] and ctx["gen"] is not None:
                        try:
                            await ctx["gen"].aclose()
                        except Exception:
                            pass

            async def _record_fail(st_attempt, ctx, et, raw_body, cooling=True):
                stream_errs.append({"attempt": st_attempt, "model": ctx["full_id"], "error": et})
                _decision_attempt(conversation_id, provider=ctx["prov"].name, model=ctx["mdl"].model_id,
                                  status="failed", attempt=st_attempt,
                                  latency_ms=int((time.time() - ctx["start"]) * 1000),
                                  ttft_ms=ctx["attempt_ttft_ms"], error=et)
                cd_seconds = ar.config.cooling_period_seconds
                fc = (ar.health_checker._fail_count.get(ctx["mdl"].id, 0) if (ar.health_checker and ctx["mdl"].id) else 0)
                cd_actual = min(cd_seconds * (2 ** max(fc - 1, 0)), 3600) if fc > 1 else cd_seconds
                err_annotated = f"{et} | cooldown={cd_actual}s fail#{fc}"
                await _write_stream_log(conversation_id, request, raw_request, "error",
                    ctx["prov"].name, ctx["mdl"].model_id, err_annotated, st_attempt, None,
                    stream_body=raw_body or et, diag_start_ts=_diag_start)
                if cooling and ar.health_checker and not _is_stream_content_validation_error(et) and ctx["mdl"].id:
                    ar.health_checker.mark_failure(ctx["mdl"].id)
                    ar.health_checker.mark_cooling(ctx["mdl"].id, ar.config.cooling_period_seconds)
                print(f"[CASCADE] attempt {st_attempt} failed ({et[:60]}), trying next (tried={tried_sids})", flush=True)

            async def _on_fail(st_attempt, et, exc):
                if isinstance(exc, _ASkip):
                    return  # 预检跳过已在 launch 内记录
                ctx = getattr(exc, "ac", None) or ctx_by_idx.get(st_attempt)
                if ctx is None:
                    stream_errs.append({"attempt": st_attempt, "error": et})
                    return
                # 上下文超限不是模型自身故障：记录但不冷却
                if is_context_error(et):
                    await record_context_overflow(ctx["mdl"].id, est_req_tokens)
                    await _record_fail(st_attempt, ctx, et, getattr(exc, "raw", None), cooling=False)
                    return
                await _record_fail(st_attempt, ctx, et, getattr(exc, "raw", None))

            async def _on_loser(st_attempt):
                ctx = ctx_by_idx.get(st_attempt)
                if ctx is None or ctx.get("committed"):
                    return
                et = (f"race_overtaken: {_race_secs}s 内无实质内容，被更快候选取代（自动罚时冷却）")
                await _record_fail(st_attempt, ctx, et, et)

            async def _finish_session():
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

            try:
                winner_idx, ctx = await _run_race(
                    _launch,
                    no_content_seconds=(_race_secs if _race_on else None),
                    max_inflight=(2 if _race_on else 1),
                    on_failure=_on_fail, on_loser=_on_loser)
            except _RaceAllFailed:
                yield _format_sse_chunk(_api_error("no more candidates", status=503,
                                                   attempts=stream_errs), "unknown")
                yield b"data: [DONE]\n\n"
                await _write_stream_log(conversation_id, request, raw_request, "error",
                    None, None, "no more candidates", 0, stream_errs, diag_start_ts=_diag_start)
                await _finish_session()
                return

            # ── 胜出候选接管外发：重放缓冲 → 继续消费 → 收尾日志 ──
            mid_full = ctx["full_id"]
            try:
                for _ck in ctx["buf"]:
                    yield _format_sse_chunk(_ck, mid_full)
                async for ck in ctx["gen"]:
                    if isinstance(ck, dict) and "error" in ck:
                        raise RuntimeError(f"upstream_stream_error: {str(ck.get('error', 'unknown'))[:200]}")
                    ctx["buf"].append(ck)
                    u = _stream_usage_dict(ck)
                    if u:
                        ctx["usage"] = u
                    yield _format_sse_chunk(ck, mid_full)
                import json as _json_mod
                _diag(conversation_id, "upstream_stream_done", _diag_start, attempt=winner_idx,
                      provider=ctx["prov"].name, model=ctx["mdl"].model_id, chunks=len(ctx["buf"]))
                resp_snapshot = _json_mod.dumps(ctx["buf"], ensure_ascii=False) if ctx["buf"] else None
                _nu = _normalize_usage(ctx["usage"])
                pt, ct = _nu.prompt_tokens, _nu.completion_tokens
                _crd, _cwt = _nu.cache_read_tokens, _nu.cache_write_tokens
                _pt, _ct = _sanitize_token_counts(request, pt, ct, _output_text_from_chunks(ctx["buf"]))
                _decision_attempt(conversation_id, provider=ctx["prov"].name, model=ctx["mdl"].model_id,
                                  status="success", attempt=winner_idx,
                                  latency_ms=int((time.time() - ctx["start"]) * 1000),
                                  ttft_ms=ctx["attempt_ttft_ms"])
                await _write_stream_log(conversation_id, request, raw_request, "success",
                    ctx["prov"].name, ctx["mdl"].model_id, None, winner_idx, None,
                    stream_body=resp_snapshot, prompt_tokens=_pt, completion_tokens=_ct,
                    cache_read_tokens=_crd, cache_write_tokens=_cwt,
                    latency_ms=int((time.time() - _send_time) * 1000),
                    ttft_ms=ctx["ttft_ms"], diag_start_ts=_diag_start,
                    est_prompt_tokens=est_req_tokens)
                if ar.health_checker and ctx["mdl"].id:
                    ar.health_checker.mark_success(ctx["mdl"].id)
                yield b"data: [DONE]\n\n"
            except Exception as se:
                # 已外发实质内容后中途失败 → 无法无感回退，截断并告知客户端
                _diag(conversation_id, "upstream_stream_error", _diag_start, attempt=winner_idx,
                      provider=ctx["prov"].name, model=ctx["mdl"].model_id, error=type(se).__name__)
                err_s = f"{type(se).__name__}: {str(se)[:200]}"
                if is_context_error(err_s):
                    await record_context_overflow(ctx["mdl"].id, est_req_tokens)
                    await _record_fail(winner_idx, ctx, err_s, _extract_error_body(se) or err_s, cooling=False)
                else:
                    await _record_fail(winner_idx, ctx, err_s, _extract_error_body(se) or err_s)
                yield _format_sse_chunk({"error": f"stream_mid_failure: {err_s}"}, mid_full)
                yield b"data: [DONE]\n\n"
            finally:
                if ctx["gen"] is not None:
                    try:
                        await ctx["gen"].aclose()
                    except Exception:
                        pass
                await _finish_session()

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
                        user_ip=_real_client_ip(raw_request, getattr(config.security, 'trust_proxy_headers', False)),
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
                content=_api_error(str(response.get("error") or "all_candidates_failed"), status=503, attempts=_attempt_errors),
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
        # 路由完成即刻快照身份与价格（纯标量）：流式收尾时 ORM 对象可能已过期，
        # 届时再读会拿不到服务商/模型身份（日志行 routed_provider 变空）
        try:
            _rt_prov_name = route_result.provider.name
            _rt_model_id = route_result.model.model_id
            _rt_in_price = float(getattr(route_result.model, "input_price", 0) or 0)
            _rt_out_price = float(getattr(route_result.model, "output_price", 0) or 0)
            _rt_cr_price = float(getattr(route_result.model, "cache_read_input_price", 0) or 0)
            _rt_cw_price = float(getattr(route_result.model, "cache_write_input_price", 0) or 0)
        except Exception:
            _rt_prov_name = _rt_model_id = None
            _rt_in_price = _rt_out_price = _rt_cr_price = _rt_cw_price = 0.0
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
                                _retry_now = (
                                    _attempt_no == 0
                                    and _stream_content_is_empty(_stream_chunks_log)
                                    and (_is_stream_content_validation_error(stream_err_detail)
                                         or _is_transient_upstream_error(stream_err_detail))
                                )
                                if _retry_now:
                                    if _is_transient_upstream_error(stream_err_detail):
                                        await asyncio.sleep(1.0)  # 429/503 类瞬态故障稍候再试
                                    continue
                                raise RuntimeError(f"upstream_stream_error: {stream_err_detail}")
                            _diag(conversation_id, "upstream_stream_done", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, chunks=len(_stream_chunks_log))
                            yield b"data: [DONE]\n\n"
                            return
                        except Exception as e:
                            _diag(conversation_id, "upstream_stream_error", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, error=type(e).__name__)
                            _err_text = f"{type(e).__name__}: {str(e)[:200]}"
                            _retry_now = (
                                _attempt_no == 0
                                and _stream_content_is_empty(_stream_chunks_log)
                                and (_is_stream_content_validation_error(_err_text)
                                     or _is_transient_upstream_error(_err_text))
                            )
                            if _retry_now:
                                if _is_transient_upstream_error(_err_text):
                                    await asyncio.sleep(1.0)  # 429/503 类瞬态故障稍候再试
                                continue
                            _stream_err = _err_text
                            yield _format_sse_chunk(
                                _api_error(f"upstream_stream_failed: {_stream_err}"), model_id_full)
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
                    _nu = _normalize_usage(_stream_usage)
                    pt, ct = _nu.prompt_tokens, _nu.completion_tokens
                    _crd, _cwt = _nu.cache_read_tokens, _nu.cache_write_tokens
                    # 上游漏报 usage 时按实际输出文本粗估 completion（prompt 由 sanitize 兜底）
                    pt, ct = _sanitize_token_counts(request, pt, ct, _output_text_from_chunks(_stream_chunks_log))
                    # ORM 属性在长请求 + session churn 后可能过期（MissingGreenlet/Detached）——
                    # 先做安全快照，参数求值绝不能在 write_log 之前炸掉（否则日志静默丢失）
                    def _snap_attr(obj, name, default=None):
                        try:
                            return getattr(obj, name, default)
                        except Exception:
                            return default
                    _prov_name = _rt_prov_name or (_snap_attr(route_result.provider, "name") if route_result else None)
                    _mdl_name = _rt_model_id or (_snap_attr(route_result.model, "model_id") if route_result else None)
                    _in_price = _rt_in_price or (_snap_attr(route_result.model, "input_price", 0) if route_result else 0)
                    _out_price = _rt_out_price or (_snap_attr(route_result.model, "output_price", 0) if route_result else 0)
                    _cr_price = _rt_cr_price or (_snap_attr(route_result.model, "cache_read_input_price", 0) if route_result else 0)
                    _cw_price = _rt_cw_price or (_snap_attr(route_result.model, "cache_write_input_price", 0) if route_result else 0)
                    _fb_count = _snap_attr(route_result, "fallback_count", 0) if route_result else 0
                    _upstream_body = None
                    try:
                        _upstream_body = _j.dumps(upstream_request.model_dump(), ensure_ascii=False) if upstream_request else None
                    except Exception:
                        _upstream_body = None
                    try:
                        _decision_attempt(
                            conversation_id,
                            provider=_prov_name,
                            model=_mdl_name,
                            status="failed" if _stream_err else "success",
                            attempt=0,
                            latency_ms=int((time.time() - _send_time) * 1000),
                            ttft_ms=_stream_ttft_ms,
                            error=_stream_err,
                        )
                    except Exception as e:
                        print(f"⚠️ 路由决策记录失败 conv={str(conversation_id)[:8]}: {type(e).__name__}: {str(e)[:150]}", flush=True)
                    # 决策收尾已移到独立的 shielded 调用（原来混在本 try 里，决策一挂日志就丢）
                    _diag(conversation_id, "stream_log_start", _diag_start, provider=_prov_name, model=_mdl_name, status="error" if _stream_err else "success")
                    from server.db import AsyncSessionLocal as _LS
                    from server.models.request_log import RequestLog as _RL
                    try:
                        async with _LS() as _ldb:
                            await write_log(_ldb,
                                conversation_id=conversation_id,
                                requested_model=request.model,
                                routed_provider=_prov_name,
                                routed_model=_mdl_name,
                                status="error" if _stream_err else "success",
                                prompt_tokens=pt,
                                completion_tokens=ct,
                                cache_read_tokens=_crd or None,
                                cache_write_tokens=_cwt or None,
                                estimated_cost_usd=(
                                    _segmented_cost({
                                        "input_price": float(_in_price or 0),
                                        "output_price": float(_out_price or 0),
                                        "cache_read_input_price": float(_cr_price or 0),
                                        "cache_write_input_price": float(_cw_price or 0),
                                    }, pt, ct, _crd, _cwt)
                                    if (not _stream_err and route_result and route_result.success and (pt or ct or _crd or _cwt)) else 0.0
                                ),
                                fallback_count=_fb_count,
                                latency_ms=int((time.time() - _send_time) * 1000),
                                ttft_ms=_stream_ttft_ms,
                                error_type="upstream_error" if _stream_err else None,
                                error_msg=(_stream_err or ""),
                                request_body=_upstream_body,
                                response_body=resp_snapshot,
                                **_proxy_log_fields(),
                            )
                        _diag(conversation_id, "stream_log_done", _diag_start, provider=_prov_name, model=_mdl_name, status="error" if _stream_err else "success")
                    except Exception as e:
                        # 失败原因必须可见（否则待响应行永远无法收尾）
                        import traceback as _tb
                        print(f"⚠️ 直连流式日志写入失败 conv={str(conversation_id)[:8]}: {type(e).__name__}: {str(e)[:300]}", flush=True)
                        _tb.print_exc()
                        _diag(conversation_id, "stream_log_error", _diag_start, provider=_prov_name, model=_mdl_name, status="error" if _stream_err else "success")
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
                _same_key_retried = False   # 瞬态故障（429/5xx/断连）允许同一把 key 原样重试一次
                _force_same_key = False
                _cur_kid, _cur_key = route_result.key_id, route_result.api_key
                for _attempt in range(8):
                    _direct_attempt_started = time.time()
                    if _attempt > 0:
                        if _force_same_key:
                            _force_same_key = False  # 同 key 原样重试（不动 key 池）
                        else:
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
                        # P1-3: httpx.HTTPStatusError 的状态码在 .response.status_code，
                        # getattr(e,"status_code") 恒 None → 401/403 硬熔断永不生效。
                        _e_status = getattr(e, "status_code", None)
                        if _e_status is None:
                            _e_status = getattr(getattr(e, "response", None), "status_code", None)
                        _rot.mark_failure(_cur_kid, _e_status)
                        _diag(conversation_id, "upstream_error", _diag_start, provider=route_result.provider.name, model=route_result.model.model_id, stream=False, error=type(e).__name__)
                        if not _same_key_retried and _is_transient_upstream_error(f"{type(e).__name__}: {str(e)[:200]}"):
                            _same_key_retried = True
                            _force_same_key = True
                            await asyncio.sleep(1.0)  # 瞬态故障稍候原样重试
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
                response = _api_error(f"upstream_call_failed: {type(e).__name__}: {str(e)[:200]}", status=503)
                response["_raw_response"] = raw_err
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
            _nu = _normalize_usage(usage)
            pt, ct = _nu.prompt_tokens, _nu.completion_tokens
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
            # P0-3: 日志入队后台批量落库，不再阻塞响应返回
            from server.core.log_queue import enqueue_log
            await enqueue_log(
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
                user_ip=_real_client_ip(raw_request, getattr(config.security, 'trust_proxy_headers', False)),
                error_type="upstream_error" if is_err else None,
                error_msg=str(response.get("error", "")) if is_err else None,
                est_prompt_tokens=est_req_tokens,
                request_body=req_body_str,
                response_body=resp_body_str,
                **_proxy_log_fields(),
            )
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
    raw_request: Request = None,
    db: AsyncSession = Depends(get_db),
    include_effort: bool = False,
):
    """OpenAI 兼容 models 端点。
    A2: 组合路由作为伪模型（id=combo:名称）一并列出，客户端下拉框可直接选用；
    include_effort=true 时为支持思考强度的模型追加 -minimal/-low/-medium/-high/-xhigh
    后缀变体（后缀语义与请求入口的思考强度后缀一致）。

    combo 前缀路由（/combo:<名称或id>/v1/models）：只列该组合的全部候选模型。"""
    _scope_ref = getattr(getattr(raw_request, "state", None), "combo_scope", None)
    if _scope_ref:
        from server.core.combo_router import find_combo_by_ref, resolve_combo_targets
        _combo = await find_combo_by_ref(db, _scope_ref)
        if _combo is None:
            return JSONResponse(status_code=404,
                                content=_api_error(f"Combo '{_scope_ref}' not found", status=404))
        _targets = await resolve_combo_targets(db, _combo)
        _now = int(time.time())
        _data = []
        for t in _targets:
            _m = t["model"]
            _p = t["provider"]
            _data.append({
                "id": t["full_id"],
                "object": "model",
                "created": int(_m.created_at.timestamp()) if _m.created_at else _now,
                "owned_by": _p.name,
                "pricing": {
                    "input": _m.input_price,
                    "output": _m.output_price,
                    "cache_read_input": getattr(_m, "cache_read_input_price", 0) or 0,
                    "cache_write_input": getattr(_m, "cache_write_input_price", 0) or 0,
                    "unit": "per_1M_tokens", "currency": "USD",
                },
                "is_free": _m.is_free,
                "auto_enabled": _m.auto_enabled,
                "aigate_combo": _combo.name,
                "capabilities": {
                    "streaming": _m.supports_streaming,
                    "vision": _m.supports_vision,
                    "reasoning_effort": getattr(_m, "supports_reasoning_effort", None),
                    "context_length": _m.context_length,
                },
            })
        return {"object": "list", "data": _data}
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
    # A2: 组合路由伪模型（combo:名称 与请求入口的解析约定一致）
    from server.models.combo import Combo
    combos = (await db.execute(
        select(Combo).where(Combo.enabled.is_(True)).order_by(Combo.name)
    )).scalars().all()
    for c in combos:
        data.append({
            "id": f"combo:{c.name}",
            "object": "model",
            "created": int(c.created_at.timestamp()) if c.created_at else int(time.time()),
            "owned_by": "combo",
            "pricing": {"input": 0, "output": 0, "cache_read_input": 0,
                        "cache_write_input": 0, "unit": "per_1M_tokens", "currency": "USD"},
            "is_free": False,
            "auto_enabled": False,
            "aigate_combo": True,
            "strategy": getattr(c, "strategy", "fallback"),
            "capabilities": {"streaming": True, "vision": None,
                             "reasoning_effort": None, "context_length": None},
        })
    # E1: 别名作为可直接选用的模型名暴露
    from server.models.model_alias import ModelAlias
    aliases = (await db.execute(
        select(ModelAlias).where(ModelAlias.enabled.is_(True)).order_by(ModelAlias.alias)
    )).scalars().all()
    for a in aliases:
        data.append({
            "id": a.alias,
            "object": "model",
            "created": int(a.created_at.timestamp()) if a.created_at else int(time.time()),
            "owned_by": "alias",
            "aigate_alias_of": a.target,
            "pricing": {"input": 0, "output": 0, "cache_read_input": 0,
                        "cache_write_input": 0, "unit": "per_1M_tokens", "currency": "USD"},
            "is_free": False,
            "auto_enabled": False,
            "capabilities": {"streaming": True, "vision": None,
                             "reasoning_effort": None, "context_length": None},
        })
    if include_effort:
        extra = []
        for entry in list(data):
            if entry.get("aigate_combo"):
                continue
            if (entry.get("capabilities") or {}).get("reasoning_effort"):
                for level in ("minimal", "low", "medium", "high", "xhigh"):
                    extra.append({**entry, "id": f"{entry['id']}-{level}",
                                  "aigate_effort_variant": level})
        data.extend(extra)
    return {
        "object": "list",
        "data": data
    }
