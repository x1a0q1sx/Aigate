"""httpx 超时按相位拆分 —— 别让「一个数字」当全部超时。

背景（2026-11-03 生产日志实锤）：Cline Pass 一条 66k token 的请求挂了
**137.2 秒**才报 `ConnectError: 连接被对端中断（TLS 握手/写入阶段被掐断）`。
适配器写的是 `httpx.AsyncClient(timeout=self.timeout)`，而 httpx 会把这一个
数字**同时**当作 connect / read / write / pool 四个相位的超时：

  - read=180s 是有意义的（推理模型首字前可以静默很久）；
  - 但 connect=180s 毫无意义 —— TCP/TLS 建不上，等 3 分钟也不会自己好；
  - write=180s 更糟：大 body（长上下文动辄几百 KB）上传途中被中间设备掐断时，
    写操作会一直等到超时或收到 RST 才报错。

后果不只是「慢」：请求在本地僵死这段时间里，auto 回退、竞速、失败重试全被
堵住，客户端只能干等，队列类上游还会被叠加压力（与 Session 41 Qoder 同源）。

本模块把超时拆成：read 保持原值，其余相位封顶。默认值与各适配器既有约定一致
（qoder/trae/codearts 早已用 httpx.Timeout(connect=30, read=...)）。
"""
import httpx

# 建连封顶：正常链路 <1s，跨境抖动 3~5s；30s 还没连上就是连不上
CONNECT_TIMEOUT_SECONDS = 30.0
# 单次写操作封顶（httpx 的 write 是每个写动作，不是整包上传总时长）
WRITE_TIMEOUT_SECONDS = 60.0
# 连接池取用封顶：这些适配器基本每请求新建 client，pool 只兜底异常场景
POOL_TIMEOUT_SECONDS = 10.0


def phase_timeout(total, *, connect: float = CONNECT_TIMEOUT_SECONDS,
                  write: float = WRITE_TIMEOUT_SECONDS,
                  pool: float = POOL_TIMEOUT_SECONDS):
    """把单一超时值 `total`（秒）拆成按相位的 httpx.Timeout；read 保留原值。

    total 若比某个封顶还小，则以 total 为准（不把短超时请求悄悄放宽）。
    非数字/空值原样返回，交回 httpx 自己处理，避免影响既有调用方。
    """
    try:
        read = float(total)
    except (TypeError, ValueError):
        return total
    if read <= 0:
        return total
    return httpx.Timeout(
        connect=min(connect, read),
        read=read,
        write=min(write, read),
        pool=min(pool, read),
    )
