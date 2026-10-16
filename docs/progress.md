# AiGate v3 开发日志
## Session 1 - 2025-07-17
### 完成
- ✅ 项目搬家至D盘：292个文件安全迁移
- ✅ 需求文档 v3 输出（requirements-v3.md）
- ✅ 架构设计文档 v3 输出（architecture-v3.md）
- ✅ Playground报错修复（api.js添加playground别名）
- ✅ 长程开发记忆文件创建（task_plan.md / findings.md / progress.md）
- ✅ 免费模型自动参选机制确认（已有代码已完整实现）
- ✅ 收费模型Auto选举UI开关确认（Models.vue已有完整实现）
- ✅ 定时评测增强（health_checker.py + write_log=True）
- ✅ 全模块验证通过（24+模块全部导入成功）
### 待启动
- 用户自行运行 `python start.py` 启动服务
- 配置自己的API Key后使用
## Session 2 - 2026-10-01
### 完成
- ✅ 归档主会话 sess_f7503d1e（2026-08-23~10-01，7285 条消息）导出并分 8 片派子代理通读提炼
- ✅ 合并产出项目上下文基线 `docs/session-archive-digest.md`（时间线/功能版图/跨阶段教训/硬约束/遗留清单）
- ✅ AGENTS.md docs 清单加入该文档指针
### 说明
- 本文件此前停在 2025-07-17，近期任务结论实际记录于项目记忆（MEMORY.md）与 git 历史；自本条起恢复收尾记录
- 两份 08-22「接管」小会话仅为迁移期只读验证，无独立结论

## Session 3 - 2026-10-03
### 完成
- ✅ 组合路由页 10 秒加载定位并修复（011aebf）：根因 = /admin/api/models 全量目录 2.4MB 裸传（服务端仅 476ms，公网实测 6.7s）
- ✅ AdminGZipMiddleware：只压 /admin 缓冲 JSON（content-length 门控天然排除流式，/v1 推理路径实测零接触）
- ✅ 新增 GET /admin/api/models/light（9 字段轻量端点），Combos 页切换
- ✅ 验收（本机走隧道）：models+gzip 6.7s/2.4MB→0.70s/82KB；models/light+gzip 0.50s/42.6KB；/v1 无 content-encoding
- ✅ 测试 906→914 全绿；服务器 PID==pm2 PID 已核对；探针临时会话已删除

## Session 4 - 2026-10-04
### 完成（漫剧白嫖渠道：调研 + 探针打通，跨会话任务，源自 TypeTale 会话）
- ✅ 调研国内视频生成 API 免费额度（官方定价页验证 + 社区交叉，逐条置信度）：白嫖 Top3 = 阿里云百炼（HappyHorse 各模型 10 秒免费可叠加）＞腾讯混元（50 积分 ≈ 10 条图生 720P，失败不扣分）＞智谱 CogVideoX-Flash（官方标注「免费」）；火山 Seedance 效果顶级但无免费额度（付费量产档）
- ✅ 渠道注册表 `scripts/manga_channels.yaml`：9 渠道（bigmodel/siliconflow/minimax/bailian/hunyuan/vidu/kling/volc_ark/qiniu），含 api_type 与 video_adapter 协议对应关系（siliconflow/minimax 已支持，bigmodel 探针内置，dashscope/tc3/vidu/kling/ark 需后续适配）
- ✅ 探针 `scripts/manga_channel_probe.py`：L1 连通（无 key 安全）/L2 鉴权启发式+硅基余额/L3 免费模型真实生成冒烟（cogvideox-flash 提交+轮询）；key 走 --key 或 AIGATE_MANGA_KEY_*，报告落盘 data/manga_channel_probe_report.{md,json}
- ✅ L1 实测 9/9 全部可达（vidu 405 证明 /ent/v2/img2video 端点真实存在）；离线单测 16 项全绿（tests/test_manga_channel_probe.py）
- ✅ 调研文档 `docs/manga-free-channels-survey-2026-10.md`（含 GPU 自托管渠道附录：AI Studio 每日 8 点/OpenI 积分制均在）
### 待办
- 用户注册拿 key 后跑 L2/L3：`--key bigmodel=xxx --gen`（免费）优先，硅基流动先小额验证赠金抵不抵视频
- 漫剧渠道 key 就绪后在 admin 服务商页建 provider，bigmodel/siliconflow/minimax 可直接走 /api/media/video
- 变更未提交（scripts/manga_channels.yaml、scripts/manga_channel_probe.py、tests/test_manga_channel_probe.py、docs/manga-free-channels-survey-2026-10.md）
### 补充（同日）：跑马灯对比
- ✅ `scripts/manga_bakeoff.py`：同提示词多渠道出片 → 下载成片 → `data/manga_bakeoff/对比.html` 视频并排页供人工选优。渠道适配器 7 家（bigmodel/bailian/siliconflow/minimax/kling/vidu/volc_ark），hunyuan(TC3)/qiniu 未适配进人工栏；计费护栏 = 默认只跑免费档（bigmodel cogvideox-flash + bailian happyhorse），付费须 --include/--all；支持 --image 图生视频（本地转 base64）与 <渠道名>_model 模型覆盖
- ✅ `data/manga_keys.json` key 模板（--init 生成，data/ 已 gitignore）；单测 +10（tests/test_manga_bakeoff.py），其中 test_all_includes_paid_but_never_manual 抓到并修复 --all 泄漏 manual 档的 bug
- ✅ 全套件收集 940 项正常（914+26）

## Session 4 - 2026-10-08
### 完成
- ✅ OpenCode Free/exo-free 持续 502 排查定性：上游判据再次收紧——连官方 CLI（opencode serve prompt API）新会话也被 403 FreeTierError（"can only be used from within OpenCode"），sidecar 架构失效，属上游政策非网关 bug（findings 文档第六节）
- ✅ 生产处置：`opencode_bridge.enabled=false` + `auto_start=false`（PUT /admin/api/opencode 热生效持久）；闲置 serve 进程已清（~460MB）；combo 撞该候选即时降级实测 0.1s（此前 180s 超时）
- ✅ 排查期间服务无恙：PM2 restart 后 PID==pm2 PID 已核对、/admin 200
- ✅ 记忆更新：aigate-opencode-free-tier 判为当前失效；pkill -f 误杀 ssh 会话教训入档
### 待用户决策
- 是否把 OpenCode Free 从 combo 918 候选里摘除（现在只是秒失败降级，不拖慢响应；保留=上游若放宽自动复活）

## Session 5 - 2026-10-09
### 完成
- ✅ tokenharbor「乱码模型名」定性：乱码=87 个纯数字/字母数字串（124、2765、l645…），来源是官网 /pricing 营销页里 **SVG 折线图的坐标数字**。链路=该站 /v1/models 按国别封锁（服务器 CN 出口 403 region_blocked）→ 刷新在线列表失败 → 定价文本兜底（xyusec_pricing._extract_pricing_from_text）正则把 SVG 坐标当「模型名+两价格」抓进库。连带定性：**该服务商当前所有推理请求从服务器出去都会 403**（封锁在鉴权前），现不可用；provider 99 于当日 07:41 添加、07:42 手动刷新入库
- ✅ OpenCode Free 桥接重开 + 上游复测（findings 第七节）：10-08 的「一刀切收紧」已演变为**按模型分策略**——space-bunny-free 实测可用；mimo/nemotron/longcat 等仍 403 FreeTierError、muse-spark RegionError、**exo-free 已下架（410）**。`opencode_bridge.enabled=true`+`auto_start=true` 已生效，sidecar 存活；combo 失败候选秒级降级
- ✅ 运维坑入档：`POST /admin/api/models/refresh` 的 provider_id 是查询参数非 JSON body——误用会触发全量刷新（本次核对刷新日志：无破坏性偏移，删除均为上游真实下架）
### 待用户决策
- OpenCode Free 已下架模型（exo-free 等）是否从目录/combo 918 清理（free_tier 分支刷新不删模型，会一直挂着 410 失败）

### 补充（同日）：tokenharbor 处置完成（10310e7/9114124/e68c408/fa794b6/2231ade）
- ✅ 模型获取：经服务器 mihomo(7890) 出口拉 `/v1/models` 成功，**87 行 SVG 坐标垃圾全删、68 个真实模型入库**（display_name/单价采纳上游自声明 label 与 pricing.input_usd_per_1m）；后续刷新在线列表成功即自动维护
- ✅ 排查中揪出「服务商走代理开关」只在 combo/旧调用点生效的**四处旁路漏洞**并全收口：刷新 list_models（10310e7）、playground api_key 分支（e68c408）、统一凭证解析器标准分支（fa794b6）、**/v1 直连路由**（2231ade）——此前 CN 出口站点对这些路径永远直发；全套 946 项绿（含新增 6 项回归）
- ✅ 定价兜底三防：文本兜底剥 `<svg>` 块 + 拒纯数字/「单字母+数字」键；库里已有模型时列表失败**禁止猜名重建**；兜底建模 list_source 记 `pricing`（10310e7）
- ⚠️ 服务商「代理」语义澄清：provider 的开关=**强制走全局代理池**（设置→代理池要有可用出口）；`proxy_url` 列是历史死字段（无消费点）。mihomo 抖动时表现为 pool 冷却→直连→403，症状与「没开代理」相同，看错误码区分（403=国别封锁/直发，402/401=已过代理）
- ℹ️ tokenharbor 现状：`:free` 免费模型经 /v1 直连实测 200；付费模型报 402 **账户余额 $0**（需去其 dashboard 充值；期间一度报 401 Invalid or revoked key，当前 key 可用）

### 补充 2（同日）：流式断流定性 + sidecar 守护修复（bde00a4）
- ✅ 用户报 `RemoteProtocolError: peer closed connection ... (incomplete chunked read)` 定性=tokenharbor 直连流式**偶发断流**（09:02 仅一条；随后 6/6 流式探测全过、ttft 1.6~2.5s；与 9-18 烁公益站/9-03 基元律动同类）。网关已内置「首字前断流自动重试一次」；首字后断流原样报给客户端（重试会造成重复输出，不可透明）——非网关缺陷
- ✅ 顺带揪出并修复真 bug：`main.py` OpenCode 守护 `create_subprocess_exec` **漏 await** → 协程永不执行、**sidecar 自动拉起从未生效**（14 条 never-awaited 警告；此前存活全靠设置页「重启 sidecar」按钮）。修复+记 pid+静态回归测试锁死；部署后守护第一个 tick 即拉起，4096 监听、`space-bunny-free` 真实会话 finish=stop。09:02:01 combo 里「sidecar 未运行」即源于此
- ℹ️ 通用教训：循环里裸调协程工厂函数（create_subprocess_exec 等）完全静默，自检看 stderr 的 `RuntimeWarning: coroutine ... never awaited`

### 补充 3（同日）：用户二次断流复现 → 瞬态分类漏判修复（56c7593）
- ✅ 复现关键证据：直连上游对照组里 `mimo-v2.6-flash:free` 挂 **30s 后零字节掐断**（chunks=0 时 RemoteProtocolError）——上游拿到 200 后静默关闭，属上游/线路偶发（haiku 长文 8/8 全过、gateway 路径 12/12 过）
- ✅ 真缺陷在**分类表**：`peer closed connection ... incomplete chunked read` / `ServerDisconnected` 不在 `_TRANSIENT_UPSTREAM_RE` → 「首字前自动重试一次」的保护漏判成不可重试、直接透传给客户端。补进正则后，正文未出的掐流会自动原样重试一次（首字后仍如实报错，避免重复输出）；测试锁死 + 部署复验 6/6
- ⚠️ 部署提醒：本地→GitHub 通道今天明显不稳（直连常 443 拒、SOCKS 偶发 TLS 断），push 必须**循环重试直到看到 `main -> main`**，不能凭一次失败就认为已同步

## Session 6 - 2026-10-10
### 完成（用户报「09:26:31 tokenharbor/claude-haiku-5.5:free」日志 → 空消息异常 + 重试预算优化，86baa01）
- ✅ 定性：该请求（request_logs.id=37477，UTC 01:26:31=北京 09:26:31）两次尝试都经代理 7890 → mihomo `🇯🇵|日本-中转 01` 节点，TCP 连上后 TLS 握手阶段被掐（376ms/1658ms 快速失败，错误类型 ConnectError）。mihomo journal 实证两次拨号同节点；同日 10:12 该节点还有一次拨号超时——**代理线路抖动，非网关缺陷、非模型问题**
- ✅ 真缺陷 1「错误文案不可读」：`error_msg` 落库为 `ConnectError: ` **空消息**——httpcore `map_exceptions` 把 str 为空的底层异常（anyio.BrokenResourceError/EndOfStream）直接包进 ConnectError（本地+服务器双向复现签名）；历史库同类空消息形态（`ReadError: ` / `WriteTimeout: `）各 19 条。新增 `server/core/error_text.py::err_text()`：沿异常链兜底出人话描述 + 传输层异常追加「（本次经代理 <url>）」
- ✅ 真缺陷 2「重试撞同一线路」：原「首字前重试一次」两次都撞同一抖动线路。新增 `_retry_budget_for()`：快速连接类故障（ConnectError/ProxyError）预算 2 次（重建 httpx 客户端与 TCP 连接，瞬断多为偶发）；流式循环 range(2)→range(3)、非流式 `_same_key_retried` 布尔→`_same_key_retries` 计数。ConnectTimeout 不给额外预算（黑洞型故障每次等满超时，代价太大）
- ✅ 全链路接入 err_text：v1_router（流式/非流式/auto 级联/combo 失败点）、admin_router（playground 流式+非流式）、passthrough_router（embeddings/images/audio.speech）、model_catalog（刷新 list_models 失败日志）；前端 Dashboard 失败行改显示 error_msg 首行摘要（此前只显示 error_type 如 "upstream_error"，用户看不懂）
- ✅ 测试 947→956（9 项新增：兜底/预算/瞬态耦合/源码守卫）；部署三验：非流式 200 'ok'（2159ms via 7890）、流式 5 chunks 全过（1939ms via 7891 round-robin 生效）、/admin 引用新 bundle（errBrief）
- ⚠️ 服务器存量教训：服务器 passthrough_router.py 有未提交的 `/audio/speech` TTS 路由（Moss 媒体工作遗留）→ stash→pull→pop 干净合并，两改动共存已核验
### 待用户决策（沿用）
- OpenCode Free 已下架模型（exo-free 等）是否从目录/combo 918 清理

## Session 7 - 2026-10-11
### 完成（用户报「OpenCode 调用了一下还是失败」→ 双层根因定性 + 全线打通，966876f/fdadac9/4fe4cdd）
- ✅ 定性第一道门「出口 IP 国别」：最新失败行 id=39834 muse-spark-1.3-contributor-free 报 `403 RegionError: This model is not available in your country`。服务器直连出口=腾讯云上海(CN)、经 mihomo=东京(JP)；CLI 挂代理后 muse/ling/mimo/nemotron/step-5 **全部打通**。实现 `opencode_bridge.proxy_url`（守护与重启接口都注入大小写双份代理 env；设置页输入框；GET /opencode effective 暴露）；生产已设 7890
- ✅ 定性第二道门「agent 工具清单」（**推翻 10-09「按模型分策略」部分结论**）：逐项二分实证——单字符串 `permission:"deny"`（旧实现）或 deny `bash`/`read` 任一核心工具 → 403 FreeTierError "can only be used from within OpenCode"；deny glob/grep/edit/write 等无害；agent 名/提示词无害。**核心三件套（bash/read/glob）保持 ask + 其余 deny 即放行**。修复 `bridge_agent_config` 为对象形态、`_cli_config_problem` 校验新形态（旧配置启动自动迁移）
- ✅ 修复后生产实测 7/7 全通（muse 1.2/1.3、ling-3.1、mimo-v2.6、nemotron-3-ultra、longcat-2.5、space-bunny 回归），非流式 + 流式双验
- ✅ 顺带修复真 bug：free_tier 非流式成功路径**漏写完成日志**——muse 成功 200 但 request_logs 行永远 pending（流式有 _write_stream_log、错误有 _early_error_log，唯独成功路径没写入者）；补 enqueue_log + 静态回归测试
- ⚠️ mihomo `AUTO-sabusuku` url-test 每 300s 换节点：LA 窗口期代理路径反而全失败（muse/space-bunny 全挂）、东京窗口期全通——代理不是万能药，国别门 + 节点质量双重依赖
- ⚠️ 复证限流：本次上百次试探触发上游 429 FreeUsageLimitError（所有模型一律 429、与配置无关，静置自愈）；生产验证要节制
- ✅ 测试 956→963（proxy 4 + 权限形态 6 - 旧断言替换 + 日志回归 1）；三端同步（本地/GitHub/服务器 = 4fe4cdd），前端 bundle index-D3YYIJb-.js 已含代理输入框
### 教训（长期有效）
- **判「上游封了某模型」前先排除自己的请求形态**：6 个被 10-09 判死的模型实际全部可用，是网关 agent 配置（deny 核心工具）触发的误伤
- 免费层这类「真 CLI 会话」校验是**多层组合**：出口国别 + 工具清单 + 会话前缀（9-22 结论），任何一层不满足都可能以不同错误包装出现

### Session 7 冷却后终验（同日 12:55+）
- ✅ 429 限流冷却后终验通过：space-bunny 非流式 200 `cooled-final`（2596ms），**日志行 id=40166 正常落库**（success / 892+6 tokens / routed_provider_id=26）——修复前此路径的完成日志缺失（永远 pending），现已原位收尾。修复前失败的 muse/ling/mimo/nemotron/longcat 5 模型也在冷却前实测全通
- ℹ️ 限流窗口实测：上百次试探触发 429 后约需 25+ 分钟冷却（8min/20min 两次探测仍 429，~25min 后恢复）

## Session 8 - 2026-10-12
### 完成（用户两项需求：Playground 鹈鹕一键测试 + 分析页布局调整）
- ✅ Playground「🐦 鹈鹕一键测试」按钮（发送键旁）：按固定提示词（SVG 鹈鹕骑自行车 2D 动画）流式请求当前所选模型（auto/具体模型均可）→ 前端 SSE 增量累积 → `extractHtml` 提取（``` 围栏 → `<!DOCTYPE/<html` 截到末 `</html>` → `<svg…</svg>` 兜底；尾随说明文字剥离）→ iframe `sandbox="allow-scripts"` srcdoc 内嵌预览 + Blob 下载，文件名 `服务商-模型-日期时间.html`（非法字符清洗）；另有「新标签打开」、取消（AbortController）、原始输出折叠、流中途报错显示已接收部分
- ✅ 分析页：实时区「最近请求」表删除（与请求日志重复）；请求日志（LogsPanel）从页面底部移至用量趋势上方；「看日志」按钮因日志区在上方改为 `scrollIntoView` 联动；页面副标题同步
- ✅ `api.js` 新增命名导出 `apiStream`（POST → ReadableStream，401 统一处理，支持 AbortSignal）
- ✅ 测试 963→972（9 项前端静态回归：按钮/prompt/流式/提取/sandbox/文件名/最近请求清除/日志区在趋势上方/看日志滚动）；本地 build 通过（bundle index-3ZgLNa-Q.js 含「鹈鹕」）
- ℹ️ SSE 消费细节：跨分片缓冲 + 末事件无尾随空行时冲刷（node 模拟不规则切分实测通过）
### Session 8 补充：鹈鹕测试暴露的 sidecar 停滞误杀（同日，339d75e/1db6b24/6005fb0）
- ✅ 端到端验证鹈鹕提示词（长输出）时揪出真 bug：**CLI message API 在正常生成中会长时间零增量**（实测 70s 仅空 reasoning part，140s 一次性 flush 24k 字符）——旧停滞检测 20s 即判「工具挂起」发 interrupt → CLI 报 `Provider turn interrupted`，**所有长输出（长代码/长文）都会被截死**
- ✅ 修复：新增 `_turn_in_tool_wait()`——只有 `finish=tool-calls` 或末腿有未完成 tool part（真权限挂起）才允许 interrupt；普通慢生成只等待，deadline 兜底。2 项回归测试（含 FakeClient 模拟「前 7 轮零增量、第 8 轮 flush」场景断言 interrupt 计数为 0）
- ✅ 超时默认 180→300s（生产 PUT 设为 300；慢生成实测 140~242s）
- ✅ 生产复验：OpenCode space-bunny 鹈鹕提示词 242s 成功返回 22k 字符（含 `<svg>`）；CodeBuddy deepseek-v4.1-flash 19s 返回 12k 字符（提取 fence → 12k HTML）。300s deadline 撞线场景（模型陷入 50KB 规划循环）返回 reasoning-only——前端现捕获 `reasoning_content`，提示「只输出思考、可能生成太慢撞超时」并可在原始输出查看
- ℹ️ 部署顺序：先发后端（interrupt 修复 + 超时），再发前端（reasoning 捕获）；三端同步 6005fb0，bundle index-BP5guqoD.js

## Session 9 - 2026-10-13
### 完成（CodeBuddy 新模型刷新看不到：step-5-preview 修复，d1a4055）
- ✅ 根因（F37）：CodeBuddy 无 /v2/models → 刷新永远落静态种子（09-23 死数据）→ 官方新模型（step-5-preview，10-12~25 限免）进不来；此前 F31 只接了「在线倍率叠加」，叠加只作用于已有模型，救不了新模型
- ✅ 生产探针（真实账号）：scoped `personal/models` 与 `/v3/config` 两端点均 200 且都含 step-5-preview（x0.00/1M ctx/64k out/vision；chat 实测 200）
- ✅ 目录质量实测：33 条里 11 条不可用（8 死条目 11102 + 2 生图 11103 + glm-5.0-turbo 11150 需 effort）；档位别名（auto/balanced-model 等 8 个）不在目录里但实测全可用 → 只能「目录 ∪ 种子」
- ✅ 实现：`fetch_codebuddy_catalog()` 两端点并行拉取取并集 + 三类过滤；刷新链路目录∪种子（只补不删）+ 倍率 overlay 保留；种子/倍率表按 10-13 探测再校准
- ✅ 线上验证：CN 刷新 `+6（含 step-5-preview）~27 -0 total=33 online`；Intl `+1 ~29 -0 total=30 online`；DB ratio=0.0/free/1M 落库；网关真实调用流式 200；模型列表 API 透出；刷新日志 id=850/851 落库
- ✅ 测试 966→992 全绿（新增 tests/test_codebuddy_catalog.py 17 项）；pm2 已重启（aigate online）
### 备注
- 前端无需改动（模型页自带「在线倍率」标签 path 已支持 codebuddy source）
- 留在服务器 /tmp 的探针脚本已清理；cb_check.py（09-22 旧会话遗留）未动

## Session 10 - 2026-10-13
### 完成（用户四项需求：分析页删实时区 / 隐藏已关闭服务商 / 服务商页排序 / 新增账号强制选号）
- ✅ 分析页实时区整体删除：`RealtimePanel.vue` 删文件、Analytics 引用与副标题清理、`/monitor` 重定向去掉 `#realtime` 锚点、Dashboard「实时详情」链接改「分析页」
- ✅ 模型管理（筛选下拉 + 添加模型下拉）与 Playground 服务商选择只列**启用中**的服务商（`enabled !== false`）——已关闭的从选择器隐藏
- ✅ 服务商页默认排序改「固定+开启优先」：tier = 开启 (2)+ 固定 (1) 降序 → 固定且开启 → 开启 → 固定关闭 → 关闭（`providerSort` 值仍为 `pinned`，旧 localStorage 无需迁移）
- ✅ CodeBuddy 新增账号强制选号（F38）：官方登录页检测到已有 SSO 会话会静默复用账号 → registry 加 `sso_logout_url`，已有连接时 authorize 接口返回 KC 登出链（实测 302 直达登录页），前端新增账号先登出再登录；新连接默认名用邮箱（Intl）/手机号（CN）；同账号重登按身份写回改名后的原连接（防重复连接）
- ✅ 测试 992→1005 全绿：新增 8 项后端（默认名/自动名判定/身份归一/邮箱命名/手动名保护/重登复用/registry 登出配置/登出链构造）+ 6 项前端静态（三处 UI 改动），改写 2 项（RealtimePanel 删除后）
- ✅ 本地 build 通过（bundle index-23QP0_BZ.js）
### 追加（muse-spark RegionError 修复，F39）
- ✅ **根因**（mihomo journal 回溯）：失败行 id=45844（10:45 CST）请求经 `AUTO-sabusuku` url-test 选中**🇭🇰香港节点** → muse-spark 地区门在 HK 出口判 403；JP/SG 出口下可通
- ✅ **修复**：mihomo 增加 `OPENCODE-REGION` 组（过滤 JP/SG 节点）+ `DOMAIN-SUFFIX,opencode.ai` 规则 → 该域名固定到已验证可用的出口池；测速目标改为 `https://opencode.ai/`（真实业务）避免 gstatic 选点偏差
- ✅ **线上验证**：rule match 日志（`DomainSuffix(opencode.ai) using OPENCODE-REGION[🇯🇵|日本 - 中转 01]`）；网关真实路径非流式×2 + 流式×1 **全部成功**（3.0s/2.3s/1.7s）；一般流量 google/github/ipinfo 仍正常（未影响全局代理）
- ✅ **文档**：新增 `docs/findings-muse-region-error.md`（F39）；进度日志追加 Session 10-F39 小节
### 备注
- 登出链只在「已有连接」时返回（首次连接复用浏览器会话是合理路径）
- CN 登出必须打 `www.codebuddy.cn`（SSO cookie 域；copilot.tencent.com/login 301 跳过去）
- space-bunny-free 报 429 `FreeUsageLimitError` → 上游限流，与本 fix 无关
- mihomo 补丁 `/opt/clash/clash_config.yaml`（属主 clashm:clashm，模式 rule 分流）；重载后组 alive=100%（JP/SG 各候选延迟~600~1000ms）
- ⚠️ 修复验证后（14:26 起）出现 429 `FreeUsageLimitError` 持续限流：**两个不同日本节点（日本-中转 01、日本原生-中转 01）同样 429** → 账号级限流（`Bearer public` 免费层），与本次修复无关；同 10-11 先例（高频试探触发、静置自愈，冷却 25min+）。生产使用不受影响（正常调用频次远低于验证探针）
- ⚠️ 本地 push 通道变化：直连与 10889 SOCKS 均 SSL eof，本次经本地 Clash Verge **7897** 端口成功推送（`7de080a..3437809`）；SSH 到服务器不受影响

## Session 11 - 2026-10-13
### 完成（用户六项需求：OpenCode 全模型 429 / CodeBuddy Intl 总断 / 日志显示账号 / 按到期选号 / 耗尽包不统计到期 / 报错账号冷却）
- ✅ **OpenCode 全 free 模型 429 定性**（任务1，F40 §1）：15:16 批量探测实为**三类错误混合**——真限流（429 FreeUsageLimitError，muse/space-bunny）、上游端点不可用（429/400 "Endpoint/Model is unavailable"，step-5/ling）、模型已下架（410 ModelDeprecated，exo/fledge）；mihomo journal 证实当时全走 JP 出口（**非** F39 的地区门复发）。机理=匿名 `Bearer public` 共享池 + 官方 CLI 指纹按「出口 IP+模型」分别限流；16:20 复测 muse/space-bunny/nemotron/longcat **全部恢复**（静置 ~50min 自愈；10-11 先例 ~25min）。**网关侧修复**：free_tier 直连失败（流式+非流式）补模型冷却——`is_free_failure_coolable` 判据（429/端点不可用/下架/5xx/超时→冷却 300s；FreeTierError 配置类→不冷却），auto/combo 不再每轮撞坏模型
- ✅ **CodeBuddy Intl「总断」根因**（任务2）：`codebuddy_intl` 有 2 账号（id=9/10），自动模式固定打 **id 最早**（旧兜底=id 最小）→ 账号 9 被上游 429 频控（code 6004/14003）后全量 429，12 条连败实证（10-12 14:08-14:15）；且无账号级冷却/切换。修复=多账号治理机制（任务4/6）
- ✅ **日志显示所用账号**（任务3）：`request_logs.routed_account` 列 + 全路径写入（直连非流式/流式、auto 级联非流式/流式、combo 非流式/流式、playground、fusion 经 `ResolvedCredential.account`/`RouteResult.account` 链路）；列表+详情 API 返回；前端列表行 `👤 账号` 徽标 + 详情「使用账号」行
- ✅ **按积分最近到期选号**（任务4）：自动模式（`oauth_owner` 空）排序键 `(冷却, 额度档位, __default, id)`——有余额且**到期最近**优先（先用快过期的）；额度数据由新增预热任务 `_usage_prewarm_loop`（300s，仅多账号 provider）填充 `oauth_usage` 进程内缓存，**请求路径只读缓存零网络**；无数据时退化为旧语义（__default/id 最早）
- ✅ **耗尽包不参与最早到期**（任务5）：QuotaPanel `bonusAgg.firstExpiry` 只统计 `remaining>0` 的包（后端 `quota_rank_key` 同口径）
- ✅ **报错账号冷却**（任务6）：`oauth_tokens.cooldown_until/fail_count` 列；`note_request_result`（429→60s/401·403→300s/其余→120s，指数退避封顶 1h；成功走脏标记零开销清零）；选号跳过冷却账号（**全冷却仍回退**防 503；**点名不受冷却**——用户明确指定）；资格闸 `is_account_worthy_error`（空输出/race 取消/上下文超限不冷却）；接入全部 8 条调用路径
- ✅ **测试 1005→1028 全绿**（新增 `tests/test_oauth_multi_account.py` 23 项：排序键/冷却跳过/退避清零/资格闸/状态码提取/free 判据/DB 列/全路径静态锁/前端静态锁）；改写 `test_resolver_passes_oauth_owner`（`pick_access_token_ex`）；前端 build 通过（bundle `index-Igfu_2y0.js`）；E2E 冒烟（临时 DB：到期排序选中 → 冷却切换）通过
- ✅ **文档**：新增 `docs/findings-oauth-multi-account.md`（F40 全文）
### 备注
- 匿名共享池限流的唯一缓解=静置/换 IP/降频；生产正常频次远低于探针，无需额外动作
- `getattr(rc, "account", None)` 全链兼容旧替身对象（测试桩无该属性也不会炸）
- 预热任务启动延迟 20s（让位服务初始化）；单账号 provider 不预热（无需排序）

## Session 12 - 2026-10-14
### 完成（用户需求：清理A机上本项目的无用数据残留，日志留近3天）
- ✅ **「A机」认定与私钥定位**：A机=**182.254.147.87**（Ali/AIGate 生产，key=`id_a.pem`，可登录）。历史曾称 114.55.170.147 为「机器A」（8 月前 AIGate 旧部署点，codex/aigate-V2）；182.92.141.30 是 7 月代理隧道机器（当时称「A机」）。**.30 与 .147 今天均无法登录**（主机密钥均变、x1a0q1sx.pem 全用户名被拒 → 疑似重装），无处可清，清理在 182.254.147.87 执行。`x1a0q1sx.pem` 现存副本在本地 WPS 云盘目录（Downloads 原件已丢；指纹 `SHA256:XtRAhUgsZVyI3J9y7CDdp86cA6xSwVeRT6bET/5uRTw`；对应公钥已在会话中给出，可重装到 .30/.147 恢复访问）
- ✅ **清理执行（182.254.147.87）**：①pm2 日志 `aigate-out/error.log` 保留近 3 天（截断到 10-11 12:01 启动点；旧 25MB→5.6MB，err 2.7MB→41KB）②`data/backups` 删 28 个孤儿 `.db-shm/.db-wal`（对应 .db 已在 9-28 被保留策略清掉）+ 5 个 8-26 更新暂存目录（411M）；保留 14 份每日备份（与 `backup.keep=14` 策略一致）③删一次性修复产物 `repair_prompt_double_count-*.json`、`client/build_err.log`、根级 0 字节 `aigate.db`、`.wb-bridge/context-handoff.jsonl`（WBBridge 域、6 周未动）④`freebuff-login.log` 截为尾部（留近 3 天）⑤项目 `__pycache__/.pytest_cache` 清理 ⑥journal `--vacuum-time=3d`（1.1G→64M）+ `/var/log` 轮转旧件 >3 天删除 47 个（1.3G→143M）⑦clash 遗留 pm2 时代日志 71M 删除（clash 现为 systemd 服务，输出走 journal，已核验 journal 实时写入）
- ⚠️ **pm2 日志截断的坑（本次实踩并修复）**：pm2 God Daemon 以 O_APPEND 持有日志 fd，**直接替换/删除文件会让写入继续落到旧 inode**（新路径不再增长，且旧 inode 空间不释放）。正确做法：`/proc/<daemon>/fd` 找到被标 `(deleted)` 的旧 inode → `cat` 抢救内容 → 写新文件 → `pm2 reloadLogs` 重挂句柄。本次已完整执行：旧句柄内容零丢失找回（含替换后增量），重建 3 天窗口后 reloadLogs，探针验证新文件实时增长（5658879→5658931 字节）
- ✅ **未动项（非本项目资产）**：`wb-bridge-*` 日志（WBBridge 项目，10-14 仍在写）、服务器 home 目录下 trendradar/opencode/LSJ/Meridian 等他项目目录、`/var/log/btmp`（安全审计留证）
- ✅ **磁盘对比**：22G used/59% → **21G/54%**（释放约 1.6G：journal 1.1G + /var/log 47 个旧件 + backups 411M + 日志截断 28M）；服务全程存活（aigate pid 3653694 online，HTTP 200，wb-bridge online）
- ✅ **安全网**：删除前小文件备份至服务器 `/tmp/aigate-cleanup-20261014-125055/`（含 `passthrough_router.py.bak-audio` 副本——与当前版本有 diff，确认是旧版；已剥除其中的 config/credential 副本并 chmod 700；确认无需回滚后可删）
### 备注
- 服务器上两个未提交改动（media_router 视频超时头、passthrough /audio/speech 路由）为既有工作遗留，**未动**
- 182.92.141.30 上原「本项目残留」无从核实——该机不可登录；114.55.170.147 同理（主机密钥变更+密钥被拒）。若用户需要，可先在云控制台 VNC 装回公钥再清
- 本地临时文件（key 副本/known_hosts 测试件）已全部清除

## Session 13 - 2026-10-15
### 完成（排查 aigate 访问不到）
- ✅ **根因链（已实锤）**：10-15 02:56 内核 OOM killer 杀掉 aigate（anon-rss 2.4GB，机器仅 3.7GB 内存、swap 已用 1GB）→ 同机 PM2 God Daemon 一并消失，且当时 PM2 **无开机自启服务**、`dump.pm2` 停在 10-03 → 无人拉起 → 8000 无监听、Web 全不可达。磁盘/网络/nginx 均正常（nginx 80 仍在，仅代理 wb-bridge；aigate 走 8000 直连）
- ✅ **压力来源=同机本地进程 artex**（ARTEX AI 渗透平台，systemd 服务，非本项目）：持续打 `/v1/chat/completions`，实测 ~60-120 次/分钟、8+ 并发连接；保留窗口内 28,235 次 chat POST 全部来自 127.0.0.1（19,400 成功 / 8,686 个 500 / 128 个 503）。artex 日志显示其多 worker 做上下文压缩且频繁遇到 `empty body from model` → 客户端重试形成放大回路
- ✅ **崩溃前症状取证**：`aigate-error.log` 14 小时从 41KB 涨到 174MB、含 17,908 次 `QueuePool limit of size 20 overflow 10 reached ... timeout 30.00`；调用栈高度集中在 `chat_completions → auth.dispatch → model_catalog.get_by_full_id`（每请求一次查库）；定时任务 `_run_model_refresh_tick` 被饿 16 分钟（事件循环/内存压力已极端）；日志停写导致 10-14 15:37 之后请求无落库
- ✅ **恢复**：`pm2 resurrect` 从 dump 拉回 aigate + wb-bridge，HTTP 200 验证通过（`/`、`/health`、`/admin/`）
- ✅ **防复发（运维层，本次已落地）**：①`max_memory_restart`：aigate 1024M / wb-bridge 512M（此前无闸，进程只能被内核 OOM 杀）②`pm2 save` 刷新 dump（原存档陈旧）③`pm2 startup systemd` 生成并 enable `pm2-ubuntu.service`（Type=forking + PIDFile），另加 drop-in `Restart=always/RestartSec=5`；`pm2 kill` + `systemctl start pm2-ubuntu` 已把守护进程迁入 `system.slice/pm2-ubuntu.service`（此前跑在 SSH 登录会话 scope 内）
### 未完成 / 建议
- artex 属另一项目，**未动其进程**。若其调用不降频，aigate 在 1GB 内存闸下会周期性重启（实测当前负载下 RSS 约 30-35MB/分钟爬升）；建议与用户确认限流/重试退避
- 网关侧硬化（**待确认后再做**，属代码改动）：①chat 热路径 `get_by_full_id` 改用进程内目录缓存，消除每请求 DB session ②`admin_gzip`/`combo_prefix` 为 `BaseHTTPMiddleware`，高压下缓存响应体，可改原生 ASGI 中间件或加并发背压 ③连接池 20+10 与 30s 等待在洪峰下无背压，考虑排队上限与快速失败
- 风暴期证据保留在服务器 `~/.pm2/logs/aigate-error.log`（174MB），磁盘无压力，暂留查证

## Session 14 - 2026-10-15
### 完成（排查「aigate 巨卡」）
- ✅ **结论：网关进程本身健康，卡在连接池排队**。服务器内部 `/health`、`/` 响应 2-3ms（200），aigate **无重启**（RSS 533M，远低于 1GB 闸），load 0.20、CPU 95% idle、swap 无换入换出、磁盘 57%——「巨卡」不是 CPU/内存/磁盘瓶颈
- ✅ **根因链（实锤）**：同机 **artex**（pid 186780，systemd 服务，非本项目）以 **81 条并发连接 / 35 线程**持续打 `/v1/chat/completions` 流式请求（模型 `CodeBuddy CN (腾讯)/step-5-preview`）；上游单条**平均 58.2 秒**（观测 max 62.4s，疑上游超时或长生成）；而 `/v1/chat/completions` 用 `Depends(get_db)`（`server/api/v1_router.py:1517`）→ **DB 会话生命周期 = 整个请求生命周期**，每条流式请求在整个 58 秒内独占一条池连接 → 池 20+10 被 81 并发瞬间打满 → 后续请求 `QueuePool limit of size 20 overflow 10 reached ... timeout 30.00` 排队 30 秒后 500
- ✅ **量化证据**：①`aigate-out.log` 最近 2MB：7,180 个 chat POST 中 **6,747 个 500（94%）**、仅 691 个 200；`响应已就绪` 耗时均值 58,246ms（n=659）②`aigate-error.log` 20 秒涨 590KB（≈2.5GB/天），最近 6MB 内 308 次 `sqlalchemy.exc.TimeoutError`（全为 QueuePool 超时）③`request_logs` 近 3h 共 4,738 行**全部 `user_ip=127.0.0.1`、`api_key_id=NULL`**（本地无 key 直连 → 按 key 的限流天然不生效），状态 error 3,967 / pending 765 / success 6；逐时均值延迟 18s→35s→50s 递增 ④落库严重滞后（3h 仅 4,738 行，而单个 2MB 日志段就有 7,180 个 POST）——**日志写入与业务共用同一个已耗尽的池**，写日志自己也在排队
- ✅ **恢复手段**：压力源在 artex 而非网关，`sudo systemctl stop artex` 可立即解除（随时 start 恢复）。**本次未执行，待用户决定**
- ⚠️ **旁路发现（非本次卡的直接原因）**：mihomo 通用 `PROXY` 组（Selector）`now=DIRECT`，其订阅源 **XFLTD 拉取 403、0 节点（已过期）** → api.openai.com / grok.com 等走 `Match` 规则的出站全部 `i/o timeout`；`opencode.ai` 走的 `OPENCODE-REGION`（日本中转，19 节点）不受影响仍正常；`iKuuu_V2`(7) / `sabusuku`(102) 订阅正常更新。**需翻墙的渠道（走 `PROXY`/`Match`）当前实际不可用**，需换订阅或把 `PROXY` 指向可用组
### 未完成 / 建议
- **结构性硬化（待确认，属代码改动）**：①`get_db` 不跨越上游流式调用（进入流式前取完数据、上游调用期间不持连接）+ 热路径结果落进程内缓存 ②连接池加排队上限/快速失败（当前 30s 排队完全无背压）③`admin_gzip`/`combo_prefix` 改原生 ASGI 中间件
- artex 侧降频/重试退避（其 `empty body from model` 重试是放大回路），或网关侧对本地无 key 调用加并发闸

## Session 15 - 2026-10-15/16
### 完成（高并发适配 + 日志机制 + muse/gemini 调研）
- ✅ **过载保护三件套（已部署生产，commit 56875f6）**：
  - ① `server/core/inflight_gate.py` 推理入口全局并发闸（纯 ASGI）：只拦生成类入口（chat/messages/responses/Gemini generate/streamGenerateContent + `/combo:<ref>/` 前缀），在途超限先等 `acquire_timeout_seconds`、仍无名额 → **503 + Retry-After**（`gateway_overloaded`）。默认 128 在途/等 15s；`config.concurrency` 热改即时生效；设置页新增「推理并发闸」卡片（含实时统计：在途/峰值/放行/拒绝），`/admin/api/concurrency` + live summary `overload` 段
  - ② `server/core/exception_guard.py` 最外层兜底护栏：QueuePool 超时/DB 不可用/超时 → **503 短 JSON**（内部细节不泄漏）；未处理异常日志按 `(异常类型,路径)` **10 秒窗口限流**（首条记类型/摘要/前 6 帧栈，其余计数、下窗口补记 +N suppressed）——根治每请求 ~180 行 traceback 洪泛
  - ③ `_release_db()` DB 连接不横跨上游调用：路由/取数完成后、上游调用（尤其流式）之前提前归还连接，覆盖 **六条路径**（auto 级联 / combo 流式 / combo 非流式 / 直连流式 / 直连非流式 / free_tier）；fusion 保持不释放（并行任务全程需要 db）。机制实证：AsyncSession 读查询即开启事务独占连接直到 close；close 后连接归还、已加载属性仍可读、会话可继续复用（测试锁死）
- ✅ **实测验证（生产）**：60 并发 + 40 并发流式压测（重放 artex 事故场景）——**全部 200、零 QueuePool 超时、零 ASGI 异常、错误日志增长 0 字节**；闸统计峰值在途 62、拒绝 0（128 上限未触）；对照=重启前旧代码时段同一场景 24 个 500 + 56 次 QueuePool。20 并发非流式也全 200
- ✅ **服务器 pm2-logrotate**（此前单文件 439MB 无轮转）：`max_size 200M` + 每天 00:00 轮转 + `retain 3` + `compress`，装后已实际轮转两次（23:28 按大小、00:00 按 cron）；`pm2 save` 已持久化
- ✅ **测试**：新增 `tests/test_overload_protection.py` 12 项（路径判定/过载 503/非生成路径放行/闸关/异常分类/防洪窗口/端到端 503/池独占-释放-复用/幂等/配置默认值），全量 **1040 全绿**
- 📋 **调研结论（muse / 9router / gemini）**：
  - **muse = Meta 的 Muse Spark 模型**（Muse Code 订阅，官方登录=auth.meta.com 设备码 RFC 8628 → `POST api.meta.ai/muse-code/key` 铸 key，API=`api.meta.ai/v1` 的 Chat Completions + Responses）；AIGate 接入的 `muse-spark-*-contributor-free` 是 **opencode Zen 免费层转售的 Meta contributor 档**
  - **能登录 muse 的项目（实锤）**：`TheStreamCode/opencode-muse-auth`（opencode 插件，设备码）、`TheStreamCode/hermes-muse-code`、`BlockedPath/pi-meta-oauth`（每日重铸 key）、**`router-for-me/CLIProxyAPI`（5.5 万★，内置 Meta Muse provider：OAuth 登录 + Model API key）**、`JohnWangggg/magpie-muse-web`（muse.ai 网页 cookie + 无头 Chrome）；opencode 免费层纯 HTTP 侧车项目一批（`azrinaldi/opencode-free-bridge`、`TiaraBasori/OpenCode2API` 341★ 等）
  - **9router 支持 muse（源码实锤）**：双通道——`open-sse/providers/registry/muse.js`（Meta Muse OAuth 设备码 + 别名 muse-code/meta-model-api）+ `opencode.js` 免费层伪造头（`Bearer public` + `x-opencode-client: desktop`）；CHANGELOG v0.5.59~v0.5.99 一路在修 muse 路由（chat→responses 改道、contributor 模型、OAuth 登录）。**注意**：9router 当前是纯 HTTP 指纹路线（非 CLI sidecar），且 10-14~15 上游收紧后正在高频打补丁（issue #4845 作者独立得出与 AIGate 一致结论：纯 HTTP 复刻仍 403，建议驱动本地 `opencode serve`）；**AIGate 的 CLI sidecar 路线仍是更稳的**
  - **gemini 登录式接入（实锤）**：**Gemini CLI 免费层已被 Google 于 2026-06-18 关闭**（9router README 原文）；现役路线=**Antigravity**（Google IDE，附带 Gemini 3.x/Claude 5.x 额度）：`su-kaka/gcli2api`（5251★，10-15 活跃，GeminiCLI+Antigravity→OpenAI/Gemini/Claude API）、`liuw1535/antigravity2api-nodejs`（815★，多账号轮换+额度查看，但 6-19 后未更新）、`router-for-me/CLIProxyAPI`（Antigravity OAuth 完整支持）、9router `antigravity.js`（OAuth authorization_code + IDE UA + 429/500/503 重试）。**AIGate 现状**：`oauth_registry.py` 已有 antigravity 配置（authorization_code + PKCE + 5 分钟前置刷新 + 静态模型 12 个），但生产 oauth_tokens 表无 antigravity 连接；**历史否决记录**（09-26 jet-hub 调研）：①需网关主机跑 IDE 进程 ②公共 API 403 SUBSCRIPTION_REQUIRED ③违反其 9 条防封号约束——但 9router/gcli2api 的现役实现走的是 **OAuth + cloudcode-pa/v1internal** 通道，与此前否决的「读本机 IDE 状态」通道不同，**值得重新评估**（用户已表达接入意愿）
### 未完成 / 建议
- **combo 多模型负载分摊评估**（用户设想：建组合多放模型分摊）：combo 现支持 fallback/round_robin/weighted/fusion 四种策略，round_robin 与 weighted 都能跨请求分摊流量（实测 918 组合 18 候选）；但**分摊解决的是上游侧选择，不解决本地并发上限**——真正防崩的是本次的并发闸+连接释放（已验证 100 并发级压测零故障）。建议：①生产 combo 用 round_robin/weighted 分摊上游压力 ②若 artex 洪峰再来，128 上限 + 15s 等待会把超额请求快速 503（客户端可重试）而不是打穿池子 ③如需更高并发，先升 DB 池（20+10 是 SQLite 单写者场景的保守值）或迁 PG
- **artex 侧未动**（属另一项目）：仍建议降频/重试退避；网关侧闸已能兜住其洪峰
- **mihomo PROXY 组仍 DIRECT**（XFLTD 订阅 403 过期）：需翻墙的渠道受影响，待换订阅或改指可用组

## Session 16 - 2026-10-15
### 完成（修「用量趋势最多只显示到 10 点」）
- ✅ **定性：后端无恙，纯前端「时间窗冻结」**。用户 22:00 看到趋势停在十点左右，两层叠加：
  - ① **预设窗口冻结（主因）**：`RangePicker.presets` 是含 `new Date()` 的 `computed` 且**无响应式依赖** → Vue 首次求值后永久缓存；`Analytics.refreshAll`/`setBucket` 又复用冻结的 `this.range` → 「刷新」= 重放打开那一刻的窗口。现场复现：页面 01:20 挂载、01:24 点「今日」仍发 `end=2026-10-15T17:20:00.000Z`；生产访问日志同一 `end=` 反复出现（`11:20Z`×9、`10:19Z`×4、`10:59Z`×2、`15:54Z`×2）
  - ② **小时轴裸显 UTC（放大因素）**：`TrendChart` 小时桶 `k.slice(11,16)` 直接显示 UTC 小时（后端 `_trend_slots` 按 UTC 分桶），北京用户看「今日」从 16:00 起轴，把「滞后几小时」视觉再放大
- ✅ **修复（前端，后端零改动）**：
  - 新增 `client/src/rangePresets.js`：`buildPresets(now)` / `freshRange(range, now)`（**只推进预设，`custom` 不动**）/ `defaultCustomWindow(now)` / `toLocalInput` / `localToUtcIso`，`now` 全部显式注入（组件不自行取时刻）
  - `RangePicker.vue`：删掉 computed（根因）→ `data.presets` + `mounted` 初始化 + **`pick()` 先 `refreshPresets()` 再 emit**（按点击当下重算）
  - `Analytics.vue`：`refreshAll`/`setBucket` 均先 `syncRange()`（内部走 `freshRange`）再拉数据；范围提示 `（UTC）`→`（本地时间）`
  - `TrendChart.vue`：小时标签 `label()` 与悬浮 `tipDay()` 把 UTC 槽键 `new Date(k + 'Z')` 转浏览器本地时区
- ✅ **测试**：新增 `tests/test_frontend_range_freshness.py` 5 项静态守卫（导出齐全 / custom 不变 / RangePicker 无 computed / pick 重算 / refresh 经 freshRange）；连同既有前端回归共 **16 项全绿**；`npm run build` 通过
### 部署与验证记录
- 纯前端改动 → 服务器 `git pull` + `cd client && npm run build`，**无需重启 pm2**（见下）
- 修复后浏览器复核：见下方「验证」小节（隧道 18600 → 点「今日」/刷新，确认 `end` 随当前时刻前进）
### 未完成 / 建议
- 若还有别的页面用「挂载时刻快照」做时间窗（同类 computed 陷阱），应按同一模式排查；本次已把工具抽到 `rangePresets.js` 供复用

## Session 17 - 2026-10-15
### 完成（OAuth 多账号频率感知分流：高频自动负载、低频粘住）
- ✅ **需求**：用户观察到 workbuddy（CodeBuddy）国际服与 CN 自动选号一直打同一个账号。生产实锤：近 24h CN 4620 次里 **91.6%** 集中在 159 账号（386 次给 135 账号=冷却回退期）；Intl 594 次里 **68.7%** 集中在 1355386364@qq.com。要求：高频自动多账号负载、低频保持 F40 粘住
- ✅ **实现**（commit 13bab80，后端，已部署重启）：
  - 新增 `server/core/account_balancer.py`：纯进程内滑窗计数（默认 60s）+ 滞回阈值（`enter_requests`=10 进入分流 / `exit_requests`=3 退回粘住）；`note_selection`（自动+点名取号成功都记账）、`is_balancing`、`account_load_key`、`stats`、`reset`
  - 接线：`oauth_client._rank_auto_rows` 分流模式在「额度档位」后插 `(窗口请求数, 最近使用)`；低频路径恒定负载位 `(0,0.0)` 与 F40 完全等价；`pick_access_token_ex` 两条成功路径记账（永不影响取号）
  - **闸门不变**：冷却账号、已耗尽账号（档位 2）照旧排最后，分流绝不动它们；点名不受影响；单账号无操作
  - 配置 `config.oauth_balance`（enabled 默认开），每调用读配置热改即时生效（照 inflight_gate 模式）
- ✅ **测试**：新增 `tests/test_oauth_balance.py` 14 项 + `tests/conftest.py` autouse 重置夹具（**新教训：进程全局态会跨测试累积**——19 次 pick/60s 就会误开分流，必须逐测试 reset）；既有 23 项多账号测试不改一字全绿；全量 **1059 全绿**
- ✅ **生产验证**：pm2 restart 后 14 并发请求 `CodeBuddy CN/deepseek-v4-flash` 全 200，routed_account 分布 **10 + 4** —— 前 10 次粘住（阈值内）、第 11 次起自动分流给计数 0 的第二账号。与设计逐步吻合
### 行为说明（给用户）
- 低频（60s 内 <10 次）：保持原样——优先「有余额且积分最近到期」的账号，失败才冷却切换
- 高频（60s 内 ≥10 次）：同额度档位的账号自动轮流（计数落后者先接管）；掉回低频 30s 内回到粘住
- 阈值可调：服务器 config.yaml 加 `oauth_balance: {enter_requests, exit_requests, window_seconds, enabled}`，改完即时生效（此配置段默认不在文件里，走代码默认值）
### 未完成 / 建议
- 分流状态（`account_balancer.stats()`）暂未透出到设置页/分析页，需要时可以加一个只读卡片
- 若希望「达到阈值立即均匀 50/50」而不是「落后者先追平再轮流」，把 `account_load_key` 的 count 比较换成 LRU-only 即可（现实现=负载均衡语义）

## Session 18 - 2026-10-16
### 完成（三项用户问询：账号冷却语义 / 测活请求来源 / CodeBuddy Intl 11128）
- ✅ **Q1 账号冷却 + 跨账号重试（确认，未改码）**：机制存在且生效，但语义与「一失败当场换号」有三处差异——
  - ① **冷却只在「值得冷却」的错误上触发**（`is_account_worthy_error`：401/403/429/5xx 或限流文案）→ 失败写 `oauth_tokens.cooldown_until/fail_count`（429=60s 起 / 401·403=300s 起 / 其余 120s 起，指数退避封顶 1h，落库跨重启存活）；**换号发生在「下一次取号」**（`_rank_auto_rows` 把冷却账号排最后），不是同一请求内
  - ② **同一请求内不会换号**：候选身份是 (服务商, 模型)，`tried_ids` 按模型排重 → 同一 provider+model 不会被另一账号重试。直连路径 `resolve_credential_async` 每请求只解析一次（`v1_router.py:2432`）；「同模型换 key 重试」(`v1_router.py:3285` `next_key_for_model`) 只作用于标准 API key 集合，OAuth 账号不参与。auto/combo 会在同一请求内退到**其它模型/服务商**
  - ③ **「全部账号都不好使」不是硬报错**：全冷却时仍回退使用（防 503 饿死，`test_pick_all_cooling_still_falls_back`）；点名 `provider.oauth_owner` 不受冷却影响
- ✅ **Q2 「12:10:50 一大批测活请求」= 上一会话（Session 17）自己的 F41 生产验证**，非第三方探测：生产 `request_logs` 12:10:50–51 CST（=04:10:50Z）14 条 `CodeBuddy CN/deepseek-v4-flash` 全 200，`routed_account` 分布 **10 + 4**、`user_ip=127.0.0.1`、无网关 key —— 与 Session 17 记录「14 并发验证、分布 10+4」逐条吻合；时间落在 commit 13bab80（12:08）与 docs 2ebd54d（12:12）之间
- ✅ **Q3 `CodeBuddy (International)/deepseek-v4.1-flash` 报 11128 = 上游对该账号的账号级拦截，与请求体/模型/链路无关**。生产 A/B 实测：
  - 现象：`{"code":11128,"msg":"conversation id missing","displayMsg":"请求被拦截，请重新发送"}`（HTTP 400）。**全部命中同一账号 `1355386364@qq.com`**（今日 Intl 6 次请求：5 错 1 对）
  - 排除项：同 payload 换另一账号（`x1a0q1sx@gmail.com`）**12/12 全 200**；被拦账号 **9/9 全败**（含直连与 127.0.0.1:7890 代理出口、静置 180s 后单发、5 个模型、CLI/IDE 两种头形态、`conversation_id`/`conversationId`/`X-Conversation-Id` 各承载位、70k token 大 prompt）→ **既非出口 IP、非 payload 形态、非 conversation id 字段缺失**（该提示是上游风控的通用文案）
  - token 与账号均正常：两账号 access_token 有效期至 2027、`/v2/plugin/login/account` 200、billing 正常（被拦号 2 个 Bonus Pack 350 credit，有效期至 10-24）
  - 时间线：被拦账号 10-13/14/15 分别 364/732/424 次成功，**2026-10-15 17:29 UTC（10-16 01:29 CST）起开始 11128**；此前 1 小时内以 ~15s 间隔连续跑 **126k token** 大 prompt（`deepseek-v4.1-flash` 是 0 倍率免费档）→ 符合上游风控对「免费档 + 高频超长上下文」的处置特征；期间偶有放行（04:21Z 一条成功）
  - **网关侧放大因素**：400 不在 `is_account_worthy_error` 白名单 → 该账号**不会进冷却**，而它在额度排序里因最早到期（10-24）保持第一 → Intl 流量持续打向被拦账号、每条都失败
### 未完成 / 建议（未实施，待用户决定）
- 若要让网关自动绕开这类「上游账号级拦截」：把 CodeBuddy 系 `11128`（风控拦截）纳入账号冷却判据（需与「payload 形态导致的 11128」区分——本次实测排除法表明可从「同账号跨模型全失败」判定），或对 400+风控码做一次账号级探测后冷却
- 短期可用：把 `CodeBuddy (International)` 服务商 `oauth_owner` 点名到可用账号（`x1a0q1sx@gmail.com`），或重连被拦账号换新 token（上游是否解除未验证）
- 上游侧账号被风控的真实触发阈值未知（免费档 + 高频 + 超长上下文的组合），需继续观察该账号是否自行恢复

## Session 19 - 2026-10-16
### 完成（OpenCode 免费模型可用性实测 + muse 登录现状）
- ✅ **OpenCode 免费模型生产实测（免费 executor → CLI sidecar → opencode.ai 全链路）**：
  - zen 上游当前 **13 个免费模型**；**10 个实测 200**：big-pickle、cat-jam-free、mimo-v2.6-flash-free、muse-spark-1.2/1.3-contributor-free、nemotron-3.5-lightning-free、longcat-2.5-preview-free、deepseek-v4.1-flash-free、space-bunny-free + 复测后通的 **nemotron-3.5-super-vl-free**（34.6s）
  - **3 个不可用：jev-1.13-free / ll-alpha-free / glm-5.3-flash-free** —— CLI 1.18.32 模型注册表（models.json）里**根本没有这几个 id** → `ModelUnavailableError`；最新 CLI v1.19.2（10-16 发布）或等注册表收录后才可用
- ✅ **重大发现：CLI 内存模型表过期 → 免费模型「假超时」**。`opencode serve`（10-13 启动）内存里的模型表是启动时刻快照，上游新加的模型（nemotron-3.5-super-vl、deepseek-v4.1-flash-free）**一律 `ModelUnavailableError`**，而 sidecar 对该错误**不报错、空等到 300s 超时**（CLI 日志有 ERROR，网关侧只见 Timeout）→ **kill 旧 serve、120s 守护自动拉起（代理 env 注入正确）后立刻恢复**。以后「免费模型突然超时」先重启 sidecar 再排查
- ✅ **网关模型目录已刷新**（provider 26，additive-only）：+5（nemotron-3.5-super-vl-free、deepseek-v4.1-flash-free、ll-alpha-free、glm-5.3-flash-free、gemini-4-argon），目录 94→99
  - ⚠️ 目录里仍有 **11 个上游已下架的残留**（exo-free、fledge-alpha-free、ling-3.0/3.1、mimo-v2.5-free、step-5-preview-free、deepseek-v4-flash-free、nemotron-3-ultra-free、claude-sonnet-4、deepseek-v4-flash-vision-exp、test-novita-dsf4.1）——free_tier 刷新只增不删；选中会打上游 404/410（free_tier 失败冷却 300s 兜底，但用户侧会等一轮）
  - nit：目录里有 `test` 条目（zen 列表里的占位模型）
- ✅ **muse 登录现状（确认，未实施）**：AIGate **没有** muse 账号登录——oauth_registry 15 家里无 muse；muse-spark 目前只作为 **OpenCode 免费层模型**（免账号、匿名 Bearer public + CLI 会话）与 Freebuff 静态种子（provider 98 已禁用、账号被封）出现。要登 Meta 官方 Muse Code 账号需新增 OAuth provider（auth.meta.com 设备码 RFC 8628，clientId 1031625952748946 → `POST api.meta.ai/muse-code/key` 铸 `LLM|…` key）+ Responses wire 适配（device_poll 模式有 claude_code/codex 先例、Responses 适配有 codex_responses 可参考），且**当前服务器出口到 Meta 域名全不通**（直连超时；经 mihomo facebook/api.meta.ai/auth.meta.com 也全失败）→ 上线前必须先解决出口
### 未完成 / 建议
- 3 个 CLI 未收录的免费模型（jev-1.13-free 等）：升级 opencode CLI 到 v1.19.2 后复测（未执行，动生产 CLI 待用户确认）
- sidecar 对 `ModelUnavailableError` 空等 300s：可在 sidecar 轮询里识别 CLI 日志的 ERROR 事件快速失败（未实施）
- 11 个残留条目是否清理（free_tier 刷新不删，需手动 disable）待用户决定

## Session 20 - 2026-10-16
### 完成（opencode CLI 升级 + 目录清理 + Meta Muse Code 接入上线）
- ✅ **生产 opencode CLI 升级 1.18.32 → 1.19.2**（`npm pack opencode-linux-x64@1.19.2` → 换 `~/opencode/bin/opencode`，旧版留 `opencode.1.18.32.bak`）；并**关键一步：刷新 models.json 注册表**——升级本身不够，CLI 的模型注册表是启动时抓 models.dev 的快照，缓存仍是旧的（124 条），直接换成当次抓到的 126 条
  - **全量复测 13 个 zen 免费模型 → 12 个通**：新增 **ll-alpha-free**（曾因注册表缺条目被挡）、**glm-5.3-flash-free**（首测 "HTTP transport failed" 属瞬时，复测 30.3s 通）；**jev-1.13-free 仍不可用 —— models.dev 至今未收录该 id**（`api.json` 里确实没有），非网关问题，只能等上游发布
  - 回归：big-pickle / cat-jam-free / mimo-v2.6-flash-free / muse-spark-1.2·1.3-contributor-free / nemotron-3.5-lightning·super-vl-free / longcat-2.5-preview-free / deepseek-v4.1-flash-free / space-bunny-free 全 200
- ✅ **清理 provider 26 目录 11 个上游已下架残留**（`ModelCatalog.update_model(enabled=False)` 走应用层逻辑，非直改 SQL）：claude-sonnet-4、deepseek-v4-flash-free、deepseek-v4-flash-vision-exp、exo-free、fledge-alpha-free、ling-3.0-flash-fin-free、ling-3.1-flash-free、mimo-v2.5-free、nemotron-3-ultra-free、step-5-preview-free、test-novita-dsf4.1 → 启用数 99→**88**（与 zen 在线列表条数一致）。`test` 是 zen 在售占位条目，保留
- ✅ **mihomo 出口：META-REGION 组 + Meta 域名规则**（`/opt/clash/clash_config.yaml`，改前备份 `.bak-1016`，`PUT /configs?force=true` 热重载）
  - 新增 url-test 组 `META-REGION`（filter 日本|新加坡，与 OPENCODE-REGION 同池，健康检查 URL = `https://auth.meta.com/`）+ 规则 `meta.ai / meta.com / facebook.com / fbcdn.net / fb.com → META-REGION`（插在 `MATCH,PROXY` 前）
  - 验证（默认路由，PROXY=DIRECT）：`api.meta.ai/v1/models` → **401**、`auth.meta.com/.well-known/openid-configuration` → **200**（此前直连/经代理均超时）
- ✅ **Meta Muse Code 接入（新 OAuth provider `muse` + Responses wire 适配）**
  - 协议取自 **TheStreamCode/opencode-muse-auth 源码逐行核对**（MIT）：`POST auth.meta.com/oidc/device/authorization/`（form client_id=1031625952748946）→ 浏览器输 user_code 批准 → 轮询 `POST auth.meta.com/oidc/device/token/`（`authorization_pending` 继续 / `slow_down` +5s / 否则终止）→ `POST api.meta.ai/muse-code/key`（`{"onboard":true}`，Bearer=OAuth token，头 `x-api-version:1.0.0`）铸 `LLM|…` key
  - **实测确认真实可用**：对生产 auth.meta.com 发起真设备授权 → HTTP 200 + device_code/user_code（`https://auth.meta.com/oauth/device/`）
  - 设计要点：**推理凭证 = 铸出的 `LLM|…` key**（存 `access_token` 列），OAuth token 另存 `refresh_token` 列仅作重铸材料（官方插件只在内存里保留它、从不落盘）；scope 列存 `{account_id,email}`；新连接自动名换成人账号邮箱
  - **刷新语义特殊**：`refresh_style=muse` 的「刷新」= 重铸 key。**重铸失败绝不下线连接**（现有推理 key 可能仍有效），只记 `last_error`；并按错误分级推迟 `expires_at`（401/403 → +30 天，瞬时/429 → +30 分钟）——否则 60s 调度器会把「激进限流」的铸 key 端点打穿
  - 适配器 `MuseResponsesAdapter` 继承 `CodexResponsesAdapter`（Responses wire 的 payload/SSE/chunk 全复用，含 `reasoning.encrypted_content` 回放），只覆盖三处指纹：①不注入 Codex 品牌 system prompt ②只用 `x-api-version`（不带 originator/X-Codex-Client/OpenAI-Beta）③effort 档位补 minimal/max（`models.dev` 声明 minimal…max）
  - 出口：注册表 `extra_params.force_proxy=True` → 自动登记服务商时置 `proxy_enabled=True`，推理/刷新/刷新模型都带 `__proxy_force` 走代理池（mihomo 再按上面规则路由到 JP/SG）；OAuth 三段调用（设备码/换 token/铸 key）统一走 `_force_proxy_kwargs()`
  - 目录/UI：`api_type=muse_responses` 进 adapter 工厂 + 健康检查工厂 + `infer_reasoning_effort_support`；Providers.vue 选项、OAuthConnections.vue 「设备流」徽标补 muse_device
  - 测试：新增 `tests/test_muse.py`（25 项：注册表/设备码 start/轮询分类/铸 key 判据/刷新退避语义/适配器指纹与 payload/工厂/基类回归）；**全量 1084 项通过**
### 未完成 / 建议
- **muse 仅在代码层就绪，尚未建立连接**：需用户在「OAuth 连接」页点 Meta Muse Code → 浏览器登录 Meta 账号批准设备码，之后自动铸 key + 自动登记服务商
- `jev-1.13-free` 只能等 models.dev 收录；sidecar 对 `ModelUnavailableError` 空等 300s 仍未做快速失败（本次靠重启 serve 绕过）
- muse 的 `LLM|key` 是否真有日级有效期未实证（按官方插件「stable key、仅 401 时重登」的设计按长期凭证处理，`expires_in=2d` + lead 1d → 每天重铸一次做自愈）；首条真实连接建立后可观察 `last_error` 判断 OAuth token 寿命

## Session 21 - 2026-10-16
### 完成（CodeBuddy Intl 渠道校验根因修复 + Playground 连续下发历史 + muse 登录失败定性）
- ✅ **CodeBuddy Intl 11128「没通过渠道校验」根因修复（生产 A/B 实证）**
  - 现场：Intl 全量 400/11128（`request illegal`），用户看到「这次调用没通过渠道校验，可能不是官方客户端发出的。请用官方版本重新发送。」（同码另一文案变体；displayMsg 还有「请求被拦截」等，**别按文案区分语义**）
  - 实测定位：点名 `x1a0q1sx@gmail.com` 200 正常、`1355386364@qq.com` 恒 11128 → 上游**按账号灰度**强制「渠道校验」（社区实证约 22% 账号进强制桶）
  - 根因（子代理交叉 9router PR #4944/#4949、workbuddy2api-hub #381、dsh-connect-workbuddy #66 二分、workbuddy-manager #193）：**官方客户端头族 `X-Conversation-ID / X-Conversation-Request-ID / X-Conversation-Message-ID` 三件套缺一即拦**（AIGate 一个都没发）；UA 不是判据；CN 无此校验。另注意 11128 还有 body 侧变体（`Illegal API invocation from an unapproved channel` = body 含 Claude Code 身份句，与缺头是同码两义，**诊断必须读 `msg`**）
  - 修复：`provider_quirks.codebuddy_client_headers()`（会话 ID 按「账号×首条 user 消息」uuid5 稳定派生——多账号池必须落到账号维度，同一 ID 挂 N 号反成关联特征；Request/Message-ID 每请求随机 32hex；trace/agent/IDE 族对齐官方），`client_headers=True` 只挂 Intl；`openai_compat._get_headers` 仅对 `/chat/completions` 注入（models/billing 不带）
  - **生产验证：被拦账号 1355386364@qq.com 补头后 200 正常回复**（此前 9/9 全败）→ 头族就是灰度强制桶的判据
  - 兜底：11128/「渠道校验」文案进 `is_account_worthy_error` 资格闸（error_text.is_risk_block_text），账号冷却风控档 **30 分钟起步**（补头族后仍被拦才触发——那才是真账号级封禁）；agent A 调研认为 11128 不该冷却（补头即好），保留理由：万一再出现（新灰度/新判据）不至于每条请求都钉死在被拦号上
- ✅ **Playground 连续下发 + 测试记录（前端自持）**（4d7cb4b）
  - 每次发送/鹈鹕测试追加**独立流式记录**（互不阻塞、各自可取消/复制/下载/删除）；对话测试也改流式（与鹈鹕同一 SSE 消费器），实时显示接收字符数；usage 透传显示 tok
  - 记录存 **sessionStorage**（键 `pg_runs`，内容绑定 admin session 尾号）：切路由/切浏览器标签不丢、重新登录自动清空；上限 30 条、单条 60k 截断、超配额自动丢最旧
  - 切走路由**不 abort** 在跑的流：后台跑完写回 sessionStorage，切回来能看到完整结果（刷新页则标「已中断」）
- ✅ **muse 登录「Sign-in failed / Token exchange failed」定性 + 加固**
  - 取证：生产日志 3 次设备码流全部 `authorization_pending` 直到 `expired_token`（授权从未被 approved），无 minted key；另有 2 次 authorize 400 = mihomo 出口 SSL EOF 瞬断（重放实测确认，非 Meta 限流）
  - 调研（CLIProxyAPI #6242 同错关单）：错误由 **Meta 自己的页面在用户浏览器侧**渲染——Muse 分区域开放，浏览器出口地区不支持/被标记、或用了托管/工作账号都会在页面交换环节失败；**与服务端出口和我们的请求形态无关**（与官方插件/oh-my-pi 逐字节比对：poll 带 client_id、form-urlencoded 全一致）
  - 加固：`start_muse_device` 传输层失败重试 2 次；poll 收到非 JSON 错误体（WAF/403 拦截页，9router-go PR #302 实证 Meta 会这么干）按终止处理不再空转到过期；authorize 提示讲清 10 分钟时限/个人账号/支持区域干净出口
  - **待用户重试**：个人 Meta 账号 + 支持区域（如美国）的干净浏览器出口（建议无痕窗口），10 分钟内完成批准；失败后回页面重点连接换新码
- 顺带观察（未处理）：CN 12:57 曾有 step-5-preview 连续 429（copilot.tencent.com 限流），与本次 Intl 渠道校验无关
- 测试：**1093 全过**（新增 muse start 重试/400 不重试×2、11128 资格闸+风控冷却档×3、头族×5）；前端 build 通过；部署 4d7cb4b 并生产 A/B 验证

## Session 22 - 2026-10-16
### 完成（workbuddy/CodeBuddy CN+Intl 每日限额识别 → (账号×模型) 冷却到重置时间 + 双页面展示）
- ✅ **需求**：用户要求「workbuddy 国内版和国外版超出每日限制都会在返回包里提示，读取识别到提示后自动进入冷却到它的那个时间点，并在模型里和 oauth 都显示」
- ✅ **生产报文取证（决定设计粒度）**
  - CN（copilot.tencent.com）：`{"code":6004,"msg":"您的使用量已超出频率限制，将在 2026-10-17 15:26:42 UTC+8 重置，您也可以切换其他模型继续使用。"}`
  - Intl（www.codebuddy.ai）：同码 6004 英文 `usage exceeds frequency limit, ... your usage will reset at ... UTC+8, alternatively, you can switch to the other models ...`
  - 关键判据：① 文案**自带精确重置时间**（UTC+8）；② 上游明说「可切换其他模型」→ **限额是 (账号×模型) 粒度，不是账号级**（若按整号冷却会误伤该账号其他模型）；③ `14003 too many requests` 是瞬时频率限流（无重置时间），**必须区分**，仍走原有短冷却
  - 痛点实证：生产日志同一账号对已限额的 step-5-preview 在限额期内反复重试 **2293 次**（旧逻辑 429→60s 指数退避→到点再撞墙）
- ✅ **后端实现**
  - `server/core/quota_limit.py`：识别（code 6004 / 中英文案）+ 解析（`UTC+8` 时间戳 → naive UTC；已过期返回 None；无时间戳 1h 兜底）+ `is_quota_related_error`（供模型冷却/瞬态重试排除）
  - `server/models/quota_limit.py`（`quota_limits` 表，唯一键 `(provider_code, owner, model_id)`，reset_at 过期自动失效无需清理）+ `server/core/quota_store.py`（选号跳过/列表聚合查询）
  - `note_request_result(..., model_id, provider_id)`：命中限额文案 → **upsert 限额记录而非整号冷却**；该账号**同一模型**成功 → 清该记录（提前恢复）
  - 自动选号（`pick_access_token_ex` 传 model_id）跳过该模型上限额的账号、其他账号顶上；全部账号都限额 → resolver 返回 `quota_limited: ... resets at ...`（自动路由换模型，不再误报「未连接」）
  - 限额错误**不进模型冷却**（该模型其他账号仍可用）、**不进瞬态重试**（等 1 秒必然再撞）；全链路 9 处账号记账点带 model_id，且用「短形态+全量原文」合并文本判限额（重置时间可能落在 120/200/300 字符截断之外）
  - 模型列表接口（`/admin/api/models`）按 `(provider_id, model_id)` 聚合输出 `quota_limited_until` + `quota_limited_accounts`
- ✅ **前端展示**
  - 模型页：模型行「限额 x时x分」徽标（悬浮 = 受限账号清单 + 重置时刻 + 说明「该账号其他模型/其他账号不受影响」）
  - OAuth 连接页：连接行「每日限额：模型（x时后重置）」（与账号冷却独立显示，蓝色区分）
- 测试：新增 `tests/test_quota_limit.py` 18 项（识别/解析/记账/选号跳过/成功清记录/resolver quota_limited）；修复 1 项既有 spy 测试（新 kwarg）；`conftest.py` 重置新增进程全局态 `_ACCOUNT_DIRTY`；**全量 1111 全绿**；前端 build 通过
- 部署：commit + push + 服务器 pull + `npm run build` + pm2 restart（后端改动，需重启）；生产验证：quota_limits 表随启动建表，首次真实限额触发时自动建行

## Session 23 - 2026-10-17
### 完成（muse 登录失败定性 + 设备流失败原因透出 + 刷新窗口内禁用服务商）
- ✅ **muse「还是报错」排查定性（用户任务 1）**
  - **生产取证**：5 次设备流全部轮询到 `expired_token`（授权从未被 approved）；`authorization_pending`/`slow_down` 语义均正确返回——**设备码申请与轮询端点全正常**；2 次 authorize 400 = mihomo 出口 SSL EOF 瞬断（重放确认）
  - **多源交叉定性**：「Sign-in failed / Token exchange failed」是 **Meta 自己的认证门户（auth.meta.com）在用户浏览器侧渲染**的（CLIProxyAPI #6242 维护者同判，not-planned 关闭归因上游）；根因=① Muse 按 Meta Geographic Use Policy 分区域开放（韩/印尼/UAE/中国含港澳有实报；建议支持地区如美国干净出口）② 企业/托管账号（MMA）不支持设备码流必须个人号 ③ 代理/扩展/防火墙干扰、出口 IP 频繁跳变
  - **服务器侧对照实验**：auth.meta.com **网页**从 JP/SG 多节点均 400（同域 API 端点全通、facebook.com/meta.com 200）——但服务器 curl 无浏览器会话，400 不能作为「机房 IP 被封」证据；真实批准发生在用户浏览器，**服务端出口不是变量**
  - **AIGate 侧修复（体验缺口）**：设备流失败此前只进服务端日志 → 前端要等 6 分钟超时才报「未检测到新连接」，看不到原因。现在：
    - `oauth_client.record_device_flow()`（进程内 `_DEVICE_FLOW_STATUS`）在 expired_token/非 JSON 拦截/铸 key 失败/超时各终态记录失败原因 + 排障指引（含「浏览器报 Sign-in failed 需支持地区干净网络+个人账号」）
    - 新增 `GET /admin/oauth/device-status/{provider_code}`；前端 `watchDeviceFlow` 每 3s 轮询，failed **立即停轮询并弹真实原因**；授权弹窗文案同步补充指引
    - 测试补 4 项（expired_token 记录/成功记录 ok/铸 key 失败记录/超时记录）；`conftest.py` 增加 `_DEVICE_FLOW_STATUS` 逐测试重置
- ✅ **模型刷新结果窗口内直接禁用失败/超时服务商（用户任务 2）**
  - `ModelRefreshModal` 失败清单每条渲染「禁用」按钮（已禁用标「已禁用」不发重复请求）+ 多条时「全部禁用（N）」
  - 复用既有 `PUT /admin/api/providers/{id}`（不新增后端端点）；成功后发 `provider-disabled` 事件，Models/Providers 两页同步本地列表（筛选下拉/开关/排序即时更新）
  - 后端 `failed_details` 补 `enabled` 字段（刷新前就已禁用者同样会失败，前端据此显示「已禁用」）
  - 新增 `tests/test_frontend_refresh_disable.py` 6 项静态回归
- 测试：**全量 1120 全绿**（1111 + muse 3 + 前端 6）；前端 build 通过
