# Provider-neutral Review Workflow Implementation Plan

**Authorization:** 用户要求立即推进，所有需要补充的信息使用占位符并汇总，不再等待配置问答。沿用 Python / LangGraph 和已批准的总体设计。按 inline TDD 执行，最后一次独立审查；不 commit/push，不全局安装，不调用模型服务。

**Goal:** 交付 2B 的离线可执行流程切片：单 Agent 基线、LangGraph Planner→Reviewers→Verifier→报告，以及未来在线配置模板。不是完整 2B 或真实审查能力验收。

**Spec:** 本文细化 `../specs/2026-11-02-structured-review-design.md` 的 2B；下述选择为当前实现边界，不代替未来在线适配器和审批设计。

## Decisions / interfaces

- 保持 provider-neutral；provider/model/API Key/API 类型/发送范围/价格预算全部待填。当前不装任何厂商 SDK。`--review` 明确报尚未配置/未接入，不会静默退回演示；默认 `review` 仍仅盘点。
- `review --demo --mode single|multi` 是显式无网络流程演示，text/JSON/Markdown 均明确 simulated，不当成 AI 审查或实验准确率。--demo 与 --review 互斥。
- 可选 review extra 增加 LangGraph 1.x。使用 StateGraph 的 prepare→plan→review→verify→report 有界节点；single 跳过 Planner/Verifier。Reviewer 顺序调度但首轮输入相互隔离；并行性能优化不在本轮。
- `ReviewContext.read_file(path)->str` 复用每次读取的身份/大小/哈希/编码校验；read_lines 继续原规则。准备阶段只发送完整小文件（<=200 LF 行、<=32768 UTF8 bytes），最多20文件、总<=131072 bytes。超限/空文件明确记录未覆盖，不截断假装完整。
- `ModelRequest(stage, system, payload, response_schema)` / `ReviewBackend.complete(request)->str` 纯接口，无网络实现。DemoBackend 仅固定演示回应，按固定标记生成演示候选；普通文件也只能得到 simulated 报告。自建 fixture 不执行。
- strict Plan(tasks<=8)、ReviewOutput(findings<=20, reviewed_paths<=20)；任务ID/问题ID全局唯一。输出JSON最多131072 bytes；先校验schema再2A证据/关系；不自动修复坏JSON、不重试、不接受超范围引用。
- TaskOutcome 保存成功/失败与错误类别，不保存原始异常/源码。FindingOutcome 保存候选、角色、判定（或未独立验证）；不跨角色猜测根因合并，保留全部来源。Verifier unresolved 不补充（0次<=父设计最多1次），后续按预算增加。
- Limits: max_calls=12, max_tasks=8, max_findings=40；超限明确 partial，模型失败/非法输出也不能变成零问题成功。每次请求计数；全部候选预算检查；无后台线程超时伪装取消，在线超时、token/美元预算留适配器阶段。
- 报告区分纳入、已供给上下文、模型自报reviewed_paths、未覆盖、任务失败、未验证候选；演示 semantic_review_performed 永远false，target_tests_executed 永远false，真实费用不编造（demo=0调用网络）。
- 在 LangGraph 运行外层 `langsmith.tracing_context(enabled=False)` 禁止环境变量触发隐式追踪；测试启用 tracing 环境并禁网验证。无持久checkpointer/遥测，不接受外部callbacks。
- config.example.toml 只含非秘密占位和 false 授权，`.env.example` 只含假key。没有自动读取目标配置、dotenv或任意Python插件；真实key只填本地环境，不能贴聊天。当前在线配置只作为接入交接，不声称填完就能联网。

## Review Focus

- --demo 污染 tracing/PYTHONPATH 时不可联网、执行目标代码或冒充真实审查。
- 非法/重复ID、伪造快照/证据/任务外路径、超大输出必须拒绝且报告可诊断不泄漏响应。
- 部分失败/预算耗尽/未审查路径不能变成 completed/no issues，single 永远不伪造 independent accepted。
- 旧 CLI/Pydantic-free inventory 必须保持可用；新依赖懒加载，帮助不扫描。
- 配置占位与未授权不能触发 SDK、环境凭据探测或源代码发送。

## Tasks

- [x] 1. RED→GREEN: read_file + workflow schemas/bounded request interface; full 270 tests passed.
- [x] 2. RED→GREEN: actual StateGraph orchestration; injected synthetic backends test good/invalid/timeout/budget/counterevidence/independence/coverage cases; LangGraph installed only in project venv; full 293 tests passed.
- [x] 3. RED→GREEN: explicit demo backend + CLI dispatch/report; fixture demonstrates candidate and verifier; stdout safe, no raw context packets in reports, error exit nonzero, no optional deps for inventory; full 305 tests passed.
- [x] 4. Config placeholders, fill-in checklist, README/user guide status updates complete. Final full pytest: 305 passed (2026-11-03); Ruff check/format clean (35 files). Actual isolated demo smoke and network-tripwire tests passed. Independent review a466e0f27aa651b8e: no Critical/Important/Minor findings in the offline slice; reviewer ran 47 scoped tests and 12 additional isolated adversarial probes. Local tree and ledger preserved; no commit, merge, push or publish. Live adapter and semantic-review validation remain out of scope.

## Sources

LangGraph API checked at https://docs.langchain.com/oss/python/langgraph/graph-api (2026-11-02): StateGraph/START/END/compile/invoke, recursion_limit top-level config.
