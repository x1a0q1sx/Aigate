"""服务商密钥启用/停用行为测试：

  1) KeyRotator 的模型归属选择路径过滤 DB is_active（v3.5 起 provider 级
     pick_active_key 已过滤，模型级路径曾漏掉，本测试锁死）
  2) reactivate() 清除进程内熔断（hard_disabled/冷却/失败计数）
"""
import pytest
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.key_rotator import KeyRotator
from server.models.api_key import ApiKey
from server.models.base import Base
from server.models.model import Model  # noqa: F401  model_api_keys 外键引用 models 表
from server.models.model_api_key import ModelApiKey
from server.models.provider import Provider


class FakeCrypto:
    def encrypt(self, s):
        return "enc:" + (s or "")

    def decrypt(self, s):
        return (s or "").removeprefix("enc:")


async def _make_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_pick_key_for_model_skips_deactivated_keys(tmp_path):
    Session = await _make_db(tmp_path)
    async with Session() as db:
        prov = Provider(name="p1", base_url="https://x.example/v1", api_type="openai_compat")
        db.add(prov)
        await db.flush()
        k_on = ApiKey(provider_id=prov.id, key_encrypted="enc:KEY-ON", key_prefix="sk-on", is_active=True)
        k_off = ApiKey(provider_id=prov.id, key_encrypted="enc:KEY-OFF", key_prefix="sk-off", is_active=False)
        db.add_all([k_on, k_off])
        await db.flush()
        from server.models.model import Model
        m = Model(provider_id=prov.id, model_id="m1")
        db.add(m)
        await db.flush()
        db.add_all([
            ModelApiKey(model_id=m.id, api_key_id=k_off.id),
            ModelApiKey(model_id=m.id, api_key_id=k_on.id),
        ])
        await db.commit()
        model_id, prov_id = m.id, prov.id

    rot = KeyRotator(crypto=FakeCrypto())
    model = SimpleNamespace(id=model_id, provider_id=prov_id)
    async with Session() as db:
        picked = await rot.pick_key_for_model(db, model)
    assert picked is not None
    # 停用的 k_off 绝不能被选中（哪怕它是集合里唯一的归属 key 之一）
    assert picked[1] == "KEY-ON"


@pytest.mark.asyncio
async def test_pick_key_for_model_all_deactivated_falls_back(tmp_path):
    Session = await _make_db(tmp_path)
    async with Session() as db:
        prov = Provider(name="p1", base_url="https://x.example/v1", api_type="openai_compat")
        db.add(prov)
        await db.flush()
        k_a = ApiKey(provider_id=prov.id, key_encrypted="enc:A", key_prefix="a", is_active=False)
        k_b = ApiKey(provider_id=prov.id, key_encrypted="enc:B", key_prefix="b", is_active=True)
        db.add_all([k_a, k_b])
        await db.flush()
        from server.models.model import Model
        m = Model(provider_id=prov.id, model_id="m1")
        db.add(m)
        await db.flush()
        db.add(ModelApiKey(model_id=m.id, api_key_id=k_a.id))  # 模型只归属一把已停用的 key
        await db.commit()
        model_id, prov_id = m.id, prov.id

    rot = KeyRotator(crypto=FakeCrypto())
    model = SimpleNamespace(id=model_id, provider_id=prov_id)
    async with Session() as db:
        picked = await rot.pick_key_for_model(db, model)
    # 归属 key 停用 → 兜底该 provider 第一把 active（k_b）
    assert picked is not None and picked[1] == "B"


def test_reactivate_clears_all_in_memory_state():
    rot = KeyRotator(crypto=FakeCrypto())
    from datetime import datetime, timedelta
    rot._hard_disabled.add(11)
    rot._fail_count[11] = 3
    rot._cooldown_until[11] = datetime.utcnow() + timedelta(seconds=60)
    assert rot._is_available(11) is False
    rot.reactivate(11)
    assert 11 not in rot._hard_disabled
    assert rot._is_available(11) is True


@pytest.mark.asyncio
async def test_pick_active_key_respects_is_active(tmp_path):
    Session = await _make_db(tmp_path)
    async with Session() as db:
        prov = Provider(name="p1", base_url="https://x.example/v1", api_type="openai_compat")
        db.add(prov)
        await db.flush()
        db.add(ApiKey(provider_id=prov.id, key_encrypted="enc:X", key_prefix="x", is_active=False))
        await db.commit()
        prov_id = prov.id
    rot = KeyRotator(crypto=FakeCrypto())
    async with Session() as db:
        assert await rot.pick_active_key(db, prov_id) is None
