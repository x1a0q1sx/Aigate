"""
Combos 组合路由器
策略:
  - fallback:   从候选顺序尝试第一个可用，失败则取下一个，最多 max_fallbacks 次
  - round_robin: 每次按内存中维护的下标轮到下一个候选，失败同 fallback 跳下一个
  - fusion:     暂未实现（9Router 也只是占位），后续做扇出+judge 合并

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


# ── Fusion 策略：扇出并行请求多个候选 + judge 合并 ────────────────────
import asyncio
import json as _json

async def _call_one_candidate(db, target: dict, request, conversation_id: str) -> dict:
    """
    对单个候选 target 走一次非流式 chat_completion，
    返回 {
        "target_full_id": "...",
        "content": "模型回复",
        "ok": bool,
        "error": str,
        "usage": {...}
    }
    复用 v1_router 内部已有的"直接路由"逻辑——但为了避免循环依赖，这里单独做精简实现。
    """
    try:
        provider = target["provider"]
        model = target["model"]
        from server.core.key_rotator import get_key_rotator
        rotator = get_key_rotator()
        picked = await rotator.pick_key_for_model(db, model)
        if not picked:
            return {"target_full_id": target["full_id"], "ok": False, "error": "no_active_key"}
        _kid, key_plain = picked
        # 适配器
        from server.core.model_catalog import create_adapter_for_provider
        from server.schemas.chat import ChatMessage
        from server.config import get_config
        cfg = get_config()
        adapter = create_adapter_for_provider(provider.api_type)
        # 复用 apply_rtk 的预处理（尊重 token_saver.enabled 总开关）
        from server.core.token_saver import apply_rtk
        ts_cfg = getattr(cfg, 'token_saver', None)
        ts_enabled = getattr(ts_cfg, 'enabled', True) if ts_cfg else True
        msgs = [m.model_dump() if hasattr(m, "model_dump") else m for m in request.messages]
        new_msgs, _stats = apply_rtk([ChatMessage(**m) if isinstance(m, dict) else m for m in msgs], enabled=ts_enabled)
        upstream_req = request.model_copy(update={
            "model": model.model_id,
            "messages": new_msgs,
            "stream": False,
        })
        extra_headers = provider.headers or None
        result = await adapter.chat_completion(upstream_req, key_plain, provider.base_url, extra_headers)
        content = ""
        if isinstance(result, dict):
            choices = result.get("choices") or []
            content = (choices[0]["message"]["content"] if choices else "") or ""
        rotator.mark_success(_kid)
        return {
            "target_full_id": target["full_id"],
            "ok": True,
            "content": content,
            "raw": result if isinstance(result, dict) else {},
            "usage": (result or {}).get("usage", {}) if isinstance(result, dict) else {},
        }
    except Exception as e:
        try:
            rotator.mark_failure(_kid)
        except Exception:
            pass
        return {"target_full_id": target["full_id"], "ok": False, "error": str(e)[:200]}


async def fusion_resolve_and_judge(
    db, combo, request, conversation_id: str,
    top_k: int = 3, judge_full_id: str = "auto"
) -> dict:
    """
    Fusion 策略：
    1) 取 combo 候选的前 top_k（默认 3）个 target 并行扇出请求
    2) 收集所有成功答案
    3) 用 judge 模型（默认 auto，即系统当前的 best）从多份答案中合成最终回复
       — judge 给出一个 "synthesis" 字段带最佳原文片段 + 合并
    返回：{"content": "...", "raw_candidates": [...], "judge_prompt": "...", "usage": {...}}
    """
    targets = await resolve_combo_targets(db, combo)
    if not targets:
        return {"content": "", "error": "combo_no_active_targets"}
    selected = targets[: top_k]
    # 并发请求
    results = await asyncio.gather(*[
        asyncio.create_task(_call_one_candidate(db, t, request, conversation_id))
        for t in selected
    ])
    ok_results = [r for r in results if r.get("ok")]
    if not ok_results:
        return {"content": "", "error": "all_candidates_failed",
                "raw_candidates": results}
    if len(ok_results) == 1:
        return {"content": ok_results[0]["content"],
                "raw_candidates": results, "usage": ok_results[0].get("usage", {})}
    # 走 judge 合并
    synthesis_prompt = _build_synth_prompt(ok_results, request)
    judge_response, judge_usage = await _invoke_judge(db, synthesis_prompt, judge_full_id)
    return {
        "content": judge_response,
        "raw_candidates": results,
        "judge_prompt": synthesis_prompt,
        "usage": judge_usage,
    }


def _build_synth_prompt(candidates: list, original_request) -> str:
    """
    构造 judge 提示词：让 LLM 综合多份候选答案，给最终版回复。
    """
    parts = ["You are an expert editor. Multiple AI assistants answered the same question."]
    parts.append("Combine the strengths of each answer into a single final response.")
    parts.append("Prefer the most accurate and complete details, drop contradictions, and keep it concise.")
    parts.append("\n---\nQUESTION (excerpt):\n")
    msgs = getattr(original_request, "messages", None) or []
    user_msg = ""
    for m in msgs:
        if hasattr(m, "role") and m.role == "user":
            content = m.content if isinstance(m.content, str) else str(m.content)
            user_msg = content[:2000]
            break
    parts.append(user_msg)
    parts.append("\n---\nCANDIDATES:\n")
    for i, c in enumerate(candidates):
        parts.append(f"\n### Candidate {i+1} ({c['target_full_id']}):\n")
        parts.append(str(c.get("content", ""))[:4000])
    parts.append("\n---\nFINAL ANSWER (respond with one composed answer only):\n")
    return "".join(parts)


async def _invoke_judge(db, prompt: str, judge_full_id: str) -> tuple:
    """调用 judge 模型 — "auto" 时用现有 ranking top1"""
    try:
        from server.core.auto_router import AutoRouter
        from server.schemas.chat import ChatMessage, ChatCompletionRequest
        if judge_full_id == "auto":
            ar = AutoRouter()
            candidates = await ar.get_candidates(db)
            if not candidates:
                # fallback: 用第一个 model 直接调用
                from server.models.model import Model
                from sqlalchemy import select
                fallback = (await db.execute(
                    select(Model).where(Model.enabled == True).limit(1)
                )).scalar_one_or_none()
                if not fallback:
                    return "", {}
                judge_full_id = f"{fallback.provider_id}/{fallback.model_id}"
                from server.models.provider import Provider
                p = await db.get(Provider, fallback.provider_id)
                judge_full_id = f"{p.name}/{fallback.model_id}"
        req = ChatCompletionRequest(
            model=judge_full_id,
            messages=[ChatMessage(role="user", content=prompt)],
            stream=False,
            max_tokens=2000,
            temperature=0.2,
        )
        # 直接路由
        if "/" in judge_full_id:
            prov_name, mod_id = judge_full_id.split("/", 1)
            from server.core.model_catalog import ModelCatalog
            model = await ModelCatalog().get_by_full_id(db, prov_name, mod_id)
        else:
            from server.core.model_catalog import ModelCatalog
            ml = await ModelCatalog().list_models(db, enabled_only=True)
            model = next((m for m in ml if m.model_id == judge_full_id), None)
        if not model:
            return "", {}
        from server.models.provider import Provider as ProvModel
        provider = await db.get(ProvModel, model.provider_id)
        if not provider:
            return "", {}
        from server.core.key_rotator import get_key_rotator
        rotator = get_key_rotator()
        picked = await rotator.pick_key_for_model(db, model)
        if not picked:
            return "", {}
        _kid, key_plain = picked
        from server.core.model_catalog import create_adapter_for_provider
        adapter = create_adapter_for_provider(provider.api_type)
        upstream_req = req.model_copy(update={"model": model.model_id})
        result = await adapter.chat_completion(upstream_req, key_plain, provider.base_url, provider.headers or None)
        rotator.mark_success(_kid)
        content = ""
        if isinstance(result, dict):
            choices = result.get("choices") or []
            content = (choices[0]["message"]["content"] if choices else "") or ""
        return content, (result or {}).get("usage", {}) if isinstance(result, dict) else {}
    except Exception:
        return "", {}
