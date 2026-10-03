# -*- coding: utf-8 -*-
"""AdminGZipMiddleware：只压 /admin 的缓冲 JSON，推理流式路径零接触。

门控：路径 /admin 前缀 + 客户端 Accept-Encoding 含 gzip + 响应 JSON +
无既有 content-encoding + 声明 content-length ≥ 阈值。依赖 content-length
存在天然排除流式响应（分块传输无此头）。
"""
import gzip
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from server.core.admin_gzip import AdminGZipMiddleware


def _build_app():
    app = FastAPI()
    app.add_middleware(AdminGZipMiddleware)

    big = {"data": [{"model": f"m{i}", "desc": "x" * 40} for i in range(120)]}

    @app.get("/admin/api/big")
    async def admin_big():
        return JSONResponse(big)

    @app.get("/admin/api/small")
    async def admin_small():
        return JSONResponse({"ok": True})

    @app.get("/admin/api/already-gzipped")
    async def admin_gz():
        body = gzip.compress(json.dumps(big).encode())
        from fastapi.responses import Response
        return Response(content=body, media_type="application/json",
                        headers={"Content-Encoding": "gzip"})

    @app.get("/v1/big")
    async def v1_big():
        return JSONResponse(big)

    @app.get("/admin/api/stream")
    async def admin_stream():
        async def gen():
            yield b"chunk"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _client(app, **kw):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test", **kw)


@pytest.mark.asyncio
async def test_big_admin_json_gets_gzipped():
    async with _client(_build_app()) as c:
        r = await c.get("/admin/api/big")
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in r.headers.get("vary", "").lower()
    # httpx 自动解压：内容与原始一致
    assert len(r.json()["data"]) == 120


@pytest.mark.asyncio
async def test_small_admin_json_not_compressed():
    async with _client(_build_app()) as c:
        r = await c.get("/admin/api/small")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers


@pytest.mark.asyncio
async def test_non_admin_path_not_compressed():
    async with _client(_build_app()) as c:
        r = await c.get("/v1/big")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers


@pytest.mark.asyncio
async def test_client_without_gzip_support_untouched():
    async with _client(_build_app(), headers={"Accept-Encoding": "identity"}) as c:
        r = await c.get("/admin/api/big")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert len(r.json()["data"]) == 120


@pytest.mark.asyncio
async def test_precompressed_response_not_double_compressed():
    async with _client(_build_app()) as c:
        r = await c.get("/admin/api/already-gzipped")
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    # 双重压缩会让自动解压一次后仍是 gzip 字节；这里应原样透传
    assert len(r.json()["data"]) == 120


@pytest.mark.asyncio
async def test_streaming_admin_response_untouched():
    async with _client(_build_app()) as c:
        async with c.stream("GET", "/admin/api/stream") as r:
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
    assert body == b"chunk"
