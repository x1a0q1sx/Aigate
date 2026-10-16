"""OAuth 多账号频率感知分流（2026-10-16 用户要求）。

背景：F40 的自动选号是「锁定最优账号直到冷却」——额度排序键（quota_rank_key）
全量参与排序，最优账号吃掉几乎全部流量（生产实测 CodeBuddy CN 24h 4620 次里
91.6% 打同一个账号）。用户要求：请求频率高时自动多账号负载，频率低时保持粘住。

机制（纯进程内、零网络，与 F40 的内存态 + DB 冷却同构）：
- 每次 OAuth 取号成功（自动与点名都算——都真实消耗该账号），记一笔
  (provider_code, owner) 时刻戳；
- provider 级滑窗计数（默认 60s）≥ enter_requests（默认 10）→ 进入分流模式；
  退到 < exit_requests（默认 3）→ 回粘住模式（滞回防临界抖动）；
- 分流模式下排序在「额度档位」之后插入 (窗口内请求数, 最近使用时刻)——
  同档位账号之间轮流用；冷却/已耗尽账号照旧排最后，绝不为分流而往
  无余额账号打流量；
- 显式点名（provider.oauth_owner）取号不受影响；单账号 provider 自然无操作。
"""
import time
from collections import deque
from typing import Optional, Tuple

# provider_code -> {"picks": deque[monotonic], "accounts": {owner: deque}, "lb": bool}
_STATE: dict = {}


def _cfg() -> Tuple[bool, int, int, int]:
    """每调用读一次配置（热改即时生效）；任何异常回退内置默认。"""
    try:
        from server.config import get_config
        c = getattr(get_config(), "oauth_balance", None)
        if c is not None:
            return (
                bool(getattr(c, "enabled", True)),
                int(getattr(c, "window_seconds", 60) or 60),
                int(getattr(c, "enter_requests", 10) or 10),
                int(getattr(c, "exit_requests", 3) or 3),
            )
    except Exception:
        pass
    return True, 60, 10, 3


def _state(provider_code: str) -> dict:
    st = _STATE.get(provider_code)
    if st is None:
        st = {"picks": deque(), "accounts": {}, "lb": False}
        _STATE[provider_code] = st
    return st


def _trim(dq: deque, window: float, now: float) -> None:
    while dq and dq[0] <= now - window:
        dq.popleft()


def note_selection(provider_code: str, owner: str, now: Optional[float] = None) -> None:
    """取号成功后记一笔（永不抛异常；enabled=False 时完全静默）。"""
    if not provider_code or not owner:
        return
    try:
        enabled, window, enter, exit_ = _cfg()
        if not enabled:
            return
        now = time.monotonic() if now is None else now
        st = _state(provider_code)
        _trim(st["picks"], window, now)
        st["picks"].append(now)
        dq = st["accounts"].setdefault(owner, deque())
        _trim(dq, window, now)
        dq.append(now)
        # 滞回：高水位进入分流、低水位退回粘住
        if not st["lb"] and len(st["picks"]) >= enter:
            st["lb"] = True
        elif st["lb"] and len(st["picks"]) < exit_:
            st["lb"] = False
    except Exception:
        pass


def is_balancing(provider_code: str, now: Optional[float] = None) -> bool:
    """该 provider 当前是否处于分流模式（now 仅供测试注入）。"""
    try:
        enabled, window, _enter, exit_ = _cfg()
        if not enabled:
            return False
        st = _STATE.get(provider_code)
        if not st or not st.get("lb"):
            return False
        _trim(st["picks"], window, time.monotonic() if now is None else now)
        if len(st["picks"]) < exit_:
            st["lb"] = False
        return st["lb"]
    except Exception:
        return False


def account_load_key(provider_code: str, owner: str, now: Optional[float] = None) -> Tuple[int, float]:
    """分流模式排序分量：(窗口内请求数, 最近使用时刻)，较小者优先。

    刚被用过的账号 (count 更大或 last_used 更新) 排后 → 连续取号自然轮流。"""
    try:
        _, window, _enter, _exit = _cfg()
        now = time.monotonic() if now is None else now
        dq = (_STATE.get(provider_code) or {}).get("accounts", {}).get(owner)
        if not dq:
            return (0, 0.0)
        _trim(dq, window, now)
        return (len(dq), dq[-1] if dq else 0.0)
    except Exception:
        return (0, 0.0)


def stats(now: Optional[float] = None) -> dict:
    """观测用：各 provider 的滑窗计数与分流状态（诊断日志/接口可透出）。"""
    try:
        _, window, _enter, _exit = _cfg()
        now = time.monotonic() if now is None else now
        out = {}
        for code, st in _STATE.items():
            _trim(st["picks"], window, now)
            out[code] = {
                "picks_in_window": len(st["picks"]),
                "balancing": st["lb"],
            }
        return out
    except Exception:
        return {}


def reset() -> None:
    """测试辅助：清空全部进程内状态。"""
    _STATE.clear()
