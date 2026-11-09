# Agent 职责与专业技能

更新：2026-11-04。本功能是 ReviewFlow 产品自己的 Agent 配置，不是开发助手的插件技能。角色文件与技能文件已经接入实际请求生成，**不是仅供展示的 Markdown**。

## 文件位置

本机源码根目录（可复制）：

`/Users/bred/PycharmProjects/project/reviewflow/src/reviewflow/agents/`

```text
agents/
├── SOUL.md                         # 所有角色共享：证据、诚实、安全、权限边界
├── profiles.py                     # 固定绑定、加载、大小校验、不可变快照、摘要
├── roles/
│   ├── planner/AGENT.md
│   ├── correctness/AGENT.md
│   ├── security/AGENT.md
│   ├── verifier/AGENT.md
│   ├── patch/AGENT.md
│   └── general/AGENT.md
└── skills/
    ├── planning/SKILL.md
    ├── correctness/SKILL.md
    ├── security/SKILL.md
    ├── verification/SKILL.md
    └── patching/SKILL.md
```

只有一份共享 `SOUL.md`，不为每个角色制造不同的“人格”。专业差异放在各自职责和检查方法中。

## 角色之间的实际区别

| 角色 | 固定技能 | 专业重点 | 不负责什么 |
|---|---|---|---|
| Planner | planning | 覆盖规划、正确性/安全性互补任务；候选同根因分组建议 | 不预先告诉 Reviewer 应发现什么，不代替 Verifier 判定 |
| Correctness Reviewer | correctness | 输入→分支→输出、状态迁移、边界值、异常和资源生命周期 | 不做风格评分，不把“缺少测试”单独当作功能缺陷 |
| Security Reviewer | security | 攻击者能力、输入来源、信任边界、敏感操作及有效防护 | 不因出现危险函数就断言可利用，不执行攻击 |
| Independent Verifier | verification | 独立重建触发链，主动找反证，accepted/rejected/unresolved | 不随提出问题的 task.role 切换身份，不把静态分析说成测试通过 |
| Patch Agent | patching | 针对一个选定问题，最小修改已有、证据引用的普通 Python 文件 | 不扩大修复范围，不修改测试/配置/依赖/构建/迁移，不自行应用 |
| General Reviewer | correctness + security | `--mode single` 的综合单 Agent 基线 | 不是两个独立 Reviewer，也没有独立复核背书 |

两个 Reviewer 的输入保持隔离；候选在各自审查之后才汇集。`multi-no-verifier` 使用两个专业 Reviewer，但跳过独立复核。角色分工不要求不同供应商或不同模型：当前继续使用同一个私有模型配置，主要区别是系统指令、技能、输入和图中的职责。

## 运行时如何使用

每个新审查加载产品包内资源，组成不可变 `PromptBundle`。每条请求的系统消息为：

```text
共享 SOUL.md + 当前角色 AGENT.md + 该角色固定绑定的 SKILL.md
```

- `reviewer` 根据已校验任务的 correctness/security/general 选择配置。
- `planner`（包括去重建议阶段）、`verifier`、`patch` 由阶段选择，不被 task.role 覆盖。
- 模拟图与真实开发图都使用此组装逻辑。但模拟后端返回预设响应，**不能证明真实模型遵循了技能或准确率提高**。
- Patch 配置接入离线 `propose_patch` 和真实 `propose_live_patch`；后者用精确编辑 schema。`repair-run` 已接通补丁→Docker测试→新复审→受保护写回，实际 CLI＋Docker＋合成 HTTP 接线已验证，用户真实模型修复效果仍待独立验收，不能只因角色文件存在就声称修复成功。
- `importlib.resources` 从安装的产品包读取，wheel 包含全部 Markdown，不依赖当前工作目录。

## 修改与重新批准

在可信 ReviewFlow 安装中维护这些文件；可编辑安装对应上面的源码路径，wheel 安装应更新产品包，不要修改目标项目来“配置”Agent。

真实开发预览中的 `agent_profiles` 包含共享文件、职责文件、技能文件的相对路径及 SHA-256、技能 ID 和 `bundle_digest`。`prompt_digest` 绑定该 manifest 和检索工具说明，随后一起绑定 `approval_digest`；报告 JSON 保留在 `preview.agent_profiles`。

1. 修改任一已打包职责/技能/共享文件后，应重新预览和批准。即使修改的是本次不会调用的角色，也会改变整组摘要。
2. 一轮已批准运行使用预览时加载的同一份不可变快照；中途改文件不会替换该轮系统消息。下一轮加载新内容。
3. 缺失、空白、非法 UTF-8、NUL 或超限资源拒绝加载，不退回通用提示。单资源最多8192字节，组装后的角色系统消息最多16384字节。CLI 仅输出安全的输入/配置拒绝信息，不泄漏文件内容。
4. 文件合法不意味着提示内容合理；维护者仍需审查变更。摘要绑定用于批准一致性，不是对本机产品安装的防篡改签名。

## 安全与预算不靠提示词保证

目标仓库中的 `AGENT.md`、`SOUL.md`、`SKILL.md` 即使被纳入源码上下文，也只是**不可信数据**，不会被发现或加载为系统配置。角色与技能的绑定由产品代码固定，不接受模型动态选择路径或目标仓库覆盖。

权限、路径/快照/证据校验、只读检索、预算、输出 schema、修复范围和 Git 门禁仍由代码执行。Markdown 不能新增 shell、写文件、网络或测试权限。开发模式每个传输阶段继续限制最多8次适配器尝试、64KiB最终HTTP正文、2048输出tokens、每次60秒、零自动重试/重定向；`repair-run` 另有审批绑定的跨阶段1..48次总上限（不是默认已获授权）；更长系统消息也占正文预算，不会绕过上限。

## 验证范围

自动测试验证角色路由、输出请求中的专业内容、Verifier 身份、单 Agent 基线、离线 Patch、资源错误拒绝、摘要变化和运行中快照冻结；另用实际 `review` 子进程连接本地合成 Responses 服务检查传输内容，并离线构建 wheel 验证资源可用。

以上是产品接线、协议与安装验证，**不是新的真实模型验收**。模型是否更少漏报/误报，需要另行授权后，使用固定测试集与相同模型预算评估。
