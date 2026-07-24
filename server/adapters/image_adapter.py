"""
Image Adapter — 图片生成端点适配器（适配多种 /images/generations API 变体）
支持：
  - OpenAI Images API：POST /v1/images/generations (返回 b64_json 或 url)
  - SiliconFlow / Azure / 通义 等都共用 OpenAI Images 接口约定
  - 部分供应商差异：模型名、size 格式、quality 参数等通过 provider.headers 注入

参数约定（OpenAI 标准）：
  - model:        模型 ID（如 dall-e-3、flux.1-schnell）
  - prompt:       必填
  - n:            张数（默认 1）
  - size:         尺寸（256x256、512x512、1024x1024、1792x1024 等）
  - quality:      "standard" / "hd"
  - response_format: "b64_json"（base64）/ "url"
  - style:        "vivid" / "natural"（dall-e-3 专属）

返回结构：
  {
    "created": <ts>,
    "data": [
      {"b64_json": "...", "revised_prompt": "..."}  或
      {"url": "https://..."}
    ]
  }

实现层把图片生成的同步接口与本项目的 BaseAdapter 风格统一 —
作为一个「特殊 adapter」提供给 /admin/api/media/* 调用。
"""
from __future__ import annotations
import base64
import logging
import time
import httpx
from typing import Optional, AsyncGenerator, List
from dataclasses import dataclass
from .base_adapter import BaseAdapter, ModelInfo, HealthResult
from server.schemas.chat import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger(__name__)


@dataclass
class ImageGenRequest:
    """图片生成请求参数"""
    prompt: str
    model: str = "dall-e-3"
    n: int = 1
    size: str = "1024x1024"
    quality: str = "standard"
    response_format: str = "b64_json"      # b64_json / url
    style: Optional[str] = None             # vivid / natural (dall-e-3)
    seed: Optional[int] = None             # 固定随机种子（可复现）
    negative_prompt: Optional[str] = None  # 负面提示词
    image_url: Optional[str] = None        # 图生图：参考图 URL
    extra_params: dict = None                # provider-specific


@dataclass
class ImageGenResult:
    """图片生成返回结果"""
    success: bool
    images: list = None                     # [{"data": "base64...", "format": "png"}, {"url": "..."}]
    model: str = ""
    error: str = ""
    raw: dict = None
    elapsed_ms: float = 0.0


class ImageAdapter(BaseAdapter):
    """图片生成适配器 — 复用 OpenAI Images API 协议"""

    def __init__(self, timeout: int = 120):
        # 图片生成通常更慢，默认 120 秒
        self.timeout = timeout

    def _build_images_url(self, base_url: str, edit: bool = False) -> str:
        base = base_url.rstrip('/')
        endpoint = "images/edits" if edit else "images/generations"
        if '/api/paas/' in base:
            return f"{base}/{endpoint}"
        if not base.endswith('/v1'):
            base += '/v1'
        return f"{base}/{endpoint}"

    def _get_headers(self, api_key: str, extra_headers: dict = None) -> dict:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        return headers

    async def generate_images(
        self,
        req: ImageGenRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
    ) -> ImageGenResult:
        """图片生成 / 图生图主入口"""
        # 有 image_url 时走 /images/edits；否则走 /images/generations
        # 注：OpenAI 官方 edits 需要 multipart file，这里优先支持 Agnes/LiteLLM 等 JSON image URL 兼容写法。
        is_edit = bool(req.image_url)
        url = self._build_images_url(base_url, edit=is_edit)
        headers = self._get_headers(api_key, extra_headers)
        payload = {
            "model": req.model,
            "prompt": req.prompt,
            "n": req.n,
            "size": req.size,
        }
        # response_format：部分上游（litellm 网关 / agnes 等）不支持此参数，
        # 只有用户显式选择非默认值时才发送，默认 b64_json 时不放入 payload
        has_extra_body = req.extra_params and "extra_body" in (req.extra_params or {})
        if not has_extra_body and req.response_format and req.response_format != "b64_json":
            payload["response_format"] = req.response_format
        if req.quality and req.quality != "standard":
            payload["quality"] = req.quality
        if req.style:
            payload["style"] = req.style
        if req.seed is not None:
            payload["seed"] = req.seed
        if req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt
        if req.image_url:
            # 图生图：部分上游支持 image 参数
            payload["image"] = req.image_url
        if req.extra_params and isinstance(req.extra_params, dict):
            payload.update(req.extra_params)
        start = time.time()
        try:
            from server.core.proxy_pool import get_proxy_pool
            proxy_kwargs = get_proxy_pool().proxied_kwargs()
            async with httpx.AsyncClient(timeout=self.timeout, **proxy_kwargs) as client:
                resp = await client.post(url, headers=headers, json=payload)
                elapsed = (time.time() - start) * 1000
                if resp.status_code >= 400:
                    body = resp.text[:500]
                    return ImageGenResult(success=False, error=f"HTTP {resp.status_code}: {body}", elapsed_ms=elapsed)
                data = resp.json()
                images = []
                for item in data.get("data", []):
                    if "b64_json" in item and item["b64_json"]:
                        images.append({"data": item["b64_json"], "format": "base64",
                                       "revised_prompt": item.get("revised_prompt")})
                    elif "url" in item and item["url"]:
                        images.append({"url": item["url"], "format": "url",
                                       "revised_prompt": item.get("revised_prompt")})
                if not images:
                    return ImageGenResult(success=False, error="empty result", elapsed_ms=elapsed)
                return ImageGenResult(success=True, images=images, model=req.model, raw=data, elapsed_ms=elapsed)
        except httpx.HTTPStatusError as e:
            return ImageGenResult(success=False, error=f"{type(e).__name__}: {str(e)[:200]}",
                                   elapsed_ms=(time.time() - start) * 1000)
        except Exception as e:
            return ImageGenResult(success=False, error=f"{type(e).__name__}: {str(e)[:200]}",
                                   elapsed_ms=(time.time() - start) * 1000)

    # ── BaseAdapter 兼容占位 — 因为图片生成不属于 chat/list_models 语义 ─

    async def chat_completion(self, request, api_key, base_url, extra_headers=None):
        raise NotImplementedError("ImageAdapter does not support chat_completion")

    async def stream_chat_completion(self, request, api_key, base_url, extra_headers=None):
        raise NotImplementedError("ImageAdapter does not support streaming")
        yield  # noqa  # 为了让 Python 把它识别为 async generator

    async def list_models(self, api_key: str, base_url: str, extra_headers: dict = None) -> List[ModelInfo]:
        """图片模型列表 — 部分供应商共享 base_url"""
        url = self._build_images_url(base_url).replace("/images/generations", "/models")
        headers = self._get_headers(api_key, extra_headers)
        try:
            from server.core.proxy_pool import get_proxy_pool
            async with httpx.AsyncClient(timeout=self.timeout, **get_proxy_pool().proxied_kwargs()) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code >= 400:
                    return _builtin_image_models()
                data = resp.json()
                models = []
                for item in data.get("data", []):
                    mid = item.get("id", "")
                    if not mid:
                        continue
                    mid_lower = mid.lower()
                    is_image = any(tag in mid_lower for tag in
                                    ("dall-e", "flux", "sd", "stable-diffusion", "imagen", "midjourney"))
                    if is_image or "image" in str(item.get("capabilities", "")).lower():
                        models.append(ModelInfo(
                            model_id=mid, display_name=mid, is_free=False,
                            supports_streaming=False, supports_vision=False, context_length=0,
                        ))
                # 兜底：上游未识别任何图模型但本适配器被调用 → 假定这是图片专用 provider
                return models if models else _builtin_image_models()
        except Exception:
            return _builtin_image_models()

    async def health_check(self, model, api_key, base_url, extra_headers=None, timeout=10):
        # 图片端点健康检查走最小请求探测
        try:
            req = ImageGenRequest(prompt="ping", model=model, n=1, size="1024x1024", response_format="url")
            r = await self.generate_images(req, api_key, base_url, extra_headers)
            if r.success:
                return HealthResult(status="healthy", latency_ms=r.elapsed_ms)
            return HealthResult(status="unhealthy", error_message=r.error[:200])
        except Exception as e:
            return HealthResult(status="unhealthy", error_message=str(e)[:200])


# 内置图片模型清单（用于 list_models 兜底，避免上游 /models 不返回图模型时前端空白）
def _builtin_image_models() -> List[ModelInfo]:
    builtins = [
        "dall-e-3", "dall-e-2",
        "flux-1.1-pro", "flux.1-dev", "flux.1-schnell",
        "stable-diffusion-3.5-large", "stable-diffusion-xl-1.0",
        "imagen-4.0-generate-preview", "imagen-3.0-generate-002",
    ]
    return [ModelInfo(model_id=m, display_name=m, is_free=False, supports_streaming=False, context_length=0)
            for m in builtins]
