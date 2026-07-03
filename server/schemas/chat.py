"""
OpenAI Chat Completions 格式 schema
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
class ChatMessage(BaseModel):
    role: str
    content: Optional[str | Any] = None  # tool_calls 时 content 可为空
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    refusal: Optional[str] = None
class ChatCompletionRequest(BaseModel):
    """OpenAI 兼容的聊天补全请求"""
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    stop: Optional[List[str]] = None
    n: Optional[int] = 1
    seed: Optional[int] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    @property
    def is_auto(self) -> bool:
        """是否请求 auto 模型"""
        return self.model == "auto"
class ChatCompletionUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Optional[str] = None
class ChatCompletionResponse(BaseModel):
    """OpenAI 兼容的聊天补全响应"""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]
    usage: ChatCompletionUsage
class ChatCompletionChunk(BaseModel):
    """流式响应块"""
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[Dict[str, Any]]