"""
请求日志 ORM
v0.2: 每次 chat_completion 调用（含 fallback 重试）一行
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Index
from .base import Base
class RequestLog(Base):
    __tablename__ = "request_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(64), nullable=True)
    requested_model = Column(String(200), nullable=True)
    routed_provider = Column(String(100), nullable=True)
    routed_model = Column(String(200), nullable=True)
    status = Column(String(20), nullable=False, default="success")  # success/error
    http_status = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    error_type = Column(String(50), nullable=True)
    error_msg = Column(Text, nullable=True)
    fallback_count = Column(Integer, nullable=False, default=0)
    user_ip = Column(String(64), nullable=True)
    api_key_id = Column(Integer, nullable=True)
    request_body = Column(Text, nullable=True)  # 请求包 JSON
    response_body = Column(Text, nullable=True)  # 返回包 JSON
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
Index("idx_request_logs_model_time", RequestLog.routed_model, RequestLog.created_at)
Index("idx_request_logs_status_time", RequestLog.status, RequestLog.created_at)
Index("idx_request_logs_conv_time", RequestLog.conversation_id, RequestLog.created_at)
