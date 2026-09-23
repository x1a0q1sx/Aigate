"""真实客户端 IP 解析（9router 对比缺口：real client IP）。

nginx / 反代之后 raw_request.client.host 恒为 127.0.0.1——日志归属与限速
都失去意义。config.security.trust_proxy_headers=true 时启用
X-Forwarded-For 最左值（可信反代场景）；默认关闭，防客户端伪造头。
"""


def real_client_ip(request, trust_proxy: bool = False):
    """解析请求真实来源 IP。无可信来源时回退 socket 对端。"""
    if trust_proxy:
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            for part in xff.split(","):
                part = part.strip()
                if part:
                    return part[:64]
    client = getattr(request, "client", None)
    return client.host if client else None


# ── 会话标识（P1-4：session sticky 的键必须跨请求稳定）──────────────────
# 此前每请求 uuid4() → sticky 永远不命中，配置项形同虚设。
# 取值优先级：
#   1) 客户端显式头（x-conversation-id / x-session-id / conversation_id）
#      —— 支持客户端自行声明会话，最准确；
#   2) 无显式头时按「客户端身份 + 会话锚点」派生稳定哈希：同一客户端、
#      同一 system 提示、同一首条 user 消息 → 同一会话。
#      这是启发式：真正的多轮对话首条消息不变，故能稳定命中；
#      不同会话（首条消息不同）自然区分开。
def derive_conversation_id(request, messages, client_ip=None) -> tuple:
    """派生稳定的会话 ID。返回 (conversation_id, explicit)。

    注意：本函数刻意让同一会话的多次请求得到相同值（这正是 sticky 所需）。
    """
    import hashlib

    for header in ("x-conversation-id", "x-session-id", "conversation_id"):
        try:
            raw = (request.headers.get(header) or "").strip()
        except Exception:
            raw = ""
        if raw:
            return raw[:128], True

    sys_text = ""
    first_user = ""
    try:
        for m in (messages or []):
            if isinstance(m, dict):
                role, content = m.get("role"), m.get("content")
            else:
                role, content = getattr(m, "role", None), getattr(m, "content", None)
            if isinstance(content, list):
                # 多模态 content 数组：取其中的文本片段
                content = " ".join(
                    str(p.get("text", "")) for p in content
                    if isinstance(p, dict) and p.get("text")
                )
            content = str(content or "")
            if role == "system" and not sys_text:
                sys_text = content
            elif role == "user" and not first_user:
                first_user = content
            if sys_text and first_user:
                break
    except Exception:
        pass

    seed = "\x1f".join([str(client_ip or ""), sys_text[:2000], first_user[:2000]])
    digest = hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:32]
    return f"conv-{digest}", False
