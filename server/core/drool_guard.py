"""DroolGuard 流口水（复读）检测与罚时。

同一模型**连续** threshold 次返回"一字不差"的答案 → 判为流口水 → 强制冷却
cooldown_minutes（默认 5 次 / 30 分钟），罚时经 health_checker 落库持久。

现场依据（2026-09-19 北京 06:08 前后）：烁公益站/kimi-k3 连着 5 次逐字返回同一条
"The input channel is healthy..."（agent 客户端与模型互刷死循环）。日志的
response_body_hash 抓不到这种复读——流式响应的 chunk id 每次不同 → 必须比对
**答案正文**（拼接 delta.content 后去空白）。

挂载点：request_logger.write_log（所有协议/路径的日志都汇聚于此）。
计数纯内存（重启清零可接受，罚时已落库）；触发后的 model_pk 查询与冷却为异步任务，
不阻塞请求路径。过短答案（< min_answer_chars）不参与并断链，防"好的"类正常短答误伤。
"""
import hashlib
import json
import logging
import re

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")
# (provider_name, model_id) -> [answer_fingerprint, consecutive_run]
_state: dict = {}
_MAX_KEYS = 4096


def _config():
    from server.config import get_config
    return get_config().drool_guard


def extract_answer(body) -> str:
    """从日志响应体提取模型答案正文。兼容 SSE chunk 列表（流式落库）与
    chat.completion dict（非流式）；解析失败按原文处理。"""
    if not body:
        return ""
    data = body
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return data
    if isinstance(data, list):
        parts = []
        for ck in data:
            if not isinstance(ck, dict):
                continue
            for ch in (ck.get("choices") or []):
                c = (ch.get("delta") or ch.get("message") or {}).get("content")
                if c:
                    parts.append(c)
        return "".join(parts)
    if isinstance(data, dict):
        ch = (data.get("choices") or [{}])[0]
        c = (ch.get("message") or ch.get("delta") or {}).get("content")
        if isinstance(c, str):
            return c
    return str(data)


def note_success(provider_name, model_id, response_body) -> None:
    """成功响应记账（同步、纯内存）；命中阈值 → 调度异步罚时。"""
    try:
        cfg = _config()
        if not cfg.enabled or not provider_name or not model_id:
            return
        answer = extract_answer(response_body)
        norm = _WS_RE.sub("", answer or "")
        if len(norm) < max(1, int(cfg.min_answer_chars)):
            _state.pop((str(provider_name), str(model_id)), None)
            return
        fp = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        key = (str(provider_name), str(model_id))
        prev_fp, run = _state.get(key, ("", 0))
        run = run + 1 if fp == prev_fp else 1
        _state[key] = (fp, run)
        if len(_state) > _MAX_KEYS:  # 防无界增长：清最早一半
            for k in list(_state.keys())[: _MAX_KEYS // 2]:
                _state.pop(k, None)
        threshold = max(2, int(cfg.threshold or 5))
        if run >= threshold:
            _state.pop(key, None)
            minutes = max(1, int(cfg.cooldown_minutes or 30))
            logger.warning(
                "[DroolGuard] %s/%s 连续 %d 次返回完全相同答案（归一化 %d 字符）→ 罚时冷却 %d 分钟",
                provider_name, model_id, run, len(norm), minutes)
            try:
                import asyncio
                asyncio.get_running_loop().create_task(
                    _penalize(str(provider_name), str(model_id), minutes))
            except RuntimeError:
                pass
    except Exception as e:
        logger.debug("drool_guard note failed: %s", e)


async def _penalize(provider_name: str, model_id: str, minutes: int):
    try:
        from sqlalchemy import select
        from server.db import AsyncSessionLocal
        from server.models.provider import Provider
        from server.models.model import Model
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(Model.id)
                .join(Provider, Model.provider_id == Provider.id)
                .where(Provider.name == provider_name, Model.model_id == model_id)
                .limit(1)
            )).first()
        if not row:
            logger.warning("[DroolGuard] 找不到模型 %s/%s，跳过罚时", provider_name, model_id)
            return
        from server.api.v1_router import get_auto_router
        hc = get_auto_router().health_checker
        if hc:
            hc.penalize(int(row[0]), minutes * 60)
            logger.info("[DroolGuard] %s/%s 已强制冷却 %d 分钟 (model_pk=%s)",
                        provider_name, model_id, minutes, row[0])
    except Exception as e:
        logger.warning("[DroolGuard] 罚时执行失败: %s", e)


def snapshot():
    """调试/测试用：当前 (provider,model) → 连续计数。"""
    return {f"{k[0]}/{k[1]}": v[1] for k, v in _state.items()}


def reset_state():
    _state.clear()
