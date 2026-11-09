# 第二阶段：结构化审查设计

日期：2026-11-02；更新：2026-11-03。状态：2A 已实现；2B 的 LangGraph 与单 Agent / 多角色离线模拟切片已实现。真实模型适配、发送审批及费用控制仍待提供方信息后完成；真实语义审查尚未验收。不是完整第二阶段交付声明。配置占位见 [模型接入清单](../../model-setup.md)。

父设计：[系统设计](2026-11-02-reviewflow-design.md)。CLI 体验见[命令设计](2026-11-02-cli-experience-design.md)。

## 1. 阶段边界

完整第二阶段目标：真实模型驱动的 Planner → 独立 Correctness/Security Reviewer → 语义 Verifier → 结构化报告，并具备相同输入/模型配置的单 Agent 基线。LangGraph 承载有界状态流，确定性代码控制范围、计数、权限和最终报告。

依赖关系拆成可独立验证的两个里程碑：

- **2A：协议与可信证据上下文**。Pydantic schema、来源与行号校验、只读上下文访问；沿用盘点覆盖统计，语义审查覆盖协议留到 2B，分配任务/读取片段不算完成审查；全部离线。不创建模型客户端，不安装目标依赖，不执行目标。
- **2B：真实模型适配与 LangGraph 流程**。在提供方选定后实现 SDK 适配器及离线假响应测试，再经预算和上传审批执行小样例真实验收。真实调用前仍需用户同意；模拟响应只证明流程可运行，不能证明审查准确率。

未选择提供方不应阻塞 CLI 和 2A，但不能据此擅自选定服务、使用当前编码助手的登录凭据或把 2A 宣称为第二阶段全部完成。

## 2. 2A 数据协议

新增可选依赖组 `review`，使用 Pydantic >=2,<3；基础文件盘点仍仅需标准库，不能因安装缺少 Pydantic 而无法运行。LangGraph 在 2B 加入该可选组。

新协议位于 `reviewflow.review` 包，不替换第一阶段 dataclass，不改变清单 JSON schema。

### 通用规则

- Pydantic 模型禁止未知字段，字段不自动强制转换，冻结模型，序列字段使用 tuple，JSON 使用 model_validate_json / model_dump_json 往返。
- 任务 ID / 问题 ID 匹配 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`；路径最长 4096 字符；snapshot_id / content_sha256 为 64 位小写十六进制。单对象证据引用不得重复，所有文本必须至少含一个非空白字符。
- 路径是规范化的 POSIX 相对路径；拒绝空、绝对路径、`.`、`..`、重复斜杠和 NUL；合法 Unicode 和换行文件名保留，由报告层转义。
- 行号从 1 开始，结束行不能小于起始行；字段字符串长度与集合数量有明确上限。
- 不保存原始内部推理链，只保存简短依据、触发条件、影响及证据。

### 模型

- `ReviewTask`：task_id、snapshot_id、role（correctness/security/general）、paths（1–20 个唯一允许路径）、objective（1–2000 字符）。general 供单 Agent 基线使用。
- `EvidenceRef`：snapshot_id、path、content_sha256、start_line、end_line。只引用真实来源，不允许模型填入“测试已通过”。
- `Finding`：finding_id、task_id、title（1–200）、root_cause / trigger / impact（各 1–4000）、severity（critical/high/medium/low）、evidence（1–20 条）。候选与判定分离，不能自填 accepted。
- `VerificationResult`：finding_id、verdict（accepted/rejected/unresolved）、rationale（1–4000）、evidence（最多 20 条）；accepted/rejected 必须有证据，unresolved 可无证据。此结果始终只是语义判定，不包含可任意写入的动态测试成功字段。

这些 schema 只保证格式；跨对象权限/关系必须由确定性 validator 检查，不能依赖 Pydantic 本身证明真实性。

## 3. 只读证据上下文

`ReviewContext(source, inventory)` 使用原有 SourceReader 和 Inventory，构造时检查来源 mode/root/commit 与原始清单匹配并重算 snapshot_id，防止把另一来源装入旧快照。

- 只接受清单中 `included` 的路径。被过滤的文件即使真实存在也拒绝访问。
- 每次读取通过 SourceReader.read_bytes，在文件大小界限内验证实际长度及 SHA-256 与清单一致，严格 UTF-8 / NUL 检查。变化则报错，不能更新旧快照后继续声称仍是原版本。
- `read_lines(path, start_line, end_line) -> str` 读取范围内完整原文，最多 200 行且最多 32768 UTF-8 字节。不静默截断；越界/超限明确报错。仅按 LF 划分行，保留 CRLF 内容，末尾 LF 不制造额外空行。
- `validate_task(task)` 验证快照和每个路径均属于纳入范围。
- `validate_finding(task, finding)` 首先调用 validate_task，再验证 task_id、每条证据属于任务路径、快照一致、内容哈希及行范围真实存在。
- `validate_verification(task, finding, result)` 首先调用 validate_finding，之后验证关联 finding_id 和所有证据在当前快照纳入范围；Verifier 可以引用任务之外的反证，但不能引用过滤/未知路径。无需调用者记忆验证顺序，不能省略候选的来源链。
- 不执行 import、eval、AST 编译、pytest 或任何目标 shell 命令，不读取目标 `.env`。内存中的源文本不进入 inventory/report 的元数据 schema。
- 单次文本范围限制不是整个多 Agent 运行总预算；2B 的调用预算、累计上下文和报告覆盖将另行控制。

## 4. 2B 工作流合约（待提供方确认后细化实施）

1. 快照和上下文建立后，Planner 只获得受控目录概况与按需片段，输出 ReviewTask。
2. 校验计划的路径、任务数量和预算。非法路径/结构化输出不进入 Reviewer。
3. Correctness 与 Security 首轮输入仅包含自身任务及相关上下文；不能读取对方候选问题。
4. 收集候选并验证每条证据引用；保留来源，不能通过投票增加可信度。跨角色根因去重需保留原候选关联，宁可暂不合并也不误合并不同问题。
5. Verifier 获得候选、原文与允许范围内的反证上下文；输出 accepted/rejected/unresolved。不接受模型自报工具退出码作为真实执行证据。
6. 证据不足最多一次补充，然后 unresolved。模型失败、结构错误、超时、预算耗尽分别保留状态，不能当作零问题成功。
7. 汇总确定性报告并结束本阶段：无修复入口、无写文件工具、无 Git 写操作。
8. 单 Agent 基线复用同一快照、模型配置、上下文权限和报告 schema；没有 Verifier 的结果明确为未独立验证，不能直接记成 accepted。

## 5. 真实模型接入前需明确

- 提供方与 API 接口类型；模型由用户选择，必要时依据当前官方文档推荐，不使用猜测的 SDK 接口。
- 凭据由本地环境/正式配置提供，不能让用户贴进聊天、提交源码或写入目标仓库。
- 哪些项目/文件允许发送；源代码中仍可能含文件名规则无法识别的秘密。
- 单次调用、单次运行预算和超时；SDK 自动重试必须计入预算设计。
- 禁止未经授权的追踪上传，包括 LangSmith 环境变量触发的隐式遥测；在离线测试中验证没有网络副作用。
- 真正的发送审批绑定本次快照、提供方、模型和范围；默认 `review` 不能因环境里偶然已有 Key 而自动上传代码。

## 6. 验收与声明

2A 验收包括：合法对象 JSON 往返、未知字段/非法行号/非规范路径/重复任务路径拒绝、伪造快照和哈希拒绝、跨任务引用拒绝、过滤路径读取拒绝、读取后内容变化拒绝、合法 Unicode 名称及 CRLF 行号一致、Git HEAD 与 dirty 工作树分离、整个测试不执行目标代码。

2B 验收必须分别报告离线流程测试和真实模型测试。没有真实模型测试时必须明确写“真实审查能力尚未验收”。不在这一阶段承诺修复有效性或课程实验结果。
