# 后端使用与验收边界

更新：2026-11-06。新增显式有界的自动修复 CLI；已完成真实 Docker 与合成 HTTP 模型的实际 CLI 接线验证，用户目标真实模型修复仍待独立审批及验收。前端仍未实现。此文区分**可运行命令、后端服务、尚未实机验收的边界**，不把离线测试当成真实模型或容器安全认证。

## 1. 功能地图

| 能力 | 当前状态 | 入口 |
| --- | --- | --- |
| 配置检查、源码发送范围预览 | 简化模式读取私有模型文件；不打印/发送 Key、不联网 | `config-check` / `prepare` |
| 一次性、过期、绑定操作的授权 | 服务层已实现，不能从 JSON 恢复权限 | `backend.approval.ApprovalGate` |
| 调用前费用预留、调用后结算 | 服务层及假传输测试完成；尚无提供方 SDK | `backend.transport.GuardedTransport` |
| 长文件分片、字面量搜索 | 可运行的本地证据索引 | `context` |
| 单 Agent / 多 Agent / 无 Verifier 消融 | LangGraph 离线模拟 | `--demo --mode ...` |
| Patch Agent 请求与输出校验 | 离线整文件接口保留；新真实通道使用哈希绑定的精确编辑 | `backend.patch_agent.propose_patch` / `backend.live_patch.propose_live_patch` |
| 补丁预览、批准后在 worktree 应用 | 可运行；原工作区不变 | `repair-preview` / `repair-apply` |
| 两次尝试、验证失败恢复、审计 | 服务层；未知用户修改阻止恢复 | `backend.repair_loop.run_repair` |
| 固定 Docker 验证 | 新 volume runner 已实机验证，禁网/非root/只读及资源边界通过自有合成检查 | `backend.volume_runner.VolumeDockerRunner` |
| 真实自动修复控制器 | 实际 CLI + Docker + 合成 HTTP 接线验证通过；真实用户目标效果待验收 | `repair-run --development` |
| 最终验证、commit/push 分别授权 | 服务层；临时仓库 commit 测试通过；未做远程 push | `ValidationAuthority` / `Publisher` |
| 30 个评估样例、离线实验彩排 | 可运行；不输出模型准确率 | `benchmark-demo` |
| 人工判定结果评分 | 可运行；输入真实性由实验者负责 | `evaluate` |
| 白名单源码 ZIP | 可运行；不覆盖旧文件、不上传 | `package` |

本轮新增能力：

| 能力 | 当前状态 | 入口 |
| --- | --- | --- |
| 专用私有 TOML Key 文件 | owner-only 校验、脱敏 SecretStr；只检查格式不验证提供方 | `credentials-check --file` |
| 实际请求/证据绑定授权与预算 | 模拟通道及显式真实开发通道；开发模式只控次数/正文/输出，金额未知 | `backend.gateway.WorkflowGateway` |
| Responses 真实只读开发审查 | 默认预览；明确批准后运行现有 LangGraph，独立进程 HTTP 适配器 | `--review --development` |
| read/search 有界检索、Verifier 一次补充 | LangGraph 新交互流程；Reviewer 最多2轮，每轮最多3请求 | `review.interactive.run_interactive_review` |
| Supervisor 重复候选分组建议 | 仅建议；保留全部来源和独立验证，不证明同根因 | 同上 |
| 保存/查看/重新完整模拟审查/问题导出 | 可运行；磁盘报告不是权限凭据 | `session-start/show/resume/export` |
| 顺序应用多问题候选、逐步回退 | 进程内服务；每步单独批准，不验证、不发布 | `backend.candidate_chain.CandidateChain` |

**不是已完成的能力：** 正式计费模式（可信单价/完整 token 上界）、供应商 SDK（当前采用窄 HTTP 协议）、模型准确率与语义去重效果实验、精确图节点恢复/跨进程审批与实时计费恢复、任意仓库沙箱认证、用户真实目标的组合修复效果验收和前端。commit/push 的进一步集成也按用户要求延后。真实开发调用不等于这些能力已验收。

### 真实开发模式边界

用法见 [模型配置指南](model-setup.md)。开发模式固定8次请求、最终请求正文65,536字节、输出上限2,048 tokens、60秒父进程总超时，不重试、不重定向。`DevelopmentTransport` 独立于 `GuardedTransport`：未知金额始终为 null，不能假装满足后者的美元预算。普通配置 HTTPS 和未知价格拒绝规则未放宽。

预览摘要绑定来源/快照/模型/endpoint/范围/限额/报告位置；批准一次有界运行后，每个 Gateway 请求仍检查证据并消费一次性子授权。新报告目录必须在目标外且不存在；报告不是续跑、修复或发布权限。网络在可信 `-I -m` 子进程执行，Key 只经内存管道传入；输入/输出大小有界，父进程超时终止并等待子进程。超时不保证服务端停止计费，未知用量会阻止后续请求。

## 2. 安装与配置

在可信工具根目录安装 `pip install -e '.[dev,review]'`；使用安全启动器 `.venv/bin/review`。不要在目标仓库运行安装脚本。macOS 本机 libexpat 处理沿用用户指南。

```bash
# 保留 TODO 的模板会明确报错，退出 2；不是联网失败
.venv/bin/review config-check --config config.example.toml

# 自行复制并填写可信工具目录的 config.local.toml 后
.venv/bin/review config-check --config /absolute/tool/config.local.toml
.venv/bin/review prepare -d /absolute/project --source directory \
  --config /absolute/tool/config.local.toml
```

推荐配置只含 `secrets_file = "secrets.local.toml"`。该私有文件顶层包含 provider/api_type/model/base_url/api_key 五项，示例见 `secrets.example.toml`。不要求用户手填 budget/privacy：默认$1/轮、$0.25/请求、12次请求、60秒超时、16000输入/4000输出 token、0重试。单价留作未配置，由未来适配器核对；未知价格使 GuardedTransport 在任何发送/批准消耗之前报 `pricing_unconfigured`，不会按免费处理。

简化模式的 `prepare` 从明确选定的来源生成 root 和实际纳入文件范围，上传/遥测仍为 false。它只输出快照、文件哈希、提供方、endpoint 和预算，不输出源码或 Key，不产生跨进程授权。`config-check/prepare` 会读取私有文件，报告 `credentials_read=true`；默认盘点/demo/session 不读取。配置文件和 dotenv 从不在目标目录自动发现。

兼容旧版完整配置：显式 root 与目标必须一致、allowed_paths 必须已纳入；这类非秘密配置检查不读取 Key。旧版费用/单价用十进制字符串，token/时间/次数用整数；endpoint 仍只接受无用户信息/query/fragment 的 HTTPS 地址，遥测关闭、重试为0。

发送服务的授权绑定完整请求摘要、快照、声明文件、root、提供方/模型/endpoint 和预算。调用前预留最大费用；失败或未知 usage 不退款并阻止后续调用；超出预留按已知 usage 记录并停止。**可信适配器必须提供完整请求的可靠 token 上界、执行超时/输出上限并关闭内部重试**。费用限制依赖正确价格和提供方计费语义，尚不是已验证的线上账单保证。

## 3. 长文件索引与流程消融

```bash
.venv/bin/review context -d /path/to/project --source directory
.venv/bin/review context -d /path/to/project --source directory --query "authenticate"
.venv/bin/review --demo --mode multi-no-verifier \
  -d examples/demo-project --source directory
```

`context` 索引将文件按 LF 拆成最多200行/32768字节的块，默认总131072字节/100块；超长单行和预算不足均保留 omission。搜索是字面量搜索，每次重新检查原文件；CLI 只输出证据元数据，不打印源码。它不改变旧 `--demo` 的小文件规则。

新 `session-start --demo` 使用最多8MiB/1000块索引、最多20初始文件、每角色累计131072字节源码上下文。Reviewer 最多2轮检索、Verifier 最多1轮补充；每轮最多3请求，所有后续模型调用也计入默认12次总上限。只有实际供给完整行范围才允许声明整文件覆盖。首轮 Reviewer 输入独立，Supervisor 分组不消除原始候选。

会话使用方法见 [用户指南第8节](user-guide.md#8-保存查看与重新运行离线会话)。仅新建外部目录，保存为权限600的 `run-NNNN.json`；加载校验来源、快照和证据。`resume --demo` 从头再审查、追加版本，**不是加载旧权限/执行证据**。计量使用模拟字节数/价格，不代表真实 token 或支出。

## 4. 不需要 Key 的完整补丁演示

```bash
# 指定不存在的目录。此命令会创建自有样例仓库、样例初始提交和修复 worktree。
# 不要把任何已有真实项目路径传给 --output。
.venv/bin/review repair-demo --output /tmp/reviewflow-demo-unique
```

目录中有：

- `project/`：自建原始样例，保持不变。
- `workspaces/demo/`：应用固定模拟补丁的独立分支。
- `workspaces/demo.audit.jsonl`：事件及补丁哈希，不包含授权令牌或原始源码。
- `task.json`、`finding.json`、`proposal.json`：协议样例，便于接入 Patch Agent。

明确返回 `execution=simulated`、`target_tests_executed=false`、`commit_ready=false`。没有执行样例 Python，没有真实模型判定，没有提交修复或推送。目标路径已存在则拒绝，不自动删除任何文件。

## 5. 对真实本地仓库预览和应用一个人工提供的补丁

此功能不需要 Key；并不意味着输入 finding 已经过真实模型验证。用户需检查具体内容并明确授权。

```bash
review repair-preview -d /path/to/git-project \
  --task /outside/task.json --finding /outside/finding.json \
  --proposal /outside/proposal.json \
  --workspace-parent /existing/trusted/workspaces --run-id fix-001

# 阅读 diff 与 creation 范围后，把输出 approval_digest 原样复制到 --approve
review repair-apply -d /path/to/git-project \
  --task /outside/task.json --finding /outside/finding.json \
  --proposal /outside/proposal.json \
  --workspace-parent /existing/trusted/workspaces --run-id fix-001 \
  --approve EXACT_PREVIEW_DIGEST
```

- 只接受有有效提交的 Git HEAD；原有 dirty/staged/untracked 文件不变。
- 当前要求整个 Git 树均为盘点可纳入的普通、非可执行文本文件（Git mode `100644`）。含可执行条目 `100755`、敏感排除、二进制、软链接、子模块、超限文件的仓库会阻止自动应用，而不是复制秘密或偷偷改变基线权限；后续手动增加执行权限也会使工作区完整性检查失败。
- workspace-parent 必须在目标仓库外，且已存在、可信；run-id 只接受小写字母/数字/短横线。
- 只允许替换候选证据引用的现有普通 `.py` 文件，最多5文件；不新增/删除/改名，不修改测试、配置、迁移、安装与启动定制文件。每文件最多65536 UTF-8字节，总131072字节。
- 快照、原内容哈希、补丁哈希和审批必须一致；预览后改变输入需重新预览/批准。
- worktree 通过 `--no-checkout` 创建，再受控写入已校验的 blob，避免 checkout filters/hooks 执行。
- `repair-apply` 结果始终是**未验证候选**。它没有 `--force-commit` 后门，也不会自动测试、提交或推送。

## 6. 自动修复 CLI 与旧发布服务

### `repair-run`：独立的工作区写回权限

```bash
review repair-run --development --config /absolute/tool/config.local.toml \
  -d /absolute/git-project --include package/module.py \
  --image 'sha256:<64位不可变镜像ID>' --output /absolute/new-report --max-calls 48
# 审查预览后，原命令追加 --approve <approval_digest>
```

- 默认仅预览，不联网、不创建 worktree、不执行测试。必须有有效 HEAD 和命名分支。
- 批准绑定原分支/HEAD/索引/所选源码与完整导出、模型配置、固定角色资源、镜像/执行权限、输出和全局调用上限。`--max-calls` 为1..48，默认48；不是已获付费许可。每阶段8次、正文65536字节、输出2048tokens、60秒，无自动网络重试。失败或未知用量不换 transport 继续发送。
- 模型只看选定源码；分批每批最多32768源码字节，单文件超限拒绝。容器可接收已提交普通UTF8文件（最多200/8MiB，单文件256KiB）；禁止真实凭据、二进制、软链接和子模块。`.env.example`/`.sh` 只作明确列出的本地测试输入，保留 Git 执行位，不隐式上传。
- 原HEAD先跑固定分项目pytest：按导出清单中的默认测试文件名分组，每组独立解释器/工作目录，项目目录及仓库根目录作为导入路径；所有发现文件恰好分配一次。组间模块状态隔离，不提前结束后续组。普通测试失败允许继续，收集/setup/teardown/内部错误、全部未执行或超时阻止继续。每个已被产品独立接受的问题最多两次Patch尝试；精确编辑只能改已有且被证据引用的非受保护.py，不能改测试/配置/依赖。
- 产品保留补丁后，对受影响且仍有待处理问题的整个批准批次重新运行 correctness/security 审查。每条旧问题必须明确判定 present/resolved/unresolved，并用新证据独立复核；不按标题去重，不直接改写旧哈希，不把未再次报告当作解决。新问题也要独立复核后入队。每轮最多8条问题、全程最多4轮刷新和8次Patch提案，且共用原全局模型预算；未知结论、遗漏、越权、上下文超限或预算耗尽均阻断。未受影响批次仍保留当前快照独立复核。外部修改不触发这种恢复，仍按所有权冲突停止。已有基线失败时可保留中间失败的临时候选，但最终所有可发现测试必须通过。
- 初审成功且有待修问题时，首个补丁前按每个语义批次8次保留终审调用；剩余额度无法同时容纳保留额和至少一次补丁请求时，在 `final_review_budget` 阶段停止。所有修复/刷新/依赖调用均不能消费保留额。只有待修队列已处理且最终全量测试通过后，控制器才释放保留额进入终审；不会增加总计48次上限，也不保证额外纠错/依赖调用一定够用。策略 `final_review_budget` 绑定审批；仅测试模式为null。计量中 `final_review_calls_held` 和 `final_review_started` 展示当前状态。
- 对最终同一候选重新运行两类Reviewer和Verifier；所选路径完整供给和覆盖、无错误/遗漏/未解决问题。批次报告仍可为partial，绝不改写为全仓库complete。
- 最后只写回选定且原本与HEAD一致的变更文件；branch/HEAD/index/父目录/所选文件冲突即停。逐文件替换不是跨外部编辑器的事务，请勿并发编辑这些文件。安全回滚不成立时返回partial_application并保留现场，不reset/stash。
- 测试对象是HEAD加候选补丁，不是原工作区所有dirty内容。没有单测数量/原始日志，也没有逐项动态复现证明。`commit_ready` 始终false。
- 报告及同名 `-candidate` 目录必须在目标之外且不存在。输出 JSON 含绝对报告路径；`report.md` 是摘要，`report.json` 是详细证据，`candidate.diff` 是候选差异。退出0为applied/no_changes，1为blocked/partial_application，2为输入/审批/输出失败；从不自动清理或重试付费请求。

### 零模型测试与安全诊断（2026-11-06）

在上述命令添加 `--tests-only`，预览 `max_calls` 固定为0；批准只运行HEAD基线并保留候选，不构造Responses适配器，不进入审查/Patch/写回。此模式不受32KiB语义上下文限制，但仍遵守导出范围和单文件256KiB限制。模式变化使旧审批无效。

`--test-timeout 1..300` 默认60秒，是全部组共用的Docker测试期限。它绑定预览，不能在审批后静默提高。目标测试包含较慢子进程时可显式选择180秒；不因此允许网络、root、宿主共享或临时目录直接执行。

固定控制器通过Docker stdin传入，测试组共用一份固定pytest包装程序；每组清单走子进程stdin，避免大清单或多组突破Linux单参数长度限制。目标测试stdout/stderr仍丢弃，未开启原始日志。

| 诊断代码 | 结果含义 |
|---|---|
| `tests_passed` | 至少执行一个测试且各组无失败/基础设施错误 |
| `test_assertions_failed` | pytest执行阶段存在失败；不保证都是种入bug或纯断言异常 |
| `test_collection_error` | 测试收集失败 |
| `test_setup_error` / `test_teardown_error` | pytest fixture阶段失败 |
| `test_internal_error` / `test_usage_error` | 内部/参数错误或异常子进程退出 |
| `no_tests_executed` | 全组没有执行测试，含全跳过/全xfail |
| `sandbox_timeout` | 容器测试期限耗尽，清理后阻断 |
| `sandbox_preparation_error` / `container_execution_error` | 导出/容器准备或执行失败 |
| `sandbox_cleanup_failed` | 清理失败，覆盖先前成功结果 |

`test_runs` 每轮保存 `stage`（baseline/candidate/final）、顺序、内容摘要，候选轮关联 `finding_id` 和 `patch_sha256`。`test_failures` 仅保存批准测试清单中的项目/文件、测试定义行号、错误行号、collection/setup/call/teardown阶段和固定错误类别（例如 assertion_error、permission_error、connection_error）。用例按源码定义位置识别，不保存参数化实例名称、异常消息、断言值、locals 或原始日志；同一位置/类别的参数化失败可能合并，不能作为失败用例总数。

诊断经独立管道传输，内外层各最多保留65536字节，最多50条记录，超额继续排空但不无限缓存或落盘。宿主再次检查结构、枚举、批准清单和源码行号。继承/导入的用例使用定义文件及其行号；定义不在批准测试清单或位置无法对应时标记为不完整，保留其余有效诊断。`diagnostics_status` 为 complete/truncated/partial/missing/invalid/oversized/not_collected；诊断缺失或损坏不会改变测试退出状态，也不绕过超时/清理失败。只有前后两轮均执行完成且诊断完整时，`comparison` 才列出新增和未再报告的诊断；这不证明业务bug已解决，错误类别也不单独证明环境是根因。安全诊断策略进入预览和审批摘要，旧审批不授权新版运行。

`finding_refreshes` 保留新旧快照、逐条重新审查/独立复核结论、调用计量、关联补丁和完整新报告；原始初审不会被覆盖。刷新中的独立Verifier如返回错误finding ID、快照、哈希或行号，最多对同一批次/当前快照/任务/问题独立纠错一次，`issues[].verification_correction` 保留原错误和结果；先检查全部引用的范围，越界或传输失败不纠错。纠正后仍须与Reviewer的明确处置一致，未知结果不能直接移除问题。此策略现绑定 `finding_refresh` v4（v3的批量Verifier规则保持不变），仍共享总请求预算。同一任务/角色/当前快照的刷新问题按最多2条分组独立复核，每条有自己的finding ID、结论和证据；批量输出每条最多3个引用、200字符理由，输出总上限仍为2048 tokens。缺失/重复/外来ID直接阻断，不为整个无效批次申请纠错；单条请求仍保留一次ID纠错。批次内全部引用先检查范围，之后才允许逐条元数据纠错和跨批次依赖取证；unresolved不会被省略。补丁后的静态 resolved 不等于整轮 applied。诊断不含目标异常文本、测试总数量或每个测试的堆栈。测试本身可能薄弱或恶意，退出状态不认证其诚实性。已提交测试需要真实服务、特定构建工具或可执行临时文件时，严格隔离可能使其失败；不能通过修改测试或跳过子项目制造成功。

v4增加未决Reviewer的有界依赖重审：`reviewer_assessments` 在提前退出前保留已解析的任务/问题身份、处置和有界理由；不保存原始响应或隐式推理。若原批准选择还有跨批文件，可对同一当前问题/快照重审一次，全轮最多8次，每次最多3个请求、每轮3个显式read、每段200行、累计32768唯一源码字节。`reviewer_reassessments` 保留实际供应引用、计量、结果和固定诊断。没有证据时只提供原引用文件的当前前40行作为查找起点，**不把它自动认作问题的新证据**；Reviewer必须实际读取批准依赖并重新提供原任务路径内、真实已供给的当前引用。快照选择变化只投射已检查引用的快照身份，绝不改写哈希或行号。

恢复后的Reviewer结论还必须经过独立Verifier和跨批依赖复核，双方处置一致才可继续。`present/accepted`仍入队，`resolved/rejected`才可移除；继续unresolved、缺失依赖读取、错误证据、分歧、传输失败或预算不足仍停止。依赖读取不扩展原Reviewer覆盖或可编辑范围。提示词要求下游Agent检查授权，不等于运行时授权已被证明。所有请求计入原总预算，终审预留不可使用；v4策略进入新审批摘要，旧授权不可复用。

仅测试模式：退出0=`tests_passed`；退出1=`tests_failed/blocked`；退出2仍为输入/审批/输出拒绝。完整修复模式的退出语义不变。

### 复核证据诊断与一次性纠错（2026-11-06）

`repair-run` 现在把以下证据错误分开记录，而不把所有原因压成 `invalid_evidence`：

| 诊断代码 | 含义 | 自动纠错 |
|---|---|---|
| `verification_finding_mismatch` | 复核结果的问题编号不匹配 | 最多一次 |
| `evidence_snapshot_mismatch` | 引用的快照编号不匹配 | 最多一次，且当前源码必须未变 |
| `evidence_hash_mismatch` | 引用的文件哈希不匹配 | 最多一次，且当前源码必须未变 |
| `evidence_line_range_invalid` | 引用行号超出真实文件或单片段限制 | 最多一次 |
| `evidence_path_outside_scope` | 引用了未授权文件 | 不纠错，停止 |
| `source_changed` | 供给源码或来源在验证期间变化 | 不纠错，停止 |
| `unsupplied_evidence` / `invalid_output` / `invalid_evidence` | 未供给范围、结构错误或未分类错误 | 不自动猜测或重试 |

纠错不是把错误元数据替换为“正确答案”，而是由Verifier基于当前完整选定批次代码重新给出结果。只在初审/终审中存在明确可恢复的Verifier错误，且原问题、其他结果及两类角色覆盖均通过校验时启动。每个问题、快照、审查阶段最多一次；新结果仍要严格验证，再次错误会停止。若元数据纠正后仍为 `unresolved`，只有存在本批之外的已批准源码时才交给下述依赖复核；否则停止。纠错成功仅表示证据有效，不代表问题已确认或消除。终审即使纠错成功，仍不得留下accepted问题。

纠错用单独的父级授权阶段，只有一次模型请求；原阶段最多8次、全局最多48次、正文/输出/期限限制均不变，未知用量或传输失败不能借新阶段继续。没有余额则报告 `recovery_budget_exhausted`，不发送请求。该策略进入 `verification_recovery` 预览字段与审批摘要，因此旧预览不能直接用于新版执行。`--tests-only` 的策略为null，不触发纠错或模型调用。

同一响应中先检查所有引用的范围，再检查可恢复错误；越权不能被前面的坏哈希掩盖。原始 `initial_reviews` / `final_reviews` 不删除错误；`verification_recoveries` 记录固定错误类型、尝试、实际请求数及结果，并立即保存每项已通过校验的结构化 `verification`，即使后续另一项失败也不丢失；`recovered_reviews` 只在整批纠错成功后保存有效报告。Markdown也显示原始证据诊断及恢复成功数量。报告不保存无效模型原文或任意异常文本；旧运行未保留的错误字段无法事后还原，不应把合成回归当成旧错误的精确重放。

此恢复仅用于同一进程内正在运行的 `repair-run`；不从旧报告恢复权限，不自动重启已结束的付费运行。最终测试、独立复审与受保护写回门禁没有放宽。

### 显式分组补丁调度（2026-11-08）

默认仍为 `--patch-scheduling serial`。选择 `--patch-scheduling grouped` 时，同一当前审查批次/快照中最多4条独立接受的发现共用一次补丁请求；每条保留 `(task_id, finding_id)` 身份与自己的证据、可编辑路径。响应必须完整、无重复地覆盖所有成员，不能按标题合并问题，也不能用一个合成问题替代成员。原坐标下不冲突的编辑才可合并；相同精确编辑可以共有但保留全部归属，冲突则整组拒绝。

每组最多尝试2次，每位成员每次都消耗一个补丁尝试名额，全轮最多8个成员尝试。源上下文32768字节、请求65536字节、输出2048 tokens以及单文件65536/整组131072替换字节和最多5个修改文件限制不变。整组在候选区重验后应用、共用一次Docker测试。grouped v2比较补丁前实际保留的候选状态：从通过退化为失败，或前后诊断完整且出现新的失败测试定义时，整组恢复后才可重试。测试定义按项目/路径/定义行定位，不能把同一测试的断言行、错误类别变化当作新测试失败；也不据此断言补丁存在因果回归。诊断不完整或只剩旧失败时仍可暂存候选，但最终全库测试必须通过。未知用量/传输失败不重试。

分组请求的 `test_feedback` 包含当前候选的有界测试观察；回退后的重试还包含最近一次被拒绝的精确编辑及其测试内容摘要，明确不属于恢复后的当前源码。观察只上传固定状态/类别/阶段、数字位置及不透明测试定义摘要，不上传测试路径、源码、日志、断言值或参数标识。刷新Reviewer获得当前候选的 `test_observation`，独立Verifier仍依据源码判断；观察不能充当发现证据或自动解决问题。报告的 `patch_groups[].new_failing_definitions` 为新增失败定义数量，null表示不可比较，不是新增业务缺陷数。

测试后每个受影响的待处理审查范围刷新一次，**包括本组刚修的发现**；跨批次复核范围依赖已改文件时也需刷新。生成补丁或候选测试通过不能自动删除问题，Reviewer和独立Verifier仍须明确确认；unresolved、遗漏和结论分歧会阻断。刷新的范围排在队尾，避免持续抢占未处理范围。`patch_groups` 记录成员、尝试、快照、组合补丁摘要和状态，`patch_attempts` 保留每成员记录；组测试通过摘要关联成员，`finding_id` 为null。

不同批次的源码哈希也可能改变包含范围的快照身份；未改变的发现仍需当前源码复核。`current_finding_verifications` 保留分组调度中的此类逐条复核；明确rejected的成员移出队列，整组都被独立否定时不申请补丁。unresolved仍阻断，不能把旧结论投射为新的接受结论。

完整固定调度策略进入 `patch_scheduling` 预览及审批摘要，serial的旧授权不适用于grouped。`--tests-only` 中该字段为null且不触发模型或写回。总48次、终审预留、最终全库测试、两角色完整终审以及原工作区保护门禁均不变；分组减少重复调度，不保证任何仓库一定在固定预算内修复成功。

### 显式导入未验证候选（2026-11-08）

`repair-run --seed-report /absolute/prior/report.json` 可把上一轮暂存候选的选定源码作为**未验证输入**带入新候选。必须重新生成预览并使用新的审批摘要；不恢复旧运行权限，不复用旧审查结论或测试凭证，也不支持与 `--tests-only` 混用。该参数不会自动循环执行。

输入必须来自保留候选且用量完整的 blocked 报告，逐次传输记录必须完整成功、数量与请求数相符；即使用量已知，传输失败记录仍拒绝导入。新输出/候选目录不能位于旧候选内部；源仓库、HEAD、分支、选定范围及原工作区状态必须一致。控制器读取实际候选字节，核对内容摘要、权限和文件树，拒绝符号链接、额外文件/目录、未选定路径变化和原本脏的选定文件变化。每个替换最多65536字节、合计131072字节，最多20个且仅限新审批选定的可编辑源码。原报告和候选保持不动；审批绑定报告哈希、实际替换哈希、候选身份和报告提供的历史用量，应用前再次核验。

新运行先对原始HEAD做Docker基线测试，再导入新候选并执行 `seed` 阶段测试，然后重新初审。即使初审没有新发现，也必须通过最终全库测试和独立两角色终审，才能由产品写回原仓库。种子测试失败不等于修复成功，只能保留为待修候选；最终测试、证据和覆盖门禁不变。

每轮仍最多48次模型调用、8次成员补丁尝试；固定策略最多2层续跑（含起始轮共3轮），报告累计上限144次调用/24次成员尝试。`cumulative_reported_accounting` 明确标记 `historical_accounting: report_supplied`：历史数据未经认证，不是可恢复权限或真实账单凭证，金额仍未知。限制依据所提供的历史记录，不宣称可阻止人为伪造或另起独立运行。终审预留不借给修复，也不因导入候选而放宽源上下文或传输限制。

### 显式可执行临时测试区（2026-11-08）

`repair-run --test-sandbox strict` 是默认配置，保留原有 `/tmp:rw,noexec,nosuid,size=32m`。某些已提交测试需要在临时目录生成并执行模拟安装器；可显式选择 `--test-sandbox exec-scratch`，额外挂载仅容器内的 `/scratch:rw,exec,nosuid,nodev,size=32m,mode=1777`，并设置 `TMPDIR=/scratch`。这明确扩大容器内临时程序的执行权限，不是与strict完全等价的安全配置。

源码卷和根文件系统仍只读，`/tmp`仍noexec；禁网、非root、禁止提权、资源限制和无宿主目录挂载保持不变。scratch是容器tmpfs，随容器清理；不允许传入任意挂载路径或Docker参数。完整固定配置出现在 `test_policy.sandbox` 和执行intent内，配置切换使旧审批失效，包括 `--tests-only`。未选择时绝不自动切换，失败后也不降级隔离。建议先用 `--tests-only --test-sandbox exec-scratch` 做零模型验收；环境兼容不等于业务测试全部通过，不代表候选修复获准写回。

### 有界 HTTP 状态诊断（2026-11-09）

Responses适配器v10对HTTP400–499保留固定 `http_status_NNN` 代码；401/403仍归为 `http_auth_error`，429仍为 `http_rate_limit`。这些代码来自有限整数映射，不包含服务端正文、错误消息、头字段或请求标识；非200响应仍在读取正文前拒绝，且不重试。状态码用于区分请求拒绝类别，不能单独证明具体参数、业务逻辑或服务商故障原因。

旧报告的 `http_client_error` 仍可展示，但不能反推出已丢弃的精确状态。请求失败后用量仍为未知，父运行关闭，不把4xx视为免费调用。诊断版本变更需要新预览/审批，不放宽超时或测试、终审、写回条件。

### 隔离模型请求的阶段诊断（2026-11-08）

Responses适配器v7在POSIX宿主通过私有临时文件描述符记录最多六个固定阶段：`worker_started`、`connection_established`、`request_sent`、`response_headers`、`response_body`、`response_validated`。每条只有阶段编号和worker主函数启动后的毫秒数（0–60000），不含解释器启动耗时；连接阶段包括DNS/TCP以及HTTPS的TLS。`request_sent`只表示请求交给本地socket，不证明服务端已接收或执行。响应校验完成也不是目标测试或补丁验收通过。

worker最多写30字节，父进程在worker退出或被终止并回收后最多读31字节；仅接受连续、单调、有界的阶段序列。临时描述符随请求关闭，不保存原始诊断流，不解析stdout/stderr为进度，不记录凭据、源码、响应正文、异常文本或模型推理。缺失/损坏诊断标记为不可用，不猜测停滞阶段。`accounting.records[].transport_progress`保存有效序列；Markdown在传输失败时展示固定错误码、最后完成阶段和该阶段完成时间，不能将此时间误读为失败时间或服务端处理耗时。

父级硬超时仍最多60秒；未知用量仍关闭整轮，不增加请求或自动重试。v7使旧预览审批失效。阶段证据只能缩小排查边界，不能独自证明网络故障或提供商故障。

v8进一步将21个固定的非支持输出类型分类为 `response_type_<固定名称>_unsupported`，例如custom_tool_call或tool_use；这些是拒绝诊断，不是新增工具能力。任意未知名称及非字符串type仍统一为 `response_output_type_unsupported`，原始值不保留。function_call仍使用原固定拒绝码。所有这些响应仍被拒绝、用量视为未知并终止整轮；v8也需要新预览审批。

v9在请求中显式设置 `tools=[]`、`tool_choice=none`，并说明控制器read/search请求只能作为所给schema中的JSON数据返回。控制器读取不等于提供商原生工具调用；不依赖中间服务的默认工具选择。若服务仍返回未知输出，保持拒绝与用量未知关停，不将其转成读取或复核结论。这项请求约束本身不能证明此前未知类型的根因；v9需要新预览审批。

### 源码供给完整性（2026-11-08）

Planner收到的文件前缀元数据不代表Reviewer实际收到的源码。交互协议现在向Planner说明预载策略，并向Reviewer/Verifier逐轮提供控制器计算的 `source_coverage.files`：允许路径、LF行数和 `fully_supplied`。后者仅在本次请求片段连续覆盖1到EOF时为true，显式读取后会重新计算；不包含范围外路径。即使源码全部供给，也不自动填写或扩大 `reviewed_paths`，空覆盖仍会阻塞修复，未供给源码仍不能引用。这些协议说明进入预览/批准摘要。

### Reviewer任务证据纠错（2026-11-07）

Reviewer的哈希、快照或行号错误会使整个任务失败，不能套用仅针对已验证finding的Verifier纠错。范围内finding引用的路径遗漏在其 `reviewed_paths` 中时，单独报告 `finding_evidence_not_reviewed`，不再与范围越界混为 `invalid_coverage`。`repair-run` 对这些明确错误允许将原任务重新审查一次：提供同一批次当前源码和原任务，不替换旧哈希，不复用无效finding，也不将失败任务算作覆盖。每个新finding都必须再经独立Verifier；缺失覆盖、再次错误、范围越界、未知用量或传输失败均阻塞，不递归纠错。有效但语义未知的结果只能继续进入有范围和预算限制的依赖复核，不能直接用于补丁。

策略 `reviewer_recovery` 绑定预览及审批：每个阶段/批次/快照/任务最多一次，全程最多8次，每次独立阶段最多8个请求，仍共享总计最多48次。`--tests-only` 为null，不增加任何请求。原始初审/终审保留失败，`reviewer_recoveries` 保存原错误、任务范围、请求数、纠错报告和结果，`recovered_reviews` 保存合并后的有效报告。纠错成功不代表发现获准或业务修复成功；初审/终审的两角色覆盖、证据、测试和写回门禁仍然适用。Markdown现在同时显示Reviewer任务的安全错误代码，避免有任务失败却显示“证据诊断：无”。

### 已批准范围内的跨批依赖复核（2026-11-06）

源码按32KiB划分Reviewer批次，但批次不再是语义未知结果的唯一取证范围。初审/终审中证据结构有效的 `unresolved`，以及补丁后独立Verifier的未知结果，可以进入单独的依赖复核阶段；不是强制accepted，也不是忽略该发现。补丁后Reviewer本身无法给出当前证据的 unresolved assessment 仍会停止。

- 只提供当前发现引用的源码片段和本轮已批准路径/行数目录，再由独立Verifier明确请求读取必要行。不分析自由文本理由来授权路径，不自动上传全仓库，也不扩大Patch权限。
- 每次最多3个模型请求，每轮最多3个read，每个片段最多200行，累计供给源码不超过32768字节；单次请求正文仍不超过65536字节。v2按同一文件/快照/哈希的已供给行去重：重叠读取仅追加缺失区间，不重复上传、重复消耗字节预算，也不会把未读取的间隙视作已供给。重复读仍重新验证源内容和范围。请求中给出 `context_budget` 的已用/剩余字节与目录的 `byte_count`，引导模型选择必要片段。真正超额仍停止，`context_rejection` 只保存批准的拒绝引用、缓冲字节数、新增字节数和上限，不含源码或任意异常。相同阶段/任务/问题/当前快照最多尝试一次，全程最多8次依赖复核，全部消耗原父级上限（最多48次），不是额外48次。
- 至少实际供给一个本批之外的已批准文件片段，才能接受明确结果。引用必须来自实际供给内容；越权、伪造快照/哈希、未供给行号、仍未知、额度不足或外部修改均停止。未知用量/传输失败仍禁止后续请求，不换阶段绕过。
- 批次覆盖与Verifier取证范围分别保存。`FindingOutcome.verification_scope` 只描述独立复核的批准清单，不算Reviewer覆盖。清单变化会改变snapshot ID，因此先验证原批次证据，再将**相同哈希与行号**关联到依赖阶段快照；独立Verifier必须据此给出新结论。原始finding、批次snapshot和覆盖字段不被改写。候选补丁导致快照变化时，依赖结论不能直接用于下一次Patch，必须重新独立复核。
- `dependency_verifications` 保留阶段、任务/问题身份、新旧快照、取证范围、实际供给的证据引用、原结论、新结论、请求计量和固定停止代码；不保存额外原始源码或任意异常。原始 `initial_reviews` / `final_reviews` 保持不变，整批成功的有效报告进入 `dependency_reviews`；刷新报告内也区分原覆盖与复核范围。
- `dependency_verification` 策略绑定预览及审批摘要，旧批准不授权新版运行；tests-only中为null。不能从旧报告或上轮剩余额度自动续跑。

所有批次仍要分别完成correctness/security审查。最终测试通过、无accepted/unresolved问题、当前快照与原仓库保护检查，仍是写回的必要条件。补充依赖后的静态判断不证明业务修复或真实目标验收成功。

### 原有更严格的发布服务（未放宽）

服务顺序：`propose_patch → Workspace.apply → DockerRunner → ValidationAuthority → Publisher`。`run_repair` 为一个选中问题最多进行两次尝试；失败恢复已知基线，遇到用户修改则 blocked 并保留现场。多文件补丁写入中途失败时，`Workspace.apply` 会恢复已经写入且仍由本次操作持有的内容；恢复不安全或失败则返回 `blocked_partial_application` 并停止循环，不继续验证或重试，不覆盖用户修改。旧服务不提供自动批量 CLI 或跨进程工作区恢复；新 repair-run 使用独立应用门禁，不能读一个 audit JSON 就恢复审批或 commit 权限。

`CandidateChain` 为新增进程内候选组合服务：只接管全新自有 Workspace；每个问题的 task/finding/proposal 必须基于**当前候选目录快照**重新验证（不能重用原 HEAD 的旧证据）。`intent(...)` 绑定内容、当前摘要、步骤与尝试；人审 diff 后每步单独批准，再 `apply(..., gate, grant)`。最多40个保留步骤/80次链尝试，Workspace 原有每 finding 两次限制仍在。`rollback_last()` 只撤回最后一个自有步骤；第二步失败恢复第一步，未知用户修改或无法恢复则阻止继续并保留现场。不要与直接 Workspace.apply/run_repair 混用。此服务没有磁盘恢复或 CLI 批量入口；所有 summary 固定 `validation=not_run`、`commit_ready=false`，不能跳过真实组合验证。

执行前要在可信环境准备固定 `name@sha256:...` 或本地 `sha256:...` 镜像，内含 Python、pytest 和审核过的依赖。已获用户许可在开发机安装 Colima/Docker、用独立可信 Dockerfile 准备镜像；这不授权其他机器自动安装。运行目标测试时始终 `--pull=never`，不安装依赖，不执行目标构建/安装脚本。

容器约束：只读、UID65534、禁网、去 capabilities、no-new-privileges、CPU1、内存256MiB、进程64、临时区32MiB、固定超时、`--pull=never`。只挂载复制出的批准文件，不挂 `.git`、凭据或 Docker socket；目标输出直接丢弃，避免秘密和无限日志。无论正常退出或超时，都会显式删除该操作唯一命名的容器并检查返回码；删除异常或非零返回码为 `cleanup_failed`，不能获得验证资格。此状态表示容器可能仍在运行，操作方应使用结果的 `run_id` 在 Docker 管理端检查并终止；本轮未做实际 Docker 终止验收。不支持宿主执行 fallback。

验证采用两个固定检查：`tests/` 回归（排除专用复现文件）和 `tests/test_reviewflow_reproducer.py`。原基线回归需通过、复现需以 pytest 测试失败码1失败；候选两个检查均需通过，再由**新的真实 multi 审查**确认同一候选快照与完整覆盖。假结果、模拟审查、blocked/timeout、旧快照均不能生成 commit 资格。复现文件由人审阅准备，Patch Agent 无权修改。

commit 还需单独审批作者、父提交、分支和最终内容摘要；用 blob/index/commit-tree 精确提交，不执行 hooks/filters，不 `git add .`。push 又需单独审批 HTTPS 地址、修复分支、提交和**可能传输的全部祖先历史**；不强推、不合并、不改 remote。拒绝 URL rewrite、include、代理或执行型 transport 配置（含 worktree config）。当前禁用 credential helper 和交互提示，私有远程认证可能失败并保留本地成果；宿主凭据管理器的受控接入仍需单独实现与验收。

CLI 未暴露可以由磁盘 JSON 伪造验证结果的 commit/push 入口。后续真实 SDK 与可信执行服务连通后，UI/CLI 应在同一可信进程调用上述授权接口。当前不宣称已有真实“发现→修复→验证→推送”的端到端成功案例。

## 7. 实验与打包

```bash
review benchmark-demo --corpus benchmarks/corpus --split dev --mode single
review benchmark-demo --corpus benchmarks/corpus --split dev --mode multi-no-verifier
review benchmark-demo --corpus benchmarks/corpus --split dev --mode multi
review evaluate --manifest benchmarks/corpus/manifest.json --results /outside/adjudicated-results.json
review package --project /absolute/reviewflow --output /outside/reviewflow-source.zip
```

实验方法及结果结构见 [evaluation.md](evaluation.md)。ZIP 使用源码白名单、哈希清单与有限秘密模式检查，排除 `.git`、环境、缓存、配置私密文件和结果目录；拒绝 symlink、覆盖、超限、可疑密钥和已知凭据文件名（例如 `credentials.json`，大小写不敏感）。**秘密模式检查不是完整 DLP，发布前仍需人工检查。** ZIP 不包含依赖，不等于已在新机器离线安装成功。

2026-11-03 安装复现已补做：在工具虚拟环境安装声明的构建依赖 `setuptools>=68`，将源码 ZIP 解包到新目录、核验清单/逐文件 SHA-256，再构建 wheel。全新虚拟环境从该 wheel 安装 `dev,review` 及其 PyPI 依赖，确认 `reviewflow.__file__` 指向新环境的 site-packages（不复用原 editable checkout 或原依赖路径）。实际安装后的 session-start/show/resume/export、盘点、demo、拒绝真实入口和凭据占位检查均符合预期；全量工程测试通过，`pip check` 无依赖冲突。

首次安装版测试因独立调用目录没有语料相对路径出现3失败/429通过；修正验证脚本，仅复制自建 benchmarks 数据到调用目录后，完整重跑432通过。未为通过测试修改产品逻辑。构建使用 setuptools84.2.1；测试主要版本 Python3.12.16/Pydantic2.14.2/LangGraph1.2.19/pytest9.2.0/Ruff0.17.3。**此为当前 macOS 环境的真实 wheel 安装验收，不是无网依赖包，也不是 Linux/Windows 验收。**

独立代码审查另发现并修复两处检索错误：空白查询曾绕过结构检查并异常退出；全局搜索先截断再按任务过滤曾漏掉范围内结果。4项新增回归先失败再通过，修复后本地全量436项通过。最终交付验证已再次完成源码包的 wheel 构建、独立环境安装及436项全量测试（27.01秒），安装后 CLI 冒烟检查和 `pip check` 同样通过。最终文档封版仅更新验收记录与计划勾选，并核对源码/测试哈希与已安装验证的包一致；打包不包含真实配置、凭据或开发执行记录。

以上436项 wheel/ZIP 验收对应上一轮后端集成交付。其后的统一配置简化已通过450项本地全量测试、Ruff 和实际 config-check/credentials-check/prepare 命令验证；旧交付 ZIP 不含此次配置改动，交付时应从当前工作目录重新打包。

## 8. 仍需提供／验证的条件

1. 模型提供方、官方 API 类型、模型 ID、endpoint、价格/预算；Key 最后在本地设置。
2. 已审核、固定 digest 的本地沙箱镜像，以及平台上的隔离验收。
3. 真正的模型效果、token/费用、真实修复及远程发布验收。
4. 前端（按本轮要求未开发）。

所有模拟、环境阻塞、未集成边界都应在课程演示中如实说明，不将工程测试数量说成真实缺陷修复数量。
