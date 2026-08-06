"""
/admin/api/* 管理 API
v2.0: 新增手动测速 API
"""
from typing import Optional, Any
from datetime import datetime, timezone
import logging
logger = logging.getLogger(__name__)
import httpx
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete
from server.db import AsyncSessionLocal
from server.models.provider import Provider
from server.models.api_key import ApiKey
from server.models.model import Model
from server.models.health_check import HealthCheck
from server.models.request_log import RequestLog
from server.schemas.provider import (
    ProviderCreate, ProviderUpdate, ProviderResponse,
    ApiKeyCreate, ApiKeyResponse,
    ModelUpdate, ModelInfoResponse, ModelsRefreshResponse,
    PingResult, PingAllResponse, LatencyStatsResponse
)
from server.schemas.admin import (
    DashboardSummary, HealthStatusResponse, HealthStatusItem,
    PlaygroundRequest
)
from server.core.key_manager import KeyManager
from server.core.model_catalog import ModelCatalog
from server.core.health_checker import HealthChecker
from server.core.crypto_service import get_crypto_service
from server.config import get_config, save_config
from sqlalchemy import select, func, desc
router = APIRouter(prefix="/admin/api")

def utc_iso(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
config = get_config()
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
# 实例化服务
_key_manager = KeyManager(get_crypto_service())
_model_catalog = ModelCatalog()
@router.get("/token-saver")
async def get_token_saver_config():
    """读取 RTK Token Saver 配置 + 可用规则清单"""
    from server.core.token_saver import list_rules
    ts = getattr(config, 'token_saver', None)
    return {
        "enabled": getattr(ts, 'enabled', True) if ts else True,
        "min_chars": getattr(ts, 'min_chars', 80) if ts else 80,
        "rules": list_rules(),
    }
@router.put("/token-saver")
async def update_token_saver_config(enabled: Optional[bool] = None, min_chars: Optional[int] = None):
    """更新 RTK Token Saver 配置并持久化"""
    if not hasattr(config, 'token_saver') or config.token_saver is None:
        from server.config import TokenSaverConfig
        config.token_saver = TokenSaverConfig()
    if enabled is not None:
        config.token_saver.enabled = enabled
    if min_chars is not None:
        config.token_saver.min_chars = max(0, min_chars)
    save_config()
    return {"ok": True, "enabled": config.token_saver.enabled, "min_chars": config.token_saver.min_chars}
@router.post("/token-saver/preview")
async def preview_token_saver(data: dict):
    """Preview compression effects for the Token Saver UI."""
    messages = data.get("messages") or []
    if isinstance(data.get("text"), str) and not messages:
        messages = [{"role": "user", "content": data.get("text", "")}]
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    from server.core.compress_service import compress_messages
    return compress_messages(
        messages,
        rtk_enabled=data.get("rtk_enabled"),
        caveman_enabled=data.get("caveman_enabled"),
        ponytail_enabled=data.get("ponytail_enabled"),
    )

@router.post("/atomcode/load-auth")
async def atomcode_load_auth(path: Optional[str] = None):
    """读取 ~/.atomcode/auth.toml（可由 query 参数 path 覆盖）并解析为 AtomCode 鉴权 JSON。

    该文件由 AtomCode / AtomGit 桌面客户端本地写入，结构与适配器期望的 auth
    字典一致（access_token 在顶层，[user] 段为嵌套对象）。解析后直接回传，
    前端可一键填入 AtomCode 直连反代鉴权框，无需用户手动复制 JSON。
    """
    import os, tomllib
    raw = path or os.path.expanduser("~/.atomcode/auth.toml")
    ap = os.path.abspath(os.path.expanduser(raw))
    if not os.path.exists(ap):
        raise HTTPException(status_code=404, detail=f"未找到 AtomCode 鉴权文件: {ap}")
    try:
        with open(ap, "rb") as f:
            auth = tomllib.load(f)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析 TOML 失败: {e}")
    if not isinstance(auth, dict) or not auth.get("access_token"):
        raise HTTPException(status_code=400, detail="auth.toml 缺少 access_token 字段")
    return {"path": ap, "auth": auth}

@router.get("/atomcode/exe-status")
async def atomcode_exe_status():
    """返回 atomcode 可执行文件探测状态，供前端判断是否需要提示用户配置。"""
    from server.adapters.atomcode_daemon import atomcode_exe_status as _st
    return _st()

@router.post("/atomcode/set-exe-path")
async def atomcode_set_exe_path(data: dict):
    """持久化用户配置的 atomcode 可执行文件/目录，并立即拉起 daemon 做健康检查验证。

    请求体: {"path": "安装目录 或 exe 绝对路径"}
    """
    from server.adapters.atomcode_daemon import save_atomcode_exe_path, get_manager
    raw = data.get("path") if isinstance(data, dict) else None
    try:
        exe = save_atomcode_exe_path(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 立即拉起 daemon 并做健康检查，给用户即时反馈（最多等待约 20s）
    try:
        client = await get_manager().get_client()
        await client.is_running()
    except Exception as e:
        # 路径已保存，但 daemon 拉起失败：返回 200 + warning，便于前端提示但仍保留配置
        return {"ok": True, "exe_path": exe, "daemon_running": False,
                "warning": f"路径已保存，但 daemon 拉起/健康检查失败：{e}"}
    return {"ok": True, "exe_path": exe, "daemon_running": True}

@router.get("/providers")
async def list_providers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Provider).order_by(Provider.id))
    providers = list(result.scalars().all())
    return [ProviderResponse.model_validate(p) for p in providers]


# ==========================================================================
# 一键备份 / 恢复（全系统配置 + 数据）
# ==========================================================================
from server.models.combo import Combo
from server.models.routing_config import RoutingWeights
from server.models.oauth_token import OAuthToken

BACKUP_KIND = "aigate.backup"
BACKUP_VERSION = 1

# 备份时跳过的 config 节（含敏感或运行时专属配置）
_CONFIG_SKIP_SECTIONS = {"server", "database"}


@router.get("/backup")
async def full_backup(db: AsyncSession = Depends(get_db)):
    """一键导出全系统配置：服务商+模型+密钥+OAuth+组合+路由权重+config.yaml 设置。

    密钥和 OAuth token 以明文导出（用于换机迁移）。
    """
    from server.core.crypto_service import get_crypto_service
    crypto = get_crypto_service()

    # ── 1) 服务商 + 模型 + 密钥 ──
    providers_out = []
    all_providers = list((await db.execute(
        select(Provider).order_by(Provider.id)
    )).scalars().all())

    for p in all_providers:
        models = list((await db.execute(
            select(Model).where(Model.provider_id == p.id).order_by(Model.model_id)
        )).scalars().all())

        keys = list((await db.execute(
            select(ApiKey).where(ApiKey.provider_id == p.id).order_by(ApiKey.id)
        )).scalars().all())

        exported_keys = []
        for k in keys:
            try:
                plaintext = crypto.decrypt(k.key_encrypted)
            except Exception as e:
                logger.warning("[备份] 服务商 %s 的密钥 #%s 解密失败：%s", p.name, k.id, e)
                continue
            exported_keys.append({
                "label": k.label or "",
                "key": plaintext,
                "is_active": bool(k.is_active),
            })

        providers_out.append({
            "name": p.name,
            "base_url": p.base_url,
            "api_type": p.api_type,
            "credential_type": p.credential_type or "api_key",
            "oauth_code": p.oauth_code,
            "headers": p.headers or {},
            "description": p.description or "",
            "models": [_serialize_model_for_export(m) for m in models],
            "keys": exported_keys,
        })

    # ── 2) OAuth tokens ──
    oauth_tokens = list((await db.execute(
        select(OAuthToken).order_by(OAuthToken.id)
    )).scalars().all())
    oauth_out = []
    for t in oauth_tokens:
        entry = {
            "provider_code": t.provider_code,
            "owner": t.owner or "__default",
            "token_type": t.token_type or "Bearer",
            "scope": t.scope or "",
            "is_active": bool(t.is_active),
        }
        try:
            entry["access_token"] = crypto.decrypt(t.access_token_enc)
        except Exception:
            entry["access_token"] = ""
        try:
            entry["refresh_token"] = crypto.decrypt(t.refresh_token_enc) if t.refresh_token_enc else ""
        except Exception:
            entry["refresh_token"] = ""
        if t.expires_at:
            entry["expires_at"] = utc_iso(t.expires_at)
        oauth_out.append(entry)

    # ── 3) 组合路由 ──
    combos = list((await db.execute(select(Combo).order_by(Combo.id))).scalars().all())
    combos_out = [{
        "name": c.name,
        "description": c.description or "",
        "strategy": c.strategy,
        "model_ids": c.model_ids or [],
        "priority": c.priority,
        "enabled": bool(c.enabled),
    } for c in combos]

    # ── 4) 路由权重 ──
    rw = (await db.execute(select(RoutingWeights).where(RoutingWeights.id == 1))).scalar_one_or_none()
    weights_out = None
    if rw:
        weights_out = {
            "w_speed": float(rw.w_speed),
            "w_intel": float(rw.w_intel),
            "w_stab": float(rw.w_stab),
        }

    # ── 5) config.yaml 设置（排除 server/database 等运行时节）──
    config_dump = config.model_dump()
    config_out = {k: v for k, v in config_dump.items() if k not in _CONFIG_SKIP_SECTIONS}
    # 脱敏 security 中的 encryption_key（各机器不同），保留 aigate_api_key
    if "security" in config_out:
        config_out["security"].pop("encryption_key", None)

    return {
        "kind": BACKUP_KIND,
        "version": BACKUP_VERSION,
        "exported_at": utc_iso(datetime.utcnow()),
        "summary": {
            "providers": len(providers_out),
            "models": sum(len(p["models"]) for p in providers_out),
            "keys": sum(len(p["keys"]) for p in providers_out),
            "oauth_tokens": len(oauth_out),
            "combos": len(combos_out),
        },
        "providers": providers_out,
        "oauth_tokens": oauth_out,
        "combos": combos_out,
        "routing_weights": weights_out,
        "config": config_out,
    }


class BackupRestoreRequest(BaseModel):
    """恢复请求体"""
    data: Any
    restore_keys: bool = True
    restore_models: bool = True
    restore_combos: bool = True
    restore_oauth: bool = True
    restore_weights: bool = True
    restore_config: bool = True
    conflict: str = "merge"   # skip / merge / replace


@router.post("/restore")
async def full_restore(payload: BackupRestoreRequest, db: AsyncSession = Depends(get_db)):
    """一键恢复全系统配置。接受 /backup 导出的 JSON bundle。"""
    import json as _json
    from server.core.crypto_service import get_crypto_service
    from server.core.oauth_client import get_oauth_client
    crypto = get_crypto_service()

    raw = payload.data
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"JSON 解析失败: {e}")

    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="数据格式错误")

    kind = raw.get("kind", "")
    if kind and kind != BACKUP_KIND and kind != EXPORT_KIND:
        raise HTTPException(status_code=400, detail=f"不认识的备份类型: {kind}")

    conflict = (payload.conflict or "merge").lower()
    if conflict not in ("skip", "merge", "replace"):
        raise HTTPException(status_code=400, detail="conflict 必须是 skip / merge / replace 之一")

    stats = {
        "providers_created": 0, "providers_updated": 0, "providers_skipped": 0,
        "models_added": 0, "models_updated": 0,
        "keys_added": 0,
        "oauth_restored": 0,
        "combos_created": 0, "combos_updated": 0, "combos_skipped": 0,
        "weights_restored": False,
        "config_restored": False,
    }
    errors = []

    # ── 1) 服务商 + 模型 + 密钥 ──
    entries = raw.get("providers", [])
    if not isinstance(entries, list):
        entries = []

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        base_url = str(entry.get("base_url") or "").strip()
        if not name or not base_url:
            continue

        try:
            existing = (await db.execute(
                select(Provider).where(Provider.name == name)
            )).scalar_one_or_none()

            if existing and conflict == "skip":
                stats["providers_skipped"] += 1
                continue

            if existing:
                provider = existing
                provider.base_url = base_url
                provider.api_type = entry.get("api_type") or provider.api_type
                provider.credential_type = entry.get("credential_type") or provider.credential_type
                if "enabled" in entry:
                    provider.enabled = bool(entry.get("enabled", True))
                if "oauth_code" in entry:
                    provider.oauth_code = entry.get("oauth_code")
                if entry.get("headers") is not None:
                    provider.headers = entry.get("headers") or {}
                if entry.get("description") is not None:
                    provider.description = entry.get("description") or ""
                stats["providers_updated"] += 1

                if conflict == "replace":
                    await db.execute(delete(ApiKey).where(ApiKey.provider_id == provider.id))
                    await db.execute(delete(Model).where(Model.provider_id == provider.id))
            else:
                provider = Provider(
                    name=name, base_url=base_url,
                    api_type=entry.get("api_type") or "openai_compat",
                    credential_type=entry.get("credential_type") or "api_key",
                    oauth_code=entry.get("oauth_code"),
                    enabled=bool(entry.get("enabled", True)),
                    headers=entry.get("headers") or {},
                    description=entry.get("description") or "",
                )
                db.add(provider)
                stats["providers_created"] += 1

            await db.flush()

            # 模型
            if payload.restore_models:
                for raw_model in (entry.get("models") or []):
                    if not isinstance(raw_model, dict):
                        continue
                    mid = str(raw_model.get("model_id") or "").strip()
                    if not mid:
                        continue
                    fields = {f: raw_model[f] for f in _MODEL_FIELDS if f in raw_model and f != "model_id"}
                    found = (await db.execute(
                        select(Model).where(Model.provider_id == provider.id, Model.model_id == mid)
                    )).scalar_one_or_none()
                    if found:
                        for f, v in fields.items():
                            setattr(found, f, v)
                        stats["models_updated"] += 1
                    else:
                        db.add(Model(
                            provider_id=provider.id, model_id=mid,
                            display_name=fields.pop("display_name", None) or mid,
                            created_at=datetime.utcnow(), **fields,
                        ))
                        stats["models_added"] += 1

            # 密钥
            if payload.restore_keys and entry.get("keys"):
                existing_plain = set()
                for k in (await db.execute(
                    select(ApiKey).where(ApiKey.provider_id == provider.id)
                )).scalars().all():
                    try:
                        existing_plain.add(crypto.decrypt(k.key_encrypted))
                    except Exception:
                        pass
                for raw_key in entry["keys"]:
                    if isinstance(raw_key, str):
                        raw_key = {"key": raw_key}
                    if not isinstance(raw_key, dict):
                        continue
                    plaintext = str(raw_key.get("key") or "").strip()
                    if not plaintext or plaintext in existing_plain:
                        continue
                    existing_plain.add(plaintext)
                    db.add(ApiKey(
                        provider_id=provider.id,
                        key_encrypted=crypto.encrypt(plaintext),
                        key_prefix=plaintext[:3] if len(plaintext) >= 3 else plaintext,
                        label=str(raw_key.get("label") or ""),
                        is_active=bool(raw_key.get("is_active", True)),
                    ))
                    stats["keys_added"] += 1

        except Exception as e:
            logger.warning("[恢复] 服务商 %s 恢复失败：%s", name, e)
            errors.append(f"服务商「{name}」: {str(e)[:200]}")

    # ── 2) OAuth tokens ──
    if payload.restore_oauth:
        for raw_tok in (raw.get("oauth_tokens") or []):
            if not isinstance(raw_tok, dict):
                continue
            pc = str(raw_tok.get("provider_code") or "").strip()
            at = str(raw_tok.get("access_token") or "").strip()
            if not pc or not at:
                continue
            owner = raw_tok.get("owner") or "__default"
            try:
                existing_tok = (await db.execute(
                    select(OAuthToken).where(OAuthToken.provider_code == pc, OAuthToken.owner == owner)
                )).scalar_one_or_none()
                if existing_tok and conflict == "skip":
                    continue
                if existing_tok:
                    existing_tok.access_token_enc = crypto.encrypt(at)
                    rt = str(raw_tok.get("refresh_token") or "").strip()
                    if rt:
                        existing_tok.refresh_token_enc = crypto.encrypt(rt)
                    existing_tok.is_active = bool(raw_tok.get("is_active", True))
                else:
                    rt = str(raw_tok.get("refresh_token") or "").strip()
                    db.add(OAuthToken(
                        provider_code=pc, owner=owner,
                        access_token_enc=crypto.encrypt(at),
                        refresh_token_enc=crypto.encrypt(rt) if rt else None,
                        token_type=raw_tok.get("token_type") or "Bearer",
                        scope=raw_tok.get("scope") or "",
                        is_active=bool(raw_tok.get("is_active", True)),
                    ))
                stats["oauth_restored"] += 1
            except Exception as e:
                errors.append(f"OAuth {pc}/{owner}: {str(e)[:200]}")

    # ── 3) 组合路由 ──
    if payload.restore_combos:
        for raw_combo in (raw.get("combos") or []):
            if not isinstance(raw_combo, dict):
                continue
            cname = str(raw_combo.get("name") or "").strip()
            if not cname:
                continue
            try:
                existing_combo = (await db.execute(
                    select(Combo).where(Combo.name == cname)
                )).scalar_one_or_none()
                if existing_combo and conflict == "skip":
                    stats["combos_skipped"] += 1
                    continue
                if existing_combo:
                    existing_combo.description = raw_combo.get("description") or ""
                    existing_combo.strategy = raw_combo.get("strategy") or "fallback"
                    existing_combo.model_ids = raw_combo.get("model_ids") or []
                    existing_combo.priority = raw_combo.get("priority", 0)
                    existing_combo.enabled = bool(raw_combo.get("enabled", True))
                    stats["combos_updated"] += 1
                else:
                    db.add(Combo(
                        name=cname,
                        description=raw_combo.get("description") or "",
                        strategy=raw_combo.get("strategy") or "fallback",
                        model_ids=raw_combo.get("model_ids") or [],
                        priority=raw_combo.get("priority", 0),
                        enabled=bool(raw_combo.get("enabled", True)),
                    ))
                    stats["combos_created"] += 1
            except Exception as e:
                errors.append(f"组合 {cname}: {str(e)[:200]}")

    # ── 4) 路由权重 ──
    if payload.restore_weights and raw.get("routing_weights"):
        rw_data = raw["routing_weights"]
        try:
            rw = (await db.execute(select(RoutingWeights).where(RoutingWeights.id == 1))).scalar_one_or_none()
            if rw:
                rw.w_speed = rw_data.get("w_speed", 0.3)
                rw.w_intel = rw_data.get("w_intel", 0.5)
                rw.w_stab = rw_data.get("w_stab", 0.2)
            else:
                db.add(RoutingWeights(
                    id=1,
                    w_speed=rw_data.get("w_speed", 0.3),
                    w_intel=rw_data.get("w_intel", 0.5),
                    w_stab=rw_data.get("w_stab", 0.2),
                ))
            stats["weights_restored"] = True
        except Exception as e:
            errors.append(f"路由权重: {str(e)[:200]}")

    # ── 5) config.yaml 设置 ──
    if payload.restore_config and raw.get("config"):
        try:
            cfg_data = raw["config"]
            for section_key, section_val in cfg_data.items():
                if section_key in _CONFIG_SKIP_SECTIONS:
                    continue
                if not isinstance(section_val, dict):
                    continue
                target = getattr(config, section_key, None)
                if target is None:
                    continue
                for k, v in section_val.items():
                    if hasattr(target, k):
                        setattr(target, k, v)
            save_config()
            stats["config_restored"] = True
        except Exception as e:
            errors.append(f"config.yaml: {str(e)[:200]}")

    await db.commit()

    return {
        "ok": len(errors) == 0,
        "stats": stats,
        "errors": errors,
    }


# ==========================================================================
# 服务商配置导入 / 导出
# 注意：这两个路由必须注册在 /providers/{provider_id} 之前，否则 "export"
# 会被当成 provider_id 去匹配参数化路由。
# ==========================================================================
EXPORT_KIND = "aigate.providers"
EXPORT_VERSION = 1

# 导出/导入时携带的模型字段（其余如 success_rate / avg_latency 属运行期统计，不搬运）
_MODEL_FIELDS = (
    "model_id", "display_name", "input_price", "output_price", "is_free",
    "enabled", "auto_enabled", "auto_excluded", "supports_streaming",
    "supports_vision", "context_length", "priority_boost", "is_manual",
    "request_overrides", "pricing_source",
)


class ProviderImportRequest(BaseModel):
    """导入请求体。data 允许直接传对象，也允许传原始 JSON 字符串（方便前端粘贴）。"""
    data: Any
    # skip 跳过同名 / merge 合并补齐（默认）/ replace 覆盖（清空原有模型与密钥）
    conflict: str = "merge"
    import_keys: bool = True
    import_models: bool = True


def _serialize_model_for_export(m: Model) -> dict:
    out = {}
    for f in _MODEL_FIELDS:
        v = getattr(m, f, None)
        if f == "request_overrides" and not v:
            continue
        if f == "pricing_source" and not v:
            continue
        out[f] = v
    return out


@router.get("/providers/export")
async def export_providers(
    include_keys: bool = False,
    provider_ids: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """导出服务商配置为可移植 JSON。

    - include_keys=true 时明文导出 API Key（用于换机迁移），默认不导出。
    - provider_ids 为逗号分隔 id 列表，缺省导出全部。
    """
    query = select(Provider).order_by(Provider.id)
    wanted: Optional[set] = None
    if provider_ids:
        try:
            wanted = {int(x) for x in provider_ids.split(",") if x.strip()}
        except ValueError:
            raise HTTPException(status_code=400, detail="provider_ids 必须是逗号分隔的整数")
        if not wanted:
            raise HTTPException(status_code=400, detail="provider_ids 不能为空")
        query = query.where(Provider.id.in_(wanted))

    providers = list((await db.execute(query)).scalars().all())
    if wanted and not providers:
        raise HTTPException(status_code=404, detail="指定的服务商都不存在")

    bundle_providers = []
    key_count = 0
    model_count = 0
    for p in providers:
        models = list((await db.execute(
            select(Model).where(Model.provider_id == p.id).order_by(Model.model_id)
        )).scalars().all())
        model_count += len(models)

        entry = {
            "name": p.name,
            "base_url": p.base_url,
            "api_type": p.api_type,
            "credential_type": p.credential_type or "api_key",
            "oauth_code": p.oauth_code,
            "enabled": p.enabled if p.enabled is not None else True,
            "headers": p.headers or {},
            "description": p.description or "",
            "models": [_serialize_model_for_export(m) for m in models],
        }

        keys = list((await db.execute(
            select(ApiKey).where(ApiKey.provider_id == p.id).order_by(ApiKey.id)
        )).scalars().all())
        if include_keys:
            exported_keys = []
            for k in keys:
                try:
                    plaintext = _key_manager._crypto.decrypt(k.key_encrypted)
                except Exception as e:
                    # 单个密钥解密失败（换过 secret_key）不该让整包导出失败
                    logger.warning("[导出] 服务商 %s 的密钥 #%s 解密失败：%s", p.name, k.id, e)
                    continue
                exported_keys.append({
                    "label": k.label or "",
                    "key": plaintext,
                    "is_active": bool(k.is_active),
                })
            entry["keys"] = exported_keys
            key_count += len(exported_keys)
        else:
            # 不导出明文时仍记录数量，导入方可据此知道需要自行补密钥
            entry["key_count"] = len(keys)

        bundle_providers.append(entry)

    return {
        "kind": EXPORT_KIND,
        "version": EXPORT_VERSION,
        "exported_at": utc_iso(datetime.utcnow()),
        "includes_keys": include_keys,
        "summary": {
            "providers": len(bundle_providers),
            "models": model_count,
            "keys": key_count,
        },
        "providers": bundle_providers,
    }


@router.post("/providers/import")
async def import_providers(payload: ProviderImportRequest, db: AsyncSession = Depends(get_db)):
    """导入服务商配置。返回逐个服务商的处理结果，便于前端展示明细。"""
    import json as _json

    conflict = (payload.conflict or "merge").lower()
    if conflict not in ("skip", "merge", "replace"):
        raise HTTPException(status_code=400, detail="conflict 必须是 skip / merge / replace 之一")

    raw = payload.data
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"JSON 解析失败: {e}")

    # 允许三种输入：完整 bundle / 直接给 providers 数组 / 单个 provider 对象
    if isinstance(raw, dict) and "providers" in raw:
        if raw.get("kind") and raw.get("kind") != EXPORT_KIND:
            raise HTTPException(status_code=400, detail=f"不认识的导出类型: {raw.get('kind')}")
        entries = raw.get("providers")
    elif isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict) and raw.get("name"):
        entries = [raw]
    else:
        raise HTTPException(status_code=400, detail="缺少 providers 数组")

    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=400, detail="providers 为空")

    results = []
    stats = {"created": 0, "updated": 0, "skipped": 0, "failed": 0,
             "models_added": 0, "models_updated": 0, "keys_added": 0}

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            results.append({"index": idx, "name": None, "action": "failed", "error": "条目不是对象"})
            stats["failed"] += 1
            continue

        name = str(entry.get("name") or "").strip()
        base_url = str(entry.get("base_url") or "").strip()
        if not name or not base_url:
            results.append({"index": idx, "name": name or None, "action": "failed",
                            "error": "缺少 name 或 base_url"})
            stats["failed"] += 1
            continue

        try:
            existing = (await db.execute(
                select(Provider).where(Provider.name == name)
            )).scalar_one_or_none()

            if existing and conflict == "skip":
                results.append({"index": idx, "name": name, "action": "skipped",
                                "reason": "同名服务商已存在"})
                stats["skipped"] += 1
                continue

            if existing:
                provider = existing
                provider.base_url = base_url
                provider.api_type = entry.get("api_type") or provider.api_type
                provider.credential_type = entry.get("credential_type") or provider.credential_type
                if "enabled" in entry:
                    provider.enabled = bool(entry.get("enabled", True))
                if "oauth_code" in entry:
                    provider.oauth_code = entry.get("oauth_code")
                if entry.get("headers") is not None:
                    provider.headers = entry.get("headers") or {}
                if entry.get("description") is not None:
                    provider.description = entry.get("description") or ""
                action = "updated"
                stats["updated"] += 1

                if conflict == "replace":
                    # 覆盖模式：先清掉原有模型与密钥，再按包内容重建
                    await db.execute(delete(ApiKey).where(ApiKey.provider_id == provider.id))
                    await db.execute(delete(Model).where(Model.provider_id == provider.id))
            else:
                provider = Provider(
                    name=name,
                    base_url=base_url,
                    api_type=entry.get("api_type") or "openai_compat",
                    credential_type=entry.get("credential_type") or "api_key",
                    oauth_code=entry.get("oauth_code"),
                    enabled=bool(entry.get("enabled", True)),
                    headers=entry.get("headers") or {},
                    description=entry.get("description") or "",
                )
                db.add(provider)
                action = "created"
                stats["created"] += 1

            await db.flush()   # 拿到新建 provider 的自增 id

            m_added = m_updated = k_added = 0

            # ── 模型 ──
            if payload.import_models:
                for raw_model in (entry.get("models") or []):
                    if not isinstance(raw_model, dict):
                        continue
                    mid = str(raw_model.get("model_id") or "").strip()
                    if not mid:
                        continue
                    fields = {f: raw_model[f] for f in _MODEL_FIELDS
                              if f in raw_model and f != "model_id"}
                    found = (await db.execute(
                        select(Model).where(Model.provider_id == provider.id, Model.model_id == mid)
                    )).scalar_one_or_none()
                    if found:
                        for f, v in fields.items():
                            setattr(found, f, v)
                        m_updated += 1
                    else:
                        db.add(Model(
                            provider_id=provider.id,
                            model_id=mid,
                            display_name=fields.pop("display_name", None) or mid,
                            created_at=datetime.utcnow(),
                            **fields,
                        ))
                        m_added += 1

            # ── 密钥 ──
            if payload.import_keys and entry.get("keys"):
                existing_plain = set()
                for k in (await db.execute(
                    select(ApiKey).where(ApiKey.provider_id == provider.id)
                )).scalars().all():
                    try:
                        existing_plain.add(_key_manager._crypto.decrypt(k.key_encrypted))
                    except Exception:
                        pass
                for raw_key in entry["keys"]:
                    if isinstance(raw_key, str):
                        raw_key = {"key": raw_key}
                    if not isinstance(raw_key, dict):
                        continue
                    plaintext = str(raw_key.get("key") or "").strip()
                    if not plaintext or plaintext in existing_plain:
                        continue   # 同一把密钥不重复导入
                    existing_plain.add(plaintext)
                    db.add(ApiKey(
                        provider_id=provider.id,
                        key_encrypted=_key_manager._crypto.encrypt(plaintext),
                        key_prefix=plaintext[:3] if len(plaintext) >= 3 else plaintext,
                        label=str(raw_key.get("label") or ""),
                        is_active=bool(raw_key.get("is_active", True)),
                    ))
                    k_added += 1

            stats["models_added"] += m_added
            stats["models_updated"] += m_updated
            stats["keys_added"] += k_added
            results.append({
                "index": idx, "name": name, "action": action,
                "models_added": m_added, "models_updated": m_updated, "keys_added": k_added,
            })
        except Exception as e:
            await db.rollback()
            logger.warning("[导入] 服务商 %s 导入失败：%s", name, e)
            results.append({"index": idx, "name": name, "action": "failed", "error": str(e)[:300]})
            stats["failed"] += 1
            # 回滚会丢掉本轮之前累计的未提交改动，如实反馈而不是假装成功
            stats["created"] = stats["updated"] = 0
            stats["models_added"] = stats["models_updated"] = stats["keys_added"] = 0
            return {"ok": False, "conflict": conflict, "stats": stats, "results": results,
                    "detail": f"在导入「{name}」时出错，本次导入已整体回滚"}

    await db.commit()
    return {"ok": True, "conflict": conflict, "stats": stats, "results": results}
@router.post("/providers")
async def create_provider(data: ProviderCreate, db: AsyncSession = Depends(get_db)):
    provider = Provider(
        name=data.name,
        base_url=data.base_url,
        api_type=data.api_type,
        credential_type=data.credential_type,
        oauth_code=data.oauth_code,
        enabled=data.enabled,
        headers=data.headers or {},
        description=data.description or ""
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return ProviderResponse.model_validate(provider)
@router.put("/providers/{provider_id}")
async def update_provider(provider_id: int, data: ProviderUpdate, db: AsyncSession = Depends(get_db)):
    provider = await db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if data.name is not None:
        provider.name = data.name
    if data.base_url is not None:
        provider.base_url = data.base_url
    if data.api_type is not None:
        provider.api_type = data.api_type
    if data.credential_type is not None:
        provider.credential_type = data.credential_type
    if data.oauth_code is not None:
        provider.oauth_code = data.oauth_code
    if data.enabled is not None:
        provider.enabled = data.enabled
    if data.headers is not None:
        provider.headers = data.headers
    if data.description is not None:
        provider.description = data.description
    await db.commit()
    await db.refresh(provider)
    return ProviderResponse.model_validate(provider)
@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    provider = await db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    # v3.3: SQLite 默认不执行外键约束，显式级联删除关联的 models / api_keys
    # 防止删完 provider 后孤儿 model 行残留，导致模型列表混乱
    from server.models.model import Model as _M
    from server.models.api_key import ApiKey as _AK
    await db.execute(delete(_AK).where(_AK.provider_id == provider_id))
    await db.execute(delete(_M).where(_M.provider_id == provider_id))
    # 清理该 provider 所有 model 在 HealthChecker 中的缓存（避免过时延迟数据干扰）
    from server.main import get_health_checker
    hc = get_health_checker()
    if hc:
        orphan_model_ids = [m.id for m in (await db.execute(
            select(_M.id).where(_M.provider_id == provider_id)
        )).scalars().all()]
        for mid in orphan_model_ids:
            hc._status_cache.pop(mid, None)
            hc._cooling.pop(mid, None)
            hc._fail_count.pop(mid, None)
    await db.delete(provider)
    await db.commit()
    return {"ok": True}
@router.get("/keys")
async def list_keys(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    keys = list(result.scalars().all())
    return [ApiKeyResponse.model_validate(k) for k in keys]

@router.get("/keys/{key_id}/reveal")
async def reveal_key(key_id: int, db: AsyncSession = Depends(get_db)):
    plaintext = await _key_manager.decrypt_key(db, key_id)
    if plaintext is None:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"id": key_id, "key": plaintext}

@router.get("/aigate-key")
async def get_aigate_key(reveal: bool = False):
    key = getattr(config.security, "aigate_api_key", "") or ""
    if not key:
        return {"configured": False, "masked": "", "key": "" if reveal else None}
    masked = _key_manager.mask_key(key)
    return {
        "configured": True,
        "masked": masked,
        "key": key if reveal else None,
    }
@router.post("/keys")
async def add_key(data: ApiKeyCreate, db: AsyncSession = Depends(get_db)):
    provider = await db.get(Provider, data.provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    key = await _key_manager.add_key(db, data.provider_id, data.key, data.label or "")
    return ApiKeyResponse.model_validate(key)
@router.delete("/keys/{key_id}")
async def delete_key(key_id: int, db: AsyncSession = Depends(get_db)):
    success = await _key_manager.delete_key(db, key_id)
    if not success:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"ok": True}
@router.get("/models")
async def list_models(
    provider_id: Optional[int] = None,
    is_free: Optional[bool] = None,
    auto_enabled: Optional[bool] = None,
    q: Optional[str] = None,
    limit: int = 0,
    offset: int = 0,
    db: AsyncSession = Depends(get_db)
):
    """列出模型，附带最新延迟信息；limit>0 时分页返回（offset 起 limit 条）"""
    from server.main import get_health_checker
    hc = get_health_checker()
    from server.models.provider import Provider as ProvModel
    models = await _model_catalog.list_models(db, provider_id, is_free, auto_enabled)
    # 预加载 provider 关系；v3.3 跳过 provider 已被删除的孤儿 model
    orphan_count = 0
    valid_models = []
    for m in models:
        provider = await db.get(ProvModel, m.provider_id)
        if not provider:
            orphan_count += 1
            continue
        m.provider = provider
        valid_models.append(m)
    models = valid_models
    result = []
    if q:
        needle = q.strip().lower()
        models = [m for m in models if needle in (m.model_id or "").lower() or needle in (m.display_name or "").lower() or needle in ((m.provider.name if getattr(m, "provider", None) else "") or "").lower()]
    for m in models:
        item = ModelInfoResponse.from_orm(m)
        # 附加延迟 + 冷却信息
        if hc:
            cached = hc.get_cached_status(m.id)
            if cached:
                item.latency_ms = cached.latency_ms
                item.health_status = cached.status
            # 冷却信息
            item.fail_count = hc._fail_count.get(m.id, 0)
            cd = hc._cooling.get(m.id)
            if cd and datetime.now(timezone.utc).replace(tzinfo=None) < cd:
                item.cooldown_until = cd.isoformat() + "Z"
        result.append(item)
    if limit and limit > 0:
        result = result[offset:offset + limit]
    return result
@router.put("/models/{model_id}")
async def update_model(model_id: int, data: ModelUpdate, db: AsyncSession = Depends(get_db)):
    model = await _model_catalog.update_model(
        db, model_id,
        display_name=data.display_name,
        auto_enabled=data.auto_enabled,
        enabled=data.enabled,
        input_price=data.input_price,
        output_price=data.output_price,
        cache_read_input_price=data.cache_read_input_price,
        cache_write_input_price=data.cache_write_input_price,
        success_rate=data.success_rate,
        is_free=data.is_free,
        priority_boost=data.priority_boost,
        auto_excluded=data.auto_excluded,
        request_overrides=data.request_overrides
    )
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return ModelInfoResponse.from_orm(model)
@router.delete("/models/orphans")
async def delete_orphan_models(db: AsyncSession = Depends(get_db)):
    """Delete model rows whose provider no longer exists."""
    provider_ids = set((await db.execute(select(Provider.id))).scalars().all())
    orphan_models = (await db.execute(select(Model).where(~Model.provider_id.in_(provider_ids)))).scalars().all()
    orphan_ids = [m.id for m in orphan_models]
    if not orphan_ids:
        return {"ok": True, "deleted": 0}

    from server.models.rate_limit import RateLimitState
    await db.execute(delete(HealthCheck).where(HealthCheck.model_id.in_(orphan_ids)))
    await db.execute(delete(RateLimitState).where(RateLimitState.model_id.in_(orphan_ids)))
    await db.execute(delete(Model).where(Model.id.in_(orphan_ids)))

    from server.main import get_health_checker
    hc = get_health_checker()
    if hc:
        for mid in orphan_ids:
            hc._status_cache.pop(mid, None)
            hc._cooling.pop(mid, None)
            hc._fail_count.pop(mid, None)

    await db.commit()
    return {"ok": True, "deleted": len(orphan_ids), "model_ids": orphan_ids}
@router.delete("/models/{model_id}")
async def delete_model(model_id: int, db: AsyncSession = Depends(get_db)):
    """删除指定模型"""
    from sqlalchemy import delete as sqldelete
    result = await db.execute(sqldelete(Model).where(Model.id == model_id))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Model not found")
    await db.commit()
    return {"ok": True, "deleted": model_id}
@router.post("/models/refresh")
async def refresh_models(
    provider_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db)
):
    """刷新模型列表，如果 provider_id 指定则只刷新该服务商"""
    if provider_id:
        provider = await db.get(Provider, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
        providers = [provider]
    else:
        # 刷新所有有密钥的 provider
        result = await db.execute(
            select(Provider).join(ApiKey, Provider.id == ApiKey.provider_id).distinct()
        )
        providers = list(result.scalars().all())
    total_added = 0
    total_updated = 0
    total_total = 0
    total_removed = 0
    total_pricing_updated = 0
    total_metric_updated = 0
    pricing_sources = []
    added_details = []
    removed_details = []
    for provider in providers:
        result = await _model_catalog.refresh_models_from_provider(db, provider, _key_manager)
        if "error" in result:
            continue
        total_added += result.get("added", 0)
        total_updated += result.get("updated", 0)
        total_total += result.get("total", 0)
        total_removed += result.get("removed", 0)
        total_pricing_updated += result.get("pricing_updated", 0)
        total_metric_updated += result.get("metric_updated", 0)
        source = result.get("pricing_source")
        if source and source not in pricing_sources:
            pricing_sources.append(source)
        am = result.get("added_models") or []
        if am:
            added_details.append({
                "provider_id": provider.id,
                "provider_name": provider.name,
                "models": am,
            })
        rm = result.get("removed_models") or []
        if rm:
            removed_details.append({
                "provider_id": provider.id,
                "provider_name": provider.name,
                "models": rm,
            })
    # 刷新后主动清理所有 combo 中指向已失效(被上游移除)模型的脏条目
    try:
        from server.core.combo_router import prune_stale_combo_targets
        pruned = await prune_stale_combo_targets(db)
        if pruned:
            logger.info("[组合路由] 模型刷新后共清理 %d 个 combo 的失效候选", pruned)
    except Exception as e:
        logger.warning("[组合路由] 模型刷新后 combo 清理失败：%s", e)
    return ModelsRefreshResponse(
        added=total_added,
        updated=total_updated,
        removed=total_removed,
        total=total_total,
        pricing_updated=total_pricing_updated,
        metric_updated=total_metric_updated,
        pricing_sources=pricing_sources,
        added_details=added_details,
        removed_details=removed_details,
    )
# ========== 手动添加模型 ==========
class ManualModelAdd(BaseModel):
    model_id: str
    display_name: str = ""
    input_price: float = 0.0
    output_price: float = 0.0
    cache_read_input_price: float = 0.0
    cache_write_input_price: float = 0.0

@router.post("/providers/{provider_id}/models")
async def add_model_to_provider(provider_id: int, data: ManualModelAdd, db: AsyncSession = Depends(get_db)):
    """手动为指定服务商添加模型"""
    provider = await db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    existing = (await db.execute(
        select(Model).where(Model.provider_id == provider_id, Model.model_id == data.model_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Model '{data.model_id}' already exists")
    now = datetime.utcnow()
    model = Model(
        provider_id=provider_id,
        model_id=data.model_id,
        display_name=data.display_name or data.model_id,
        input_price=data.input_price,
        output_price=data.output_price,
        cache_read_input_price=data.cache_read_input_price or 0.0,
        cache_write_input_price=data.cache_write_input_price or 0.0,
        is_free=(data.input_price == 0.0 and data.output_price == 0.0),
        enabled=True,
        auto_enabled=True,
        supports_streaming=True,
        context_length=4096,
        created_at=now,
        is_manual=True,
    )
    db.add(model)
    await db.commit()
    return {"id": model.id, "model_id": model.model_id, "added": True}

# ========== 导入定价 JSON ==========
class PricingImport(BaseModel):
    json_data: str  # 原始 JSON 字符串

@router.post("/providers/{provider_id}/import-pricing")
async def import_provider_pricing(provider_id: int, data: PricingImport, db: AsyncSession = Depends(get_db)):
    """导入 newapi/one-api 格式的 /api/pricing 返回数据，自动匹配模型并更新定价"""
    provider = await db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    try:
        import json as _j
        raw = _j.loads(data.json_data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    items = raw.get("data", []) if isinstance(raw, dict) else []
    if not items:
        raise HTTPException(status_code=400, detail="No 'data' array found in JSON")
    
    group_ratio = (raw.get("group_ratio", {}) or {}).get("default", 1) if isinstance(raw, dict) else 1
    
    updated = 0
    created = 0
    now = datetime.utcnow()
    for item in items:
        name = item.get("model_name", item.get("model", ""))
        if not name:
            continue
        mr = float(item.get("model_ratio", item.get("model_price", 0)) or 0)
        cr = float(item.get("completion_ratio", 1) or 1)
        mp = float(item.get("model_price", 0) or 0)
        
        if mp > 0:
            inp = round(mp * mr * group_ratio, 6)
            out = round(mp * mr * cr * group_ratio, 6)
        elif mr > 0:
            inp = round(mr * group_ratio, 6)
            out = round(mr * cr * group_ratio, 6)
        else:
            continue

        # 缓存价：优先取绝对价，否则按 ratio（相对 input 价）折算
        def _cf(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        cr_abs = _cf(item.get("cache_read_input_price"))
        cw_abs = _cf(item.get("cache_write_input_price"))
        cr_ratio = _cf(item.get("cache_read_ratio", item.get("cache_ratio", 0))) or 0.0
        cw_ratio = _cf(item.get("cache_write_ratio", item.get("cache_creation_ratio", 0))) or 0.0
        cache_read = cr_abs if cr_abs is not None else (round(inp * cr_ratio, 6) if cr_ratio else 0.0)
        cache_write = cw_abs if cw_abs is not None else (round(inp * cw_ratio, 6) if cw_ratio else 0.0)

        # 更新匹配的模型
        result = await db.execute(
            select(Model).where(Model.provider_id == provider_id, Model.model_id == name)
        )
        model = result.scalar_one_or_none()
        if model:
            model.input_price = inp
            model.output_price = out
            model.cache_read_input_price = cache_read
            model.cache_write_input_price = cache_write
            model.is_free = (inp == 0 and out == 0)
            model.pricing_source = provider.base_url
            model.pricing_updated_at = now
            updated += 1
        else:
            # 创建不存在的模型
            db.add(Model(
                provider_id=provider_id,
                model_id=name,
                display_name=name,
                input_price=inp,
                output_price=out,
                cache_read_input_price=cache_read,
                cache_write_input_price=cache_write,
                is_free=(inp == 0 and out == 0),
                enabled=True,
                auto_enabled=False,
                supports_streaming=True,
                context_length=4096,
                created_at=now,
                pricing_source=provider.base_url,
                pricing_updated_at=now,
            ))
            created += 1

    await db.commit()
    return {"updated": updated, "created": created, "total_models": len(items), "group_ratio": group_ratio}

# ========== v2.0 新增：手动测速 API ==========
@router.post("/models/{model_id}/ping", response_model=PingResult)
async def ping_single_model(model_id: int, db: AsyncSession = Depends(get_db)):
    """手动探测单个模型延迟"""
    from server.main import get_health_checker
    hc = get_health_checker()
    model = await db.get(Model, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    provider = await db.get(Provider, model.provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    # free_tier / oauth 类服务商无 ApiKey（空密钥或走 OAuth token），由 hc.check_model 内部按凭据类型处理
    # 执行探测
    health_result = await hc.check_model(db, model, provider, _key_manager)
    return PingResult(
        model_id=model.id,
        model_full_id=f"{provider.name}/{model.model_id}",
        status=health_result.status,
        latency_ms=health_result.latency_ms,
        error_message=health_result.error_message,
        checked_at=utc_iso(datetime.utcnow())
    )
@router.post("/models/ping-all", response_model=PingAllResponse)
async def ping_all_models(db: AsyncSession = Depends(get_db)):
    """批量探测参与 auto 的已启用模型"""
    from server.main import get_health_checker
    import asyncio
    hc = get_health_checker()
    # 只批量探测参与 auto 的已启用模型，避免未选择 auto 的收费模型被误探测
    result = await db.execute(
        select(Model)
        .join(Provider, Model.provider_id == Provider.id)
        .where(
            Model.enabled == True,
            Model.auto_enabled == True,
            Model.auto_excluded == False
        )
    )
    models = list(result.scalars().all())
    if not models:
        return PingAllResponse(
            total=0, healthy=0, degraded=0, rate_limited=0, unhealthy=0, results=[]
        )
    results = []
    stats = {"healthy": 0, "degraded": 0, "rate_limited": 0, "unhealthy": 0}
    for model in models:
        provider = await db.get(Provider, model.provider_id)
        try:
            health_result = await hc.check_model(db, model, provider, _key_manager)
            stats[health_result.status] = stats.get(health_result.status, 0) + 1
            results.append(PingResult(
                model_id=model.id,
                model_full_id=f"{provider.name}/{model.model_id}" if provider else str(model.model_id),
                status=health_result.status,
                latency_ms=health_result.latency_ms,
                error_message=health_result.error_message,
                checked_at=datetime.utcnow().isoformat()
            ))
        except Exception as e:
            stats["unhealthy"] += 1
            results.append(PingResult(
                model_id=model.id,
                model_full_id=f"{provider.name}/{model.model_id}" if provider else str(model.model_id),
                status="unhealthy",
                latency_ms=0,
                error_message=str(e),
                checked_at=datetime.utcnow().isoformat()
            ))
        # 避免请求太密集
        await asyncio.sleep(0.5)
    return PingAllResponse(
        total=len(models),
        healthy=stats.get("healthy", 0),
        degraded=stats.get("degraded", 0),
        rate_limited=stats.get("rate_limited", 0),
        unhealthy=stats.get("unhealthy", 0),
        results=results
    )
@router.get("/models/latency-stats", response_model=LatencyStatsResponse)
async def get_latency_stats(db: AsyncSession = Depends(get_db)):
    """获取延迟统计（最快/最慢/平均）"""
    from server.main import get_health_checker
    hc = get_health_checker()
    # 获取所有有健康检查记录的模型
    models = await _model_catalog.list_models(db, enabled_only=True)
    model_latencies = []
    for m in models:
        provider = await db.get(Provider, m.provider_id)
        cached = hc.get_cached_status(m.id) if hc else None
        latency = cached.latency_ms if cached and cached.latency_ms else None
        status = cached.status if cached else "unknown"
        if latency is not None and latency > 0:
            model_latencies.append({
                "model_id": m.id,
                "model_full_id": f"{provider.name}/{m.model_id}" if provider else m.model_id,
                "latency_ms": latency,
                "status": status
            })
    # 按延迟排序
    model_latencies.sort(key=lambda x: x["latency_ms"])
    fastest = [
        PingResult(
            model_id=m["model_id"],
            model_full_id=m["model_full_id"],
            status=m["status"],
            latency_ms=m["latency_ms"],
            checked_at=datetime.utcnow().isoformat()
        )
        for m in model_latencies[:5]
    ]
    slowest = [
        PingResult(
            model_id=m["model_id"],
            model_full_id=m["model_full_id"],
            status=m["status"],
            latency_ms=m["latency_ms"],
            checked_at=datetime.utcnow().isoformat()
        )
        for m in model_latencies[-5:][::-1]  # 反转使最慢的排前面
    ]
    avg_latency = (
        sum(m["latency_ms"] for m in model_latencies) / len(model_latencies)
        if model_latencies else 0
    )
    return LatencyStatsResponse(
        fastest=fastest,
        slowest=slowest,
        average_latency_ms=round(avg_latency, 1),
        total_models=len(model_latencies)
    )
# ========== 原有 API 继续 ==========
@router.get("/health")
async def get_health(
    model_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db)
):
    """获取健康状态：仅看 auto 模型，数据来自 request_logs 真实调用历史（不再自动探测）"""
    from server.core.ranking_service import RankingService
    _rs = RankingService()
    result = await db.execute(
        select(Model, Provider)
        .join(Provider, Model.provider_id == Provider.id)
        .where(Model.enabled == True, Model.auto_enabled == True, Model.auto_excluded == False)
    )
    items = []
    for model, provider in result.all():
        if model_id and model.id != model_id:
            continue
        h = await _rs.compute_model_health(db, model.id, model.model_id)
        model_full_id = f"{provider.name}/{model.model_id}" if provider else str(model.id)
        items.append(HealthStatusItem(
            model_id=model.id,
            model_full_id=model_full_id,
            status=h["status"],
            latency_ms=h["latency_ms"],
            last_checked=h["last_checked"] or "",
            error_message=h["error_message"],
        ))
    return HealthStatusResponse(items=items)
@router.get("/dashboard")
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    """仪表盘汇总"""
    from server.core.ranking_service import RankingService
    _rs = RankingService()
    auto_rows = (await db.execute(
        select(Model, Provider)
        .join(Provider, Model.provider_id == Provider.id)
        .where(Model.enabled == True, Model.auto_enabled == True, Model.auto_excluded == False)
    )).all()
    counts = {"healthy": 0, "degraded": 0, "rate_limited": 0, "unhealthy": 0}
    for model, provider in auto_rows:
        h = await _rs.compute_model_health(db, model.id, model.model_id)
        if h["status"] in counts:
            counts[h["status"]] += 1
    total_providers_result = await db.execute(select(func.count(Provider.id)))
    total_providers = total_providers_result.scalar_one() or 0
    total_keys_result = await db.execute(select(func.count(ApiKey.id)))
    total_keys = total_keys_result.scalar_one() or 0
    total_models_result = await db.execute(
        select(func.count(Model.id)).where(Model.enabled == True)
    )
    total_models = total_models_result.scalar_one() or 0
    auto_candidates_result = await db.execute(
        select(func.count(Model.id)).where(Model.enabled == True, Model.auto_enabled == True)
    )
    auto_candidates = auto_candidates_result.scalar_one() or 0
    return DashboardSummary(
        total_providers=total_providers,
        total_keys=total_keys,
        total_models=total_models,
        auto_candidates=auto_candidates,
        healthy_models=counts.get("healthy", 0),
        degraded_models=counts.get("degraded", 0),
        rate_limited_models=counts.get("rate_limited", 0),
        unhealthy_models=counts.get("unhealthy", 0)
    )
# ========== 当前 auto 模型 ==========
@router.get("/current-model")
async def get_current_model(db: AsyncSession = Depends(get_db)):
    """返回 auto 排名第一的模型（非历史最新使用）"""
    try:
        from server.core.ranking_service import RankingService
        from server.core.health_checker import HealthChecker
        rs = RankingService()
        models = (await db.execute(
            select(Model, Provider).join(Provider, Model.provider_id == Provider.id)
            .where(Model.enabled == True)
        )).all()
        mlist = [m for m, _ in models]
        prov_by_pid = {}
        for m, p in models:
            prov_by_pid[m.provider_id] = p
        cooling = {}
        hc = HealthChecker()
        for m in mlist:
            if hc.is_cooling(m.id):
                cooling[m.id] = datetime.utcnow()
        scores = await rs.rank_all(db, mlist, prov_by_pid, cooling)
        if scores:
            top = scores[0]
            # 排除不可用的模型（skip excluded/unhealthy）
            for s in scores:
                if not s.excluded_reason:
                    return {
                        "model": s.model_id_str,
                        "provider": s.provider_name,
                        "display_name": s.display_name,
                        "final_score": s.final_score,
                        "rank": 1,
                    }
            # 全部不可用时返回第一个
            return {
                "model": top.model_id_str,
                "provider": top.provider_name,
                "display_name": top.display_name,
                "final_score": top.final_score,
                "excluded_reason": top.excluded_reason,
            }
    except Exception as e:
        print(f"[WARN] current-model ranking failed: {e}")
    # fallback: 用最近一次 auto 请求
    last = (await db.execute(
        select(RequestLog).where(RequestLog.requested_model == "auto")
        .order_by(desc(RequestLog.created_at)).limit(1)
    )).scalar_one_or_none()
    if not last:
        return {"model": None, "provider": None, "time": None}
    return {
        "model": last.routed_model,
        "provider": last.routed_provider,
        "time": last.created_at.isoformat() if last.created_at else None,
    }
@router.post("/playground")
async def playground_chat(data: PlaygroundRequest, raw_request: Request, db: AsyncSession = Depends(get_db)):
    """Playground 测试聊天（含请求日志写入）"""
    import time, uuid, json as _json_mod
    from server.schemas.chat import ChatCompletionRequest, ChatMessage
    from server.api.v1_router import get_auto_router, _auto_route_with_runtime_fallback, _format_sse_chunk, _auto_request_with_cascade_fallback, _proxy_log_fields
    from server.models.request_log import RequestLog as _RL
    from server.core.request_logger import write_log  # v3.6 消息级去重写入
    ar = get_auto_router()
    conversation_id = str(uuid.uuid4())
    _send_time = time.time()
    messages = [ChatMessage(**m) for m in data.messages]
    request = ChatCompletionRequest(
        model=data.model or "auto",
        messages=messages,
        stream=data.stream or False
    )
    is_auto = request.is_auto
    made_by_cascade = False
    route_result = None

    # ─── 日志写入辅助（提前定义到 try 之前，避免 free_tier / oauth 早期 miss 兜底 _write_log 未绑定） ───
    async def _write_log(status, resp_dict, latency_ms, error_msg=None, *, _route_result=route_result, _conversation_id=conversation_id, _request=request, _raw_request=raw_request):
        try:
            from server.db import AsyncSessionLocal as _LogSession
            usage = resp_dict.get("usage", {}) if resp_dict else {}
            pt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            ct = usage.get("completion_tokens") or usage.get("output_tokens") or 0
            req_s = _json_mod.dumps(_request.model_dump(), ensure_ascii=False)
            resp_s = _json_mod.dumps(resp_dict, ensure_ascii=False)[:100000] if resp_dict else None
            async with _LogSession() as _ldb:
                await write_log(_ldb,
                    conversation_id=_conversation_id, requested_model=_request.model,
                    routed_provider=_route_result.provider.name if (_route_result and _route_result.success) else None,
                    routed_model=_route_result.model.model_id if (_route_result and _route_result.success) else None,
                    status=status, latency_ms=latency_ms,
                    prompt_tokens=int(pt) if pt else 0,
                    completion_tokens=int(ct) if ct else 0,
                    fallback_count=_route_result.fallback_count if _route_result else 0,
                    user_ip=_raw_request.client.host if _raw_request.client else None,
                    error_type="upstream_error" if error_msg else None,
                    error_msg=(error_msg or "")[:500],
                    request_body=req_s, response_body=resp_s,
                    **_proxy_log_fields(),
                )
        except Exception:
            pass

    if not is_auto:
        if "/" in request.model:
            provider_name, model_id = request.model.split("/", 1)
            model = await _model_catalog.get_by_full_id(db, provider_name, model_id)
        else:
            models = await _model_catalog.list_models(db, enabled_only=True)
            model = next((m for m in models if m.model_id == request.model), None)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
        provider = await db.get(Provider, model.provider_id)
        # v4.0: 服务商被禁用 → playground 直连同样不可用
        if provider is None or not getattr(provider, "enabled", True):
            raise HTTPException(status_code=404, detail=f"Provider for model {request.model} is disabled")

        # ─── v3.2 free_tier / OAuth 路径：playground 也走专用分支 ───
        cred_type = getattr(provider, "credential_type", "api_key") or "api_key"
        if cred_type == "free_tier":
            # MiMo / OpenCode 等 9Router 来源免登录 provider — 走 free_providers executor
            from server.core.free_providers import get_free_executor, resolve_free_code, _FREE_PROVIDERS_META as _FPM
            free_code = resolve_free_code(provider.name, getattr(provider, "oauth_code", None))
            free_exec = get_free_executor(free_code) if free_code else None
            if free_exec:
                free_req = request.model_copy(update={"model": model.model_id})
                _send_dur = lambda: int((time.time() - _send_time) * 1000)
                if data.stream:
                    async def _playground_free_stream():
                        try:
                            async for ck in free_exec.execute_stream(free_req):
                                yield _format_sse_chunk(ck, f"{provider.name}/{model.model_id}")
                            yield b"data: [DONE]\n\n"
                            await _write_log("success", None, _send_dur())
                        except Exception as e:
                            yield _format_sse_chunk({"error": f"free_provider_stream_failed: {e}"}, f"{provider.name}/{model.model_id}")
                            yield b"data: [DONE]\n\n"
                            await _write_log("error", None, _send_dur(), str(e)[:500])
                    return StreamingResponse(_playground_free_stream(), media_type="text/event-stream")
                try:
                    upstream_result = await free_exec.execute_non_stream(free_req)
                    if isinstance(upstream_result, dict) and "model" in upstream_result:
                        upstream_result["model"] = f"{provider.name}/{model.model_id}"
                    await _write_log("success", upstream_result, _send_dur())
                    return upstream_result
                except Exception as e:
                    await _write_log("error", None, _send_dur(), str(e)[:500])
                    return JSONResponse(status_code=502, content={"error": f"free_provider_failed: {e}"})
            # 没匹配上 free_code — 不再回退 adapter（避免 URL 被错误二次追加 / 403）
            known_codes = ", ".join(f"'{c}' ({_FPM[c]['name']})" for c in _FPM)
            await _write_log("error", None, 0,
                f"free_tier provider '{provider.name}' has no matching executor "
                f"(oauth_code={getattr(provider, 'oauth_code', None)!r})")
            return JSONResponse(
                status_code=400,
                content={
                    "error": f"free_tier provider '{provider.name}' has no matching executor. "
                             f"请编辑该服务商，将 oauth_code 填为 {known_codes} 之一。"
                },
            )
        elif cred_type == "oauth":
            # OAuth 订阅 provider — 用 oauth_client.pick_access_token，未连接报 503
            from server.core.oauth_client import get_oauth_client
            oauth_code = getattr(provider, "oauth_code", None) or provider.name
            try:
                api_key = await get_oauth_client().pick_access_token(oauth_code, db)
            except Exception:
                api_key = None
            if not api_key:
                raise HTTPException(status_code=503, detail=f"OAuth provider '{oauth_code}' not connected")
            from server.core.model_catalog import create_adapter_for_provider
            from server.core.auto_router import RouteResult
            adapter = create_adapter_for_provider(provider.api_type)
            route_result = RouteResult(
                success=True, model=model, provider=provider,
                api_key=api_key, adapter=adapter, fallback_count=0
            )
        elif cred_type == "atomcode":
            # AtomCode — 本地 daemon 自鉴权，无需 API key（playground / 模型页测试也走此分支）
            from server.core.model_catalog import create_adapter_for_provider
            from server.core.auto_router import RouteResult
            adapter = create_adapter_for_provider(provider.api_type)
            route_result = RouteResult(
                success=True, model=model, provider=provider,
                api_key="", adapter=adapter, fallback_count=0
            )
        else:
            # 标准 API Key provider
            result = await db.execute(
                select(ApiKey)
                .where(ApiKey.provider_id == provider.id, ApiKey.is_active == True)
                .limit(1)
            )
            key = result.scalar_one_or_none()
            if not key:
                raise HTTPException(status_code=400, detail="No active API key")
            api_key = _key_manager._crypto.decrypt(key.key_encrypted)
            # v3.1 优先用 KeyRotator
            try:
                from server.core.key_rotator import get_key_rotator
                picked = await get_key_rotator().pick_key_for_model(db, model)
                if picked:
                    api_key = picked[1]
            except Exception:
                pass
            from server.core.model_catalog import create_adapter_for_provider
            from server.core.auto_router import RouteResult
            adapter = create_adapter_for_provider(provider.api_type)
            route_result = RouteResult(
                success=True, model=model, provider=provider,
                api_key=api_key, adapter=adapter, fallback_count=0
            )
    elif data.stream:
        route_result, _ = await _auto_route_with_runtime_fallback(ar, db, request, conversation_id)
        if not route_result.success:
            # 写失败日志
            try:
                from server.db import AsyncSessionLocal as _LogSession
                async with _LogSession() as _ldb:
                    await write_log(_ldb,
                        conversation_id=conversation_id, requested_model=request.model,
                        status="error", error_type="playground_routing_error",
                        error_msg=str(route_result.error)[:500],
                        user_ip=raw_request.client.host if raw_request.client else None,
                        request_body=_json_mod.dumps(request.model_dump(), ensure_ascii=False),
                    )
            except Exception: pass
            return JSONResponse(status_code=503, content={"error": route_result.error})
    else:
        route_result, response, _attempt_errors = await _auto_request_with_cascade_fallback(ar, db, request, conversation_id)
        if not route_result.success:
            return JSONResponse(status_code=503, content={"error": response.get("error", "all_candidates_failed")})
        made_by_cascade = True

    model_id_full = f"{route_result.provider.name}/{route_result.model.model_id}"
    upstream_request = request.model_copy(update={"model": route_result.model.model_id})
    extra_headers = route_result.provider.headers if route_result.provider.headers else None

    if data.stream:
        async def wrap_stream():
            resp_dict = None
            try:
                async for chunk in route_result.adapter.stream_chat_completion(
                    upstream_request, route_result.api_key, route_result.provider.base_url, extra_headers
                ):
                    yield _format_sse_chunk(chunk, model_id_full)
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as e:
                yield _format_sse_chunk({"error": f"upstream_stream_failed: {type(e).__name__}: {str(e)[:200]}"}, model_id_full)
                await _write_log("error", resp_dict, int((time.time() - _send_time)*1000), str(e)[:500])
            yield b"data: [DONE]\n\n"
        return StreamingResponse(wrap_stream(), media_type="text/event-stream")

    if made_by_cascade:
        # response 已经在级联回退中获得
        result = response
    else:
        try:
            result = await route_result.adapter.chat_completion(
                upstream_request, route_result.api_key, route_result.provider.base_url, extra_headers
            )
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as e:
            await _write_log("error", None, int((time.time() - _send_time)*1000), str(e)[:500])
            return JSONResponse(status_code=503, content={"error": f"upstream_call_failed: {type(e).__name__}: {str(e)[:200]}"})

    if isinstance(result, dict) and "model" in result:
        result["model"] = model_id_full

    # 写成功日志
    resp_dict = result if isinstance(result, dict) else None
    await _write_log("success", resp_dict, int((time.time() - _send_time)*1000))
    return result
@router.get("/auto/ranking")
async def get_auto_ranking(db: AsyncSession = Depends(get_db)):
    """获取 Auto 综合评分排名，兼容旧前端入口。"""
    from server.core.ranking_service import RankingService
    rs = RankingService()
    models = (await db.execute(
        select(Model, Provider).join(Provider, Model.provider_id == Provider.id)
    )).all()
    mlist = [m for m, _ in models]
    prov_by_pid = {m.provider_id: p for m, p in models}
    scores = await rs.rank_all(db, mlist, prov_by_pid, {})
    weights = await rs.get_weights(db)
    ranking = []
    for i, s in enumerate(scores):
        ranking.append({
            "rank": i + 1,
            "model_id": s.model_id,
            "model_full_id": f"{s.provider_name}/{s.model_id_str}",
            "display_name": s.display_name,
            "provider_name": s.provider_name,
            "is_free": s.is_free,
            "speed_score": s.speed_score,
            "intel_score": s.intel_score,
            "stab_score": s.stab_score,
            "avg_ms": s.avg_ms,
            "success_rate": s.success_rate,
            "final_score": s.final_score,
            "excluded_reason": s.excluded_reason,
            "priority_boost": s.priority_boost,
            "auto_excluded": s.auto_excluded,
        })
    return {
        "weights": weights,
        "ranking": ranking,
        "best": next((x for x in ranking if not x.get("excluded_reason")), ranking[0] if ranking else None),
        "total_candidates": len(ranking),
        "timestamp": utc_iso(datetime.utcnow()),
    }
