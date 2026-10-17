#!/usr/bin/env python3
"""Meta Muse Code 本机设备码登录 → 打印 LLM| 推理 key（供 AIGate「导入 Token」）。

为什么需要这个脚本
------------------
AIGate 服务器侧的 muse 设备流是「服务器经代理申请设备码 → 用户浏览器批准」。
如果 Meta 把 device_code 绑定到申请时的出口地区，而浏览器在另一个网络，
浏览器侧就会报 “Sign-in failed / Token exchange failed”，轮询只能等到
expired_token（CLIProxyAPI #6242 同判：Meta 门户侧拒绝，非请求参数问题）。

本脚本让**申请与批准发生在同一台机器/同一出口**（和 opencode-muse-auth、
hermes-muse-code 等能登录的本地 CLI 一致），然后把铸出的 key 贴回 AIGate。
只用 Python 标准库，不需要 pip 安装任何东西。

用法
----
    python scripts/muse_login.py                       # 直连（本机已能访问 auth.meta.com）
    python scripts/muse_login.py --proxy http://127.0.0.1:7897
    python scripts/muse_login.py --no-browser          # 只打印链接，自己复制到浏览器

拿到 `LLM|...` 后：AIGate → OAuth 连接 → muse → 导入 Token → 粘贴。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

CLIENT_ID = "1031625952748946"          # 官方 Muse Code 插件内置的公开 client id
DEVICE_URL = "https://auth.meta.com/oidc/device/authorization/"
TOKEN_URL = "https://auth.meta.com/oidc/device/token/"
KEY_URL = "https://api.meta.ai/muse-code/key"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
UA = "muse-code/1.0.2"                  # 与 CLIProxyAPI / 官方 CLI 一致
API_VERSION = "1.0.0"
TIMEOUT = 25


def _opener(proxy: str):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _request(opener, url: str, data: bytes = None, headers: dict = None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise SystemExit(f"HTTP {status} 非 JSON 响应：{raw[:300]!r}")
    return status, body


def _post_form(opener, url: str, fields: dict):
    headers = {
        "Accept": "application/json",
        "User-Agent": UA,
        "x-api-version": API_VERSION,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    return _request(opener, url, urllib.parse.urlencode(fields).encode(), headers)


def _post_json(opener, url: str, payload: dict, access: str):
    headers = {
        "Accept": "application/json",
        "User-Agent": UA,
        "x-api-version": API_VERSION,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access}",
    }
    return _request(opener, url, json.dumps(payload).encode(), headers)


def main() -> int:
    ap = argparse.ArgumentParser(description="Meta Muse Code 本机设备码登录")
    ap.add_argument("--proxy", default="", help="代理地址，如 http://127.0.0.1:7897")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    opener = _opener(args.proxy.strip())

    print("① 申请设备码…", flush=True)
    status, body = _post_form(opener, DEVICE_URL, {"client_id": CLIENT_ID})
    if status >= 400:
        raise SystemExit(f"申请设备码失败：HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")
    device_code = str(body.get("device_code") or "")
    user_code = str(body.get("user_code") or "")
    url = str(body.get("verification_uri_complete") or body.get("verification_uri") or "")
    interval = max(1, int(body.get("interval") or 5))
    expires_in = max(30, int(body.get("expires_in") or 600))
    if not device_code or not user_code or not url:
        raise SystemExit(f"设备码响应缺字段：{json.dumps(body, ensure_ascii=False)[:300]}")

    print(f"② 在浏览器打开：{url}")
    print(f"   设备码：{user_code}（链接已内嵌时无需手输；{expires_in} 秒内有效）")
    print("   请用【个人 Meta 账号】登录并批准；企业/托管账号不支持设备码流。")
    print("   浏览器必须和本脚本走同一个出口地区（同一台机器直接跑最稳）。", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    print("③ 等待批准…", flush=True)
    deadline = time.time() + expires_in
    access = ""
    while time.time() < deadline:
        time.sleep(interval)
        try:
            _, tok = _post_form(opener, TOKEN_URL, {
                "grant_type": GRANT,
                "client_id": CLIENT_ID,
                "device_code": device_code,
            })
        except SystemExit:
            raise
        except Exception as e:                       # 网络抖动：继续等
            print(f"   轮询失败（{type(e).__name__}），重试…", flush=True)
            continue
        if not tok.get("error"):
            access = str(tok.get("access_token") or "")
            break
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "expired_token":
            raise SystemExit("设备码已过期：请重新运行本脚本。")
        if err == "access_denied":
            raise SystemExit("用户拒绝了授权。")
        raise SystemExit(f"授权失败：{err} {tok.get('error_description') or ''}")
    if not access:
        raise SystemExit("等待超时：浏览器侧未完成批准。")

    print("④ 已批准，正在铸造推理 key…", flush=True)
    mint = None
    for attempt in range(3):
        status, data = _post_json(opener, KEY_URL, {"onboard": True}, access)
        if status != 429 and status < 500:
            mint = (status, data)
            break
        print(f"   铸 key 被限流（HTTP {status}），{5 * (attempt + 1)}s 后重试…", flush=True)
        time.sleep(5 * (attempt + 1))
    if mint is None:
        raise SystemExit("铸 key 连续失败（429/5xx），请稍后重试。")
    status, data = mint
    if status >= 400:
        msg = " ".join(str(x) for x in (data.get("title"), data.get("detail")) if x) \
            or json.dumps(data, ensure_ascii=False)[:300]
        raise SystemExit(f"铸 key 失败：HTTP {status} {msg}")
    if data.get("is_subs_active") is False:
        raise SystemExit("该账号的 Muse Code 订阅未激活（is_subs_active=false）。")
    key = str(data.get("api_key") or "").strip()
    if not key:
        action = data.get("action_url") or data.get("require_payment_action_url") or ""
        raise SystemExit("响应里没有 api_key" + (f"，需要订阅/付费：{action}" if action else ""))

    print("\n" + "=" * 60)
    print("推理 key（复制整段，含 LLM| 前缀）：")
    print(key)
    print("=" * 60)
    print("AIGate → OAuth 连接 → muse → 导入 Token → 粘贴上面的 key（Owner 自填）。")
    email = str(data.get("user_email") or "").strip()
    if email:
        print(f"账号：{email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
