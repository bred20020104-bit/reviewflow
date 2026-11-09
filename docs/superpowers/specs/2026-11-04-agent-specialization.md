# Agent 职责与技能运行时设计

## 已确认目标

用户确认按照独立职责文件、专业技能清单、共享行为规范、实际运行时加载的建议继续开发，并明确最终版本必须体现各 Agent 的区别。

这不是增加角色名称，而是让真实请求采用不同的专业检查方法。保留现有 Python/LangGraph 编排、结构化消息、证据核验和授权边界。

## 本轮实现决策

- 资源随产品安装在 `src/reviewflow/agents/`。一份 `SOUL.md` 保存共享行为/证据规范，不模拟人格，不赋予权限。
- `roles/{planner,correctness,security,verifier,patch,general}/AGENT.md` 定义职责、输入/输出、非目标。general只用于single基线模式，不冒充两个独立Reviewer。
- `skills/{planning,correctness,security,verification,patching}/SKILL.md` 提供具体方法。general复用正确性和安全两份技能；其余角色绑定各自技能。
- 角色到资源的映射是产品内固定白名单，不由模型、目标仓库、环境变量或任意路径参数决定。不加载目标的AGENT.md/SOUL.md为系统指令。
- 每次运行加载一份不可变资源快照；使用importlib.resources，确保wheel安装后可用。缺失、空白、非UTF-8或超限资源失败关闭，无静默通用提示词回退。
- 实际system消息由共享规范+对应AGENT.md+绑定SKILL.md组合。reviewer按已校验task.role选择correctness/security/general；verifier始终使用独立核验配置，而不是沿用原Reviewer角色。
- 预览展示角色、技能及内容摘要；prompt_digest和approval_digest绑定资源快照。同一次已批准运行复用同一快照，不能在审批后重新读取被改动的角色文件。
- Patch配置接入已有可信离线propose_patch请求构造器；不新增真实模型自动修复入口，不放宽修复路径或授权。
- 不改RunReport/模拟session持久化结构。真实预览/报告通过preview携带资源manifest。

## 专业差异

Planner做范围/职责分配和同根因分组建议；Correctness按触发→状态/数值/边界→错误结果追踪；Security按不可信输入→信任边界→敏感操作追踪并检查可利用前提；Verifier独立寻找反证、区分证据事实与推测；Patch仅围绕单个选定finding生成最小候选修改。

## 不变约束

不执行目标代码，不读取目标配置为指令；不自动调用真实API，不改私有model配置；64KiB请求正文、2048输出tokens、60秒、零自动重试和32KiB上下文保持不变。技能只指导推理，权限/预算/证据/补丁范围仍由代码控制。无前端、commit、push或发布。

## 验收

测试实际请求中两位Reviewer的system内容不同且分别包含对应方法；Verifier不继承Reviewer指令；Patch离线构造器使用自己的配置且仍验证补丁。目标同名指令文件不能替换系统配置。资源变化使旧审批无效，运行中变化不改变已加载快照。完整测试、实际CLI+本地HTTP合成服务、wheel资源加载验证；明确这些不等于真实模型准确率或自动修复验收。
