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
