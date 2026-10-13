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
- ✅ 服务商页默认排序改「固定+开启优先」：tier = 开启(2)+固定(1) 降序 → 固定且开启 → 开启 → 固定关闭 → 关闭（`providerSort` 值仍为 `pinned`，旧 localStorage 无需迁移）
- ✅ CodeBuddy 新增账号强制选号（F38）：官方登录页检测到已有 SSO 会话会静默复用账号 → registry 加 `sso_logout_url`，已有连接时 authorize 接口返回 KC 登出链（实测 302 直达登录页），前端新增账号先登出再登录；新连接默认名用邮箱（Intl）/手机号（CN）；同账号重登按身份写回改名后的原连接（防重复连接）
- ✅ 测试 992→1005 全绿：新增 8 项后端（默认名/自动名判定/身份归一/邮箱命名/手动名保护/重登复用/registry 登出配置/登出链构造）+ 6 项前端静态（三处 UI 改动），改写 2 项（RealtimePanel 删除后）
- ✅ 本地 build 通过（bundle index-23QP0_BZ.js）
### 备注
- 登出链只在「已有连接」时返回（首次连接复用浏览器会话是合理路径）
- CN 登出必须打 `www.codebuddy.cn`（SSO cookie 域；copilot.tencent.com/login 301 跳过去）
