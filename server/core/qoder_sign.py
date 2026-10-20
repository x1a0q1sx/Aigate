"""Qoder COSY 签名 + WAF 绕过 body 编码 + 端点常量。

端口自 9router open-sse/shared/qoder/{constants,cosy,encoding}.js
（上游溯源 CLIProxyAPIPlus qoder-provider 分支）。

- COSY：每个签名请求携带 RSA 包裹的 AES 密钥 + AES-CBC 加密的用户信息
  payload + MD5(payload\\ncosyKey\\nts\\nbody\\nsigPath) 签名，以及 17 个
  Cosy-* 客户端指纹头。头名大小写与 qodercli 报文逐一对齐（Cosy-Machineid）。
- Encode：base64 → 三段重排 [tail][mid][head] → 自定义字母表替换；
  URL 需带 &Encode=1，阿里云 WAF 无法明文匹配请求体。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_pem_public_key

# ── 端点 ─────────────────────────────────────────────
# Qoder 是**两个独立站点**（账号互不相通，协议同构）：
#   - qoder     国际版：qoder.com / openapi.qoder.sh / api3.qoder.sh
#   - qoder_cn  国内版：qoder.cn / openapi.qoder.com.cn / gateway.qoder.com.cn
# 国内版协议取自 @qodercn-ai/qoderclicn 1.1.74 产物（sign-in 页 / 账号接口 /
# 网关三件套），并与 @magpie-community/opencode-qoder-auth 0.2.21 的 SITES
# 定义逐字段对齐；CN 的 device token 直接当聊天 token 用（CLI 不换 job token）。
QODER_OPENAPI_BASE = "https://openapi.qoder.sh"
QODER_CENTER_BASE = "https://center.qoder.sh"
QODER_CHAT_BASE = "https://api3.qoder.sh"
# jt-（job token）流量 api3 会 403 "Login expired"，官方 qodercli 走 api2
QODER_CHAT_BASE_ALT = "https://api2.qoder.sh"

QODER_LOGIN_URL = "https://qoder.com/device/selectAccounts"
QODER_DEVICE_TOKEN_URL = f"{QODER_OPENAPI_BASE}/api/v1/deviceToken/poll"
QODER_USERINFO_URL = f"{QODER_OPENAPI_BASE}/api/v1/userinfo"
QODER_QUOTA_USAGE_URL = f"{QODER_OPENAPI_BASE}/api/v2/quota/usage"
QODER_JOB_TOKEN_EXCHANGE_URL = f"{QODER_OPENAPI_BASE}/api/v1/jobToken/exchange"

QODER_CHAT_SIG_PATH = "/api/v2/service/pro/sse/agent_chat_generation"
QODER_MODEL_LIST_SIG_PATH = "/api/v2/model/list"
QODER_CHAT_URL_ENCODED = (
    f"{QODER_CHAT_BASE}/algo{QODER_CHAT_SIG_PATH}"
    "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
)

# 站点表：site 键即 OAuth provider_code（qoder / qoder_cn）
QODER_SITES: dict = {
    "qoder": {
        "openapi": "https://openapi.qoder.sh",
        "chat": "https://api3.qoder.sh",
        "chat_alt": "https://api2.qoder.sh",
        "login": "https://qoder.com/device/selectAccounts",
        # 国际版登录页当前不校验 client_id（现行设备流一直没带，勿改以免回归）
        "client_id": "",
        "redirect_uri": "",
        "label": "Qoder",
    },
    "qoder_cn": {
        "openapi": "https://openapi.qoder.com.cn",
        "chat": "https://gateway.qoder.com.cn",
        "chat_alt": "https://gateway.qoder.com.cn",
        "login": "https://qoder.cn/device/selectAccounts",
        # 国内版 CLI 生产 client id（@qodercn-ai/qoderclicn 产物内常量），
        # 登录页需要它；CLI 不传 redirect_uri，这里同样不传。
        "client_id": "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        "redirect_uri": "",
        "label": "Qoder CN",
    },
}


def qoder_site(site: str = "qoder") -> dict:
    """取站点配置；未知值回退国际版（永不让配置错误打断推理）。"""
    return QODER_SITES.get(str(site or "").strip() or "qoder") or QODER_SITES["qoder"]


def qoder_openapi_base(site: str = "qoder") -> str:
    return qoder_site(site)["openapi"]


def qoder_site_from_base_url(base_url: str) -> str:
    """按服务商 base_url 判站点（适配器只拿得到 base_url，拿不到 provider 行）。

    CN 的推理网关是 gateway.qoder.com.cn，国际版是 api2/api3.qoder.sh；
    判据用域名后缀而不是精确 URL，容忍用户手填路径差异。
    """
    b = str(base_url or "").lower()
    if "qoder.com.cn" in b or "qoder.cn" in b:
        return "qoder_cn"
    return "qoder"

# ── 指纹常量（上游签名校验依赖，勿改） ─────────────────
# ⚠️ 模型目录**按客户端版本下发**（2026-10-17 实测）：
#   1.0.0 → 15 个（含已下架的 performance，无 fast/Sonus/Cantus）
#   1.1.73（当前 npm @qoder-ai/qodercli latest）→ 17 个（fast/smodel/cmodel 上新）
# 版本钉死会让「刷新模型」永远看不到上游新增模型，升级 CLI 后需同步改这里。
# 可用环境变量 QODER_CLI_VERSION 覆盖（排查新目录/灰度时无需改代码）。
QODER_IDE_VERSION = os.environ.get("QODER_CLI_VERSION") or "1.1.73"
QODER_CLIENT_TYPE = "5"
QODER_DATA_POLICY = "disagree"
QODER_LOGIN_VERSION = "v2"
QODER_MACHINE_OS = "x86_64_windows"
QODER_MACHINE_TYPE = "5"

QODER_RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""


def qoder_inference_base(token: str, site: str = "qoder") -> str:
    """jt- 流量走 api2，dt-/pt- 换出的其它 token 默认 api3；CN 固定走网关。"""
    s = qoder_site(site)
    t = token or ""
    if s["chat"] == s["chat_alt"]:
        return s["chat"]
    if not t.startswith("pt-") and t.startswith("jt-"):
        return s["chat_alt"]
    return s["chat"]


def model_list_url(token: str, site: str = "qoder") -> str:
    base = qoder_inference_base(token, site)
    return f"{base}/algo{QODER_MODEL_LIST_SIG_PATH}"


def chat_url(token: str, site: str = "qoder") -> str:
    base = qoder_inference_base(token, site)
    return (f"{base}/algo{QODER_CHAT_SIG_PATH}"
            "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1")


# ── WAF 绕过编码 ─────────────────────────────────────
_QODER_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_QODER_CUSTOM = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"

_S2C = {ord(s): ord(c) for s, c in zip(_QODER_STD, _QODER_CUSTOM)}
_S2C[ord("=")] = ord("$")
_C2S = {v: k for k, v in _S2C.items()}


def qoder_encode_body(plaintext: bytes) -> bytes:
    """base64 → 三段重排 → 字母替换；返回按 latin-1 落字节的编码串。"""
    std = base64.b64encode(plaintext).decode()
    n = len(std)
    a = n // 3
    rearranged = std[n - a:] + std[a:n - a] + std[:a]
    return rearranged.translate(_S2C).encode("latin-1")


def qoder_decode_body(encoded: bytes) -> bytes:
    """逆向：字母还原 → 三段重排还原 → base64 解码（服务端与测试用）。

    encode: std=[H|M|T] → 发出 [T|M|H]；故 std = 发出串的 尾段+中段+头段。
    """
    s = encoded.decode("latin-1").translate(_C2S)
    n = len(s)
    a = n // 3
    std = s[n - a:] + s[a:n - a] + s[:a]
    return base64.b64decode(std.encode())


# ── COSY ─────────────────────────────────────────────
_PUBKEY = None


def _pubkey():
    global _PUBKEY
    if _PUBKEY is None:
        _PUBKEY = load_pem_public_key(QODER_RSA_PUBLIC_KEY.encode())
    return _PUBKEY


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    p = block - (len(data) % block)
    return data + bytes([p]) * p


def _aes_encrypt_cbc_b64(plaintext: str, key_str: str) -> str:
    key = key_str.encode("utf-8")
    assert len(key) == 16, f"aes key must be 16 bytes, got {len(key)}"
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    enc = cipher.encryptor()
    out = enc.update(_pkcs7_pad(plaintext.encode("utf-8"))) + enc.finalize()
    return base64.b64encode(out).decode()


def _rsa_encrypt_b64(data: str) -> str:
    out = _pubkey().encrypt(data.encode("utf-8"), asym_padding.PKCS1v15())
    return base64.b64encode(out).decode()


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _sig_path(request_url: str) -> str:
    try:
        path = urlparse(request_url).path or ""
    except ValueError:
        return ""
    if path.startswith("/algo"):
        return path[len("/algo"):]
    return path


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_cosy_headers(body: bytes, request_url: str, creds: dict) -> dict:
    """构建一个请求的完整 Cosy-* 头集。

    creds: {user_id, auth_token, name?, email?, machine_id?}
    body 必须是即将发送的**最终字节**（Encode 模式下传编码后的 body）。
    """
    user_id = creds.get("user_id") or ""
    auth_token = creds.get("auth_token") or ""
    if not user_id:
        raise ValueError("cosy: user id is empty")
    if not auth_token:
        raise ValueError("cosy: auth token is empty")

    body = bytes(body or b"")
    aes_key = str(uuid.uuid4())[:16]
    info_b64 = _aes_encrypt_cbc_b64(_json_bytes({
        "uid": user_id,
        "security_oauth_token": auth_token,
        "name": creds.get("name") or "",
        "aid": "",
        "email": creds.get("email") or "",
    }).decode("utf-8"), aes_key)
    cosy_key = _rsa_encrypt_b64(aes_key)

    timestamp = str(int(time.time()))
    payload_b64 = base64.b64encode(_json_bytes({
        "version": "v1",
        "requestId": str(uuid.uuid4()),
        "info": info_b64,
        "cosyVersion": QODER_IDE_VERSION,
        "ideVersion": "",
    })).decode()

    path = _sig_path(request_url)
    sig_input = "\n".join([payload_b64, cosy_key, timestamp,
                           body.decode("latin-1"), path])
    sig = _md5_hex(sig_input.encode("latin-1"))

    machine_id = creds.get("machine_id") or str(uuid.uuid4())
    return {
        "Authorization": f"Bearer COSY.{payload_b64}.{sig}",
        "Cosy-Key": cosy_key,
        "Cosy-User": user_id,
        "Cosy-Date": timestamp,
        "Cosy-Version": QODER_IDE_VERSION,
        "Cosy-Machineid": machine_id,
        "Cosy-Machinetoken": machine_id,
        "Cosy-Machinetype": QODER_MACHINE_TYPE,
        "Cosy-Machineos": QODER_MACHINE_OS,
        "Cosy-Clienttype": QODER_CLIENT_TYPE,
        "Cosy-Clientip": "127.0.0.1",
        "Cosy-Bodyhash": _md5_hex(body),
        "Cosy-Bodylength": str(len(body)),
        "Cosy-Sigpath": path,
        "Cosy-Data-Policy": QODER_DATA_POLICY,
        "Cosy-Organization-Id": "",
        "Cosy-Organization-Tags": "",
        "Login-Version": QODER_LOGIN_VERSION,
        "X-Request-Id": str(uuid.uuid4()),
    }
