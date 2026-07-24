"""
请求日志记录器 + 消息级去重（Git-blob 模型）
v3.6:
- 写入：request_body / response_body 拆成“内容单元”（请求=信封 + 逐条消息；
  响应=整包），每个单元按 sha256(规范化JSON) 只存唯一一份到 log_msg_blobs，
  日志行只存哈希引用。同一项目几十次调用里高度重复的 system prompt /
  历史轮次只落盘一次。
- 读取：reassemble_request / reassemble_response 按哈希顺序还原为与原先
  逐字节一致的 JSON 字符串（前端 / 归档零改动）。
"""
import gzip
import hashlib
import json
import time
from datetime import datetime
from typing import Any, List, Optional, Tuple

from sqlalchemy import text, bindparam
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.models.request_log import LogMsgBlob, RequestLog


# ── 规范化 / 哈希 / 压缩 ──
def _canon(obj: Any) -> str:
    """规范化序列化：排序键 + 紧凑分隔符 + 保留非 ASCII。
    保证语义相同的消息 → 字节相同 → 同一哈希（去重的关键）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def _gz(b: bytes) -> bytes:
    return gzip.compress(b, compresslevel=6)


def _ungz(b: bytes) -> bytes:
    return gzip.decompress(b)


def _parse_body(body: Any) -> Optional[dict]:
    """接受 None / str(JSON) / dict / pydantic，统一返回 dict 或 None。"""
    if body is None:
        return None
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        try:
            return json.loads(body)
        except Exception:
            return None
    if hasattr(body, "model_dump"):  # pydantic v2
        try:
            return body.model_dump()
        except Exception:
            return None
    return None


async def _upsert_units(db: AsyncSession, objs: List[Any]) -> List[str]:
    """把若干内容单元规范化→哈希→gzip→批量 upsert 进 log_msg_blobs。
    已存在的单元只把 ref_count +1。返回与入参顺序一致的 hash 列表。"""
    if not objs:
        return []
    vals = []
    for o in objs:
        try:
            canon = _canon(o)
        except Exception:
            continue  # 不可序列化则跳过该单元（极个别畸形消息）
        raw = canon.encode("utf-8", "replace")
        gz = _gz(raw)
        vals.append({
            "hash": _sha256(canon),
            "payload": gz,
            "size_raw": len(raw),
            "size_gz": len(gz),
            "ref_count": 1,
            "first_seen": datetime.utcnow(),
        })
    if not vals:
        return []
    stmt = sqlite_insert(LogMsgBlob).values(vals).on_conflict_do_update(
        index_elements=[LogMsgBlob.hash],
        set_={LogMsgBlob.ref_count: LogMsgBlob.ref_count + 1},
    )
    await db.execute(stmt)
    return [v["hash"] for v in vals]


async def _store_request(db: AsyncSession, request_body: Any) -> Tuple[Optional[str], Optional[str]]:
    """拆请求体：信封(去掉 messages) + 逐条消息。返回 (env_hash, msg_hashes_json)。"""
    d = _parse_body(request_body)
    if not isinstance(d, dict):
        return None, None
    msgs = d.get("messages")
    if not isinstance(msgs, list) or not msgs:
        # 非 chat 体（无 messages 键 / messages 非列表 / 空列表）：整包当
        # 一个单元原样存，标记 "__raw__" 让还原时不额外加 messages 键。
        hs = await _upsert_units(db, [d])
        return (hs[0] if hs else None), "__raw__"
    env = {k: v for k, v in d.items() if k != "messages"}
    units = ([env] if env else []) + list(msgs)
    hs = await _upsert_units(db, units)
    if env:
        env_hash = hs[0] if hs else None
        msg_hashes = hs[1:]
    else:
        env_hash = None
        msg_hashes = hs
    return env_hash, json.dumps(msg_hashes, ensure_ascii=False)


async def _store_response(db: AsyncSession, response_body: Any) -> Optional[str]:
    """响应整包作为一个单元（响应体量小，整包去重足够）。"""
    d = _parse_body(response_body)
    if not isinstance(d, dict):
        if isinstance(response_body, str):  # 如 "[stream]" 占位
            hs = await _upsert_units(db, [{"__raw__": response_body}])
            return hs[0] if hs else None
        return None
    hs = await _upsert_units(db, [d])
    return hs[0] if hs else None


async def reassemble_request(db: AsyncSession, row: RequestLog) -> Optional[str]:
    """按哈希还原请求体。无哈希(遗留行)→回退旧 TEXT。"""
    if row.request_msg_hashes is None and row.request_env_hash is None:
        return row.request_body
    if row.request_msg_hashes == "__raw__":
        # 整包存储：env 即完整请求体，规范化后原样返回（不额外加 messages 键）
        if row.request_env_hash:
            blob = await db.get(LogMsgBlob, row.request_env_hash)
            if blob:
                return json.dumps(
                    json.loads(_ungz(blob.payload).decode("utf-8", "replace")),
                    ensure_ascii=False,
                )
        return row.request_body
    env = {}
    if row.request_env_hash:
        blob = await db.get(LogMsgBlob, row.request_env_hash)
        if blob:
            env = json.loads(_ungz(blob.payload).decode("utf-8", "replace"))
    msgs: List[Any] = []
    if row.request_msg_hashes:
        hashes = json.loads(row.request_msg_hashes)
        if hashes:
            stmt = text(
                "SELECT hash, payload FROM log_msg_blobs WHERE hash IN :hs"
            ).bindparams(bindparam("hs", expanding=True))
            res = await db.execute(stmt, {"hs": hashes})
            m = {r[0]: r[1] for r in res}
            for h in hashes:
                if h in m:
                    msgs.append(json.loads(_ungz(m[h]).decode("utf-8", "replace")))
    env["messages"] = msgs
    return json.dumps(env, ensure_ascii=False)


async def reassemble_response(db: AsyncSession, row: RequestLog) -> Optional[str]:
    """按哈希还原响应体。无哈希(遗留行)→回退旧 TEXT。"""
    if row.response_body_hash is None:
        return row.response_body
    blob = await db.get(LogMsgBlob, row.response_body_hash)
    if not blob:
        return row.response_body
    obj = json.loads(_ungz(blob.payload).decode("utf-8", "replace"))
    if isinstance(obj, dict) and "__raw__" in obj:
        return obj["__raw__"]
    return json.dumps(obj, ensure_ascii=False)


async def dedup_log_row(db: AsyncSession, rec: RequestLog) -> None:
    """就地把一条 RequestLog 的 request_body/response_body 转为消息级去重引用。
    调用方照常 db.add(rec) + db.commit()（blob 与行同事务提交）。
    去重失败则保留原文 TEXT，绝不丢数据。"""
    rb = rec.request_body
    resp = rec.response_body
    try:
        env_hash, msg_hashes = await _store_request(db, rb)
        resp_hash = await _store_response(db, resp)
    except Exception as e:
        print(f"⚠️ 日志去重写 blob 失败(保留原文): {e}")
        return
    rec.request_env_hash = env_hash
    rec.request_msg_hashes = msg_hashes
    rec.response_body_hash = resp_hash
    # 不再存原文，省空间（读取端按哈希还原）
    rec.request_body = None
    rec.response_body = None


async def write_log(db: AsyncSession, **kwargs) -> int:
    """构造 RequestLog + 消息级去重 + 落库提交，返回 id。
    取代各处直接的 db.add(RequestLog(...)); await db.commit() 写法，
    让所有日志写入点统一走消息级去重。"""
    rec = RequestLog(**kwargs)
    await dedup_log_row(db, rec)
    db.add(rec)
    try:
        await db.commit()
        await db.refresh(rec)
        return rec.id
    except Exception as e:
        await db.rollback()
        print(f"⚠️ 写日志失败: {e}")
        return -1


class RequestLogger:
    """单次请求日志写入器（v3.6 集成消息级去重）"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.start_ts = time.time()

    async def log(self, **kwargs) -> int:
        """记录一条日志，返回 id"""
        latency_ms = int((time.time() - self.start_ts) * 1000)
        # 消息级去重：把 request_body/response_body 拆成 blob + 哈希引用
        rb = kwargs.pop("request_body", None)
        resp = kwargs.pop("response_body", None)
        env_hash = msg_hashes = resp_hash = None
        try:
            env_hash, msg_hashes = await _store_request(self.db, rb)
            resp_hash = await _store_response(self.db, resp)
        except Exception as e:
            # 去重失败 → 回退存原文，保证不丢数据
            print(f"⚠️ 日志去重写 blob 失败(回退存原文): {e}")
            kwargs["request_body"] = (
                rb if isinstance(rb, str)
                else (json.dumps(rb, ensure_ascii=False) if rb is not None else None)
            )
            kwargs["response_body"] = (
                resp if isinstance(resp, str)
                else (json.dumps(resp, ensure_ascii=False) if resp is not None else None)
            )

        rec = RequestLog(
            latency_ms=kwargs.pop("latency_ms", latency_ms),
            request_env_hash=env_hash,
            request_msg_hashes=msg_hashes,
            response_body_hash=resp_hash,
            **kwargs,
        )
        self.db.add(rec)
        try:
            await self.db.commit()
            await self.db.refresh(rec)
            return rec.id
        except Exception as e:
            await self.db.rollback()
            print(f"⚠️ 写日志失败: {e}")
            return -1
