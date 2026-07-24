"""
Anthropic Messages API 兼容 schema
用于把 Anthropic SDK / Claude Code 的请求翻译成内部 OpenAI 格式
"""
from typing import List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field


class AnthropicContentBlock(BaseModel):
    """Anthropic content block（text / tool_use / tool_result 等）"""
    type: str
    text: Optional[str] = None
    # tool_use
    id: Optional[str] = None
    name: Optional[str] = None
    input: Optional[Dict[str, Any]] = None
    # tool_result
    tool_use_id: Optional[str] = None
    content: Optional[Union[str, List[Dict[str, Any]]]] = None
    is_error: Optional[bool] = None


class AnthropicMessage(BaseModel):
    role: str
    content: Union[str, List[AnthropicContentBlock]]


class AnthropicMessagesRequest(BaseModel):
    """Anthropic Messages API 请求体"""
    model: str
    messages: List[AnthropicMessage]
    system: Optional[Union[str, List[Dict[str, Any]]]] = None
    max_tokens: int = 4096
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    metadata: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Dict[str, Any]] = None

    @property
    def is_auto(self) -> bool:
        return self.model == "auto"


class AnthropicUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class AnthropicContentBlockResponse(BaseModel):
    type: str = "text"
    text: Optional[str] = None


class AnthropicMessagesResponse(BaseModel):
    """Anthropic Messages API 非流式响应"""
    id: str
    type: str = "message"
    role: str = "assistant"
    model: str
    content: List[AnthropicContentBlockResponse]
    stop_reason: Optional[str] = None
    stop_sequence: Optional[str] = None
    usage: AnthropicUsage
