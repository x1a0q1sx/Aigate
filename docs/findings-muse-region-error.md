# F39 muse-spark RegionError 根因 + mihomo 域名路由修复 (2026-10-13)

## 一、现象回顾

用户报 `OpenCode/muse-spark-1.3-contributor-free` 调用失败：
```
RuntimeError: opencode sidecar 上游报错: {"type":"error","error":{"type":"RegionError","message":"This model is not available in your country"}}
```

最近一次失败行（DB `request_logs`）：
| id | created_at (UTC) | model | status | error_type | latency_ms |
|---|---|---|---|---|---|
| 45844 | 2026-10-13 02:45:29 | OpenCode/muse-spark-1.3-contributor-free | error | upstream_error | 3872 |

本地时间 = UTC +8 → **10:45 CST**。

## 二、根因取证（日志 + 数据库）

### 2.1 DB 时区校验
SQLite `created_at` 存 UTC：
- `SELECT datetime("now")` = `2026-10-13 05:56:57`
- `SELECT datetime("now","localtime")` = `2026-10-13 13:56:57`
- 行 45844 的 `created_at = 02:45:29` → **10:45 CST**

### 2.2 mihomo journal 实时节点追踪（10:45 时刻）
```bash
sudo journalctl -u clash.service --since "2026-10-13 10:45:00" --until "2026-10-13 10:50:00" \
  | grep "opencode.ai"
```
**命中行**：
```text
Oct 13 10:45:27 ... --> opencode.ai:443 match Match using PROXY[🇭🇰|香港-中转 01]
```

**结论**：失败时刻请求经 `AUTO-sabusuku` url-test 选中**香港节点**，而 `muse-spark` 的地区门在 HK 出口判 403。

### 2.3 今日出口分布全貌（所有 opencode.ai 连接）
```text
     15 🇭🇰|香港-中转 01
      7 🇯🇵|日本-IEPL 02
      6 🇸🇬|新加坡-直连
      1 🇯🇵|日本-IEPL 01
```
只有 **HK 时段失败**，JP/SG 出口下可通。

### 2.4 网关真实路径复现
直接调 `server/core/free_providers` 入口（不经过 UI/playground）：
```python
from server.core.free_providers import get_free_executor
from server.schemas.chat import ChatCompletionRequest
exec_ = get_free_executor("opencode")
req = ChatCompletionRequest(model="muse-spark-1.3-contributor-free", ...)
await exec_.execute_non_stream(req)
```
- **SG 出口**（当前节点）：成功 11.5s 返回 `"Hi"`
- **JP 出口**（手动切后）：成功 31s 返回 `region-ok`
- **HK 出口**（历史 10:45）：RegionError（无法重现，已飘走）

**结论**：muse 的地区门是**国别硬闸**，不是 gateway 侧 bug；只要出口 IP 合规即通。

## 三、修复方案（mihomo 配置级）

问题本质：`AUTO-sabusuku` 是 url-test 每 300s 自动换节点，选点基于 gstatic 测速，而 opencode.ai 地区门与 gstatic 不同步。

**解决思路**：
1. 新建 `OPENCODE-REGION` 组（只含 JP/SG 节点，过滤掉 HK/US/CN 等地区门敏感节点）
2. 把 `opencode.ai / models.opencode.ai` 子域用 `DOMAIN-SUFFIX` 规则固定到该组
3. 改组内测速目标为 `https://opencode.ai/`（真正业务目标），避免选到一个能 ping 但被地区门拦的节点

### 3.1 补丁内容（2026-10-13，服务器 `/opt/clash/clash_config.yaml`）

#### 新增代理组：
```yaml
  # opencode.ai 地区门专用出口组（2026-10-13）：muse-spark 等免费模型在
  # HK 出口判 RegionError、JP/SG 出口通过；AUTO-sabusuku 每 300s 自动换节点
  # 会飘到 HK → 把 opencode.ai 固定到 JP/SG 节点池，不影响其它流量。
  - name: OPENCODE-REGION
    type: url-test
    use:
      - sabusuku
    filter: "(日本 | 新加坡)"
    url: https://opencode.ai/
    interval: 300
```

#### 新增规则：
```yaml
rules:
  - GEOSITE,cn,DIRECT
  - GEOIP,CN,DIRECT
  - DOMAIN-SUFFIX,opencode.ai,OPENCODE-REGION
  - MATCH,PROXY
```

### 3.2 生效方式
- 属主恢复：`chown clashm:clashm /opt/clash/clash_config.yaml; chmod 664 ...`
- 热重载：`systemctl reload clash.service` 或 `kill -HUP <mihomo pid>`

### 3.3 实测效果
- 规则命中：`DomainSuffix(opencode.ai) using OPENCODE-REGION[🇯🇵|日本 - 中转 01]`
- 延迟检测：group delay API 回 5 个候选延迟（JP/SG 各 ~0.6~1.0s）
- muse 全链路：非流式 ×2 + 流式 ×1 **全部成功**（3.0s / 2.3s / 1.7s）
- 一般流量：google/github/ipinfo 仍正常（200），未影响全局代理

## 四、其他发现

### 4.1 space-bunny-free 429
同属 opencode.ai 域，空间兔报 429 `FreeUsageLimitError` → **上游限流**，与本 fix 无关。

### 4.2 curl SSL 瞬断
重载后首次 curl 报 SSL EOF 是 systemd SIGHUP 瞬断窗口；3s 后完全恢复。

## 五、教训与长期约束

1. **url-test 的测速目标 ≠ 业务目标**：gstatic 连通性不代表特定 API 可用，对地区敏感模型必须把测速目标设为实际域名。
2. **日志分析要跨源对齐**：DB timestamp（UTC）、journal timestamp（本地）+ pm2 restart history 三者交叉才能还原事故窗口。
3. **脚本传输长命令**：SSH 管道单引号易被吞，大脚本 base64 编码再 decode 更可靠（AGENTS.md 约定）。
4. **规则粒度**：`DOMAIN-SUFFIX,opencode.ai` 同时覆盖 `models.opencode.ai` 和主站，简化维护。

## 六、后续建议

- 若上游持续收紧（如 HK 节点突然解禁），可回滚补丁删除 OPENCODE-REGION，恢复原统一代理策略。
- 考虑增加 Prometheus 监控：`opencode_region_error_count`（按出口分组统计），提前预警节点飘移风险。
- 文档沉淀至 `docs/findings-opencode-free-tier.md` 第 10 节（2026-10-13 mihomo 域名路由修复）。

---

**验证时间**: 2026-10-13 14:03–14:30 CST  
**验证者**: AIGate Ops  
**状态**: ✅ 已上线，稳定运行
