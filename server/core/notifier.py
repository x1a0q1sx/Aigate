"""D3: 事件通知（通用 Webhook / Telegram / 钉钉群机器人）。

- notify_event(event, text): 请求路径可安全调用的同步入口 —— 内部调度异步
  发送任务，绝不阻塞、绝不抛错。
- 同类事件按 min_interval_seconds 节流（防刷屏）。
- 冷却事件异步补查模型名；预算事件由 gateway_keys 触发。
"""
import asyncio
import time
from typing import Optional

import httpx

from ..config import get_config

_throttle = {}
_SEND_TIMEOUT = 8


def _cfg():
    return getattr(get_config(), "notify", None)


def _throttled(event: str) -> bool:
    cfg = _cfg()
    min_iv = getattr(cfg, "min_interval_seconds", 300) if cfg else 300
    now = time.monotonic()
    last = _throttle.get(event, 0)
    if now - last < max(5, min_iv):
        return True
    _throttle[event] = now
    return False


def _enabled_for(event: str) -> bool:
    cfg = _cfg()
    if not cfg or not cfg.enabled:
        return False
    gates = {
        "cooldown": cfg.notify_model_cooldown,
        "all_failed": cfg.notify_all_failed,
        "budget": cfg.notify_budget_exceeded,
    }
    return gates.get(event, True)


async def _send_all(text: str) -> dict:
    cfg = _cfg()
    results = {}
    if not cfg:
        return results
    if cfg.webhook_url:
        try:
            async with httpx.AsyncClient(timeout=_SEND_TIMEOUT) as client:
                r = await client.post(cfg.webhook_url, json={"text": text, "msgtype": "text",
                                                             "content": text})
            results["webhook"] = r.status_code
        except Exception as e:
            results["webhook"] = f"err: {str(e)[:80]}"
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        try:
            async with httpx.AsyncClient(timeout=_SEND_TIMEOUT) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
                    json={"chat_id": cfg.telegram_chat_id, "text": text})
            results["telegram"] = r.status_code
        except Exception as e:
            results["telegram"] = f"err: {str(e)[:80]}"
    if cfg.dingtalk_webhook:
        try:
            async with httpx.AsyncClient(timeout=_SEND_TIMEOUT) as client:
                r = await client.post(cfg.dingtalk_webhook,
                                      json={"msgtype": "text", "text": {"content": f"[AIGate] {text}"}})
            results["dingtalk"] = r.status_code
        except Exception as e:
            results["dingtalk"] = f"err: {str(e)[:80]}"
    return results


def notify_event(event: str, text: str, *, force: bool = False) -> None:
    """同步入口：调度异步发送。任何失败静默（不影响请求路径）。"""
    try:
        if not _enabled_for(event):
            return
        if not force and _throttled(event):
            return
        loop = asyncio.get_running_loop()
        loop.create_task(_send_all(text))
    except Exception:
        pass


async def resolve_model_name(model_id: int) -> str:
    try:
        from sqlalchemy import select
        from ..db import AsyncSessionLocal
        from ..models.model import Model
        async with AsyncSessionLocal() as db:
            m = await db.get(Model, model_id)
            return m.model_id if m else f"#{model_id}"
    except Exception:
        return f"#{model_id}"


async def send_test() -> dict:
    """管理页「发送测试通知」"""
    results = await _send_all("AIGate 通知测试：如果你看到这条消息，说明通知渠道已打通 ✅")
    return results
