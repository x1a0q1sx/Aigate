"""
Video Adapter — 视频生成端点适配器

支持两类上游 API 协议：
  1) OpenAI-compatible (Sora / minimax / 通义等):
     POST /v1/videos  → 同步返回或返回 task_id 后轮询
  2) Polling-based (硅基流动 / minimax 等):
     POST /v1/video/submit → {task_id}
     GET  /v1/video/results/{task_id} → {status: "Success"/"Processing", videos:[...]}

策略：提交 → 轮询直到完成或超时 → 返回视频 URL列表
"""
from __future__ import annotations
import asyncio
import logging
import time
import httpx
from typing import Optional, List
from dataclasses import dataclass, field
from .base_adapter import BaseAdapter, ModelInfo, HealthResult
from server.core.proxy_pool import get_proxy_pool

logger = logging.getLogger(__name__)


@dataclass
class VideoGenRequest:
    """视频生成请求参数"""
    prompt: str
    model: str = ""
    n: int = 1
    duration: Optional[int] = None        # 秒
    size: Optional[str] = None            # "1280x720" 等
    fps: Optional[int] = None
    image_url: Optional[str] = None      # 图生视频时的起始帧
    negative_prompt: Optional[str] = None  # 负面提示词
    seed: Optional[int] = None            # 固定随机种子
    extra_params: dict = None


@dataclass
class VideoGenResult:
    """视频生成返回结果"""
    success: bool
    videos: list = None                   # [{"url": "...", "duration": N}]
    model: str = ""
    error: str = ""
    elapsed_ms: float = 0.0


# 支持的轮询上游协议变体
_POLL_PROTOCOLS = {
    "siliconflow": {
        "submit_path": "/video/submit",
        "result_path": "/video/results",
        "id_field": "taskId",
        "status_field": "status",
        "success_value": "Success",
        "processing_values": ("Processing", "Pending", "Queued"),
        "video_field": "results",
    },
    "minimax": {
        "submit_path": "/video_generation",
        "result_path": "/query/video_generation",
        "id_field": "task_id",
        "status_field": "status",
        "success_value": "Success",
        "processing_values": ("Processing", "Pending", "Queueing"),
        "video_field": "videos",
    },
    "agnes": {
        # agnes: POST /videos 创建, GET /videos/{task_id} 轮询
        "submit_path": "/videos",
        "result_path": "/videos",       # GET /videos/{task_id}
        "id_field": "task_id",
        "status_field": "status",
        "success_value": "Completed",
        "processing_values": ("Queued", "In_progress", "In Progress"),
        "video_field": "remixed_from_video_id",  # 视频 URL 在这个字段
    },
}


def _detect_protocol(base_url: str) -> Optional[str]:
    """根据 base_url 猜测轮询协议"""
    url_lower = (base_url or "").lower()
    if "siliconflow" in url_lower or "silflow" in url_lower:
        return "siliconflow"
    if "minimax" in url_lower:
        return "minimax"
    if "agnes" in url_lower or "apihub.agnes" in url_lower:
        return "agnes"
    return None


class VideoAdapter(BaseAdapter):
    """视频生成适配器"""

    def __init__(self, timeout: int = 600):
        # 视频生成很慢，默认 10 分钟整体超时
        self.timeout = timeout

    def _get_headers(self, api_key: str, extra_headers: dict = None) -> dict:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _build_url(self, base_url: str, path: str) -> str:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        return f"{base}{path}"

    async def generate_videos(
        self,
        req: VideoGenRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
    ) -> VideoGenResult:
        """视频生成主入口 — 自动判断同步 or 轮询"""
        headers = self._get_headers(api_key, extra_headers)
        payload = {
            "model": req.model,
            "prompt": req.prompt,
        }
        if req.n and req.n > 1:
            payload["n"] = req.n
        if req.duration:
            payload["duration"] = req.duration
        if req.size:
            payload["size"] = req.size
        if req.fps:
            payload["fps"] = req.fps
        if req.image_url:
            payload["image_url"] = req.image_url
            payload["image"] = req.image_url  # 部分上游用 image 字段
        if req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt
        if req.seed is not None:
            payload["seed"] = req.seed
        if req.extra_params and isinstance(req.extra_params, dict):
            payload.update(req.extra_params)

        start = time.time()
        try:
            pool = get_proxy_pool()
            proxy_kwargs = pool.proxied_kwargs()

            # 先尝试 OpenAI 同步式 POST /videos
            # 传输层错误（代理抖动 / 连接中断 / SOCKS 握手失败）由代理池自动换代理重试
            openai_url = self._build_url(base_url, "/videos")
            resp = await pool.request_with_fallback("POST", openai_url, timeout=self.timeout, headers=headers, json=payload)
            elapsed = (time.time() - start) * 1000

            # 同步成功 — 直接返回
            if resp.status_code < 400:
                data = resp.json()
                videos = self._extract_videos(data)
                if videos:
                    return VideoGenResult(success=True, videos=videos,
                                           model=req.model, elapsed_ms=elapsed)

            # 检查是否是"不支持 /videos" → 退到轮询协议
            if resp.status_code == 404:
                protocol = _detect_protocol(base_url)
                if protocol:
                    return await self._poll_generate(
                        req, api_key, base_url, headers, proxy_kwargs, start, protocol
                    )
                return VideoGenResult(
                    success=False,
                    error=f"上游不支持 /videos 且无法识别轮询协议 (base_url={base_url})",
                    elapsed_ms=elapsed,
                )

            # 其他错误
            body = resp.text[:500]
            # 可能上游返回了 task_id 要求轮询（HTTP 200 但 body 含 task_id）
            try:
                body_json = resp.json()
                task_id = body_json.get("task_id") or body_json.get("taskId") or body_json.get("id")
                if task_id and resp.status_code < 400:
                    return await self._poll_result(
                        task_id, req, api_key, base_url, headers, proxy_kwargs, start,
                        _detect_protocol(base_url) or "generic",
                    )
            except Exception:
                pass

            return VideoGenResult(success=False,
                                  error=f"HTTP {resp.status_code}: {body}",
                                  elapsed_ms=elapsed)

        except Exception as e:
            return VideoGenResult(success=False, error=f"{type(e).__name__}: {str(e)[:300]}",
                                  elapsed_ms=(time.time() - start) * 1000)

    def _extract_videos(self, data: dict) -> list:
        """从同步返回或轮询结果中提取视频列表。
        兼容两种 agnes 返回结构：
          - 扁平: {"status":"completed","remixed_from_video_id":"https://...mp4"}
          - 嵌套: {"data": {"status":"completed","remixed_from_video_id":"..."}}
        以及 Sora 风格 {"data":[{"url":"..."}]}。"""
        videos = []
        # 候选对象：顶层 + data 内（dict 或 list 元素）
        candidates = [data]
        inner = data.get("data") if isinstance(data, dict) else None
        if isinstance(inner, dict):
            candidates.append(inner)
        elif isinstance(inner, list):
            candidates.extend(inner)
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            # 1) Sora 风格: data[].url
            for item in (obj.get("data") or obj.get("videos") or obj.get("results") or []):
                if isinstance(item, dict):
                    url = item.get("url") or item.get("video_url") or item.get("download_url")
                    if url:
                        videos.append({
                            "url": url,
                            "duration": item.get("duration_seconds") or item.get("duration"),
                        })
            # 2) agnes 风格: remixed_from_video_id / video_url / url
            url = obj.get("remixed_from_video_id") or obj.get("video_url") or obj.get("url")
            if url:
                videos.append({
                    "url": url,
                    "duration": obj.get("seconds") or obj.get("duration"),
                })
        # 去重
        seen, out = set(), []
        for v in videos:
            if v.get("url") and v["url"] not in seen:
                seen.add(v["url"])
                out.append(v)
        return out

    async def _poll_generate(
        self, req, api_key, base_url, headers, proxy_kwargs, start, protocol: str
    ) -> VideoGenResult:
        """轮询协议：提交 → 轮询结果"""
        cfg = _POLL_PROTOCOLS.get(protocol)
        if not cfg:
            return VideoGenResult(success=False, error=f"未知轮询协议: {protocol}",
                                  elapsed_ms=(time.time() - start) * 1000)
        submit_url = self._build_url(base_url, cfg["submit_path"])
        payload = {
            "model": req.model,
            "prompt": req.prompt,
        }
        if req.image_url:
            payload["image"] = req.image_url
            payload["image_url"] = req.image_url
        if req.duration:
            payload["duration"] = req.duration
        if req.size:
            payload["size"] = req.size
        if req.extra_params and isinstance(req.extra_params, dict):
            payload.update(req.extra_params)

        pool = get_proxy_pool()
        # 传输层错误（代理抖动 / 连接中断 / SOCKS 握手失败）由代理池自动换代理重试
        resp = await pool.request_with_fallback("POST", submit_url, timeout=self.timeout, headers=headers, json=payload)
        elapsed = (time.time() - start) * 1000
        if resp.status_code >= 400:
            return VideoGenResult(success=False,
                                  error=f"Submit HTTP {resp.status_code}: {resp.text[:300]}",
                                  elapsed_ms=elapsed)
        data = resp.json()
        task_id = data.get(cfg["id_field"]) or data.get("task_id") or data.get("id")
        if not task_id:
            # 可能同步返回了结果
            videos = self._extract_videos(data)
            if videos:
                return VideoGenResult(success=True, videos=videos,
                                       model=req.model, elapsed_ms=elapsed)
            return VideoGenResult(success=False,
                                  error=f"提交后未获得 task_id: {resp.text[:300]}",
                                  elapsed_ms=elapsed)
        return await self._poll_result(
            task_id, req, api_key, base_url, headers, proxy_kwargs, start, protocol
        )

    async def _poll_result(
        self, task_id, req, api_key, base_url, headers, proxy_kwargs, start, protocol: str
    ) -> VideoGenResult:
        """轮询任务状态直到完成"""
        cfg = _POLL_PROTOCOLS.get(protocol, {})
        result_path = cfg.get("result_path", "/video/results")
        result_url = self._build_url(base_url, f"{result_path}/{task_id}")

        # 轮询参数
        poll_interval = 5       # 秒
        max_poll = 60           # 最多 60 次 = 5 分钟
        if req.duration and req.duration > 30:
            max_poll = 120       # 长视频给更多时间

        pool = get_proxy_pool()
        for attempt in range(max_poll):
            await asyncio.sleep(poll_interval)
            try:
                # 每次轮询都用新的代理（代理池会自动避开死/抖动代理），避免单代理连接中断导致整轮轮询失败
                async with httpx.AsyncClient(timeout=self.timeout, **pool.proxied_kwargs()) as client:
                    resp = await client.get(result_url, headers=headers)
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                raw_status = data.get(cfg.get("status_field", "status")) or data.get("status", "")
                status = raw_status.capitalize() if raw_status else ""
                # 成功
                if status == cfg.get("success_value", "Success") or status in ("Succeeded", "Completed", "Done"):
                    videos = self._extract_videos(data)
                    if videos:
                        elapsed = (time.time() - start) * 1000
                        return VideoGenResult(success=True, videos=videos,
                                               model=req.model, elapsed_ms=elapsed)
                    # 有成功状态但没视频 → 可能字段不同，尝试所有可能字段
                    url = (data.get("remixed_from_video_id")
                           or data.get("url") or data.get("video_url")
                           or data.get("download_url"))
                    if url:
                        return VideoGenResult(
                            success=True, videos=[{"url": url}],
                            model=req.model,
                            elapsed_ms=(time.time() - start) * 1000,
                        )
                    return VideoGenResult(
                        success=False, error=f"任务完成但无视频输出. 响应: {str(data)[:300]}",
                        elapsed_ms=(time.time() - start) * 1000,
                    )
                # 失败
                if status in ("Failed", "Error"):
                    err = data.get("error", {}).get("message", "") if isinstance(data.get("error"), dict) else str(data.get("error", ""))
                    return VideoGenResult(success=False,
                                          error=f"任务失败: {err or status}",
                                          elapsed_ms=(time.time() - start) * 1000)
                # 仍在处理（queued / in_progress / processing / pending 等都继续轮询）
                if attempt == 0 or (attempt + 1) % 6 == 0:
                    progress = data.get("progress", "?")
                    logger.info("[video] polling %s status=%s progress=%s attempt=%d/%d",
                                task_id, raw_status, progress, attempt + 1, max_poll)
            except Exception as e:
                if attempt == 0 or (attempt + 1) % 6 == 0:
                    logger.warning("[video] poll error attempt=%d: %s", attempt + 1, e)
                continue

        return VideoGenResult(success=False,
                              error=f"轮询超时 ({max_poll * poll_interval}s)",
                              elapsed_ms=(time.time() - start) * 1000)

    # ── BaseAdapter 兼容占位 ──
    async def chat_completion(self, request, api_key, base_url, extra_headers=None):
        raise NotImplementedError("VideoAdapter does not support chat_completion")

    async def stream_chat_completion(self, request, api_key, base_url, extra_headers=None):
        raise NotImplementedError("VideoAdapter does not support streaming")
        yield  # noqa

    async def list_models(self, api_key: str, base_url: str, extra_headers: dict = None) -> List[ModelInfo]:
        return []

    async def health_check(self, model, api_key, base_url, extra_headers=None, timeout=10):
        try:
            url = self._build_url(base_url, "/videos")
            headers = self._get_headers(api_key, extra_headers)
            from server.core.proxy_pool import get_proxy_pool
            async with httpx.AsyncClient(timeout=timeout, **get_proxy_pool().proxied_kwargs()) as client:
                resp = await client.get(url.replace("/videos", "/models"), headers=headers)
                if resp.status_code < 400:
                    return HealthResult(status="healthy", latency_ms=0)
                return HealthResult(status="unhealthy", error_message=f"HTTP {resp.status_code}")
        except Exception as e:
            return HealthResult(status="unhealthy", error_message=str(e)[:200])
