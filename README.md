# ReviewFlow

面向上线前检查的多智能体仓库审查与修复建议系统，COMP5584 小组项目。

> **当前状态（2026-11-09）：可运行的 CLI 后端开发版，尚未完成真实目标的完整自动修复验收，不是仅剩前端即可上线。**
> 已实现仓库盘点、LangGraph 多角色审查、证据校验、显式审批、有界补丁、Docker 回归、独立终审和受保护写回。模型适配器是窄 Responses-compatible HTTP，不是供应商 SDK。

## 项目交接入口

**接手请先读：[完整项目交接说明](docs/handoff-2026-11-09.md)**。包含开发进度、源码位置、已知 bug/风险、卡点、真实验收历史、安装配置、命令、报告判读、保护约束和接手清单。当前状态以该说明为准；早期设计与计划文件保留历史语境，不代表已实现或已验收。

| 项目 | 当前结论 |
|---|---|
| 重试版本首次完整回归 | **1243项通过，Ruff通过，154个源码/角色/测试文件前后哈希一致**；耗时16分45秒 |
| 用户要求的最后一次独立复测 | **已完成：1243项通过（16分59秒），Ruff、CLI帮助/离线demo通过，154文件哈希一致**；独立日志目录`/tmp/reviewflow-final-product-w0pauvrb` |
| 真实目标验收 | 仍未成功；第19轮在第6次模型请求收到HTTP408，停在初审，无补丁/写回 |
| 本轮新增 | repair-run默认有限超时重试2次；显式`--no-call-limit`可取消总调用数上限，其他限制保留 |
| 当前主要卡点 | 真实模型服务稳定性、真实修复闭环、新机器可复现镜像/发布基线 |
| GitHub交接基线 | 私有仓库[reviewflow](https://github.com/bred20020104-bit/reviewflow)，经用户确认使用`main`；首个产品提交`3246c15`，含源码/测试/文档/示例。私有配置、模型Key和验收产物不随源码交付 |

**三种验证不能混淆：** 产品 pytest 通过 ≠ demo 检出真实缺陷 ≠ 真实目标测试与终审通过。当前没有未解决的产品回归失败记录，但不意味着不存在未知 bug。

模型配置只需在私有 `secrets.local.toml` 中填写 `provider/api_type/model/base_url/api_key` 五项，`config.local.toml` 引用该文件。模板可交付，真实密钥不可进入 Git/ZIP/报告。填 Key 不等于批准上传或验证了服务兼容性。真实开发模式费用未知，不能套用正式费用模式的美元预算保证。

详细参考：[模型配置](docs/model-setup.md) · [后端命令](docs/backend-guide.md) · [Agent配置](docs/agent-profiles.md) · [重试实现与验证](docs/superpowers/plans/2026-11-09-bounded-transport-retry.md)。

## 日常操作

一次性安装并配置 PATH 后，在待检查项目目录执行：

```bash
review                           # 当前目录，简洁摘要
review -d /path/to/project        # 指定项目（也可 --directory）
review --source directory        # 当前文件，包括未提交修改
review --verbose                 # 完整文件清单
review --format json             # 机器报告
review --help
```

默认 auto 在 Git 仓库中读取**已提交 HEAD**，不是当前修改；尚无首次提交时使用 `--source directory`。以上现在是文件盘点，**不代表代码审查通过**。

完整安装、PATH、故障排查与安全说明见 **[用户指南](docs/user-guide.md)**。本次开发只创建了项目 `.venv/bin/review`，尚未安装用户级命令或修改 shell 配置。

### 离线多 Agent 流程演示

在可信 ReviewFlow 工具目录安装 `'.[review]'` 后执行：

```bash
# 从工具根目录运行；样例只被读取、不执行
.venv/bin/review --demo -d examples/demo-project --source directory
.venv/bin/review --demo --mode single -d examples/demo-project --source directory
.venv/bin/review --demo -d examples/demo-project --source directory --format json
```

`--demo` 使用固定标记和预设响应展示候选→复核→报告，**不具备真实缺陷检测能力**。默认 `review` 仍是盘点；真实开发审查必须显式提供 `--review --development --config ... --output ...`，先预览，再追加 `--approve 摘要` 才发送代码。demo 和真实开发流程的 partial/failed 均退出 `1`；真实结果保存在目标之外。完整步骤与限制见 [模型接入指南](docs/model-setup.md)。

### 不需要 Key 的新增后端命令

```bash
# 保存模拟审查会话（输出目录须不存在，位于目标之外）
.venv/bin/review session-start --demo -d examples/demo-project \
  --source directory --output /tmp/reviewflow-session-new
.venv/bin/review session-show -d examples/demo-project \
  --source directory --session /tmp/reviewflow-session-new

# 后续重新审查，追加报告版本；不是恢复旧权限
.venv/bin/review session-resume --demo -d examples/demo-project \
  --source directory --session /tmp/reviewflow-session-new

# 只读长文件分片索引，不发送代码
.venv/bin/review context -d examples/demo-project --source directory

# 创建全新的自有样例、独立修复 worktree；不执行目标测试，不提交修复
.venv/bin/review repair-demo --output /tmp/reviewflow-repair-demo-new

# 10个开发案例的离线实验彩排，不是模型准确率实验
.venv/bin/review benchmark-demo --corpus benchmarks/corpus --split dev --mode multi

# 只填写 secrets.local.toml 的模型五项；不需要手填预算/隐私
# 私有文件缺失或模型元数据仍为 TODO 时退出2
.venv/bin/review config-check --config config.local.toml
```

真实仓库补丁使用 `repair-preview` → 检查 diff → `repair-apply --approve 摘要`；仅生成未验证候选，不能直接提交。完整参数见 [后端指南](docs/backend-guide.md)。[实验协议](docs/evaluation.md) 与 [演示/交付材料框架](docs/course-delivery.md) 已提供。

### 有界自动修复（先预览，不会立即修改）

```bash
.venv/bin/review repair-run --development --config /absolute/tool/config.local.toml \
  -d /absolute/target --include package/module.py \
  --image 'sha256:<本机已准备镜像的64位摘要>' \
  --output /absolute/new-report --max-calls 48
# 检查输出清单、范围、镜像、费用风险及写回政策后，对同一命令追加 --approve <approval_digest>
```

批准覆盖本轮可能发生的选定源码写回；不包括 commit/push。默认总共最多48次模型请求，每个传输阶段最多8次，更小恢复阶段限额仍优先；有限模式在首次补丁前按语义批次预留终审额度。补丁后通过新证据重新审查和独立复核更新问题状态，不猜测行号、不直接改旧哈希、不按标题跳过问题。最终全库测试、两类Reviewer及Verifier终审均达到门禁才写回。

- **传输重试默认开启：** `--transport-retries 2`，仅HTTP408、response_timeout、worker_timeout，最多两次额外尝试，等待1秒/2秒；`0`关闭。每次实际请求独立计数，重试成功不消除先前未知用量。证据纠错仅1次请求的阶段不能重试；依赖恢复的3次限额包含重试。
- **取消总调用数上限：** 用`--no-call-limit`替换`--max-calls 48`（互斥），只支持新HEAD修复，不与seed/tests-only组合。不是无限修复；阶段限额、60秒单次超时、补丁/恢复次数、1小时有效期及最终门禁仍在。
- **候选导入：** `--seed-report /absolute/old/report.json`只导入符合条件的未验证候选源码，需要新审批与完整重测，不恢复旧权限；含未知用量的历史不可导入。
- **调度与测试：** 可显式使用`--patch-scheduling grouped`、`--test-timeout 180`；默认沙箱strict，只有明确授权临时执行权限才选`--test-sandbox exec-scratch`。

只测试HEAD加候选，不把原dirty内容纳入验证，也不覆盖它。报告目录及同名`-candidate`目录必须在目标之外且原先不存在。保留`report.json`、`report.md`、`candidate.diff`；修复退出0表示`applied/no_changes`，退出1表示阻塞或部分应用，退出2表示输入/审批/报告输出拒绝。**修改上述配置后必须重新预览并批准新摘要。**

### 只测试，不调用模型或写回

```bash
.venv/bin/review repair-run --development --tests-only \
  --config /absolute/tool/config.local.toml \
  -d /absolute/target --include package/module.py \
  --image 'sha256:<本机已准备镜像的64位摘要>' \
  --test-timeout 180 --output /absolute/new-test-report
# 检查预览后，同一命令追加 --approve <approval_digest>
```

此模式的审批绑定 `tests_only=true`、`max_calls=0`，不构造模型适配器、不审查、不生成补丁、不写回；仍创建并保留 HEAD 候选。沿用可信配置文件取得配置元数据，不发送凭据或源码。测试模式审批不能拿来启动修复模式。

测试清单由已提交文件静态生成，预览中的 `test_policy.test_projects` 明示所有测试文件及分组。每个子项目在独立 Python 进程和对应工作目录运行，项目目录优先、仓库根目录次之，避免同名模块串用。使用默认 `test_*.py` / `*_test.py` 文件名，不执行项目配置生成命令，不因某组失败省略其他组；全组都未执行测试才按“无测试”阻断。不是任意语言/构建系统的通用适配器。

`--test-timeout` 是所有测试组共用的容器期限，默认60秒、范围1..300秒，并绑定审批；不是每组重新获得一份期限。禁网、非root、只读导出、资源限制和 `/tmp` 的noexec保持不变。需要临时执行文件或其他运行环境的测试可能失败，不能把失败自动归因于业务bug。

仅测试模式退出0为 `tests_passed`，退出1为 `tests_failed` 或 `blocked`；JSON的 `test_runs[].diagnostic_code` 区分测试失败、收集/setup/teardown错误、无执行、超时及清理失败。不保留原始日志、数量或秘密异常文本，测试通过也不是模型修复成功。

## 项目目标

用户指定一个本地 Python 项目，无需粘贴全部源码，也无需提供 `base/head`。系统识别项目结构、规划检查任务、发现并验证问题，先输出审查报告；用户选择问题并批准后，系统才在独立工作区生成和验证补丁。

本系统辅助上线前检查，不提供“无漏洞”或“可以安全上线”的保证。

## 原始产品目标流程（非当前CLI逐步交互承诺）

以下包含尚未交付的交互选择与发布目标。当前`repair-run`采用一次完整预览审批后自动运行有界修复，不提供逐finding人工选择界面，也不自动commit/push。Verifier以静态复核为主，不能把第4步理解为已实现逐问题动态复现。实际操作以[交接说明](docs/handoff-2026-11-09.md)为准。

1. 检查输入、识别项目并固定审查快照，说明未提交修改的处理方式。
2. Supervisor / Planner 按模块和关联调用路径拆分任务。
3. Correctness Reviewer 与 Security Reviewer 独立检查。
4. Verifier 查找反证，并在条件允许时通过受限工具复现问题。
5. 输出问题、证据、未解决事项与覆盖情况，暂停等待用户选择。
6. 用户批准修复后，Patch Agent 在独立 worktree 中尝试最小修改。
7. 验证目标问题、回归测试及受影响代码；每项修复最多两次补丁尝试。
8. 展示补丁和验证报告。用户另行批准后才本地提交，再另行批准后才推送修复分支。

## 五个 Agent

各角色现已使用独立 `AGENT.md` 和专业 `SKILL.md`，共享一份 `SOUL.md`；文件内容实际组成系统请求，不再仅以任务角色名区分两个 Reviewer。另有 `general` 综合单 Agent 基线。预览/审批绑定全部配置摘要，同一轮运行固定提示快照。位置、职责差异与编辑方法见 [Agent 配置指南](docs/agent-profiles.md)。**Patch 配置同时服务离线整文件协议和 `repair-run` 的真实精确编辑请求；用户目标修复效果仍待单独验收。** 下表是各角色的总体设计职责，当前 Verifier 仍只做静态复核、不执行目标测试。

| 角色 | 责任 |
| --- | --- |
| Supervisor / Planner | 理解目标、拆分仓库检查任务、跟踪覆盖情况、在复杂失败时重新规划 |
| Correctness Reviewer | 查找逻辑错误、边界条件和异常处理问题 |
| Security Reviewer | 检查输入、危险操作和权限边界 |
| Verifier | 独立复核候选问题与补丁，保存反证和真实工具结果 |
| Patch Agent | 只针对获批问题生成最小补丁，消费验证反馈 |

LangGraph 负责当前审查流程；权限、预算、Git 操作与报告状态由确定性程序控制，不由 LLM 自由裁决。`session-*` 支持跨进程查看报告及重新完整模拟审查，不恢复中断图节点、实时计费或权限。审计文件不是授权凭据。旧 CandidateChain 仍仅为未验证的进程内组合服务；新 `repair-run` 使用独立的候选与应用权限，不伪造旧发布凭据。

## 关键约束

- 默认进行仓库级检查，不要求用户提供 diff。
- 首版聚焦小型 Python 项目和正确性、安全性问题。
- Git 仓库默认检查已提交的 `HEAD`；有未提交修改时必须明确说明未包含的内容。
- 普通目录可进行只读审查；首版不为非 Git 项目自动初始化 Git 或应用补丁。
- “语义复核通过”不等于“测试复现成功”。环境阻塞、超时和未覆盖范围必须如实报告。
- 不覆盖用户已有修改；仅获批 `repair-run` 可在门禁通过后写回选定源码，不自动 stash 或提交。
- 修复、commit、push 分开授权；审批绑定具体版本与补丁。
- 不自动合并、不强推、不直接推送主分支。
- 仓库内容属于不可信数据；执行测试需要隔离，上传代码到模型服务需要用户知情授权。

## 当前技术栈与后续方向

- Python >=3.11；本机验证使用Python3.12，POSIX/macOS/Linux为当前支持范围。
- LangGraph1：实际审查编排；审批、预算、Git/Docker操作由确定性控制器执行，没有持久化图节点恢复。
- Pydantic2：结构化协议与证据校验。
- pytest、Ruff：产品测试与静态检查；不把未执行的其他分析工具列为验证通过项。
- Docker：已经用于目标测试隔离，仅使用预先准备的可信固定镜像，不自动拉取/安装；不保证安全运行任意恶意代码。
- Git worktree：候选工作区隔离。
- Streamlit/其他前端：尚未实现，不是当前交接的运行前提。

模型由私有配置明确指定，基线与多 Agent 实验应固定同一配置。已批准的真实开发审查会产生第三方 API 用量和可能的费用；默认盘点、demo、未批准的预览不会联网。当前适配器是标准库实现的窄 Responses HTTP 协议，不是供应商 SDK。

## 开发文档

- [完整系统设计](docs/superpowers/specs/2026-11-02-reviewflow-design.md)
- [第一阶段规格：只读仓库清单与覆盖报告](docs/superpowers/specs/2026-11-02-repository-intake-design.md)
- [第一阶段实施计划](docs/superpowers/plans/2026-11-02-repository-intake.md)
- [用户指南](docs/user-guide.md)
- [简洁命令详细设计](docs/superpowers/specs/2026-11-02-cli-experience-design.md)
- [第二阶段结构化审查设计（2A / 2B）](docs/superpowers/specs/2026-11-02-structured-review-design.md)
- [CLI 与 2A 实施计划](docs/superpowers/plans/2026-11-02-cli-and-review-foundation.md)
- [协作与提交约定](CONTRIBUTING.md)

以上第一阶段文档描述的是早期盘点功能，不是当前全产品能力上限。默认盘点的输出仍不能被描述为完整代码审查结果；当前修复功能与未完成事项见交接说明。

## 本地状态与运行说明

### 安装与运行

需要 Python >= 3.11。当前在 macOS / Python 3.12.16 / Git 2.50.1 上验证；目录读取及短命令启动器依赖 POSIX，暂不支持 Windows。Git 模式建议 Git 2.50.1 或更新版本；旧版本未验证。基础盘点无第三方运行时依赖；2A 使用可选 Pydantic 2。不需要 API Key 或 Docker。依赖安装可能需要网络，盘点和 2A 校验离线运行。

在 **ReviewFlow 自身目录**（不要切换到不可信目标目录）执行：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .

# 可选：开发 / 全量测试 / 2A 协议
.venv/bin/python -m pip install -e '.[dev,review]'

# 用户自行选择执行的一次性短命令安装（不会修改 shell 配置）
.venv/bin/python -I -m reviewflow.install --bin-dir "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"
command -v review
review --help

# 此后从任意目录使用；无需每天激活 venv
review -d /absolute/path/to/project
review -d /absolute/path/to/project --source directory --format json
```

新 `review` 启动器在 Python 启动前启用 `-I`，避免当前目录 / `PYTHONPATH` 中的同名模块或 `sitecustomize.py` 被导入。旧 `reviewflow inspect PATH` 保留兼容，但普通 console script 不具备这项启动隔离，不推荐从不可信目标目录直接运行；诊断时可使用可信解释器绝对路径加 `-I -m reviewflow`。盘点路径不导入目标模块、不安装目标依赖、不运行目标测试；显式批准的`repair-run`则在Docker中执行目标测试，不能在宿主机代跑。

安装器拒绝覆盖已有 `review`；项目移动或虚拟环境重建后需检查旧启动器并重新安装。安装/PATH 只需配置一次，更多细节见用户指南。

本机 Homebrew Python 存在 `pyexpat` 加载系统旧版 libexpat 的缺失符号问题。如果遇到同类错误，可在当前终端临时设置后再运行上述命令：

```bash
export DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib
```

该修复已在本机验证，不是其他机器的通用要求，也没有写入应用代码；不需要关闭 TLS 校验。

### 输出与限制

- `--source auto` 是默认值；Git 工作树读取固定 `HEAD`，并披露 dirty 修改未纳入。Git 子目录会扩展到仓库根目录并警告。空 Git 仓库不会静默降级。
- 新 `review` 默认纯文本摘要；`--verbose` 展开清单。旧 `reviewflow inspect` 默认 Markdown；`--format markdown` / `--format json` 总是完整报告。JSON 输出 `snapshot` / `coverage` / `notice`，只含元数据、哈希，不含源码。
- 开发辅助目录 `.superpowers`、`.claude`、`.eggs`、`*.egg-info` 已排除；过滤不是完整 `.gitignore` 解释器。
- 默认最多纳入 200 文件、单文件 262144 字节、总纳入 8388608 字节。按路径排序；超预算条目保留跳过原因。
- 跳过敏感路径（包括 `.env.example`）、不支持类型、软链接、子模块、二进制与非 UTF-8 文件。剪枝目录内部的文件数量未知，不计作“已审查”。路径规则不能保证识别所有秘密。
- 快照 ID 不包含绝对根路径、时间、分支或 dirty 状态；它不是完整仓库内容的证明，跳过文件内容不参与哈希。
- 限制约束纳入内容，不是整个目录枚举或 Git 元数据的全局内存/时间上限；避免扫描巨大或持续变化的目录。普通目录不是原子文件系统快照。
- `git_repair_eligible` 只描述输入来源是否满足 Git 修复前提，不表示已获批准、通过验证或满足全部 worktree 限制。
- stdout 仅报告，stderr 为错误；退出码 `0` 表示清单完成（可有跳过），`2` 表示无效输入，`1` 表示采集/工具失败。不会自动写报告文件。
- 每份报告包含：`Inventory only: no semantic code review or target tests have been performed.`

### 已知待办

- Markdown 渲染器可能把 `__init__.py` / `_private_.py` 的下划线解释成强调标记，导致渲染后文件名显示不完整；需要精确文件名时使用默认 text 或 JSON。该显示问题尚未修复。
- 输入根目录的路径解析若遭遇权限拒绝，当前可能返回 `2`（无效输入）而非约定的 `1`（采集失败）；不会输出成功报告。
- 未安装 Git 时只能用目录标记保守识别 Git 输入；普通目录若同时含 `HEAD`、`objects/`、`refs/` 可能被拒绝，可显式使用 `--source directory`。

### 开发验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
# 可选的独立格式检查（不在已报告通过的门禁中，不能据此声称全库格式已合规）：
# .venv/bin/python -m ruff format --check src tests
```

测试使用自建临时目录和 Git fixture，覆盖输入未变、敏感内容不读取、软链接替换、固定 HEAD、Git 配置执行拦截、预算与 CLI 等行为。测试中的提交仅发生在临时 fixture，不提交开发仓库。

本地开发位置：`/Users/bred/PycharmProjects/project/reviewflow`，不是程序硬编码路径。

## 课程交付

根据课程 PDF：提交源代码及运行说明、不超过 10 分钟的 Demo 视频、Slides、个人贡献说明。截止时间为 **2026-11-23 23:59**，具体上传要求以 Canvas 为准。

评估计划包括单 Agent 基线、多 Agent 无验证版本、完整系统及无修复重试版本。分析发现准确率、误报、修复有效性、成本和覆盖限制，不预先承诺多 Agent 一定更好。

## 参考与许可

项目设计参考资料见[设计文档](docs/superpowers/specs/2026-11-02-reviewflow-design.md#参考项目)。当前没有复制参考项目的实现代码，也尚未选择项目对外许可证。

不要提交 API Key、真实 `.env`、目标仓库副本、临时 worktree 或含敏感代码的运行日志。只有经过脱敏与授权的实验材料才适合纳入版本控制。
