"""
健康探测器（精简版）
说明：
- 不再做「定时自动探测」（避免白费 token）。
- 仅保留两类能力：
  1) 真实请求失败冷却：is_cooling / mark_cooling / mark_failure / mark_success
     —— 由 v1_router 的真实流量驱动，不额外发请求。
  2) 手动测速：check_model（用户点「测速」才调用，写 request_logs 供评分使用）。
速度 / 健康数据统一来自 request_logs 的真实调用历史。
"""
import time
import sqlite3
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from server.models.provider import Provider
from server.models.model import Model
from server.models.api_key import ApiKey
from server.core.request_logger import dedup_log_row  # v3.6 消息级去重写日志
from server.models.health_check import HealthCheck
from server.models.request_log import RequestLog
from server.config import get_config
from server.adapters.base_adapter import BaseAdapter, HealthResult
from server.adapters.openai_compat import OpenAICompatAdapter
from server.adapters.codex_responses import CodexResponsesAdapter
from server.adapters.anthropic_adapter import AnthropicAdapter
from server.adapters.atomcode_adapter import AtomCodeAdapter
from .key_manager import KeyManager
from .crypto_service import get_crypto_service
import uuid
config = get_config()
@dataclass
class HealthStatus:
    model_id: int
    status: str
    latency_ms: float
    last_checked: datetime
    error_message: Optional[str]
def create_adapter_for_provider(provider: Provider) -> BaseAdapter:
    if provider.api_type in ("anthropic", "claude_code"):
        return AnthropicAdapter()
    if provider.api_type == "codex_responses":
        return CodexResponsesAdapter()
    if provider.api_type == "atomcode":
        return AtomCodeAdapter()
    return OpenAICompatAdapter()
class HealthChecker:
    def __init__(self):
        self.config = config.health_check
        self._status_cache: Dict[int, HealthStatus] = {}
        self._cooling: Dict[int, datetime] = {}  # model_id -> 冷却结束时间
        self._fail_count: Dict[int, int] = {}     # model_id -> 连续失败次数
        self.MAX_COOLING = 3600  # 最长冷却 1 小时

    def _parse_dt(self, value) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None

    def _persist_cooling(self, model_id: int):
        """把冷却状态同步到 DB。HealthChecker 是同步类，这里用 sqlite3 做轻量持久化。"""
        try:
            conn = sqlite3.connect(config.database.path)
            conn.execute(
                "UPDATE models SET auto_cooldown_until=?, auto_fail_count=? WHERE id=?",
                (
                    self._cooling.get(model_id).isoformat(sep=" ") if self._cooling.get(model_id) else None,
                    int(self._fail_count.get(model_id, 0)),
                    int(model_id),
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _load_cooling_from_db(self, model_id: int):
        """进程重启后首次访问模型时，从 DB 恢复冷却截止时间和失败计数。"""
        try:
            conn = sqlite3.connect(config.database.path)
            row = conn.execute(
                "SELECT auto_cooldown_until, auto_fail_count FROM models WHERE id=?",
                (int(model_id),),
            ).fetchone()
            conn.close()
            if not row:
                return
            cooldown_until = self._parse_dt(row[0])
            fail_count = int(row[1] or 0)
            if fail_count > 0:
                self._fail_count[model_id] = fail_count
            if cooldown_until and datetime.utcnow() < cooldown_until:
                self._cooling[model_id] = cooldown_until
        except Exception:
            pass

    def is_cooling(self, model_id: int) -> bool:
        if model_id not in self._cooling and model_id not in self._fail_count:
            self._load_cooling_from_db(model_id)
        if model_id not in self._cooling:
            return False
        now = datetime.utcnow()
        if now < self._cooling[model_id]:
            return True
        del self._cooling[model_id]
        self._persist_cooling(model_id)
        return False

    def mark_cooling(self, model_id: int, seconds: int = 30):
        """指数退避冷却：fail#1=30s, fail#2=60s, fail#3=120s ... 最长 1h。"""
        fc = self._fail_count.get(model_id, 0)
        cool = min(seconds * (2 ** max(fc - 1, 0)), self.MAX_COOLING) if fc > 0 else seconds
        self._cooling[model_id] = datetime.utcnow() + timedelta(seconds=cool)
        self._persist_cooling(model_id)

    def mark_failure(self, model_id: int):
        """记录一次失败"""
        self._load_cooling_from_db(model_id)
        self._fail_count[model_id] = self._fail_count.get(model_id, 0) + 1
        self._persist_cooling(model_id)

    def mark_success(self, model_id: int):
        """成功则清零失败计数与冷却"""
        self._fail_count.pop(model_id, None)
        self._cooling.pop(model_id, None)
        self._persist_cooling(model_id)

    def clear_cooling(self, model_id: Optional[int] = None) -> int:
        """清除模型失败冷却。model_id=None 表示清除全部模型冷却惩罚。"""
        if model_id is None:
            count = len(set(self._cooling.keys()) | set(self._fail_count.keys()))
            self._cooling.clear()
            self._fail_count.clear()
            try:
                conn = sqlite3.connect(config.database.path)
                cur = conn.execute(
                    "UPDATE models SET auto_cooldown_until=NULL, auto_fail_count=0 "
                    "WHERE auto_cooldown_until IS NOT NULL OR auto_fail_count > 0"
                )
                conn.commit()
                count = max(count, cur.rowcount if cur.rowcount is not None else 0)
                conn.close()
            except Exception:
                pass
            return count
        existed = int(model_id in self._cooling or model_id in self._fail_count)
        self._cooling.pop(model_id, None)
        self._fail_count.pop(model_id, None)
        self._persist_cooling(model_id)
        return existed

    def get_cached_status(self, model_id: int) -> Optional[HealthStatus]:
        """获取缓存的状态"""
        return self._status_cache.get(model_id)
    def get_all_cached_status(self) -> Dict[int, HealthStatus]:
        """获取所有缓存状态"""
        return self._status_cache
    async def get_latest_from_db(
        self,
        session: AsyncSession,
        model_id: int
    ) -> Optional[HealthCheck]:
        """从数据库获取最新一次检查"""
        result = await session.execute(
            select(HealthCheck)
            .where(HealthCheck.model_id == model_id)
            .order_by(desc(HealthCheck.checked_at))
            .limit(1)
        )
        return result.scalar_one_or_none()
    async def check_model(
        self,
        session: AsyncSession,
        model: Model,
        provider: Provider,
        key_manager: KeyManager,
        write_log: bool = False,  # v3.0: 是否写入 request_logs 供 RankingService 使用
    ) -> HealthResult:
        """探测单个模型"""
        cred_type = getattr(provider, "credential_type", "api_key")
        # ── free_tier：走 free executor 真实探测，无需密钥 ──
        if cred_type == "free_tier":
            from server.core.free_providers import get_free_executor, resolve_free_code
            from server.schemas.chat import ChatCompletionRequest
            free_code = resolve_free_code(provider.name, getattr(provider, "oauth_code", None))
            exec_ = get_free_executor(free_code) if free_code else None
            if not exec_:
                return HealthResult(
                    status="unhealthy", latency_ms=0,
                    error_message=f"no free executor for provider '{provider.name}'"
                )
            try:
                req = ChatCompletionRequest(
                    model=model.model_id,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                )
                start = time.time()
                await exec_.execute_non_stream(req)
                latency = (time.time() - start) * 1000
                return HealthResult(status="healthy", latency_ms=latency)
            except Exception as e:
                return HealthResult(status="unhealthy", latency_ms=0, error_message=str(e)[:300])
        # ── oauth：取 access token 作为临时 key ──
        if cred_type == "oauth":
            from server.core.oauth_client import get_oauth_client
            from server.core.oauth_registry import get_oauth_provider as _get_oauth_p
            oc = getattr(provider, "oauth_code", None) or provider.name
            oauth_p = _get_oauth_p(oc)
            api_key = await get_oauth_client().pick_access_token(oc, session) if oauth_p else None
            if not api_key:
                return HealthResult(
                    status="unhealthy", latency_ms=0,
                    error_message=f"OAuth provider '{oc}' not connected"
                )
        else:
            # atomcode：鉴权由本地 daemon 通过自身 config.toml 完成，不需要 AIGate 的 ApiKey 表密钥
            if provider.api_type == "atomcode":
                api_key = ""
            else:
                # api_key：取一个激活的 key
                key_row = await session.execute(
                    select(ApiKey)
                    .where(ApiKey.provider_id == provider.id, ApiKey.is_active == True)
                    .limit(1)
                )
                key = key_row.scalar_one_or_none()
                if not key:
                    return HealthResult(
                        status="unhealthy",
                        latency_ms=0,
                        error_message="No active API key"
                    )
                api_key = key_manager._crypto.decrypt(key.key_encrypted)
        # 创建适配器
        adapter = create_adapter_for_provider(provider)
        # 执行检查
        extra_headers = provider.headers if provider.headers else None
        result = await adapter.health_check(
            model.model_id,
            api_key,
            provider.base_url,
            extra_headers,
            self.config.ping_timeout_seconds
        )
        # 保存到数据库
        hc = HealthCheck(
            model_id=model.id,
            latency_ms=result.latency_ms,
            status=result.status,
            error_message=result.error_message
        )
        session.add(hc)
        # 清理旧记录
        total = await session.execute(
            select(HealthCheck).where(HealthCheck.model_id == model.id)
        )
        records = list(total.scalars().all())
        if len(records) > self.config.max_history_per_model:
            records.sort(key=lambda x: x.checked_at)
            for old in records[:-self.config.max_history_per_model]:
                await session.delete(old)
        # v3.0: 写入 request_logs，供 RankingService 计算速度和稳定性分
        if write_log:
            try:
                rl = RequestLog(
                    conversation_id=f"hc-{uuid.uuid4().hex[:12]}",
                    is_health_check=1,
                    requested_model="auto",
                    routed_provider=provider.name,
                    routed_model=model.model_id,
                    status="success" if result.status == "healthy" else "error",
                    latency_ms=result.latency_ms,
                    prompt_tokens=0,  # 健康探测不计 token
                    completion_tokens=0,
                    http_status=200 if result.status == "healthy" else 503,
                    error_type=result.error_message[:50] if result.error_message else None,
                    error_msg=(result.error_message or "")[:500],
                    fallback_count=0,
                    user_ip="127.0.0.1",
                )
                await dedup_log_row(session, rl)
                session.add(rl)
            except Exception as e:
                print(f"[WARN] request_log write failed during health check: {e}")
        await session.commit()
        # 更新缓存（供 auto_router 的冷却/状态过滤使用）
        self._status_cache[model.id] = HealthStatus(
            model_id=model.id,
            status=result.status,
            latency_ms=result.latency_ms,
            last_checked=datetime.utcnow(),
            error_message=result.error_message
        )
        return result
