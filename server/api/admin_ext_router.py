"""管理扩展 API（v2 路线新增能力的统一 CRUD 面）：

- /admin/api/gateway-keys   D1 下游网关密钥（明文仅创建时返回一次）
- /admin/api/aliases        E1 模型别名
- /admin/api/cache          A4 响应缓存状态/清除
- /admin/api/gateway-keys/{id}/usage  D2 该 key 今日用量
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from server.db import AsyncSessionLocal
from server.models.gateway_key import GatewayKey
from server.models.model_alias import ModelAlias

router = APIRouter(prefix="/admin/api")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ─────────────────────────── D1: 下游网关密钥 ───────────────────────────

class GatewayKeyCreate(BaseModel):
    name: str = ""
    expires_at: Optional[datetime] = None
    rpm_limit: Optional[int] = None
    daily_token_limit: Optional[int] = None
    daily_cost_limit_usd: Optional[float] = None
    over_limit_action: str = "reject"   # reject / warn


class GatewayKeyUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    expires_at: Optional[datetime] = None
    rpm_limit: Optional[int] = None
    daily_token_limit: Optional[int] = None
    daily_cost_limit_usd: Optional[float] = None
    over_limit_action: Optional[str] = None


@router.get("/gateway-keys")
async def list_gateway_keys(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(GatewayKey).order_by(GatewayKey.id)
    )).scalars().all()
    return {"keys": [r.to_info() for r in rows]}


@router.post("/gateway-keys")
async def create_gateway_key(body: GatewayKeyCreate, db: AsyncSession = Depends(get_db)):
    from server.core.gateway_keys import generate_key, hash_key
    plaintext = generate_key()
    row = GatewayKey(
        name=body.name or "",
        key_hash=hash_key(plaintext),
        key_prefix=plaintext[:10],
        enabled=True,
        expires_at=body.expires_at,
        rpm_limit=body.rpm_limit,
        daily_token_limit=body.daily_token_limit,
        daily_cost_limit_usd=body.daily_cost_limit_usd,
        over_limit_action=body.over_limit_action if body.over_limit_action in ("reject", "warn") else "reject",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"key": row.to_info(), "plaintext": plaintext}  # 明文仅此一次


@router.patch("/gateway-keys/{key_id}")
async def update_gateway_key(key_id: int, body: GatewayKeyUpdate, db: AsyncSession = Depends(get_db)):
    row = await db.get(GatewayKey, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="Gateway key not found")
    data = body.model_dump(exclude_unset=True)
    if "over_limit_action" in data and data["over_limit_action"] not in ("reject", "warn"):
        raise HTTPException(status_code=400, detail="over_limit_action must be reject|warn")
    for k, v in data.items():
        setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return row.to_info()


@router.delete("/gateway-keys/{key_id}")
async def delete_gateway_key(key_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(GatewayKey, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="Gateway key not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": key_id}


@router.get("/gateway-keys/{key_id}/usage")
async def gateway_key_usage(key_id: int, db: AsyncSession = Depends(get_db)):
    """D2: 该 key 今日（UTC）用量与预算余量"""
    row = await db.get(GatewayKey, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="Gateway key not found")
    from server.core.gateway_keys import check_budget
    budget = await check_budget(db, key_id)
    return {"key": row.to_info(), "usage": budget}


# ─────────────────────────── E1: 模型别名 ───────────────────────────

class AliasCreate(BaseModel):
    alias: str
    target: str
    note: str = ""
    enabled: bool = True


class AliasUpdate(BaseModel):
    alias: Optional[str] = None
    target: Optional[str] = None
    note: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/aliases")
async def list_aliases(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ModelAlias).order_by(ModelAlias.id)
    )).scalars().all()
    return {"aliases": [r.to_info() for r in rows]}


@router.post("/aliases")
async def create_alias(body: AliasCreate, db: AsyncSession = Depends(get_db)):
    if not body.alias.strip() or not body.target.strip():
        raise HTTPException(status_code=400, detail="alias/target 不能为空")
    dup = (await db.execute(
        select(ModelAlias).where(ModelAlias.alias == body.alias.strip())
    )).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail=f"别名 '{body.alias}' 已存在")
    row = ModelAlias(alias=body.alias.strip(), target=body.target.strip(),
                     note=body.note or "", enabled=body.enabled)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    from server.core.alias_service import invalidate_cache
    invalidate_cache()
    return row.to_info()


@router.patch("/aliases/{alias_id}")
async def update_alias(alias_id: int, body: AliasUpdate, db: AsyncSession = Depends(get_db)):
    row = await db.get(ModelAlias, alias_id)
    if not row:
        raise HTTPException(status_code=404, detail="Alias not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(row, k, v.strip() if isinstance(v, str) else v)
    await db.commit()
    await db.refresh(row)
    from server.core.alias_service import invalidate_cache
    invalidate_cache()
    return row.to_info()


@router.delete("/aliases/{alias_id}")
async def delete_alias(alias_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(ModelAlias, alias_id)
    if not row:
        raise HTTPException(status_code=404, detail="Alias not found")
    await db.delete(row)
    await db.commit()
    from server.core.alias_service import invalidate_cache
    invalidate_cache()
    return {"deleted": alias_id}


# ─────────────────────────── A4: 响应缓存 ───────────────────────────

@router.get("/cache")
async def cache_info():
    from server.core.response_cache import response_cache
    return response_cache.info()


@router.delete("/cache")
async def cache_clear():
    from server.core.response_cache import response_cache
    return {"cleared": response_cache.clear()}
