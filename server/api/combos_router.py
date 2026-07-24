"""
/admin/api/combos Combos 组合 CRUD 路由
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from server.db import AsyncSessionLocal
from server.models.combo import Combo
from server.schemas.combo import (
    ComboCreate, ComboUpdate, ComboResponse, ComboListResponse
)
from server.config import get_config

router = APIRouter(prefix="/admin/api/combos")
config = get_config()


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


@router.get("")
async def list_combos(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Combo).order_by(Combo.priority.desc(), Combo.id))
    combos = list(result.scalars().all())
    return {
        "items": [ComboResponse.model_validate(c).model_dump() for c in combos],
        "total": len(combos),
    }


@router.post("")
async def create_combo(data: ComboCreate, db: AsyncSession = Depends(get_db)):
    # 名字唯一约束校验
    existing = await db.execute(select(Combo).where(Combo.name == data.name).limit(1))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Combo 名字 '{data.name}' 已存在")
    combo = Combo(
        name=data.name,
        description=data.description or "",
        strategy=data.strategy,
        model_ids=data.model_ids or [],
        priority=data.priority,
        enabled=data.enabled,
    )
    db.add(combo)
    await db.commit()
    await db.refresh(combo)
    return ComboResponse.model_validate(combo)


@router.get("/{combo_id}")
async def get_combo(combo_id: int, db: AsyncSession = Depends(get_db)):
    combo = await db.get(Combo, combo_id)
    if not combo:
        raise HTTPException(status_code=404, detail="Combo not found")
    return ComboResponse.model_validate(combo)


@router.put("/{combo_id}")
async def update_combo(combo_id: int, data: ComboUpdate, db: AsyncSession = Depends(get_db)):
    combo = await db.get(Combo, combo_id)
    if not combo:
        raise HTTPException(status_code=404, detail="Combo not found")
    if data.name is not None:
        # 校验唯一
        if data.name != combo.name:
            dup = await db.execute(select(Combo).where(Combo.name == data.name).limit(1))
            if dup.scalar_one_or_none():
                raise HTTPException(status_code=400, detail=f"Combo 名字 '{data.name}' 已存在")
        combo.name = data.name
    if data.description is not None:
        combo.description = data.description
    if data.strategy is not None:
        s = data.strategy.lower()
        if s not in ("fallback", "round_robin", "fusion"):
            raise HTTPException(status_code=400, detail="strategy 校验失败")
        combo.strategy = s
    if data.model_ids is not None:
        combo.model_ids = data.model_ids or []
    if data.priority is not None:
        combo.priority = data.priority
    if data.enabled is not None:
        combo.enabled = data.enabled
    await db.commit()
    await db.refresh(combo)
    return ComboResponse.model_validate(combo)


@router.delete("/{combo_id}")
async def delete_combo(combo_id: int, db: AsyncSession = Depends(get_db)):
    combo = await db.get(Combo, combo_id)
    if not combo:
        raise HTTPException(status_code=404, detail="Combo not found")
    await db.delete(combo)
    await db.commit()
    return {"ok": True}
