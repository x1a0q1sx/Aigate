# AIGate 归档会话上下文提炼（2026-08-22 ~ 2026-10-01）

> 来源：主会话 sess_f7503d1e（标题「Aigate」，7285 条消息，2026-08-23 01:02 ~ 10-01 19:16，
> 含 20 次自动压缩总结）+ 两个 08-22 迁移期只读验证会话。
> 本文件由 8 个并行分析员分片通读后合并而成，作为新工作区会话的项目上下文基线。
> 更细的单点结论见 `docs/findings-*.md`、`docs/progress.md` 与项目记忆索引（MEMORY.md）。

## 一、项目定位与当前状态快照（截至 2026-10-01）

智能 LLM 聚合网关：统一 OpenAI 兼容入口，FastAPI + SQLAlchemy async + Vue + SQLite（PG16 迁移通道已备好未切换），生产 PM2 部署于 182.254.147.87:8000。测试从 45 项一路增长到 **906 项全绿**；三方一致（本地/GitHub/服务器）是每次交付的收尾标准。UI 大重构刚收官（「所有待办清零，无遗留事项」，d8553aa）。

核心已上线子系统：
- **路由**：能力感知路由（模态硬闸 + 长上下文偏好）、weighted 权重、sticky 会话、Race 竞速（15s 无首包并行下一候选）、DroolGuard 流口水冷却（连续 5 次同文罚 30 分钟）、上下文超限预检、combo 前缀端点（具体模型直达、空/`*`/combo 走整链）
- **服务商接入**：OAuth 全套（codebuddy_cn/intl、cline、u1s1、qoder、Jet-Hub 系 LobsterAI/TRAE/CodeArts、OpenCode sidecar、Freebuff 已随封号终止）、隐藏页 `/providers/oauth` 直链（无 UI 入口）、多账号寻址（`__default` 回退 + 显式点名）、密钥单独启停、DPoP/方言/quirks 层
- **签到与成长**：每日 10:30 签到监控 + 启动补签（CodeBuddy CN、LobsterAI、Qoder 已实通）；CodeBuddy 成长中心（Buddy 旅行 + 白名单任务 + 盲盒/抽奖自动执行，默认关）；Qoder 桌面身份头（ClientType 10 + Cosy-Machine 成对）才真正签到成功
- **可观测**：双行日志根治、pending 原位更新、routed_provider_id 覆盖率 98.9%、TPS/缓存命中率真实聚合、模型刷新日志 + 定时刷新、分析页 7 区编排（周/月/日 + 实时日志）、仪表盘运行+告警+权益
- **数据治理**：归档瘦身（98MB→9.6MB）、SQLite 写入队列、日志脱敏、每日备份、usage 三方言归一（Anthropic 缓存并入 prompt）、倍率 price_ratio 与 USD 分离（Qoder 动态 price_factor / CodeBuddy /v3/config 在线源，只叠加不替换）

## 二、演进时间线（八阶段）

1. **08-22~23 项目接管迁移**：从旧 CodeBuddy 会话恢复任务，WB-bridge 只读验证后迁至新服务器 182.254.147.87。教训：新服务器有未提交但已在生产跑的 WIP，pull/reset 即丢。
2. **08-23~09-05 基础体验修复潮**：思考强度（effort 透传 + `-high` 后缀统一解析）、Claude/Codex 客户端适配、上下文守护、token 统计修复、归档瘦身、智力评分 v2（LMArena 382 模型 + OpenRouter 桥接，命中 1132）、时区 8 小时错位修复。
3. **09-05~09-11 路线清账**：docs/todo.md 12 项路线全清（credential_resolver、SQLite 写入队列、usage 三方言归一等）；PG16 解锁 P2-11 + 迁移脚本（15693 行日志 0 坏行）；22 项体验提案全上线（Gemini /v1beta、响应缓存、监控页、多网关密钥+预算、通知、备份、诊断等）；Fusion 策略（judge=候选中智力最高）；tokenrouter/kiraai 报错定性为上游问题。
4. **09-11~09-20 9router 对标 + OAuth 批次**：codebuddy/cline/u1s1/qoder OAuth 全套 + provider_quirks 方言层；密钥启停；admin_sessions 持久化（重启不再全员掉线）；combo 前缀路由（后修正为「具体模型直达」语义）；Cline 免费模型 445 实时列表；09-18 bughunt 审计（P0×5、P1×23）。
5. **09-20~09-22 u1s1 攻坚**：DroolGuard + Race 四路径接入；u1s1 完整 DPoP（RFC 9449，设备私钥加密落库）；**u1s1 403 真因 = 请求体工具名黑名单**（`apply_patch`/`update_plan` 精确匹配即拦，出站改写 + 响应还原解决）；服务商详情浮窗；OAuth 账号改名/多账号不覆盖。
6. **09-22~09-23 OpenCode sidecar + v4.2/v4.3**：服务器装官方 CLI + `aigate` agent（`permission:"deny"`）+ 守护拉起；超时设置页可配；逐字符流修复（585 片→43 片）；额度按到期排序；服务商级定时刷新 + OAuth 多账号寻址；能力感知路由（模态硬闸，未知放行）；倍率数据源挖清。
7. **09-23~09-26 规模化与签到**：倍率 price_ratio 上线；审计 13 P1+5 P2 全清；模型刷新并发化（16min→60s）；routed_provider_id 三层修复（4%→98.9%）；一键签到监控上线（Jet-Hub README 宣传与源码不符的教训）；LobsterAI 全量接入；错误信息不截断（error + error_detail 双字段）。
8. **09-26~10-01 渠道深化 + UI 收官**：CI greenlet（requirements 声明缺失教训）；签到页 QuotaPanel 三页复用；Freebuff 渠道接入后账号被封（终止）；服务商页 500 四层防御；CodeBuddy token 串号修复（JWT 身份守卫 + 159 凭据逐字节恢复）；F29 缓存命中率 49.7%→98.8%（零值诱饵键欺骗方言判定）+ 历史 6317 行修复；F31 CodeBuddy /v3/config 在线倍率源（须 CLI UA）；F33/F34 成长中心 + 盲盒抽奖（线上开出「暗影喵·SR」）；F35 Qoder 签到从接入起从未成功 → 桌面身份头修复后实领；F36 Qoder 10605 排队重试协议；UI 大重构（6 组导航、Monitor 并入分析页、仪表盘重写）全部收尾。

## 三、跨阶段关键根因与教训（按主题聚类）

**上游行为类**
- u1s1：DPoP + UA(`u1s1-cli`) 硬门；工具名黑名单拦 `tools[].function.name`；竞品词仅 system 位置拦；attestation 非必需；DPoP proof 一次性（jti 重放 401）。
- CodeBuddy 全家族无模型列表端点 → 可用性探测法（流式 + 首条 system + max_tokens≥64）；后证实 CN `/v3/config` 在线目录存在；返回集由 UA 决定（CLI UA 才全）。
- Qoder：campaigns 要桌面身份（ClientType '10'，CLI '5' 恒空）；503/10605 是排队信号，重试须重签名并尊重 retryAfterSeconds 倒计时。
- OpenCode 免费层判据 = 官方 CLI 会话前缀（`x-opencode-session` 前 8 位），纯头部方案已失效 → sidecar 方案。
- Freebuff 三条静默降级约束（Buffy 前缀 / mcp__ 工具名 / codebuff_metadata）；limited 档会静默换 session 模型 → 409；POST session 即扣 Freebucks（探测先查单价）。

**统计与数据正确性类**
- 方言判定按「主计数字段」判，不按「键存在」（Anthropic 从不返回 prompt_tokens；上游会塞恒 0 诱饵键）。
- 统计列加过滤的三条件（F21）：写入全覆盖 + 历史回填 + 聚合兜底，缺一复发。
- 聚合查询 `IS NOT NULL` 会静默丢弃新加列的当天数据（routed_provider_id 教训）。
- 日期筛选时区：DB naive UTC、用户说北京时间；「数据不对」先查时区错位。
- 假密钥带连字符绕 CI 密钥扫描（`sk-TESTFAKE-KEY…`），不给 CI 开口子。

**基础设施与运维类**
- SQLAlchemy 2.1 起不捆绑 greenlet；本地传递依赖掩盖 requirements 缺失 → 验证必须用一次性 venv 模拟 CI。
- SQLite 外键默认关 → 孤儿行积累；PG 强制 FK 拒收。
- Pydantic v2 不再 int→str 强转；bool 是 int 子类须先判；TypeDecorator 不覆盖「赋值后立刻读」需 @validates。
- 收尾日志连环案：裸 `except: pass` 吞异常 + finally 读过期 ORM 属性（MissingGreenlet）→ ORM 标量先快照。
- pm2 崩溃循环真凶：`timeout` 包 pm2 restart 掐死 stop 阶段；孤儿进程按 PID 核对（持端口 pid 必须 == pm2 pid）；不 `pm2 resurrect`（dump 有陈旧条目）。
- systemd + pm2 双重守护导致 unit 每 5s 重启、48934 次启动清扫翻 pending → 已 stop+disable + 清扫不变式（只动早于进程启动 5s 的行）。
- 服务器 git pull 可能被 timeout 杀在半路、输出像成功 → 部署验证必须核对 git log + 文件内容 + 测试数，不信任命令输出文本。

**工程方法类**
- 线上实测多次优于单测（httpx 无 `.ok` 被替身掩盖）；「AI 曾虚报完成」发生过两次（审计修复范围、页面加载优化），交付声明必须以代码与生产证据为准。
- 批量重构脚本锚点选错会大面积改坏文件（v1_router.py 770 行缩进错位）→ 小步提交、git checkout 可救。
- 部署失误：`git checkout -- <未跟踪文件>` 会覆盖 pull 下来的新文件（应 `rm -f`）；`git add -A` 会把含服务器路径的未跟踪文件带入暂存。
- 三路推送全断时 `git bundle` 走 SSH 是有效兜底。

## 四、长期硬约束与用户偏好（去重合并）

**产品与数据原则**
- 模型单价必须取各家官方定价页，宁缺勿编；公益站自定义价优先于社区标准价；manual 字段永不刷新覆盖。
- 智力/智能评分不得要求 API key（项目开源）；数据源须公开无鉴权。
- 模型质量优先于额度大小（明确拒绝过 Groq/智谱等白嫖渠道清单）。
- 免费/倍率必须可分辨（price_ratio）；数据来源要可追溯（capability_source、list_source 角标）。
- 模型可用性「只标注不过滤」（Jet-Hub 快照误删 8 个别名模型的教训）；在线数据只叠加不替换。
- 错误信息必须完整不截断（error 短码 + error_detail 全量双字段）；报错要可执行（列可用清单）。
- 不编造数据；如实纠错优先于维护颜面。
- i18n 明确不做；前端不引图表库、不做可配置仪表盘、不拆 Providers 大页；卡片紧凑、数字大字号。
- 请求体默认不落库（隐私）；临时测试数据用完即删并核对恢复原状。

**自动化伦理**
- 只自动做「客户端外能完成」的白名单任务；消耗性操作（盲盒/抽奖）默认关；按天幂等只管自动触发，手动点击 = 显式授权；只读优先。

**运维与协作**
- 生产密钥/凭据只在服务器 `config.yaml`，绝不入仓库；提交钩子拦截服务器绝对部署路径（字面量见 AGENTS.md，本文档不写），不绕过。
- 网络：本地 push 回退 `socks5h://127.0.0.1:10889`；服务器 pull 走 `http://127.0.0.1:7890`；SSH key 正斜杠 `/c/Users/13553/.ssh/id_a.pem`；本地到生产 8000 不通，SSH 隧道 18600。
- 服务器长输出会渲染层吃掉：脚本写文件 → base64 → scp 回本地解码；远程执行用本地写脚本 `ssh "bash -s" < file`；服务器无 sqlite3 CLI，用 `PYTHONPATH=. venv/bin/python`。
- 永不把 pm2 restart 包进 timeout；部署后验证端口 PID == pm2 PID。
- 工作流：任务落 docs/todo.md 挨个做、做完统一汇报；用户以「继续/开搞」快速授权，关键取舍点常不回复，期望 AI 按最合理语义自主推进并留痕；brainstorming→spec→writing-plans 流程已跑通并被接受。

## 五、未完成 / 遗留事项清单

**被用户动作阻塞（勿主动推进）**
- LobsterAI / TRAE / CodeArts 真实登录验证（手动粘贴回调 URL 模式）；TRAE CN 加密未实现（SOLO 明文通道是否还通是最大未知）。
- provider 75「u1s1-公益-付费站」禁用/改名（历史零成功，多次提议未获确认）。
- growth.gacha / growth.lottery 默认关，待用户显式打开。

**技术长尾**
- 服务器有两处未提交 hotfix（media_router / passthrough_router），部署时须原样保留并择机入库。
- 09-18 bughunt 的 P2/P3 ≈ 40 条低优先级长尾（P0/P1 已清）。
- `max_output_tokens` 是否已参与预检 reserve 未见明确收尾记录。
- Qoder 09-25~27 的「已领取」日志不可信且无法追溯；约 300 个公益站自定义名模型元数据匹配不到（窗口默认 4096，8 月末记录）。
- PG 生产切换四步流程已备好未执行（仍跑 SQLite）。
- Freebuff 渠道已随账号封禁终止，勿再投入。
- Qoder 模型定时刷新已开（12h）；全局定时刷新默认关的状态未被推翻。

## 六、延伸阅读

- `docs/progress.md` — 进度日志（逐任务结论）
- `docs/findings-bughunt-2026-09-18.md` — 审计清单与处置
- `docs/findings-opencode-free-tier.md`、`docs/findings-workdaddy-features.md`、`docs/findings.md` — 专项排查
- `docs/free-channels-survey-2026-09.md`、`docs/roadmap-9router-comparison.md` — 调研
- 项目记忆索引（MEMORY.md）— F29~F36 与各渠道接入的单点硬结论（含生产值位置：服务器 config.yaml）
