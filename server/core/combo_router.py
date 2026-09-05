"""
Combos 组合路由器
策略:
  - fallback:   从候选顺序尝试第一个可用，失败则取下一个，最多 max_fallbacks 次
  - round_robin: 每次按内存中维护的下标轮到下一个候选，失败同 fallback 跳下一个
  - fusion:     未实现（strategy=fusion 当前按 fallback 顺序处理；扇出+judge 合并待后续设计）

调用入口：
  - 模型名以 "combo:" 前缀（例如 "combo:my-fast"）
  - 解析后由 ComboRouter.resolve_combo 找到 Combo 实体
"""
from __future__ import annotations
import logging
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from server.models.combo import Combo
from server.models.provider import Provider
from server.models.model import Model

logger = logging.getLogger(__name__)

_COMBO_PREFIX = "combo:"
# 内存中维护 round_robin 下标 — 进程重启不保留但可接受
_rr_cursors: Dict[int, int] = {}


async def find_combo_by_name(db: AsyncSession, name: str) -> Optional[Combo]:
    try:
        result = await db.execute(
            select(Combo).where(Combo.name == name, Combo.enabled == True).limit(1)
        )
        return result.scalar_one_or_none()
    except SQLAlchemyError as e:
        logger.warning("find_combo_by_name(%s) failed: %s", name, e)
        return None


def is_combo_request(model_name: str) -> Tuple[bool, Optional[str]]:
    """
    判断请求是否是 combo 路由。
    返回 (is_combo, combo_name)
    """
    if not model_name:
        return False, None
    if not model_name.startswith(_COMBO_PREFIX):
        return False, None
    name = model_name[len(_COMBO_PREFIX):]
    if not name:
        return False, None
    return True, name


async def resolve_combo_targets(
    db: AsyncSession, combo: Combo
) -> List[Dict[str, Any]]:
    """
    解析 combo.model_ids JSON，找到对应 (Provider, Model) ORM 对，
    过滤掉失效或不存在的目标。

    返回 [{"provider": <Provider>, "model": <Model>, "full_id": "..."}]
    按用户定义的顺序。
    """
    raw_items: List[Any] = combo.model_ids or []
    targets: List[Dict[str, Any]] = []
    stale_idx: List[int] = []  # 失效条目下标：模型已不存在 / model_id 为空 → 自动删除
    for idx, item in enumerate(raw_items):
        prov_name = None
        mod_id = None
        if isinstance(item, dict):
            prov_name = item.get("provider")
            mod_id = item.get("model_id")
        elif isinstance(item, str):
            if "/" in item:
                prov_name, mod_id = item.split("/", 1)
            else:
                # 旧格式：只有 model_id，需要后续查 provider
                mod_id = item
        if not prov_name or not mod_id:
            # 模型名空 / 缺字段 → 脏条目，标记为失效待删除
            stale_idx.append(idx)
            continue
        try:
            p_result = await db.execute(
                select(Provider).where(Provider.name == prov_name).limit(1)
            )
            provider = p_result.scalar_one_or_none()
            if not provider:
                # provider 已不存在 → 失效
                stale_idx.append(idx)
                continue
            # v4.0: 服务商被禁用 → 请求时跳过该候选，但保留组合顺序不删除
            if not getattr(provider, "enabled", True):
                continue
            m_result = await db.execute(
                select(Model).where(
                    Model.provider_id == provider.id,
                    Model.model_id == mod_id,
                    Model.enabled == True,
                ).limit(1)
            )
            model = m_result.scalar_one_or_none()
            if not model:
                # 模型已在刷新中被上游移除 → 失效，自动删除
                stale_idx.append(idx)
                continue
            targets.append({
                "provider": provider,
                "model": model,
                "full_id": f"{prov_name}/{mod_id}",
            })
        except SQLAlchemyError as e:
            logger.warning("resolve combo item %s/%s failed: %s", prov_name, mod_id, e)
    # ── 自动删除失效模型：把脏条目从 combo.model_ids 剔除并落库 ──
    if stale_idx:
        removed = [raw_items[i] for i in stale_idx]
        cleaned = [it for i, it in enumerate(raw_items) if i not in stale_idx]
        combo.model_ids = cleaned
        try:
            await db.commit()
            logger.info(
                "[组合路由] combo '%s' 自动删除 %d 个失效候选（已移除：%s），剩余 %d 个",
                combo.name, len(stale_idx), removed, len(cleaned),
            )
        except SQLAlchemyError as e:
            logger.warning("[组合路由] combo '%s' 清理失效候选失败：%s", combo.name, e)
            try:
                await db.rollback()
            except Exception:
                pass
    return targets


async def prune_stale_combo_targets(db: AsyncSession) -> int:
    """
    主动清理所有 combo 中指向已失效的候选：
      - model_id 为空 / 缺字段
      - provider 不存在
      - model 已不存在（刷新后被上游移除）
    仅删「真没了」的候选；enabled=False 的禁用模型只跳过不删。
    返回被清理的 combo 数量。在模型刷新后调用，保证前端 combo 列表立刻干净。
    """
    try:
        result = await db.execute(select(Combo).where(Combo.enabled == True))
        combos = list(result.scalars().all())
    except SQLAlchemyError as e:
        logger.warning("prune_stale_combo_targets: list combos failed: %s", e)
        return 0
    pruned_combos = 0
    for combo in combos:
        raw_items = combo.model_ids or []
        if not raw_items:
            continue
        cleaned = []
        removed = []
        for item in raw_items:
            prov_name = None
            mod_id = None
            if isinstance(item, dict):
                prov_name = item.get("provider")
                mod_id = item.get("model_id")
            elif isinstance(item, str):
                if "/" in item:
                    prov_name, mod_id = item.split("/", 1)
                else:
                    mod_id = item
            if not prov_name or not mod_id:
                removed.append(item)
                continue
            try:
                p = (await db.execute(
                    select(Provider).where(Provider.name == prov_name).limit(1)
                )).scalar_one_or_none()
                if not p:
                    removed.append(item)
                    continue
                # v4.0: 服务商被禁用 → 保留候选不删除（仅请求时跳过），保持组合顺序
                if not getattr(p, "enabled", True):
                    cleaned.append(item)
                    continue
                m = (await db.execute(
                    select(Model).where(
                        Model.provider_id == p.id,
                        Model.model_id == mod_id,
                        Model.enabled == True,
                    ).limit(1)
                )).scalar_one_or_none()
                if not m:
                    removed.append(item)
                    continue
            except SQLAlchemyError as e:
                logger.warning("prune combo %s item %s/%s failed: %s", combo.name, prov_name, mod_id, e)
                cleaned.append(item)  # 查询异常时保守保留，不误删
                continue
            cleaned.append(item)
        if removed:
            combo.model_ids = cleaned
            pruned_combos += 1
            logger.info(
                "[组合路由] 刷新后清理 combo '%s' 的 %d 个失效候选：%s，剩余 %d 个",
                combo.name, len(removed), removed, len(cleaned),
            )
    if pruned_combos:
        try:
            await db.commit()
        except SQLAlchemyError as e:
            logger.warning("[组合路由] 刷新后清理 combo 落库失败：%s", e)
            try:
                await db.rollback()
            except Exception:
                pass
    return pruned_combos


def pick_next_index(combo_id: int, total: int, strategy: str) -> int:
    """根据策略选择起始下标"""
    if total <= 0:
        return 0
    if strategy == "round_robin":
        idx = _rr_cursors.get(combo_id, 0) % total
        _rr_cursors[combo_id] = (idx + 1) % total
        return idx
    # fallback：永远从 0 开始
    return 0
