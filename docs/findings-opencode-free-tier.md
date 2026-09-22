# OpenCode 免费层 403「can only be used from within OpenCode」取证报告

日期：2026-09-22　仓库：aigate-V2　触发：用户报告 combo 中 OpenCode Free 候选持续 403

## 结论（一句话）

OpenCode 免费层**不再是「请求头鉴权」**，而是要求 `x-opencode-session` 的前缀
**命中一个官方 CLI 进程建立过的会话**。纯 HTTP 转发（无论怎么伪造头/body/运行时）
在当前上游一律 403；9router 的头部方案（UA + 4 个 `x-opencode-*` 头）**已失效**。

## 现场证据链

| # | 实验 | 结果 |
|---|---|---|
| 1 | 现状：`Authorization: Bearer public` + `x-opencode-client: desktop` | 403 FreeTierError |
| 2 | 复刻官方 CLI 抓包到的**完整请求**（头 + 32KB body 原样） | 403 |
| 3 | 加 9router 全部头（`User-Agent: opencode` + session/request/project） | 403 |
| 4 | 用 bun 运行时（与官方 CLI 同运行时/TLS 栈） | 403 |
| 5 | `curl` HTTP/1.1 与 HTTP/2（不同 TLS 栈） | 403 |
| 6 | 只改官方 session 末位字符 | **200** ✅ |
| 7 | 截短官方 session 4 位 | 403 |
| 8 | 保留官方 session 前 8 位 + 补随机 | **200** ✅ |
| 9 | 全新随机 session（任意头/body 组合） | 403 |
| 10 | 官方 CLI 同机同模型实跑（`opencode run -m opencode/mimo-v2.6-flash-free`） | 200 ✅ |
| 11 | 复用官方 session 连续 5 次请求 | 5×200 ✅ |
| 12 | 官方 session + 极简 body（无 tools / 非流式） | 403 |

**判据判定**：6/8 证明服务端按 session **前缀**匹配已登记会话（非精确值）；7 证明需要足够长的前缀；
9/10 证明该会话只能由官方 CLI 建立；12 说明会话之外仍有次要约束（tools + stream）。

## 取证方法（可复用）

服务器 8000 未对本地网络开放，且 CLI 日志不打印请求头。最终有效的组合：

1. **官方二进制**：`npm pack opencode-linux-x64@1.18.32`（真实二进制在该可选依赖里，
   `opencode-ai` 主包只是下载器）
2. **透明抓包**：`mitmdump -s <addon> --listen-port 18888`，addon 里写
   `request(flow)` 记录 `flow.request.headers` / `body`；CLI 侧设
   `NODE_EXTRA_CA_CERTS=<mitm ca>` + `HTTPS_PROXY=http://127.0.0.1:18888`
3. **A/B 判据定位**：从官方完整请求出发，逐项剥离头/body 字段，观察 403/200 翻转
4. 备注：mitmdump **必须前台 exec**（`ssh ... "exec mitmdump ..."` + 后台任务），
   用 `nohup ... &` 在 SSH 会话里反复启动失败

抓到的官方请求形态（供参考）：
```
POST https://opencode.ai/zen/v1/chat/completions
Authorization: Bearer public
User-Agent: opencode/1.18.32 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14
x-opencode-client: cli            ← 注意是 cli，不是 desktop
x-opencode-project: global
x-opencode-request: msg_<24 hex>
x-opencode-session: ses_<28 chars>
# body: model + max_tokens:32000 + 9585 字 system + 11 个 tools + tool_choice:auto
#       + stream:true + stream_options:{include_usage:true}
```

## 本次已做（不引入常驻进程的部分）

- `server/core/free_providers.py`：对齐 9router 头部形态（UA `opencode/1.18.32` +
  session/request/project），opencode 也生成稳定 session id；
  **并在 `_surface_free_error` 里识别 `FreeTierError`**，把失败原因明确成
  「上游免费层已收紧为仅官方 CLI 会话可用」而不是笼统的 502
- 代码注释与本文档记录判据，避免后续重复踩坑

## 未做（需决策）

`docs/todo.md` 待办：**是否内置官方 CLI**（两种方案）
1. **常驻 CLI 代理**：服务器装 177MB 官方二进制，常驻 headless 进程，AIGate 把
   OpenCode Free 请求转给它。最稳（上游认的就是官方客户端），代价是常驻子进程 +
   需确认其使用条款允许。
2. **会话前缀继承**：启动时跑一次 CLI 抓取被祝福的 session 前缀，之后复用。
   轻量但有规避性质，且 session 失效需重捕。

用户已知悉，尚未选择；当前保持现状（combo 中该候选会跳过，其余免费候选不受影响）。
