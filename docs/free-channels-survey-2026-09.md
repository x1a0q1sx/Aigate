# 免费 LLM 渠道调研（2026-09-25）

> 目的：盘点「还能白嫖」的渠道，与 AIGate 现有 74 家服务商去重，产出接入优先级。
> 方法：官方文档直取 + 5 个每日刷新的社区仓库（mnfst / ClawLabsAI / freellm.net 等）
> + HN 全站检索交叉验证。**置信度逐条标注**，未证实的一律写明。

## 〇、2026 年的大背景：免费层正在集体消失

半年内消失的（网上大量清单仍列着，均已过期）：

| 时间 | 事件 |
|---|---|
| 2026-04-15 | **Qwen Code CLI 免费层终止**（官方文档原文 discontinued） |
| 2026-04-17 | **iFlow CLI 停服**（迁移至 Qoder） |
| 2026-05-15 | **Roo Code 扩展关闭** |
| 2026-07-30 | **GitHub Models 完全退役** |
| 2026-08-17 | **Cerebras 免费层终止**（转绑卡送 $5） |
| 2026-08~09 | **SambaNova 半关闭**（多数模型 402）、**Together AI 免费层消亡** |

**架构启示**：任何依赖免费额度的系统都必须实现多厂商 failover（AIGate 的 combo/auto
级联天然符合），并把「免费层消失」当作必然事件设计。

## 一、AIGate 现有资产盘点（74 家中的「白嫖大户」，均已注册但大量闲置）

| provider | id | 已有模型 | 启用数 | 说明 |
|---|---|---|---|---|
| openrouter官方 | 78 | **458**（含 21 个 `:free` 免费模型） | **1**（仅 z-ai/glm-5.2:free） | 免费池几乎全闲置；glm-5.2:free / qwen3.8-27b:free 质量最高 |
| NVIDIA NIM | 27 | **82** | **0** | 40 RPM / 10k RPD/模型，100+ 模型（含 132 免费） |
| huggingface官方 | 57 | 129 | 0 | 免费额度极小（$0.10/月），价值低 |
| agentrouter | 30 | 11（Claude 系） | 0 | 签到 $25/天 |
| anyrouter | 20 | 11（Claude 系） | 0 | 签到额度；社区称常不可用 |
| goolai | 23 | 25（Claude 系） | 0 | — |
| stepfun官方 | 39 | 2（step-3.5-flash） | 0 | 官方 |
| 魔塔AI (ModelScope) | 55 | 1 | 0 | **2000 次/天免费**，额度最大的持续性国内渠道 |
| AMD | 69 | 7 | 1 | 开发者云 |
| MiMo Code Free | 21 | 2 | 1 | 小米免费层 |

**结论：Top 榜里半数渠道 AIGate 早已接入，只是模型闲置**——零成本可启用。

## 二、值得新接入的（与现有 74 家去重后）

### 国际（均无需信用卡）

| 渠道 | 免费额度 | 备注 | 置信度 |
|---|---|---|---|
| **Groq** ⭐ | 30 RPM / 1k RPD / 200k TPD | 唯一四项全满分（额度大/稳/免卡/OpenAI 兼容）；llama-3.3-70b 已于 08-16 下线，用 gpt-oss-120b | 高（官方） |
| **Google AI Studio** | Flash 系 15 RPM / 1500 RPD | 额度最大；prompt 会被用于训练（EEA/英/瑞除外） | 高 |
| **Cloudflare Workers AI** | 10k Neurons/天 | 有正式文档背书；6 个热门模型已移出免费池 | 高 |

### 国内

| 渠道 | 免费额度 | 备注 | 置信度 |
|---|---|---|---|
| **智谱 GLM** ⭐ | 8 款模型官方免费（GLM-4.7-Flash 200K 上下文/128K 输出） | 无 token 计数、无到期日；限速数字未公开 | 高（官方模型总览页） |
| **阿里云百炼** | 每模型约 100 万 token，各模型独立 | 90 天有效；领取时无需实名 | 高（官方） |
| **腾讯混元** | 100 万 tokens / 1 年 | 一次性；稳定性最高 | 高（官方计费文档） |
| ModelScope 魔搭 | 2000 次/天 | **AIGate 已注册（id=55）**，只需启用 | 高 |

### Jet-Hub 已验证但 AIGate 未接入的 4 家

| 渠道 | 凭据方式 | 服务器可接 | 工作量 | 免费额度 | 结论 |
|---|---|---|---|---|---|
| **lobsterai**（有道） | 本地回调 + authCode，无 PKCE/签名 | ✅ | **小（1天）** | 签到 +100 积分/天 | ⭐ 优先 |
| **codearts**（华为） | 浏览器 OAuth（PKCE+DPoP），回调锁 127.0.0.1 | ✅ | 中（2-3天） | **deepseek-v4 每日 1000 万 tokens** | ⭐ 额度最优 |
| trae / trae-intl | 本地回调（18080）+ ExchangeToken | ⚠️ 脆弱 | 大（1周+） | 签到 150 credits/天 | 暂缓（CN 请求体已加密，未破解） |
| antigravity | 读本机 IDE 文件 / 借本机进程 | ❌ | 不可行 | — | 三重否决，不做 |

codearts 两个坑：refresh_token **一次性轮换**（多 worker 并发是雷区，需分布式锁）；
回调主机名被 portal 锁死 127.0.0.1（远程部署需「粘贴回调 URL」降级路径）。

## 三、明确不碰的（已死或风险高）

- **已取消**：Cerebras、SambaNova、Together AI、GitHub Models、Replicate、Perplexity
- **高风险**：gpt4free 类逆向项目（法律+封号风险，仅实验）、Pollinations（只剩 1 个模型）、
  Kilo Code 网关（按 IP 限流 + prompt 被记录，适合 Agent 不适合进网关）
- **存疑待实测**：Mistral（官方称 $10/月 credits，中文源称 2026-09-01 已取消——冲突未解决）

## 四、行动优先级

1. **零成本**（只需在模型管理页勾选）：启用 OpenRouter 21 个免费模型中的强模型、
   ModelScope、NVIDIA NIM 中的优质模型
2. **小成本**（注册拿 key）：Groq、智谱 GLM、Google AI Studio、阿里百炼
3. **中成本**（写适配器）：lobsterai 渠道接入、codearts 渠道接入（1000 万 tokens/天）
4. **不碰**：trae（等加密破解）、antigravity、已死名单

## 五、诚实标注的不确定项

- 火山方舟 50 万 tokens：官方文档页多次抓取返回空，**未验证**
- 硅基流动「2000 万 tokens」：来源多为带邀请码的博客，**可能过期**
- CodeBuddy 免费额度：三个来源三个说法，官方未公布
- 各厂商对中国 IP 的支持：本次调研的最大空白，多数无公开国别限制文档
