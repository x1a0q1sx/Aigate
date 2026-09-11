# 9router 对比研究（2026-09-11）

对标项目：https://github.com/decolua/9router（近期版本 v0.4.71 → v0.5.35，约 10 个 release）

## 9router 近期更新主题归类

| 主题 | 内容摘要 | AIGate 现状 |
|---|---|---|
| 组合策略 | Fusion（并行+judge 合成）、capacity 能力自动切换、per-combo 策略+judge 选择器 | fallback/round_robin/weighted 已有；Fusion 禁用 501 → **设计文档已出待批**；capacity 未做 |
| 思考强度 | per-model thinking picker +（level）后缀、adaptive thinking、预算与 max_tokens 对账 | 后缀方案已有（-high 等）、effort→budget 映射已有；adaptive thinking 可选跟进 |
| 客户端生态 | CLI 配置写入 8 路（claude/codex/copilot/cline/opencode 等）、Kiro/Copilot/CodeBuddy CN/Antigravity/Venice 等 provider | 服务商可手动添加+内置 OAuth 三家；CLI 配置生成器未做（候选） |
| token 节省 | RTK git-log 过滤器、caveman 文言级别、**X-9Router-Token-Saver 单请求旁路头** | RTK/caveman/ponytail 已有；旁路头 **已实现（F2）** |
| 用量计费 | cached token 跟踪与成本口径、quota 可见性设置、Claude 5h 窗口自动 ping | 缓存 token/分段计价已有；自动 ping 未做 |
| 稳定性 | 流式 usage 双计修复、非 JSON SSE/重复 [DONE] 防护、Codex 流式超时硬化、开发者指令保留 | 同源问题已各自修复（usage 归一化、keepalive 注释、terminal error） |
| 安全 | **备份导出二次鉴权**、SSRF 防护、真实客户端 IP 限速、远程默认密码守卫 | 导出二次鉴权 **已实现（F4）**；真实 IP **已实现（F1）**；默认密码守卫候选 |
| Anthropic 兼容 | **count_tokens 端点**、内容块严格合规、thinking 签名清洗 | 转换器主体已有；count_tokens **已实现（F3）** |
| i18n | 泰语/波斯语/俄语等 README 与 UI | 未做（UI 中文，候选） |
| MITM/代理生命周期 | 根 CA、代理面板 | AIGate 走配置代理，未做（定位不同，暂缓） |

## 本轮已实施（commits 5cd20f5）
F1 真实客户端 IP（XFF，默认关防伪造）· F2 token saver 单请求旁路头 · F3 /v1/messages/count_tokens · F4 备份导出/恢复二次鉴权（前端 prompt）+ 6 项测试（75 全绿）

## 候选后续（按价值/成本排序）
1. **Fusion 策略**（设计文档 docs/superpowers/specs/2026-09-11-fusion-strategy-design.md，待批准）
2. CLI/客户端接入配置生成器（选做：AIGate 部署形态含远程服务器，写文件不适用，做成"生成配置片段"页更合理）
3. capacity 能力路由（多模态请求自动选有能力的候选）
4. 参数按模型裁剪（如 claude-opus temperature 400 类）
5. Claude 5h 窗口自动 ping（oauth 额度保温）
6. 远程访问默认密码守卫（检测到公网监听且密码为默认值时拒绝启动/强提示）
