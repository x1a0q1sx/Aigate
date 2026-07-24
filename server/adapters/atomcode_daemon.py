"""
AtomCode 本地 daemon 管理 + 精简 HTTP 客户端

AIGate 的 atomcode 适配器不再自己实现上游签名（上游签名算法为闭源/随版本变化，
且二进制用 rustls 自带根证书，无法在本机注入 CA 抓包反推）。改为由 AIGate 自己
拉起并管理本机 `atomcode` 可执行文件（daemon 模式）作为签名代理：daemon 直连真实
网关 llm-api.atomgit.com 并透明完成鉴权/签名，AIGate 只负责把 OpenAI 格式请求
转成 daemon 的 /chat 协议并回传。

daemon 协议（参考 AtomCode2API 的 daemon_client.py）：
  GET  /health               健康
  GET  /models               可用模型 [{provider, model, is_default, ...}]
  POST /chat  (SSE)          需要一个 `message` 字符串（agent 模式）；
                             不传 working_dir 时即为纯对话（无工具/文件操作）。
                             SSE 事件: reasoning / text / tokens / done / error / stopped
"""
from __future__ import annotations

import os
import json
import asyncio
import logging
from typing import AsyncGenerator, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_PORT = 13456

# 用户通过前端 UI 显式配置的可执行文件路径（持久化，优先级最高），
# 存于项目 data/atomcode_config.json。这样换机器/重启后无需再设环境变量。
_ATOMCODE_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "atomcode_config.json",
)


def _load_persisted_path() -> Optional[str]:
    """读取 UI 持久化的 exe 路径；文件缺失/损坏/指向不存在的文件则返回 None。"""
    try:
        if os.path.exists(_ATOMCODE_CONFIG_PATH):
            with open(_ATOMCODE_CONFIG_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            p = d.get("exe_path")
            if p and os.path.isfile(p):
                return os.path.abspath(p)
    except Exception:
        pass
    return None


def save_atomcode_exe_path(raw: str) -> str:
    """校验并持久化用户配置的 atomcode 可执行文件/目录，返回解析后的 exe 绝对路径。

    - 传入目录时自动在其中寻找 atomcode / atomcode.exe 等可执行文件；
    - 传入文件时直接校验存在性；
    - 解析结果写入 data/atomcode_config.json，进程重启后仍生效。
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("路径不能为空")
    if os.path.isdir(raw):
        found = None
        for name in ("atomcode.exe", "atomcode", "atomcode-daemon.exe", "atomcode-daemon"):
            cand = os.path.join(raw, name)
            if os.path.isfile(cand):
                found = cand
                break
        if not found:
            raise ValueError(f"目录 {raw} 下未找到 atomcode / atomcode.exe 可执行文件")
        raw = found
    if not os.path.isfile(raw):
        raise ValueError(f"文件不存在: {raw}")
    exe = os.path.abspath(raw)
    os.makedirs(os.path.dirname(_ATOMCODE_CONFIG_PATH), exist_ok=True)
    with open(_ATOMCODE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"exe_path": exe}, f, ensure_ascii=False, indent=2)
    return exe


def atomcode_exe_status() -> dict:
    """返回 atomcode 可执行文件探测状态，供前端判断是否需要提示用户配置。"""
    persisted = _load_persisted_path()
    resolved = None
    for c in _candidate_exes():
        resolved = c
        break
    return {
        "found": resolved is not None,
        "exe_path": resolved,
        "configured_path": persisted,
        "candidates": _candidate_exes(),
    }


# ---------------------------------------------------------------------------
# 可执行文件定位
# ---------------------------------------------------------------------------
def _candidate_exes() -> List[str]:
    """返回可能存在的 atomcode 可执行文件路径（按优先级）。

    优先级：① 用户 UI 持久化路径 → ② 环境变量 ATOMCODE_EXE_PATH →
    ③ PATH 探测 → ④ 官方默认安装位置 → ⑤ 已知开发目录。
    """
    cands: List[str] = []
    # ① 用户通过前端 UI 显式配置的路径（持久化，最高优先级）
    pp = _load_persisted_path()
    if pp:
        cands.append(pp)
    # ② 环境变量
    env = os.environ.get("ATOMCODE_EXE_PATH")
    if env:
        cands.append(env)
    # PATH 中的 atomcode / atomcode.exe
    from shutil import which

    w = which("atomcode") or which("atomcode.exe")
    if w:
        cands.append(w)
    # 官方默认安装位置 %LOCALAPPDATA%\\AtomCode\\atomcode.exe
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(os.path.join(la, "AtomCode", "atomcode.exe"))
    # 本机已知的开发/逆向目录（用户在此放了 v4.26.0 可执行文件）
    cands.append(r"D:\300_Study\340_AI\atomcode\atomcode-v4.26.0-windows-x64.exe")
    # 去重并仅保留真实存在的
    seen = set()
    out: List[str] = []
    for c in cands:
        c = os.path.abspath(c)
        if c in seen:
            continue
        seen.add(c)
        if os.path.exists(c) and os.path.isfile(c):
            out.append(c)
    return out


class AtomCodeDaemonError(Exception):
    """daemon 拉起/通信失败。"""


# ---------------------------------------------------------------------------
# SSE 事件解析
# ---------------------------------------------------------------------------
def _parse_sse(data_str: str) -> Optional[dict]:
    """解析单行 SSE `data: {...}` 为事件 dict；失败返回 None。"""
    try:
        return json.loads(data_str)
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 精简 daemon HTTP 客户端
# ---------------------------------------------------------------------------
class DaemonClient:
    def __init__(self, port: int = DEFAULT_PORT, timeout: int = 60) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def health(self) -> dict:
        r = await self._client.get(f"{self.base}/health")
        r.raise_for_status()
        return r.json()

    async def is_running(self) -> bool:
        try:
            await self.health()
            return True
        except Exception:
            return False

    async def list_models(self) -> list:
        r = await self._client.get(f"{self.base}/models")
        r.raise_for_status()
        return r.json()

    async def stream_chat(self, payload: dict) -> AsyncGenerator[dict, None]:
        """POST /chat（SSE），逐条 yield 事件 dict。"""
        async with self._client.stream(
            "POST",
            f"{self.base}/chat",
            json=payload,
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(300.0, connect=30.0),
        ) as resp:
            resp.raise_for_status()
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                lines = buf.split("\n")
                buf = lines.pop() or ""
                for line in lines:
                    s = line.strip()
                    if not s or s.startswith(":"):
                        continue
                    if s.startswith("data: "):
                        s = s[6:]
                    ev = _parse_sse(s)
                    if ev is not None:
                        yield ev


# ---------------------------------------------------------------------------
# daemon 进程管理器（单例）
# ---------------------------------------------------------------------------
class DaemonManager:
    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self._proc = None
        self._started_by_us = False
        self._lock = asyncio.Lock()
        self._client: Optional[DaemonClient] = None

    def _find_exe(self) -> Optional[str]:
        for c in _candidate_exes():
            return c
        return None

    async def get_client(self) -> DaemonClient:
        """返回可用的 DaemonClient，必要时拉起 daemon。"""
        async with self._lock:
            if self._client is None:
                self._client = DaemonClient(self.port)
            if await self._client.is_running():
                return self._client
            exe = self._find_exe()
            if not exe:
                raise AtomCodeDaemonError(
                    "未找到 atomcode 可执行文件。请先在「服务商管理」页面点击该服务商的"
                    "「配置可执行文件」按钮，填入 AtomCode 的安装目录或 exe 路径；"
                    "或设置环境变量 ATOMCODE_EXE_PATH 指向 atomcode 可执行文件。"
                )
            logger.info("[atomcode] 启动本地 daemon: %s daemon --port %d", exe, self.port)
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    exe, "daemon", "--port", str(self.port),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    # 独立于 AIGate 进程树，避免被一起回收
                    creationflags=getattr(os, "DETACHED_PROCESS", 0)
                    | getattr(os, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            except Exception as e:
                raise AtomCodeDaemonError(f"启动 atomcode daemon 失败: {e}") from e
            self._started_by_us = True
            # 等待健康检查通过（最多 ~20s）
            for _ in range(40):
                await asyncio.sleep(0.5)
                if await self._client.is_running():
                    logger.info("[atomcode] 本地 daemon 已就绪")
                    return self._client
            self._started_by_us = False
            raise AtomCodeDaemonError("atomcode daemon 启动后健康检查超时")

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._proc is not None and self._started_by_us:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None


_manager: Optional[DaemonManager] = None


def get_manager() -> DaemonManager:
    global _manager
    if _manager is None:
        _manager = DaemonManager()
    return _manager


async def get_daemon_client() -> DaemonClient:
    return await get_manager().get_client()
