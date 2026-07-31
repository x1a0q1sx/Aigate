"""
Provider 适配器基类
定义统一接口
"""
from abc import ABC, abstractmethod
from typing import AsyncGenerator, List
from dataclasses import dataclass
from server.schemas.chat import ChatCompletionRequest, ChatCompletionResponse
@dataclass
class ModelInfo:
    model_id: str
    display_name: str = ""
    input_price: float = 0.0
    output_price: float = 0.0
    cache_read_input_price: float = 0.0   # 每百万 token 美元（缓存读）
    cache_write_input_price: float = 0.0  # 每百万 token 美元（缓存写/创建）
    is_free: bool = False
    supports_streaming: bool = True
    supports_vision: bool = False
    context_length: int = 4096
@dataclass
class HealthResult:
    status: str  # healthy / degraded / rate_limited / unhealthy
    latency_ms: float = 0.0
    error_message: str = ""
class BaseAdapter(ABC):
    """所有 Provider 适配器都必须继承这个基类"""
    @abstractmethod
    async def chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None
    ) -> ChatCompletionResponse:
        """非流式聊天补全"""
        pass
    @abstractmethod
    async def stream_chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None
    ) -> AsyncGenerator[dict, None]:
        """流式聊天补全，yield 每个 chunk 的 dict (OpenAI 格式)"""
        pass
    @abstractmethod
    async def list_models(
        self,
        api_key: str,
        base_url: str,
        extra_headers: dict = None
    ) -> List[ModelInfo]:
        """列出服务商所有模型"""
        pass
    @abstractmethod
    async def health_check(
        self,
        model: str,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
        timeout: int = 10
    ) -> HealthResult:
        """健康探测：发一个短请求测试可用性和延迟"""
        pass