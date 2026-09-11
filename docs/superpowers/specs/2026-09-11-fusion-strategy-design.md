# Fusion 组合策略设计（对标 9router v0.5.2，待批准后实施）

日期：2026-09-11 ｜ 状态：**设计评审中（未批准不实施）** ｜ 提出：9router 对比缺口分析
关联：docs/findings.md F8（fusion 曾以 501 明确禁用）

## 背景与目标

9router v0.5.2 上线了 Combo Fusion：并行问组合内所有模型，由一个 judge 模型合成最终答案。
AIGate 当初（P0-1 H）因为"未实现却伪造成功响应"把 fusion 显式禁用为 501。现在按正规设计实现它。

目标：
- 组合策略新增 `fusion`：并行 fan-out 全部候选 → judge 合成 → 返回
- 对三种协议入口（chat/responses/messages/claude）透明可用（在 combo 层实现，客户端只感知 `combo:name`）
- 诚实的失败语义：任何环节全失败 → 终态错误对象；部分失败 → 以成功子集继续

非目标（本期不做）：
- 多轮投票/加权共识（先单轮 + 单 judge）
- 9router 的 capacity 策略（另立设计）
- judge 结果二次校验（cost 过高）

## 设计

### 1. 数据面（combo 定义扩展）
`combo.model_ids` 条目保持 `{provider, model_id, weight?}`；新增顶层字段：
- `judge`: 可选 `{provider, model_id}`。缺省规则：从候选中选智力评分最高者做 judge（评分来自现有 intelligence 体系）
- `fusion_timeout_seconds`: 单候选超时，缺省 30
- `fusion_max_targets`: 上限，缺省 6（防成本失控，超出按 priority 截取）

Combos 表已有 JSON 列，无需迁移；combo 编辑器（前端）增加策略选项 + judge 选择器。

### 2. 执行流程（combo_router 新模块 fusion_executor）
```
fan-out（asyncio.gather, return_exceptions=True）
  ├─ 每候选：credential_resolver 解析 → 非流式调用（复用现有 adapter 链）
  ├─ 单候选超时/失败 → 记入 attempts，不阻塞其他
  └─ 全部失败 → 终态错误对象（502，error.attempts=各候选原因）
部分/全部成功（≥1）
  ├─ 只有 1 个成功 → 直接返回该结果（不调 judge，省一次钱）
  └─ ≥2 成功 → judge 调用：
       prompt = 原始请求 + "以下是多个模型对同一问题的回答，综合为一个最佳回答，保留分歧点"
                + 各候选回答（标注来源模型名）
       judge 用候选成功者中智力最高者（或配置指定）；temperature=0
输出：OpenAI chat 响应格式（model 字段回写 combo:name）
```

### 3. 流式语义（关键决策）
客户端 stream=true 时：
- fan-out + judge 阶段是**非流式**的（并行聚合，无法边收边发）
- 期间每 5s 发 SSE 注释行 `: fusion-collecting\n\n` 保活（不占 event 类型，客户端兼容）
- judge 的最终答案作为单个 content chunk 发出 → finish_reason=stop → [DONE]
- 即：fusion 组合的流式 = "聚合后一次性吐答案"，首字延迟≈全候选最慢者+judge 耗时。**UI 与文档必须写明这一点**（避免用户以为流式=快）。

### 4. 计费与日志
- 每候选一条 request_logs（requested_model=`combo:name`，routed_provider=model=候选，status=success/error，cost 按候选价）——复用现有 fallback attempts 日志通道
- judge 再记一条（routed_provider=model=judge，error_type='fusion_judge' 标记，便于成本核算区分）
- analytics 自动聚合，无需新表

### 5. 失败与取消
- 客户端断开：fan-out 任务用 TaskGroup 管理，取消时向所有进行中上游传播 cancel；pending 行由既有清扫/收尾机制处理
- judge 失败（超时/拒绝）：降级为返回**最长的一个成功候选回答**并在响应附 `fusion_fallback: "judge failed"` 字段（透明可审计）
- 全候选失败：终态错误对象（规范 error object，见 commit 0b9dcb5 格式）

### 6. 前端
- Combos 编辑器：策略下拉新增 `fusion（并行咨询+合成）`；judge 选择器（候选内/自定义）；fusion_max_targets 数字框
- 组合卡片徽章 `strat-fusion` 恢复可用（现在禁用）
- 文案注明：fusion 增加延迟与成本（N+1 次调用），适合"答案质量优先"场景

### 7. 兼容与迁移
- 现有 combo（fallback/round_robin/weighted）零影响
- 501 禁用点移除；未知策略仍 501
- API 面不变：客户端仍以 `combo:name` 请求

## 测试计划（TDD）
1. fan-out 单元：mock 3 候选（2 成 1 败）→ judge 收到 2 份答案、attempts 含 1 败
2. 全败 → 502 + error object + attempts
3. 单成功 → 直接返回不调 judge（用 spy adapter 断言调用次数）
4. judge 失败 → 降级最长回答 + fusion_fallback 字段
5. 流式：断言 SSE 序列（keepalive 注释 ≥0 条 + 恰好一个 content chunk + [DONE]）
6. 取消：gather 中途 cancel → 无悬挂任务（asyncio.all_tasks 断言）
7. 计费：3 候选 + judge → 恰好 4 条日志，judge 条带 error_type='fusion_judge'

## 成本与风险
- 每次 fusion 请求 N+1 次调用；免费站额度消耗 ×N——UI 文案 + max_targets 上限约束
- 各候选回答格式差异可能让 judge 混淆：prompt 模板固定 + temperature 0
- 实现体量：后端 ~300 行 + 前端 ~150 行 + 测试 ~10 条，一次会话可完成

## 开放问题（需你拍板）
1. judge 缺省选"智力最高候选"还是"固定一个便宜模型"？（成本 vs 合成质量）
2. 单候选成功时直接返回（跳过 judge）OK 吗？（省钱但结果就单源了）
3. 上限默认 6 个候选合适吗？
