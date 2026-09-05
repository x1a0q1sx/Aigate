"""
请求日志 ORM
v0.2: 每次 chat_completion 调用（含 fallback 重试）一行
v0.3: media_type 字段区分 chat/image/video 请求
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, Index, LargeBinary
from .base import Base
class LogMsgBlob(Base):
    """消息级去重 blob 仓库（内容寻址，Git-blob 模型）。

    request_body / response_body 不再整包存储，而是拆成若干“内容单元”
    （请求拆成 信封 + 逐条消息；响应整包作为一个单元），每个单元按
    sha256(规范化JSON) 只存唯一一份，日志行只存哈希引用。
    同一项目几十次调用里高度重复的 system prompt / 历史轮次只落盘一次。
    """
    __tablename__ = "log_msg_blobs"
    hash = Column(String(64), primary_key=True)        # sha256 hex
    payload = Column(LargeBinary, nullable=False)       # gzip(规范化 JSON)
    size_raw = Column(Integer, nullable=False, default=0)
    size_gz = Column(Integer, nullable=False, default=0)
    ref_count = Column(Integer, nullable=False, default=1)
    first_seen = Column(DateTime, nullable=False, default=datetime.utcnow)
class RequestLog(Base):
    __tablename__ = "request_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(64), nullable=True)
    requested_model = Column(String(200), nullable=True)
    routed_provider = Column(String(100), nullable=True)
    routed_model = Column(String(200), nullable=True)
    status = Column(String(20), nullable=False, default="success")  # success/error
    media_type = Column(String(10), nullable=True)  # null=chat / "image" / "video"
    http_status = Column(Integer, nullable=True)
    used_proxy = Column(Boolean, nullable=False, default=False)  # 本次线请求是否走了代理
    proxy_url = Column(String(255), nullable=True)  # 实际使用的代理 URL（脱敏由前端处理）
    is_health_check = Column(Boolean, nullable=False, default=False, server_default="0")  # 1=网关自身健康检查探测，列表/聚合查询排除
    latency_ms = Column(Integer, nullable=True)
    ttft_ms = Column(Integer, nullable=True)  # 首字延迟（time-to-first-token），流式请求记录，非流式/失败为 NULL
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    cache_read_tokens = Column(Integer, nullable=True)   # 缓存命中（读）token 数
    cache_write_tokens = Column(Integer, nullable=True)  # 缓存创建（写）token 数
    error_type = Column(String(50), nullable=True)
    error_msg = Column(Text, nullable=True)
    fallback_count = Column(Integer, nullable=False, default=0)
    user_ip = Column(String(64), nullable=True)
    api_key_id = Column(Integer, nullable=True)
    routed_provider_id = Column(Integer, nullable=True)  # 关联 providers.id（方案A：配额并入分析，统一数据源）
    estimated_cost_usd = Column(Float, nullable=True, default=0.0)  # 估算美元成本（建表时按模型单价计算）
    request_body = Column(Text, nullable=True)  # 请求包 JSON（遗留列；新日志改为存哈希引用，见下方三列）
    response_body = Column(Text, nullable=True)  # 返回包 JSON（遗留列）
    # —— 消息级去重引用（v3.6）——
    request_env_hash = Column(String(64), nullable=True)   # 请求信封（model/temp/... 去掉 messages）的 blob hash
    request_msg_hashes = Column(Text, nullable=True)      # JSON 数组：逐条消息的 blob hash（含顺序）
    response_body_hash = Column(String(64), nullable=True) # 响应整包的 blob hash
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    archived_at = Column(DateTime, nullable=True)  # 非空=详细内容已归档瘦身（统计元数据保留，body/blob 已清）
    est_prompt_tokens = Column(Integer, nullable=True)  # P1-5 预检估算输入 token（用于动态校准估算系数）
class AnalyticsCumulative(Base):
    """累计统计数据（单行 id=1）。

    归档时把被归档记录的各项统计累加进来，这样归档（删除 DB 记录）后
    分析页的总请求数/成功率/Token/平均延迟等指标仍然保留；
    只有「重置统计数据」按钮会清零本表（日志本身不受影响）。
    """
    __tablename__ = "analytics_cumulative"
    id = Column(Integer, primary_key=True, default=1)
    total_requests = Column(Integer, nullable=False, default=0, server_default="0")
    success_count = Column(Integer, nullable=False, default=0, server_default="0")
    auto_requests = Column(Integer, nullable=False, default=0, server_default="0")
    total_input_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    total_output_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    sum_latency_ms = Column(Integer, nullable=False, default=0, server_default="0")  # 有延迟样本的累计和（算平均延迟用）
    latency_count = Column(Integer, nullable=False, default=0, server_default="0")    # 有延迟的样本数
    sum_ttft_ms = Column(Integer, nullable=False, default=0, server_default="0")      # 首字延迟样本累计和（算平均首字延迟用）
    ttft_count = Column(Integer, nullable=False, default=0, server_default="0")       # 有首字延迟的样本数
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

Index("idx_request_logs_model_time", RequestLog.routed_model, RequestLog.created_at)
Index("idx_request_logs_status_time", RequestLog.status, RequestLog.created_at)
Index("idx_request_logs_conv_time", RequestLog.conversation_id, RequestLog.created_at)
Index("idx_request_logs_hc_time", RequestLog.is_health_check, RequestLog.created_at)
