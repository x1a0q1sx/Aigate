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
