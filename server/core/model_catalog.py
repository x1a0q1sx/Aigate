"""
模型目录服务
管理模型元数据、刷新、auto 候选选择
v2.0: 支持 priority_boost + auto_excluded
"""
import logging
import asyncio
import json
import time
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update, delete
from server.models.provider import Provider
from server.models.model import Model
from server.models.api_key import ApiKey
from server.models.model_api_key import ModelApiKey  # v3.5 模型级密钥归属关联表
from server.adapters.base_adapter import BaseAdapter, ModelInfo
from server.adapters.openai_compat import OpenAICompatAdapter
from server.adapters.codex_responses import CodexResponsesAdapter
from server.adapters.anthropic_adapter import AnthropicAdapter
from server.adapters.github_adapter import GitHubAdapter
from server.adapters.image_adapter import ImageAdapter
from server.adapters.atomcode_adapter import AtomCodeAdapter
from server.adapters.xyusec_pricing import fetch_provider_pricing, match_model_metadata
from server.core.error_text import err_text
from .key_manager import KeyManager
from server.config import get_config

logger = logging.getLogger(__name__)


async def _fetch_lobsterai_models(oauth_client, session: AsyncSession,
                                  owner: str) -> List[ModelInfo]:
    """LobsterAI 在线模型列表（专属路径：非标准端点 + keyfrom query）。

    身份字段（uuid/first_keyfrom/…）存在连接记录的 scope 列 JSON 里 ——
    该端点的 query 就是身份载荷，发错身份会拿到错误的模型集合（Jet-Hub 结论）。
    任何失败返回空列表，由调用方回退静态种子。
    """
    import json as _json
    from server.core import lobsterai as lb
    row = await oauth_client._get_token_record(session, "lobsterai", owner)
    if not row:
        return []
    token = oauth_client._crypto.decrypt(row.access_token_enc)
    meta = {}
    if row.scope:
        try:
            parsed = _json.loads(row.scope)
            if isinstance(parsed, dict):
                meta = parsed
        except ValueError:
            pass
    cred = {
        "access_token": token,
        "uuid": meta.get("uuid", ""),
        "first_keyfrom": meta.get("first_keyfrom", ""),
        "latest_keyfrom": meta.get("latest_keyfrom", ""),
        "client_version": meta.get("client_version", ""),
        "user_id": meta.get("uid", ""),
    }
    raw = await lb.fetch_models(cred)
    out: List[ModelInfo] = []
    for m in raw:
        out.append(ModelInfo(
            model_id=m["id"],
            display_name=m.get("name") or m["id"],
            is_free=False,
            input_price=0.0,
            output_price=0.0,
            supports_streaming=True,
            context_length=int(m.get("context_length") or 0),
            # 远端权威：supportsImage 直接映射（不支持图片是实测结论）
            input_modalities=["text", "image"] if m.get("supports_vision") else ["text"],
            max_output_tokens=m.get("max_output_tokens"),
            supports_vision=bool(m.get("supports_vision")),
            supports_reasoning_effort=True,
        ))
    return out


# ── CodeBuddy 在线倍率（/v3/config 的 models[].credits）──────────────
# F31（2026-09-28）：此前倍率只来自 STATIC_PRICE_RATIOS 手工表（客户端产物，
# CN 6 条 / Intl 3 条），而**上游其实有在线倍率源**：GET {base}/v3/config 的
# data.models[].credits（形如 "x0.17 credits" / "x0.00"）。生产实测：
#   · CN  (copilot.tencent.com) → 31 条模型，全部带 credits
#   · Intl(www.codebuddy.ai)    → **UA 决定返回集**：
#       CLI/2.108.1 → 22 条（21 带 credits）；IDE/2.108.1 → 13 条（仅 7 带）；
#       CodeBuddy/2.63.2 → 0 条。故这里固定用 CLI UA（与 CN 一致）。
#
# ⚠️ **只用于倍率，绝不用它替换模型列表**：实测若把 /v3/config 的 models[] 当
# 权威清单，CN/Intl 各会误删 8 个模型（`auto` / `balanced-model` / `fast-model` /
# `deep-model` 等档位别名不在 models[] 里但**确实可用**）—— 对齐 Jet-Hub 教训：
# 「把某一刻的快照当判据会让后人误删可用模型」。倍率是纯附加标注，没有这个风险。
_CODEBUDDY_CONFIG_PATH = "/v3/config"
# 实测唯一能拿到完整 models[] 的 UA（两个域名都验过）
_CODEBUDDY_RATIO_UA = "CLI/2.108.1 CodeBuddy/2.108.1"


def parse_codebuddy_credits(text) -> Optional[float]:
    """'x0.17 credits' / 'x0.00' / '0.5x' / 0.17 → float；无法解析返回 None。

    上游形态实测：models[].credits = "x0.17 credits"（带单位后缀）；
    促销表里另有 "0.50x" / "0x" 形态。只认能明确解析出的数字，
    解析不出返回 None（调用方按「未知」处理，绝不猜 0 = 免费）。
    """
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    s = text.strip().lower().replace("credits", "").strip()
    s = s[1:] if s.startswith("x") else s
    s = s[:-1] if s.endswith("x") else s
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _codebuddy_host(base_url: str) -> str:
    """api_base_url（可能是 .../v2/chat/completions）→ scheme://host。"""
    base = (base_url or "").rstrip("/")
    if not base or "://" not in base:
        return ""
    scheme, rest = base.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0]}"


async def fetch_codebuddy_ratios(token: str, base_url: str) -> Dict[str, float]:
    """从 CodeBuddy /v3/config 取 {model_id: credits 倍率}（在线权威源）。

    只读、**只产出倍率**（不产出模型列表，见上方警告）。任何失败返回空 dict，
    由调用方回退静态表（绝不因一次网络抖动把倍率清空）。
    """
    import httpx
    base = _codebuddy_host(base_url)
    if not base:
        return {}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Domain": base.split("://", 1)[-1],
        "X-Product": "SaaS",
        "X-Product-Code": "codebuddy",
        "User-Agent": _CODEBUDDY_RATIO_UA,
        "x-requested-with": "XMLHttpRequest",
        "x-codebuddy-request": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=25.0) as c:
            r = await c.get(base + _CODEBUDDY_CONFIG_PATH, headers=headers)
        if r.status_code != 200:
            logger.info("codebuddy /v3/config HTTP %s（回退静态倍率表）", r.status_code)
            return {}
        data = (r.json() or {}).get("data") or {}
    except Exception as e:
        logger.warning("codebuddy /v3/config 拉取失败：%s", e)
        return {}
    out: Dict[str, float] = {}
    for m in (data.get("models") or []):
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        ratio = parse_codebuddy_credits(m.get("credits"))
        if isinstance(mid, str) and mid and ratio is not None:
            out[mid] = ratio
    return out

# 内置价格参考表 (美元 / 百万 tokens)
# fmt: off
BUILTIN_PRICING = {
    # OpenAI
    "gpt-4o":                {"input": 5.0, "output": 15.0, "is_free": False},
    "gpt-4o-mini":           {"input": 0.15, "output": 0.6, "is_free": False},
    "gpt-4-turbo":           {"input": 10.0, "output": 30.0, "is_free": False},
    "gpt-3.5-turbo":         {"input": 0.5, "output": 1.5, "is_free": False},
    # DeepSeek
    "deepseek-chat":         {"input": 0.14, "output": 0.28, "is_free": False},
    "deepseek-coder":        {"input": 0.14, "output": 0.28, "is_free": False},
    # Groq 免费模型
    "llama3-8b-8192":        {"input": 0, "output": 0, "is_free": True},
    "llama3-70b-8192":       {"input": 0, "output": 0, "is_free": True},
    "mixtral-8x7b-32768":   {"input": 0, "output": 0, "is_free": True},
    "gemma-7b-it":           {"input": 0, "output": 0, "is_free": True},
    # 通义千问
    "qwen-turbo":            {"input": 0.03, "output": 0.06, "is_free": False},
    "qwen-plus":             {"input": 0.4, "output": 0.8, "is_free": False},
    "qwen-max":              {"input": 0.8, "output": 1.6, "is_free": False},
    # 智谱
    "glm-4":                 {"input": 1.0, "output": 1.0, "is_free": False},
    "glm-3-turbo":           {"input": 0, "output": 0, "is_free": True},
    # Moonshot
    "moonshot-v1-8k":        {"input": 0.012, "output": 0.012, "is_free": False},
    "moonshot-v1-32k":       {"input": 0.024, "output": 0.024, "is_free": False},
    "moonshot-v1-128k":      {"input": 0.06, "output": 0.06, "is_free": False},
}
# fmt: on
def get_builtin_pricing(model_id: str) -> Optional[dict]:
    """匹配内置定价表。

    P2: 子串匹配必须取**最长**匹配键——此前按 dict 插入序首个命中，
    如 gpt-4o-mini-2024-xx 先撞上 "gpt-4o"（5.0/15.0）而非 mini 档（0.15/0.6），
    造成 30 倍高估。
    """
    if model_id in BUILTIN_PRICING:
        return BUILTIN_PRICING[model_id]
    candidates = [k for k in BUILTIN_PRICING if k in model_id]
    if not candidates:
        return None
    return BUILTIN_PRICING[max(candidates, key=len)]
def create_adapter_for_provider(api_type: str, timeout: Optional[int] = None) -> BaseAdapter:
    """根据 api_type 创建适配器。
    timeout 为可选项：传入时覆盖适配器默认超时（用于刷新模型等后台网络请求）。"""
    if api_type in ("anthropic", "claude_code"):
        return AnthropicAdapter(timeout=timeout) if timeout else AnthropicAdapter()
    elif api_type == "github":
        return GitHubAdapter(timeout=timeout) if timeout else GitHubAdapter()
    elif api_type == "image":
        return ImageAdapter(timeout=timeout) if timeout else ImageAdapter()
    elif api_type == "openai_compat":
        return OpenAICompatAdapter(timeout=timeout) if timeout else OpenAICompatAdapter()
    elif api_type == "codex_responses":
        return CodexResponsesAdapter(timeout=timeout) if timeout else CodexResponsesAdapter()
    elif api_type == "atomcode":
        return AtomCodeAdapter(timeout=timeout) if timeout else AtomCodeAdapter()
    elif api_type == "qoder":
        from server.adapters.qoder_adapter import QoderAdapter
        return QoderAdapter(timeout=timeout) if timeout else QoderAdapter()
    elif api_type == "codearts":
        from server.adapters.codearts_adapter import CodeArtsAdapter
        return CodeArtsAdapter(timeout=timeout) if timeout else CodeArtsAdapter()
    elif api_type == "trae":
        from server.adapters.trae_adapter import TraeAdapter
        return TraeAdapter(timeout=timeout) if timeout else TraeAdapter()
    elif api_type == "freebuff":
        from server.adapters.freebuff_adapter import FreebuffAdapter
        return FreebuffAdapter(timeout=timeout) if timeout else FreebuffAdapter()
    else:
        return OpenAICompatAdapter(timeout=timeout) if timeout else OpenAICompatAdapter()
class ModelCatalog:
    """模型目录服务"""
    def __init__(self):
        pass
    async def list_models(
        self,
        session: AsyncSession,
        provider_id: Optional[int] = None,
        is_free: Optional[bool] = None,
        auto_enabled: Optional[bool] = None,
        enabled_only: bool = True,
        extra_conditions: Optional[list] = None
    ) -> List[Model]:
        """列出模型，支持过滤；extra_conditions 可追加调用方自带的 SQL 条件（如模糊搜索）"""
        conditions = []
        if enabled_only:
            conditions.append(Model.enabled == True)
        if provider_id is not None:
            conditions.append(Model.provider_id == provider_id)
        if is_free is not None:
            conditions.append(Model.is_free == is_free)
        if auto_enabled is not None:
            conditions.append(Model.auto_enabled == auto_enabled)
        if extra_conditions:
            conditions.extend(extra_conditions)
        query = select(Model)
        if enabled_only:
            # v4.0: 服务商被禁用时其模型同样不参与任何请求（但仍保留在 DB 中）
            query = query.join(Provider, Model.provider_id == Provider.id)
            conditions.append(Provider.enabled == True)
        if conditions:
            query = query.where(and_(*conditions))
        query = query.order_by(Model.provider_id, Model.model_id)
        result = await session.execute(query)
        return list(result.scalars().all())
    async def get_auto_candidates(
        self,
        session: AsyncSession
    ) -> List[Model]:
        """获取可以参与 auto 选举的候选模型。
        注意：free_tier / oauth 类供应商不需要 ApiKey 表里的密钥，
        只要 enabled + auto_enabled + 未手动排除即可成为候选（否则免费模型永远进不了 auto）。
        v4.0: 服务商被禁用时其模型不参与 auto 选举。"""
        query = (
            select(Model, Provider)
            .join(Provider, Model.provider_id == Provider.id)
            .where(
                Model.enabled == True,
                Model.auto_enabled == True,
                Model.auto_excluded == False,  # v2.0: 排除用户手动排除的
                Provider.enabled == True,      # v4.0: 跳过已禁用的服务商
            )
            .order_by(Model.priority_boost.desc(), Model.is_free.desc(), Model.input_price.asc())
        )
        result = await session.execute(query)
        rows = result.all()
        # 有 active key 的 provider 一次查清（此前逐模型查询，模型多时 O(N) 次 SQL）
        has_key_providers = {
            pid for (pid,) in (
                await session.execute(
                    select(ApiKey.provider_id)
                    .where(ApiKey.is_active == True)  # noqa: E712
                    .distinct()
                )
            ).all()
        }
        valid = []
        for model, provider in rows:
            cred = getattr(provider, "credential_type", "api_key")
            if cred in ("free_tier", "oauth"):
                # 免费层 / OAuth 供应商无需 ApiKey 表中的密钥
                valid.append(model)
                continue
            if model.provider_id in has_key_providers:
                valid.append(model)
        return valid
    async def get_by_id(self, session: AsyncSession, model_id: int) -> Optional[Model]:
        """根据 ID 获取模型"""
        result = await session.execute(select(Model).where(Model.id == model_id))
        return result.scalar_one_or_none()
    async def get_by_full_id(
        self,
        session: AsyncSession,
        provider_name: str,
        model_id: str
    ) -> Optional[Model]:
        """根据 provider/model 获取模型。v4.0: 服务商被禁用时返回 None（视为不可用）。"""
        query = (
            select(Model)
            .join(Provider, Model.provider_id == Provider.id)
            .where(
                Provider.name == provider_name,
                Model.model_id == model_id,
                Provider.enabled == True,  # v4.0: 跳过已禁用的服务商
            )
        )
        result = await session.execute(query)
        return result.scalar_one_or_none()
    async def update_model(
        self,
        session: AsyncSession,
        model_id: int,
        display_name: Optional[str] = None,
        auto_enabled: Optional[bool] = None,
        enabled: Optional[bool] = None,
        input_price: Optional[float] = None,
        output_price: Optional[float] = None,
        cache_read_input_price: Optional[float] = None,
        cache_write_input_price: Optional[float] = None,
        success_rate: Optional[float] = None,
        is_free: Optional[bool] = None,
        priority_boost: Optional[int] = None,
        auto_excluded: Optional[bool] = None,
        supports_reasoning_effort: Optional[bool] = None,
        request_overrides: Optional[dict] = None,
        context_length: Optional[int] = None,
        supports_vision: Optional[bool] = None,
        input_modalities: Optional[list] = None,
        max_output_tokens: Optional[int] = None,
        price_ratio: Optional[float] = None,
        clear_price_ratio: bool = False
    ) -> Optional[Model]:
        """更新模型配置"""
        model = await self.get_by_id(session, model_id)
        if not model:
            return None
        if display_name is not None:
            model.display_name = display_name
        if auto_enabled is not None:
            model.auto_enabled = auto_enabled
        if enabled is not None:
            model.enabled = enabled
        if input_price is not None:
            model.input_price = input_price
        if output_price is not None:
            model.output_price = output_price
        if cache_read_input_price is not None:
            model.cache_read_input_price = cache_read_input_price
        if cache_write_input_price is not None:
            model.cache_write_input_price = cache_write_input_price
        if any(v is not None for v in (input_price, output_price, cache_read_input_price, cache_write_input_price)):
            # 手动改价 → 标记 manual，后续刷新模型不覆盖（想恢复自动价可手动清掉来源）
            model.pricing_source = "manual"
            model.pricing_updated_at = datetime.utcnow()
        if success_rate is not None:
            model.success_rate = success_rate
        if is_free is not None:
            model.is_free = is_free
        if priority_boost is not None:
            model.priority_boost = max(-100, min(100, priority_boost))  # 限制范围
        if auto_excluded is not None:
            model.auto_excluded = auto_excluded
        # The API uses None as "unknown"; explicit True/False is a durable admin override.
        if supports_reasoning_effort is not None:
            model.supports_reasoning_effort = supports_reasoning_effort
            model.capability_source = "manual"
        if context_length is not None and context_length > 0:
            # P1-6: 手动改窗口 → 标记 manual，刷新/回填永不覆盖
            model.context_length = int(context_length)
            model.context_source = "manual"
        # v4.3 手动能力编辑：写入即 capability_source=manual（刷新/回填不覆盖）
        _cap_touched = False
        if input_modalities is not None:
            im = [str(x).strip().lower() for x in input_modalities if str(x).strip()]
            model.input_modalities = im or None
            _cap_touched = True
        if supports_vision is not None:
            model.supports_vision = bool(supports_vision)
            # 正向声明 image 能力；False 不写模态集合（False≠已知仅文本）
            if supports_vision:
                im = list(model.input_modalities or ["text"])
                if "image" not in im:
                    im.append("image")
                model.input_modalities = im
            _cap_touched = True
        if max_output_tokens is not None:
            model.max_output_tokens = int(max_output_tokens) if max_output_tokens > 0 else None
            _cap_touched = True
        if _cap_touched:
            model.capability_source = "manual"
        # v4.4 手动倍率：写入即 price_ratio_source=manual，刷新/静态表永不覆盖。
        # 传 0 表示免费（合法值，不能当 None 跳过）。
        if price_ratio is not None:
            model.price_ratio = float(price_ratio)
            model.price_ratio_source = "manual"
            if model.price_ratio == 0.0:
                model.is_free = True
        elif clear_price_ratio:
            # 显式清空 → 回到"未知"（下次刷新可由上游/静态表重新填充）
            model.price_ratio = None
            model.price_ratio_source = ""
        if request_overrides is not None:
            model.request_overrides = request_overrides
        await session.commit()
        await session.refresh(model)
        return model
    async def refresh_models_from_provider(
        self,
        session: AsyncSession,
        provider: Provider,
        key_manager: KeyManager,
        trigger: str = "manual"
    ) -> dict:
        """刷新主体计时 + 落 model_refresh_logs（分析页「模型刷新」日志类型可查详情）。"""
        t0 = time.monotonic()
        try:
            result = await self._refresh_models_inner(session, provider, key_manager)
        except Exception as e:
            await self._record_refresh(session, provider,
                                       {"error": err_text(e)[:400]}, trigger, t0)
            raise
        await self._record_refresh(session, provider, result, trigger, t0)
        return result

    async def _record_refresh(self, session, provider, result, trigger, t0):
        result = result if isinstance(result, dict) else {}
        try:
            from server.models.model_refresh_log import ModelRefreshLog
            err = result.get("error")
            pricing_err = result.get("pricing_error")
            row = ModelRefreshLog(
                trigger=trigger,
                provider_id=getattr(provider, "id", None),
                provider_name=getattr(provider, "name", "?"),
                ok=not err,
                duration_ms=int((time.monotonic() - t0) * 1000),
                added=int(result.get("added") or 0),
                updated=int(result.get("updated") or 0),
                removed=int(result.get("removed") or 0),
                total=int(result.get("total") or 0),
                pricing_updated=int(result.get("pricing_updated") or 0),
                metric_updated=int(result.get("metric_updated") or 0),
                pricing_source=result.get("pricing_source"),
                error=(str(err)[:500] if err else
                       (f"定价源: {str(pricing_err)[:300]}" if pricing_err else None)),
                added_models=json.dumps(result.get("added_models") or [], ensure_ascii=False)[:4000],
                removed_models=json.dumps(result.get("removed_models") or [], ensure_ascii=False)[:4000],
                list_source=result.get("list_source") or "unknown",
                list_note=(result.get("list_note") or "")[:500] or None,
            )
            session.add(row)
            await session.commit()
        except Exception as e:
            logger.warning("模型刷新日志落库失败 %s: %s", getattr(provider, "name", "?"), e)
            try:
                await session.rollback()
            except Exception:
                pass

    async def _refresh_models_inner(
        self,
        session: AsyncSession,
        provider: Provider,
        key_manager: KeyManager
    ) -> dict:
        """从服务商拉取最新模型列表"""
        # v3.3：base_url 校验，防止空/非法 URL 导致 httpx 报错
        base_url = (provider.base_url or "").strip()
        if not base_url.startswith(("http://", "https://")):
            return {"error": f"Invalid or missing base_url: '{base_url[:80]}'"}
        # free_tier / oauth 无密钥的 provider 也跳过（不需要 key）
        cred_type = getattr(provider, "credential_type", "api_key") or "api_key"
        if cred_type in ("free_tier",):
            # 免费层：尝试通过 free executor 拉取模型（仅 opencode 提供 /models 端点；
            # mimo-free 无端点，保留手动管理）
            from server.core.free_providers import get_free_executor, resolve_free_code
            free_code = resolve_free_code(provider.name, getattr(provider, "oauth_code", None))
            exec_ = get_free_executor(free_code) if free_code else None
            if exec_ is not None:
                try:
                    fetched_ids = await exec_.list_models()
                except Exception as e:
                    _ft_err = err_text(e)
                    logger.warning(f"free_tier list_models failed for {provider.name}: {_ft_err}")
                    # 拉取失败：保留已有模型（不报错、不删除），避免误删手动种子模型
                    total_rows = (await session.execute(
                        select(Model).where(Model.provider_id == provider.id)
                    )).scalars().all()
                    return {
                        "added": 0, "updated": 0, "removed": 0,
                        "added_models": [], "removed_models": [],
                        "total": len(list(total_rows)), "pricing_updated": 0,
                        "metric_updated": 0, "pricing_source": None,
                        "pricing_error": f"free_tier fetch failed: {_ft_err}",
                    }
                if fetched_ids:
                    added = 0
                    updated = 0
                    added_models = []
                    for mid in fetched_ids:
                        ex = await session.execute(
                            select(Model).where(Model.provider_id == provider.id, Model.model_id == mid)
                        )
                        if ex.scalar_one_or_none() is None:
                            session.add(Model(
                                provider_id=provider.id, model_id=mid, display_name=mid,
                                enabled=True, auto_enabled=False, is_free=False,
                                supports_streaming=True, priority_boost=0, auto_excluded=False,
                                is_manual=False,
                            ))
                            added += 1
                            added_models.append({"model_id": mid, "display_name": mid})
                        else:
                            updated += 1
                    await session.commit()
                    total_rows = (await session.execute(
                        select(Model).where(Model.provider_id == provider.id)
                    )).scalars().all()
                    return {
                        "added": added, "updated": updated, "removed": 0,
                        "added_models": added_models, "removed_models": [],
                        "total": len(list(total_rows)), "pricing_updated": 0,
                        "metric_updated": 0, "pricing_source": None, "pricing_error": None,
                    }
            return {"error": f"Skipping free_tier provider (models managed manually)"}
        # 刷新网络超时（来自 config.yaml model_refresh.timeout_seconds）
        refresh_timeout = get_config().model_refresh.timeout_seconds
        adapter = create_adapter_for_provider(provider.api_type, timeout=refresh_timeout)
        extra_headers = provider.headers if provider.headers else None
        # 与推理侧（v1_router._outbound_headers）对齐：服务商开启「走代理」时，刷新同样强制走代理池。
        # 否则上游按国别封锁（如 tokenharbor 拒 CN 出口）时推理能通而刷新永远拉不到在线列表，
        # 只能掉进定价页兜底（2026-10-09 实证）。__proxy_force 是内部标记，出站前被 outbound_headers 剥离。
        if getattr(provider, "proxy_enabled", False):
            extra_headers = dict(extra_headers or {})
            extra_headers["__proxy_force"] = True

        # v3.5：多 key 拉取 —— 该 provider 下每把 active key 都调 list_models
        # 模型存在性取并集；key 归属按各 key 实际返回写 model_api_keys
        key_models: Dict[int, set] = {}            # api_key_id -> {model_id, ...}
        all_model_infos: Dict[str, ModelInfo] = {} # model_id -> ModelInfo（并集，保留首个）
        any_success = False
        # 列表数据来源：online=上游真实返回；seed=OAuth 静态种子兜底（非真实拉取）；
        # pricing=定价接口兜底建模。落 model_refresh_logs 供分析页辨别「看起来没变其实没拉到」。
        list_source = "online"
        list_note = ""
        atomcode_no_key = (provider.api_type == "atomcode")

        if atomcode_no_key:
            # daemon 完成鉴权，无 AIGate ApiKey；直接以空 key 调一次 list_models
            try:
                _m = await adapter.list_models("", provider.base_url, extra_headers)
                if _m:
                    any_success = True
                    for mi in _m:
                        all_model_infos.setdefault(mi.model_id, mi)
            except Exception as e:
                logger.warning(f"atomcode list_models failed for {provider.name}: {e}")
        elif cred_type == "oauth":
            # OAuth 服务商：在线 list_models（token 来自连接记录），失败/无端点回退注册表静态种子
            from server.core.oauth_client import get_oauth_client as _goc
            from server.core.oauth_registry import get_oauth_provider as _gop
            oauth_code = getattr(provider, "oauth_code", None) or provider.name
            oauth_p = _gop(oauth_code)
            token = None
            try:
                # v4.2: 与路由侧一致，按 provider.oauth_owner 点名取号
                _owner = (getattr(provider, "oauth_owner", None) or "").strip() or "__default"
                token = await _goc().pick_access_token(oauth_code, session, owner=_owner)
            except Exception as e:
                logger.warning(f"oauth pick_access_token failed for {provider.name}: {e}")
            if token:
                try:
                    eh = dict(extra_headers or {})
                    eh["__oauth"] = True
                    # LobsterAI：模型列表在 /api/models/available（非标准 /v1/models），
                    # 且 query 是身份载荷（keyfrom）+ 需客户端能力头 —— 走专属路径
                    if oauth_code == "lobsterai":
                        _m = await _fetch_lobsterai_models(_goc(), session, _owner)
                    else:
                        _m = await adapter.list_models(token, provider.base_url, eh)
                    if _m:
                        any_success = True
                        for mi in _m:
                            all_model_infos.setdefault(mi.model_id, mi)
                except Exception as e:
                    logger.warning(f"oauth list_models failed for {provider.name}: {e}")
            if not any_success and oauth_p and oauth_p.static_models:
                list_source = "seed"  # 在线列表失败，改用静态种子兜底
                list_note = f"静态种子兜底（{len(oauth_p.static_models)}个）"
                any_success = True
                # v4.4 倍率静态表（订阅制上游无 USD 单价，按 credit 倍率计费）
                from server.core.oauth_registry import STATIC_PRICE_RATIOS as _SPR
                _ratio_map = _SPR.get(oauth_code) or {}
                for sm in oauth_p.static_models:
                    _r = _ratio_map.get(sm["model_id"])
                    all_model_infos.setdefault(sm["model_id"], ModelInfo(
                        model_id=sm["model_id"], display_name=sm.get("display_name") or sm["model_id"],
                        is_free=(_r == 0.0), input_price=0.0, output_price=0.0,
                        supports_streaming=True, context_length=4096,
                        price_ratio=_r,
                    ))
            # ── F31：CodeBuddy 在线倍率叠加（只补倍率，不动模型列表）─────────
            # 上游 /v3/config 的 models[].credits 是**在线权威倍率**（CN 实测 31 条），
            # 远胜手工静态表（CN 6 条 / Intl 3 条，来自客户端产物、会过期）。
            # ⚠️ 只叠加到已有模型上：/v3/config 的 models[] 不含 auto/balanced-model
            # 等档位别名，若拿它当清单会误删 8 个可用模型（见 fetch_codebuddy_ratios
            # 上方注释）。取不到就保持原值（静态表/既有值），绝不因此清空倍率。
            if str(oauth_code).startswith("codebuddy"):
                try:
                    _online = await fetch_codebuddy_ratios(token, provider.base_url)
                except Exception as e:
                    _online = {}
                    logger.warning("codebuddy 在线倍率叠加失败：%s", e)
                if _online:
                    _hit = 0
                    for _mid, _mi in all_model_infos.items():
                        _r = _online.get(_mid)
                        if _r is None:
                            continue
                        _mi.price_ratio = _r
                        _mi.is_free = (_r == 0.0)
                        _hit += 1
                    logger.info("codebuddy 在线倍率：%s 覆盖 %d/%d 个模型",
                                provider.name, _hit, len(all_model_infos))
                    if list_source == "seed":
                        list_note = f"静态种子兜底（{len(oauth_p.static_models)}个）+ 在线倍率 {_hit} 条"
            if not any_success:
                return {"error": f"OAuth provider '{oauth_code}' 未连接且无静态模型种子，请先在 /providers/oauth 完成连接"}
        else:
            keys = (await session.execute(
                select(ApiKey).where(
                    ApiKey.provider_id == provider.id,
                    ApiKey.is_active == True
                ).order_by(ApiKey.id)
            )).scalars().all()
            if not keys:
                return {"error": "No active API key for this provider"}
            # 多 key 并发 list_models（缩短单服务商刷新耗时；纯网络 IO，无 DB 写）
            async def _fetch_one(k):
                ak = key_manager._crypto.decrypt(k.key_encrypted)
                try:
                    return k.id, await adapter.list_models(ak, provider.base_url, extra_headers)
                except Exception as e:
                    # err_text 兜底：代理抖动时 httpx 异常消息可能为空（'ConnectError: '）
                    logger.warning(f"list_models failed for key {k.id} of {provider.name}: {err_text(e)}")
                    return k.id, []
            results = await asyncio.gather(*[_fetch_one(k) for k in keys]) if keys else []
            for kid, _m in results:
                if _m:
                    any_success = True
                    key_models[kid] = {mi.model_id for mi in _m}
                    for mi in _m:
                        all_model_infos.setdefault(mi.model_id, mi)

        models = list(all_model_infos.values())
        list_ok = any_success
        # 批量预加载该 provider 现有模型（此前每个 model_info 一次 select，N+1）；
        # 提前到定价兜底之前——兜底建模需要先知道库里有没有真清单
        existing_models_map = {
            m.model_id: m for m in (await session.execute(
                select(Model).where(Model.provider_id == provider.id)
            )).scalars().all()
        }
        # 获取定价信息（可用于模型名称回退）
        pricing_result = await fetch_provider_pricing(provider.base_url, timeout=refresh_timeout)
        provider_metadata = pricing_result.pricing
        # 如果 list_models 失败或无结果，尝试从 pricing API 提取模型名。
        # 仅限「库里还没有模型」的场景：已有清单时上游临时故障/封锁，绝不拿页面猜名
        # 重建——文本兜底会把营销页残渣（SVG 坐标等）当模型入库（2026-10-09 tokenharbor 实证）。
        if not models and provider_metadata:
            if existing_models_map:
                list_note = f"在线列表失败；已有 {len(existing_models_map)} 个模型，不做定价页猜名兜底"
                logger.warning(f"Skip pricing fallback for {provider.name}: list failed but {len(existing_models_map)} models exist")
            else:
                models = [ModelInfo(
                    model_id=name,
                    display_name=name,
                    is_free=False,
                    input_price=0.0,
                    output_price=0.0,
                    supports_streaming=True,
                    context_length=4096
                ) for name in provider_metadata]
                list_source = "pricing"
                logger.info(f"Fallback: using {len(models)} models from pricing API for {provider.name}")
        added = 0
        updated = 0
        pricing_updated = 0
        metric_updated = 0
        added_models = []
        removed_models = []
        for model_info in models:
            # 价格来源只有两个：服务商自己的 /api/pricing（公益站自定义价）> 内置表；
            # 拿不到就留 0（未知），由用户在管理面板手动填，不用第三方"标准价"猜测
            remote_metadata = match_model_metadata(model_info.model_id, provider_metadata) if provider_metadata else None
            if remote_metadata and "input" in remote_metadata and "output" in remote_metadata:
                pricing = remote_metadata
            else:
                pricing = get_builtin_pricing(model_info.model_id)
            if pricing:
                model_info.input_price = pricing["input"]
                model_info.output_price = pricing["output"]
                model_info.cache_read_input_price = float(pricing.get("cache_read") or 0)
                model_info.cache_write_input_price = float(pricing.get("cache_write") or 0)
                model_info.is_free = pricing["is_free"]
                pricing_updated += 1
            # 移除了 "auto-mark as free" 逻辑：当上游不返回定价时，不再自动标记免费
            # 用户可在管理面板手动设置价格
            # 免费模型不再自动开启 auto（用户反馈不好使），默认 auto_enabled=False，需手动开启
            auto_enabled = False
            existing_model = existing_models_map.get(model_info.model_id)
            if existing_model:
                existing_model.display_name = model_info.display_name or existing_model.display_name
                # 手动维护的价格（pricing_source == "manual"）不被刷新覆盖
                manual_priced = (existing_model.pricing_source or "").startswith("manual")
                if not manual_priced:
                    existing_model.input_price = model_info.input_price
                    existing_model.output_price = model_info.output_price
                    existing_model.cache_read_input_price = model_info.cache_read_input_price
                    existing_model.cache_write_input_price = model_info.cache_write_input_price
                    existing_model.is_free = model_info.is_free
                existing_model.supports_streaming = model_info.supports_streaming
                existing_model.supports_vision = model_info.supports_vision
                if existing_model.supports_reasoning_effort is None and model_info.supports_reasoning_effort is not None:
                    existing_model.supports_reasoning_effort = model_info.supports_reasoning_effort
                # 窗口只在仍为默认（4096 且无来源标记）时补齐，用户手动改过的窗口不动
                if existing_model.context_length == 4096 and model_info.context_length != 4096                         and not (existing_model.context_source or ""):
                    existing_model.context_length = model_info.context_length
                    existing_model.context_source = "provider"
                # v4.3 能力感知：模态/最大输出（capability_source=manual 的手动编辑永不覆盖）
                if (existing_model.capability_source or "") != "manual":
                    if model_info.input_modalities and not (existing_model.input_modalities or []):
                        existing_model.input_modalities = model_info.input_modalities
                        existing_model.supports_vision = "image" in [
                            str(x).lower() for x in model_info.input_modalities]
                        existing_model.capability_source = "provider"
                    if model_info.max_output_tokens and not existing_model.max_output_tokens:
                        existing_model.max_output_tokens = int(model_info.max_output_tokens)
                # v4.4 倍率：在线目录（Qoder price_factor）优先；静态表值仅补空。
                # manual 手改永不覆盖（与价格同规则）。
                _rsrc = (existing_model.price_ratio_source or "")
                if model_info.price_ratio is not None and _rsrc != "manual":
                    existing_model.price_ratio = model_info.price_ratio
                    # 来源标注：Qoder 在线目录 / CodeBuddy 静态表 / 其它上游自声明
                    if provider.api_type == "qoder":
                        _new_src = "qoder"
                    elif str(getattr(provider, "oauth_code", "") or "").startswith("codebuddy"):
                        _new_src = "codebuddy"
                    else:
                        _new_src = "provider"
                    existing_model.price_ratio_source = _new_src
                    if model_info.price_ratio == 0.0:
                        existing_model.is_free = True
                if remote_metadata:
                    # P1-17: 远程缺字段时不要把本地已有值抹成 None
                    _sr = remote_metadata.get("success_rate")
                    if _sr is not None:
                        existing_model.success_rate = _sr
                    _lm = remote_metadata.get("avg_latency_ms")
                    if _lm is not None:
                        existing_model.avg_latency_ms = _lm
                    _tt = remote_metadata.get("avg_ttft_ms")
                    if _tt is not None:
                        existing_model.avg_ttft_ms = _tt
                    _tp = remote_metadata.get("avg_tps")
                    if _tp is not None:
                        existing_model.avg_tps = _tp
                    # P1-17: 手改价格标记必须被保护——此前无条件写 source_url，
                    # 把 manual 标记抹掉，下轮刷新直接覆盖用户价格。
                    if not manual_priced:
                        existing_model.pricing_source = pricing_result.source_url
                        existing_model.pricing_updated_at = datetime.utcnow()
                    if remote_metadata.get("success_rate") is not None:
                        metric_updated += 1
                if not manual_priced:
                    if existing_model.input_price == 0 and model_info.input_price > 0:
                        existing_model.input_price = model_info.input_price
                    if existing_model.output_price == 0 and model_info.output_price > 0:
                        existing_model.output_price = model_info.output_price
                    if existing_model.cache_read_input_price == 0 and model_info.cache_read_input_price > 0:
                        existing_model.cache_read_input_price = model_info.cache_read_input_price
                    if existing_model.cache_write_input_price == 0 and model_info.cache_write_input_price > 0:
                        existing_model.cache_write_input_price = model_info.cache_write_input_price
                    if not existing_model.is_free and model_info.is_free:
                        existing_model.is_free = True
                        # 不再自动开启 auto；免费模型需用户手动参与选举
                updated += 1
            else:
                # v4.3 能力入库：上游声明 > 名称推断（仅作正向提示，不参与硬拦截）> 未知
                from server.core.model_capabilities import infer_modalities
                _mods = model_info.input_modalities
                _cap_src = "provider" if _mods else ""
                if not _mods:
                    _mods = infer_modalities(model_info.model_id)
                    if _mods:
                        _cap_src = "inferred"
                _vision = bool(_mods and "image" in [str(x).lower() for x in _mods]) \
                    or bool(model_info.supports_vision)
                new_model = Model(
                    provider_id=provider.id,
                    model_id=model_info.model_id,
                    display_name=model_info.display_name or model_info.model_id,
                    input_price=model_info.input_price,
                    output_price=model_info.output_price,
                    cache_read_input_price=model_info.cache_read_input_price,
                    cache_write_input_price=model_info.cache_write_input_price,
                    success_rate=remote_metadata.get("success_rate") if remote_metadata else None,
                    avg_latency_ms=remote_metadata.get("avg_latency_ms") if remote_metadata else None,
                    avg_ttft_ms=remote_metadata.get("avg_ttft_ms") if remote_metadata else None,
                    avg_tps=remote_metadata.get("avg_tps") if remote_metadata else None,
                    pricing_source=pricing_result.source_url if remote_metadata else "",
                    pricing_updated_at=datetime.utcnow() if remote_metadata else None,
                    is_free=model_info.is_free,
                    auto_enabled=auto_enabled,
                    enabled=True,
                    supports_streaming=model_info.supports_streaming,
                    supports_vision=_vision,
                    supports_reasoning_effort=model_info.supports_reasoning_effort,
                    context_length=model_info.context_length,
                    context_source="default" if model_info.context_length == 4096 else "provider",
                    input_modalities=_mods,
                    max_output_tokens=model_info.max_output_tokens,
                    capability_source=_cap_src,
                    # v4.4 倍率（订阅制上游）
                    price_ratio=model_info.price_ratio,
                    price_ratio_source=(
                        ("qoder" if provider.api_type == "qoder" else
                         "codebuddy" if str(getattr(provider, "oauth_code", "") or "").startswith("codebuddy")
                         else "provider")
                        if model_info.price_ratio is not None else ""),
                    priority_boost=0,
                    auto_excluded=False
                )
                session.add(new_model)
                if remote_metadata and remote_metadata.get("success_rate") is not None:
                    metric_updated += 1
                added += 1
                added_models.append({
                    "model_id": model_info.model_id,
                    "display_name": model_info.display_name or model_info.model_id,
                })
                # 新增模型登记进预加载映射：后续同名校走"更新"分支；关联段 flush 前暂无 pk，
                # 由下方关联段的批量查询统一获取
                existing_models_map[model_info.model_id] = new_model
        # v3.5：写模型 ↔ key 归属（model_api_keys）——批量预加载，替代逐 mid 两次查询
        if not atomcode_no_key and key_models:
            now = datetime.utcnow()
            # 一次取 provider 全部模型（含本批新增，flush 后有 pk）
            await session.flush()
            prov_models = (await session.execute(
                select(Model).where(Model.provider_id == provider.id)
            )).scalars().all()
            prov_mid_to_pk = {m.model_id: m.id for m in prov_models}
            existing_rel_set = {
                (r[0], r[1]) for r in (await session.execute(
                    select(ModelApiKey.model_id, ModelApiKey.api_key_id)
                    .join(Model, ModelApiKey.model_id == Model.id)
                    .where(Model.provider_id == provider.id)
                )).all()
            }
            for api_key_id, mids in key_models.items():
                for mid in mids:
                    mpk = prov_mid_to_pk.get(mid)
                    if mpk is None:
                        continue
                    if (mpk, api_key_id) in existing_rel_set:
                        await session.execute(
                            update(ModelApiKey)
                            .where(ModelApiKey.model_id == mpk, ModelApiKey.api_key_id == api_key_id)
                            .values(last_seen_at=now)
                        )
                    else:
                        session.add(ModelApiKey(model_id=mpk, api_key_id=api_key_id, last_seen_at=now))
                        existing_rel_set.add((mpk, api_key_id))
            # 初始兜底：对 provider 下尚无任何归属记录的 model，归属 provider 全部 active key
            # （老数据 / 手动模型在首次 refresh 后即可轮询，无需等各 key 实际返回）
            all_keys_ids = list(key_models.keys())
            if all_keys_ids:
                touched_models = {mpk for mpk, _ in existing_rel_set}
                for m in prov_models:
                    if m.id not in touched_models:
                        for akid in all_keys_ids:
                            session.add(ModelApiKey(model_id=m.id, api_key_id=akid, last_seen_at=now))
                            existing_rel_set.add((m.id, akid))
            # 过期清理：last_seen_at 早于阈值（7 天）的归属批量删除
            expiry = now - timedelta(days=7)
            stale_ids = (await session.execute(
                select(ModelApiKey.id)
                .join(Model, ModelApiKey.model_id == Model.id)
                .where(Model.provider_id == provider.id, ModelApiKey.last_seen_at < expiry)
            )).scalars().all()
            if stale_ids:
                await session.execute(delete(ModelApiKey).where(ModelApiKey.id.in_(stale_ids)))

        # 清理已从上游下架、但本地仍存在的自动同步模型（失效模型）
        # 安全约束：仅在 list_models 成功(list_ok)且返回非空(models)时才清理，
        # 避免上游临时故障/超时导致误删该 provider 全部模型；手动添加(is_manual=True)的模型始终保留
        removed = 0
        if get_config().model_refresh.remove_missing_models and list_ok and models:
            fetched_ids = {m.model_id for m in models}
            stale = (await session.execute(
                select(Model).where(
                    Model.provider_id == provider.id,
                    Model.model_id.notin_(fetched_ids),
                    Model.is_manual.is_(False),
                )
            )).scalars().all()
            for m in stale:
                removed_models.append({
                    "model_id": m.model_id,
                    "display_name": m.display_name or m.model_id,
                })
                await session.delete(m)
                removed += 1
            if stale:
                logger.info(f"Removed {removed} stale model(s) for {provider.name} (not in upstream list)")
        await session.commit()
        total = await session.execute(
            select(Model).where(Model.provider_id == provider.id)
        )
        total_count = len(list(total.scalars().all()))
        return {
            "added": added,
            "updated": updated,
            "removed": removed,
            "added_models": added_models,
            "removed_models": removed_models,
            "total": total_count,
            "pricing_updated": pricing_updated,
            "metric_updated": metric_updated,
            "pricing_source": pricing_result.source_url,
            # online/seed/pricing；全部失败且未兜底时为 None（日志记 unknown）
            "list_source": list_source if (any_success or list_source != "online") else None,
            "list_note": list_note if list_note else (
                "在线列表成功" if list_ok else ("上游不可用（种子兜底）" if list_source=="seed" else None)
            ),
            "pricing_error": pricing_result.error,
        }
