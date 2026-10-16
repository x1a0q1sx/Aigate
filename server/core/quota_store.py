"""quota_limits 表读写辅助（2026-10-16）。

写入在 oauth_client.note_request_result（识别到每日限额文案时 upsert）；
这里提供查询侧：
  - active_limit_owners(db, provider_code, model_id)：自动选号跳过用
    → {owner: reset_at}（仅未过期）
  - list_active(db)：两个列表接口（模型页 / OAuth 连接页）聚合展示用
    → [QuotaLimit]（仅未过期）
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def active_limit_owners(db: AsyncSession, provider_code: str, model_id: str) -> dict:
    """该 (服务商, 模型) 下仍在限额期的账号 → {owner: reset_at}；失败返回 {}（永不抛）。"""
    if not provider_code or not model_id:
        return {}
    try:
        from server.models.quota_limit import QuotaLimit
        rows = (await db.execute(
            select(QuotaLimit).where(
                QuotaLimit.provider_code == provider_code,
                QuotaLimit.model_id == model_id,
                QuotaLimit.reset_at > datetime.utcnow(),
            )
        )).scalars().all()
        return {r.owner: r.reset_at for r in rows}
    except Exception:
        return {}


async def list_active(db: AsyncSession) -> list:
    """全部未过期的限额记录（列表接口聚合用）；失败返回 []（永不抛）。"""
    try:
        from server.models.quota_limit import QuotaLimit
        return (await db.execute(
            select(QuotaLimit).where(QuotaLimit.reset_at > datetime.utcnow())
        )).scalars().all()
    except Exception:
        return []
