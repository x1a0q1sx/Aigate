"""
/v1/* OpenAI 兼容端点
"""
import json
import time
from typing import Optional
import urllib.parse
from fastapi import APIRouter, Depends, Request, Response, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from server.schemas.chat import ChatCompletionRequest, ChatCompletionResponse
from server.models.model import Model
from server.models.provider import Provider
from server.models.api_key import ApiKey
from server.db import AsyncSessionLocal
from server.core.auto_router import AutoRouter
from server.core.request_logger import RequestLogger
from server.core.model_catalog import ModelCatalog
from server.core.auto_router import RouteResult
from server.config import get_config
config = get_config()
router = APIRouter(prefix="/v1")
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

def verify_aigate_api_key(raw_request: Request):
    expected = getattr(config.security, "aigate_api_key", "") or ""
    if not expected:
        return
    auth = raw_request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing AIGate API key")
    token = auth.split(" ", 1)[1].strip()
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

def _preprocess_request(req):
    """轻量截断超长 system message（保底）"""
    msgs = getattr(req, 'messages', None) or []
    for i, m in enumerate(msgs):
        if hasattr(m, 'role') and m.role == 'system' and hasattr(m, 'content') and m.content:
            if isinstance(m.content, str) and len(m.content) > _MAX_SYSTEM_CHARS:
                msgs[i] = m.model_copy(update={"content": m.content[:_MAX_SYSTEM_CHARS] + "\n...(truncated by AIGate)"})
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
                candidate.provider.headers,
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
            if ar.health_checker:
                ar.health_checker.mark_cooling(
                    candidate.model.id,
                    ar.config.cooling_period_seconds,
                )
            continue
    return last_result, attempt_errors


async def _auto_request_with_cascade_fallback(ar, db, request, conversation_id):
    """
    级联回退：对第一个 auto 候选发起完整业务请求，
    若超时/错误/空返回 → 自动尝试第二个、第三个……
    直到成功或全部失败。
    返回 (RouteResult, response_dict, list_of_attempts)
    """
    max_retries = max(1, ar.config.max_fallbacks)
    attempt_errors = []
    tried_ids = set()
    last_result = None
    for attempt in range(max_retries + 1):
        candidate = await ar.get_best_candidate(db, conversation_id, exclude_model_ids=tried_ids)
        last_result = candidate
        if not candidate.success:
            print(f"[CASCADE-NONSTREAM] exhausted at attempt {attempt}: {candidate.error} tried={tried_ids}", flush=True)
            attempt_errors.append({"attempt": attempt, "error": candidate.error})
            break
        if candidate.model and candidate.model.id in tried_ids:
            attempt_errors.append({"attempt": attempt, "error": "duplicate candidate, no more options"})
            break
        tried_ids.add(candidate.model.id)
        # 发起完整业务请求（非探测）
        upstream_request = request.model_copy(update={"model": candidate.model.model_id})
        extra_headers = candidate.provider.headers
        try:
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
            return route, result, attempt_errors
        except Exception as e:
            err_short = f"{type(e).__name__}: {str(e)[:200]}"
            attempt_errors.append({
                "attempt": attempt,
                "model": f"{candidate.provider.name}/{candidate.model.model_id}",
                "error": err_short,
            })
            if ar.health_checker:
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
                async with _LS() as _ldb:
                    _ldb.add(_RL(
                        conversation_id=conversation_id,
                        requested_model=request.model if request else "unknown",
                        routed_provider=candidate.provider.name,
                        routed_model=candidate.model.model_id,
                        status="error",
                        error_type="upstream_error",
                        error_msg=err_short + cooldown_note,
                        fallback_count=attempt,
                        request_body=_j.dumps(request.model_dump(), ensure_ascii=False) if request else None,
                        response_body=_extract_error_body(e) or err_short,
                    ))
                    await _ldb.commit()
            except Exception:
                pass
            print(f"[CASCADE-NONSTREAM] attempt {attempt} failed, trying next (tried={tried_ids})", flush=True)
            continue
    # 全部失败
    print(f"[CASCADE-NONSTREAM] all {max_retries+1} attempts exhausted", flush=True)
    failed = RouteResult(success=False, error=last_result.error if last_result else "no candidates available")
    return failed, {"error": failed.error, "attempts": attempt_errors}, attempt_errors


async def _write_stream_log(conversation_id, request, raw_request, status,
                           routed_provider, routed_model, error_msg, fallback_count, attempt_errors,
                           stream_body=None, prompt_tokens=0, completion_tokens=0, latency_ms=None):
    """在流式生成器内异步写请求日志"""
    try:
        import json as _json_mod
        from server.db import AsyncSessionLocal as _LogSession
        from server.models.request_log import RequestLog as _RL
        req_s = _json_mod.dumps(request.model_dump(), ensure_ascii=False) if request else None
        async with _LogSession() as _ldb:
            _ldb.add(_RL(
                conversation_id=conversation_id,
                requested_model=request.model if request else "unknown",
                routed_provider=routed_provider,
                routed_model=routed_model,
                status=status,
                prompt_tokens=int(prompt_tokens) if prompt_tokens else 0,
                completion_tokens=int(completion_tokens) if completion_tokens else 0,
                error_type="upstream_error" if error_msg else None,
                error_msg=(error_msg or ""),
                fallback_count=fallback_count or 0,
                latency_ms=latency_ms,
                user_ip=raw_request.client.host if raw_request.client else None,
                request_body=req_s,
                response_body=stream_body or (_json_mod.dumps(attempt_errors, ensure_ascii=False) if attempt_errors else "[stream]"),
            ))
            await _ldb.commit()
    except Exception:
        pass
        pass

def get_auto_router() -> AutoRouter:
    global _auto_router
    if _auto_router is None:
        from server.core.auto_router import AutoRouter
        _auto_router = AutoRouter(
            model_catalog=ModelCatalog(),
            health_checker=HealthChecker(),
            key_manager=KeyManager(get_crypto_service()),
        )
    return _auto_router
@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OpenAI 兼容聊天补全端点"""
    try:
        verify_aigate_api_key(raw_request)
    except HTTPException as auth_err:
        # 认证失败也写日志，方便排查
        import json as _json_mod
        try:
            from server.db import AsyncSessionLocal as _LogSession
            from server.models.request_log import RequestLog as _RL
            async with _LogSession() as _ldb:
                _ldb.add(_RL(
                    conversation_id=str(uuid.uuid4()),
                    requested_model=request.model if request else "unknown",
                    status="error",
                    error_type="auth_failed",
                    error_msg=str(auth_err.detail),
                    user_ip=raw_request.client.host if raw_request.client else None,
                    request_body=_json_mod.dumps(request.model_dump(), ensure_ascii=False) if request else None,
                ))
                await _ldb.commit()
        except Exception:
            pass
        raise auth_err
    ar = get_auto_router()
    import uuid
    conversation_id = str(uuid.uuid4())
    request = _preprocess_request(request)
    http_status_code = 200
    _send_time = time.time()  # 提前设，级联路径也需要
    route_result: Optional[RouteResult] = None
    made_by_cascade = False  # 是否已由级联回退发起过实际请求
    is_auto = request.is_auto

    # ─── 直接路由 ───
    if not is_auto:
        if "/" in request.model:
            provider_name, model_id = request.model.split("/", 1)
            model = await ar.model_catalog.get_by_full_id(db, provider_name, model_id)
        else:
            mc = ModelCatalog()
            models = await mc.list_models(db, enabled_only=True)
            model = next((m for m in models if m.model_id == request.model), None)
        if not model:
            return JSONResponse(status_code=404, content={"error": f"Model {request.model} not found"})
        provider = await db.get(Provider, model.provider_id)
        from sqlalchemy import select as sa_select
        result = await db.execute(
            sa_select(ApiKey).where(ApiKey.provider_id == provider.id, ApiKey.is_active == True).limit(1)
        )
        key = result.scalar_one_or_none()
        if not key:
            return JSONResponse(status_code=503, content={"error": f"No active API key for provider {provider.name}"})
        api_key = get_crypto_service().decrypt(key.key_encrypted)
        from server.core.model_catalog import create_adapter_for_provider
        adapter = create_adapter_for_provider(provider.api_type)
        route_result = RouteResult(
            success=True, model=model, provider=provider, api_key=api_key,
            adapter=adapter, fallback_count=0
        )

    # ─── auto 路由 ───
    elif is_auto and request.stream:
        # 流式级联回退：直接拉流，连接失败自动换下一个候选
        async def cascade_stream():
            from server.db import AsyncSessionLocal as _CS
            cascade_db = _CS()
            max_r = max(1, ar.config.max_fallbacks)
            tried_sids = set()
            stream_errs = []
            # 立即发一个 SSE 注释块，防客户端超时
            yield b": keepalive\n\n"
            try:
                for st_attempt in range(max_r + 1):
                    cand = await ar.get_best_candidate(cascade_db, conversation_id, exclude_model_ids=tried_sids)
                    if not cand.success or (cand.model and cand.model.id in tried_sids):
                        print(f"[CASCADE] exhausted at attempt {st_attempt}: {cand.error if cand else 'no cand'} tried={tried_sids}", flush=True)
                        err_data = {"error": cand.error or "no more candidates", "attempts": stream_errs}
                        yield _format_sse_chunk(err_data, "unknown")
                        yield b"data: [DONE]\n\n"
                        await _write_stream_log(conversation_id, request, raw_request, "error",
                            None, None, str(cand.error if cand else "no candidates"), st_attempt, stream_errs)
                        return
                    tried_sids.add(cand.model.id)
                    mid_full = f"{cand.provider.name}/{cand.model.model_id}"
                    up_req = request.model_copy(update={"model": cand.model.model_id})
                    try:
                        gen = cand.adapter.stream_chat_completion(
                            up_req, cand.api_key, cand.provider.base_url, cand.provider.headers
                        )
                        sc = 0
                        stream_buf = []
                        last_usage = {}
                        stream_has_error = False
                        stream_err_detail = ""
                        async for ck in gen:
                            sc += 1
                            if isinstance(ck, dict) and "error" in ck:
                                stream_has_error = True
                                stream_err_detail = str(ck.get("error", "unknown"))[:200]
                                break
                            yield _format_sse_chunk(ck, mid_full)
                            stream_buf.append(ck)
                            u = ck.get("usage", {}) if isinstance(ck, dict) else {}
                            if u:
                                last_usage = u
                        if stream_has_error:
                            raise RuntimeError(f"upstream_stream_error: {stream_err_detail}")
                        yield b"data: [DONE]\n\n"
                        resp_snapshot = None
                        if stream_buf:
                            import json as _json_mod
                            resp_snapshot = _json_mod.dumps(stream_buf, ensure_ascii=False)
                        pt = int(last_usage.get("prompt_tokens") or last_usage.get("input_tokens") or 0)
                        ct = int(last_usage.get("completion_tokens") or last_usage.get("output_tokens") or 0)
                        await _write_stream_log(conversation_id, request, raw_request, "success",
                            cand.provider.name, cand.model.model_id, None, st_attempt, None,
                            stream_body=resp_snapshot, prompt_tokens=pt, completion_tokens=ct,
                            latency_ms=int((time.time() - _send_time) * 1000))
                        if ar.health_checker:
                            ar.health_checker.mark_success(cand.model.id)
                        return
                    except Exception as se:
                        err_s = f"{type(se).__name__}: {str(se)[:200]}"
                        stream_errs.append({"attempt": st_attempt, "model": mid_full, "error": err_s})
                        if ar.health_checker:
                            ar.health_checker.mark_failure(cand.model.id)
                            ar.health_checker.mark_cooling(cand.model.id, ar.config.cooling_period_seconds)
                        cd_seconds = ar.config.cooling_period_seconds
                        fc = (ar.health_checker._fail_count.get(cand.model.id, 0) if ar.health_checker else 0)
                        cd_actual = min(cd_seconds * (2 ** max(fc - 1, 0)), 3600) if fc > 1 else cd_seconds
                        err_s_annotated = f"{err_s} | cooldown={cd_actual}s fail#{fc}"
                        raw_err = _extract_error_body(se) or err_s
                        if sc > 0:
                            yield _format_sse_chunk({"error": f"stream_mid_failure: {err_s}"}, mid_full)
                            yield b"data: [DONE]\n\n"
                            await _write_stream_log(conversation_id, request, raw_request, "error",
                                cand.provider.name, cand.model.model_id, err_s_annotated, st_attempt, stream_errs,
                                stream_body=raw_err)
                            return
                        print(f"[CASCADE] attempt {st_attempt} failed ({err_s[:60]}), trying next (tried={tried_sids})", flush=True)
                        await _write_stream_log(conversation_id, request, raw_request, "error",
                            cand.provider.name, cand.model.model_id, err_s_annotated, st_attempt, None,
                            stream_body=raw_err)
                        continue
                yield _format_sse_chunk({"error": "all cascade fallbacks exhausted", "attempts": stream_errs}, "unknown")
                yield b"data: [DONE]\n\n"
                await _write_stream_log(conversation_id, request, raw_request, "error",
                    None, None, "all cascade fallbacks exhausted", 0, stream_errs)
            finally:
                await cascade_db.close()

        return StreamingResponse(cascade_stream(), media_type="text/event-stream")

    else:
        # 非流式级联回退：完整业务请求内置于回退循环
        route_result, response, _attempt_errors = await _auto_request_with_cascade_fallback(
            ar, db, request, conversation_id
        )
        if not route_result.success:
            # 写一条失败日志
            try:
                from server.db import AsyncSessionLocal as _LS
                from server.models.request_log import RequestLog as _RL
                import json as _j
                async with _LS() as _ldb:
                    _ldb.add(_RL(
                        conversation_id=conversation_id,
                        requested_model=request.model,
                        status="error",
                        error_type="upstream_error",
                        error_msg=response.get("error", "all_candidates_failed"),
                        fallback_count=len(_attempt_errors),
                        user_ip=raw_request.client.host if raw_request.client else None,
                        request_body=_j.dumps(request.model_dump(), ensure_ascii=False),
                        response_body=_j.dumps(response, ensure_ascii=False),
                    ))
                    await _ldb.commit()
            except Exception:
                pass
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
        upstream_request = request.model_copy(update={"model": route_result.model.model_id})
        extra_headers = route_result.provider.headers if route_result.provider.headers else None
        _send_time = time.time()
        if request.stream:
            generator = route_result.adapter.stream_chat_completion(
                upstream_request, route_result.api_key, route_result.provider.base_url, extra_headers
            )
            _stream_usage = {}
            _stream_err = None
            _stream_chunks_log = []  # 收集所有 chunk 用于日志
            async def wrap_stream():
                nonlocal _stream_usage, _stream_err
                stream_has_error = False
                stream_err_detail = ""
                try:
                    async for chunk in generator:
                        if isinstance(chunk, dict):
                            # 检测上游在 SSE 流中返回的错误
                            if "error" in chunk and "choices" not in chunk:
                                stream_has_error = True
                                stream_err_detail = str(chunk.get("error", "unknown"))[:200]
                                break  # 跳出循环再抛，避免 aclose() 竞态
                            u = chunk.get("usage", {})
                            if u:
                                _stream_usage = u
                            _stream_chunks_log.append(chunk)
                        yield _format_sse_chunk(chunk, model_id_full)
                    if stream_has_error:
                        raise RuntimeError(f"upstream_stream_error: {stream_err_detail}")
                    yield b"data: [DONE]\n\n"
                except Exception as e:
                    _stream_err = f"{type(e).__name__}: {str(e)[:200]}"
                    error_chunk = {"error": f"upstream_stream_failed: {_stream_err}"}
                    yield _format_sse_chunk(error_chunk, model_id_full)
                    yield b"data: [DONE]\n\n"
                finally:
                    # 流完成后异步写日志
                    resp_snapshot = None
                    if _stream_chunks_log:
                        import json as _j
                        resp_snapshot = _j.dumps(_stream_chunks_log, ensure_ascii=False)
                    pt = int(_stream_usage.get("prompt_tokens") or _stream_usage.get("input_tokens") or 0)
                    ct = int(_stream_usage.get("completion_tokens") or _stream_usage.get("output_tokens") or 0)
                    try:
                        from server.db import AsyncSessionLocal as _LS
                        from server.models.request_log import RequestLog as _RL
                        async with _LS() as _ldb:
                            _ldb.add(_RL(
                                conversation_id=conversation_id,
                                requested_model=request.model,
                                routed_provider=route_result.provider.name,
                                routed_model=route_result.model.model_id,
                                status="error" if _stream_err else "success",
                                prompt_tokens=pt,
                                completion_tokens=ct,
                fallback_count=route_result.fallback_count if route_result else 0,
                latency_ms=int((time.time() - _send_time) * 1000),
                error_type="upstream_error" if _stream_err else None,
                error_msg=(_stream_err or ""),
                request_body=_j.dumps(upstream_request.model_dump(), ensure_ascii=False) if upstream_request else None,
                response_body=resp_snapshot,
                            ))
                            await _ldb.commit()
                    except Exception:
                        pass
            response = StreamingResponse(wrap_stream(), media_type="text/event-stream")
        else:
            try:
                result = await route_result.adapter.chat_completion(
                    upstream_request, route_result.api_key, route_result.provider.base_url, extra_headers
                )
                if isinstance(result, dict) and "model" in result:
                    result["model"] = model_id_full
                response = result
            except Exception as e:
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
            from server.db import AsyncSessionLocal as _LogSession
            from server.models.request_log import RequestLog as _RL
            import json as _json_mod
            is_err = isinstance(response, dict) and "error" in response
            resp_dict = response if isinstance(response, dict) else None
            usage = resp_dict.get("usage", {}) if resp_dict else {}
            pt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            ct = usage.get("completion_tokens") or usage.get("output_tokens") or 0
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
                    routed_model=route_result.model.model_id if (route_result and route_result.success) else None,
                    status="error" if is_err else "success",
                    latency_ms=_latency,
                    prompt_tokens=int(pt) if pt else 0,
                    completion_tokens=int(ct) if ct else 0,
                    fallback_count=route_result.fallback_count if route_result else 0,
                    user_ip=raw_request.client.host if raw_request.client else None,
                    error_type="upstream_error" if is_err else None,
                    error_msg=str(response.get("error", "")) if is_err else None,
                    request_body=req_body_str,
                    response_body=resp_body_str,
                )
                _ldb.add(_rlog)
                await _ldb.commit()
        except Exception as _e:
            print(f"[WARN] request log write failed: {_e}")

    # ─── 返回 ───
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
        data.append({
            "id": f"{provider.name}/{model.model_id}",
            "object": "model",
            "created": int(model.created_at.timestamp()) if model.created_at else int(time.time()),
            "owned_by": provider.name,
            "pricing": {
                "input": model.input_price,
                "output": model.output_price,
                "unit": "per_1M_tokens",
                "currency": "USD"
            },
            "is_free": model.is_free,
            "auto_enabled": model.auto_enabled,
            "capabilities": {
                "streaming": model.supports_streaming,
                "vision": model.supports_vision,
                "context_length": model.context_length
            }
        })
    return {
        "object": "list",
        "data": data
    }