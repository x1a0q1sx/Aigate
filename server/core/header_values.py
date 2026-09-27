"""自定义 HTTP 头的值归一化 —— 全项目唯一的真相源。

背景（2026-09-27 生产事故，日志实证）：
    /providers/import 直接吃任意 JSON、不做类型校验，把
    {"x-video-timeout": 1800} 这类**非字符串头值**原样入库。三处后果：

    1. `ProviderResponse.headers: Optional[Dict[str, str]]` 校验失败
       → `GET /admin/api/providers` 整体 500（生产 13 次）
       → 服务商管理页空白；模型页 `Promise.all([getProviders(), ...])`
         被同一个 500 带崩一并报错。
    2. httpx 拒绝非 str 头值（`TypeError: Header value must be str or bytes`）
       → 该服务商**所有推理请求**直接失败（生产 aistudio 请求日志实测到）。
    3. 同名字段在库里类型混杂，导出 → 再导入会反复带病。

本模块被四个层面共用，保证「入库 / 出库 / 出站」三处值恒为 str：
    - ORM TypeDecorator（server/models/provider.py::HeaderJSON，读写双向兜底）
    - schema 校验器（写入侧 strict、读取侧 lenient）
    - 启动存量修复（server/db.py::repair_dirty_provider_headers）
    - 请求路径合并 model request_overrides.headers（server/api/v1_router.py）
"""
from typing import Any, Dict, Optional


def normalize_header_value(v: Any) -> Optional[str]:
    """单个头值 → str；非标量（dict/list/None）返回 None 表示应丢弃。"""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):          # 必须先于 int 判断（bool 是 int 子类）
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return None


def normalize_map(value: Any, *, strict: bool = False) -> Any:
    """把 headers 映射里的值全部归一化为 str。

    strict=True（写入侧）：非标量值抛 ValueError —— 调用方（FastAPI/Pydantic）
    会把它转成 422，用户能看到明确报错，而不是把垃圾存进库。
    strict=False（读取/出站侧）：非标量值静默丢弃、**绝不抛异常** ——
    列表/详情接口的响应模型不允许因单行历史脏数据整体 500（这正是事故形态）。

    None 原样返回（schema 里 None = 未提供）；非 dict 输入原样返回，
    交给上层校验决定接受还是拒绝。
    """
    if value is None or not isinstance(value, dict):
        return value
    out: Dict[str, str] = {}
    for k, v in value.items():
        key = str(k)
        nv = normalize_header_value(v)
        if nv is None:
            if strict:
                raise ValueError(
                    f"header「{key}」的值必须是字符串/数字/布尔，"
                    f"收到 {type(v).__name__}"
                )
            continue
        out[key] = nv
    return out


def outbound_headers(extra: Any) -> Dict[str, str]:
    """出站前的 extra_headers 净化：剥离网关内部标记 + 值归一化为 str。

    网关内部标记（`__oauth` / `__proxy_force` / `__proxy_url` / `__fg` /
    `__dpop` / `__baseUrl` / `__refresh` …）由各适配器按需**读取**，绝不是上游
    协议的一部分；若原样并入出站头，httpx 会因布尔值抛
    `TypeError: Header value must be str or bytes`（`__oauth` 正是 bool）。

    各适配器此前各自维护过滤名单，漏一个键就是一类线上故障 —— 统一走本函数，
    约定「双下划线前缀 = 网关内部键，一律不出站」，新增内部键无需再改适配器。
    """
    if not isinstance(extra, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in extra.items():
        key = str(k)
        if key.startswith("__"):
            continue
        nv = normalize_header_value(v)
        if nv is not None:
            out[key] = nv
    return out
