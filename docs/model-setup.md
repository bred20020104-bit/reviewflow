# 模型配置与真实开发审查

更新：2026-11-04。模型五项统一在私有 `secrets.local.toml`；不需要用户手填 budget/privacy。现在支持有界的真实 Responses 开发审查，不是正式计费模式或完整修复验收。

## 1. 配置仍只填一个私有文件

```toml
provider = "openai"
api_type = "responses"
model = "TODO_ACCOUNT_ACCESSIBLE_MODEL_ID"
base_url = "https://api.openai.com/v1"
api_key = "TODO_REPLACE_LOCALLY"
```

第三方网关填写该平台的模型、地址和 Key，不要将第三方 Key 发往官方或其他服务。模型名由账户权限决定，此处不猜测。`provider` 为描述性标签，不证明网关真实的上游身份。

本机私有文件：`/Users/bred/PycharmProjects/project/reviewflow/secrets.local.toml`。不要在聊天、命令参数、截图、公开模板或报告中粘贴 Key。

`config.local.toml` 只需：

```toml
secrets_file = "secrets.local.toml"
```

引用相对于明确选中的工具配置目录，不从被审查项目自动发现。私有文件必须 owner-only（600）、当前用户所有、单一硬链接、非符号链接。两个 local 文件均忽略并排除打包，模板没有真实值。新机器在可信工具目录执行，已有文件不覆盖：

```bash
cp -n config.example.toml config.local.toml
cp -n secrets.example.toml secrets.local.toml
chmod 600 secrets.local.toml
```

## 2. 两套控制边界，不混淆

| 限制 | 正式费用控制服务 | 显式真实开发模式 |
| --- | --- | --- |
| 入口 | `GuardedTransport`，CLI 正式模式未启用 | `review --review --development` |
| 金额 | 默认每次审查$1、单次$0.25；单价未知则拒绝发送 | **未知，不保证美元金额上限** |
| 请求次数 | 默认12 | 最多8次适配器尝试；`--max-calls 1..8` 可降低本次上限 |
| 输入约束 | 需要可信完整请求 token 上界，默认16000 | 最终 HTTP JSON 正文最多65536字节；字节不是 token |
| 输出 | 默认4000 tokens | 请求上限2048 tokens；收到超限usage后阻止后续请求 |
| 超时 | 可信适配器必须落实 | 每次父进程60秒总期限，终止/等待工作子进程 |
| 自动重试 / 重定向 | 不允许隐式重试 | 0 / 0 |
| HTTP | 普通配置拒绝 | 仅显式开发模式允许；Key和源码明文传输 |
| 上传 | 配置不等于许可 | 先预览，显式摘要批准后运行 |

开发模式使用独立配置/传输类型，不把假价格或字节计数塞进正式计费门禁。usage 来自网关，并非 independently verified billing；记录已知 token 和未知金额。网络错误、缺少/非法usage、超量、不完整输出后阻止后续发送。超时不能撤销服务端已经产生的费用。

当前适配器是 Python 标准库实现的窄 Responses HTTP 协议，**不需要 openai SDK**；不启用代理环境发现、远端工具、previous_response_id 或隐式会话状态，不调用目标代码。

## 3. 本地检查

```bash
.venv/bin/review config-check --config config.local.toml
.venv/bin/review credentials-check --file secrets.local.toml
.venv/bin/review prepare --config config.local.toml \
  -d examples/demo-project --source directory
```

普通 config-check/prepare 仍要求 HTTPS，因此当前 HTTP 测试网关可能在这两个命令被拒绝。这不是需要改为 HTTPS 假地址；HTTP 的配置预览应使用下一节明确开发模式。

简化配置的 config-check/prepare 会读私有文件取得模型元数据，但不打印/发送 Key。Key 是否有效须通过真实服务验证；credentials-check 仅检查本地格式/权限。默认盘点、demo、模拟 session 不读真实凭据。

## 4. 真实开发审查：预览 → 批准 → 报告

在可信工具目录执行。以下 `/absolute/...` 请替换为自己的路径，报告目录必须不存在且在目标之外。

```bash
# 第一次只预览：不联网、不创建报告目录
.venv/bin/review --review --development \
  --config /absolute/tool/config.local.toml \
  -d /absolute/target --source directory \
  --include a.py --include b.py \
  --output /absolute/outside/new-report

# 检查 selected_files、endpoint、限额和未知费用提示；
# 保持所有参数不变，追加第一次返回的 approval_digest 才发送
.venv/bin/review --review --development \
  --config /absolute/tool/config.local.toml \
  -d /absolute/target --source directory \
  --include a.py --include b.py \
  --output /absolute/outside/new-report \
  --approve REPLACE_WITH_PREVIEW_DIGEST
```

- `--include` 可重复，只能指定盘点已纳入的精确相对文件名。省略表示全部纳入文件，仍需先检查预览；敏感路径过滤不能保证识别源码中的所有秘密。
- 默认 `--mode multi`，也支持 `single` / `multi-no-verifier`。本轮开发模式最多2个任务、8次工作流尝试，每位Reviewer最多2条优先候选，受输出和上下文限额影响可能更少。可用 `--max-calls 7` 等降低单次运行调用上限，该值也绑定预览。重新运行不会自动继承上次剩余额度，调用方必须按跨运行授权总量扣减，不能静默重置。
- 默认 Git auto/head 读取已提交 HEAD。`--source directory` 明确读取当前文件，但不具备 Git 修复资格。
- 发送前检查预览的 `context_preflight`：`selected_source_bytes` 是所选源码大小，`whole_selection_fits=false` 表示不能在当前 32KiB 上下文内完整供应全部所选文件。初始上下文从各文件第1行轮流分配，不再以尾块替代开头。`planner_ranges` 和 `initial_omissions` 展示实际初始范围与遗漏。
- `suggested_include_batches` 给出按源码字节/文件数限制划分的 `--include` 分组建议；`oversized_files` 中的单个文件连一批也放不下。建议不会自动执行，每批需重新预览及授权；这是源码预算估算，JSON转义/提示词/schema仍受64KiB正文限制，也不保证跨批交互已被审查。不得把分批结果说成全仓无缺陷。
- 预览分别绑定所选范围的 `snapshot` 与完整盘点的 `full_snapshot`，同时绑定目标/root/source、模型/endpoint、源码范围/hash、限额、模式和输出位置。任何这些变化都要重新预览；已有输出目录拒绝重跑，不覆盖。即使改变的是未选择上传、但已被盘点纳入的文件，也会使旧摘要失效。
- `agent_profiles` 展示共享规范、各角色职责和固定绑定技能的路径/摘要；`prompt_digest` 和审批绑定这些文件及检索说明。修改产品包内任意 `SOUL.md` / `AGENT.md` / `SKILL.md` 后必须重新预览；已批准的一轮使用同一份冻结快照，中途改文件不会替换该轮提示。目标同名文件不能覆盖系统指令。详见 [Agent 配置指南](agent-profiles.md)。
- 批准的是一次**有界运行**，不是无限批准。每个请求仍通过真实证据检查和一次性子授权。保存的 JSON 不恢复权限。
- 默认 `review` 仍只是盘点；仅写 `--review` 仍拒绝，不会静默改为模拟。

## 5. 报告和退出码

新外部目录保存 `report.json`、`report.md`（文件600、目录700）。报告含真实执行标识、候选与accepted/rejected/unresolved、覆盖/跳过、工作流错误、检索记录、请求尝试和网关用量；不保存 Key、原始请求或服务端原始错误。适配器在工作流解析前检查原始文本及 JSON 解码后的字符串/属性名，拒绝字面或 JSON 转义的 Key 回显；这不等于能识别任意秘密或所有编码方式。模型自由文本可能含源码，请按敏感材料保管，不上传。

- 完整流程退出0；partial/failed退出1并保留报告；参数/配置/审批/输出冲突退出2。
- `source_snapshot_unchanged` 比较纳入文件与清单，不涵盖未枚举的排除目录。
- `semantic_review_performed` 仅在至少一个 Reviewer 结果通过本地校验后为true，不等于无缺陷或动态复现。
- `requests_attempted` 为保守适配器尝试数，失败可能尚未发出请求，也可能已经计费。已知usage合计不包括未知usage；请同时看 `usage_complete`。
- 部分文件未选择、原始盘点存在跳过、上下文不完整、预算耗尽、复核unresolved等都会保守标记partial。
- 真实报告不由模拟 `session-show/resume` 打开，不支持精确图节点恢复，也不授予修复、测试、commit/push权限。

### 安全错误诊断

`accounting.records[].error_code` 与工作流错误会保留固定分类；成功记录为 `null`。错误分类不会包含服务端原始消息、请求体、密钥、子进程stderr或任意异常文本。未知错误仍归为安全的通用代码，不根据错误猜测原因。

| 错误代码 | 含义与排查方向 |
|---|---|
| `http_auth_error` / `http_rate_limit` / `http_server_error` | 认证类HTTP失败 / 限流 / 服务端HTTP失败；检查服务配置或服务状态，不自动重试 |
| `http_redirect` | 服务要求跳转，但产品未跟随跳转；检查已授权endpoint |
| `worker_timeout` / `response_timeout` | 父进程截止时间 / HTTP读写超时 |
| `network_error` / `tls_error` | 网络连接或TLS失败；不输出底层异常内容 |
| `response_incomplete_max_output_tokens` | 服务明确返回输出上限导致未完成；不是完整审查结果，不能把截断内容当结论 |
| `response_incomplete_content_filter` / `response_incomplete` | 服务返回过滤或其他未完成状态 |
| `usage_missing` / `usage_invalid` / `usage_output_limit` | 用量缺失、不合法或超出请求输出限额 |
| `response_json_invalid` / `response_text_invalid_or_unsafe` | 无效响应JSON，或结构文本未通过安全检查 |
| `worker_protocol_error` / `worker_failed` | 隔离Worker输出协议不合法或未分类失败 |
| `responses_request_rejected` | 请求序列化/结构/最终正文大小未通过适配器检查；通常发生在发送前 |
| `run_blocked` | 先前适配器失败已阻止后续发送，不代表又发送了一次请求 |

任一适配器失败仍会终止后续发送，不新增重试、不放宽usage验证、不调高输出或正文限制。工作流调用数 `report.calls` 与适配器尝试数 `accounting.requests_attempted` 不是同一计数，判断外部尝试应看后者。错误分类修复不能追溯恢复旧报告已经丢失的根因。

## 6. 模拟能力不变

`--demo` 与 `session-start/resume --demo` 使用固定标记和预设响应，不具备真实缺陷检测能力。其字节/价格是模拟值，不加载用户 Key。交互图支持 task-scoped read/search、Reviewer最多2轮补充、Verifier最多1轮补充、每轮最多3个请求，全部模型轮次计入限额。

真实开发模式复用同一交互图，两个 Reviewer 初始上下文隔离；Supervisor 分组建议保留所有候选，Verifier 独立判断。分组不是同根因证明，模型自报检查必须满足实际供应的完整行覆盖。

检索 `search` 是任务范围内的区分大小写字面子串查询，不是正则、文件名查找或自然语言搜索；读取指定文件/行范围应使用 `read`。每轮会把查询的匹配结果（包括明确的空结果）和剩余补充轮数反馈给模型。最后一轮的请求 schema 要求返回结果而不是继续检索；本地控制器仍拒绝超限或不合规输出，不会制造结论来填满报告。检索反馈只在运行内传递，模拟持久化会话格式保持兼容。

## 7. 最近验证状态（2026-11-04）

- 历史基线离线测试 **503 项通过**。此前真实测试暴露的选定范围快照、JSON转义Key回显、Planner任务上限、检索空结果反馈等问题已修复并添加回归测试；模拟会话保持兼容。
- **历史实际 CLI + 第三方 Responses + LangGraph 的选定3文件全流程已跑通**：Planner、两个Reviewer、分组、4次Verifier，共8次调用全部完成。两位Reviewer完成、4条候选均获静态accepted、流程错误0、所选范围未覆盖文件0；身份过滤的2条候选被建议同组。
- 原始报告为`partial`、退出1，原因是未选择其他文件以及排除目录，不是这轮发生流程错误。不能将3文件审查说成全仓库通过。accepted是模型静态判断，不等于动态复现或已确认生产漏洞。
- 最后成功运行的网关用量：输入46416、输出3238 tokens，金额未知。此前有失败/部分运行，其中1次usage未知；保留其报告，不把成功运行的用量冒充全部开发成本。
- 当前真实开发策略会在既有32KiB上下文限额内优先提供所选文件的完整分块；每位Reviewer最多提出2条优先候选（最多4条），给独立Verifier保留调用空间。这是有界优先问题审查，不是穷尽所有缺陷；策略写入预览授权。上下文超限仍受原检索和预算限制。
- 目标项目未执行测试、未应用补丁；该历史成功运行前后的完整盘点快照一致。后续实测须重新预览和明确批准，不能复用旧摘要或自动将剩余额度重置为8次。
- 后续六文件种错分支实测 **失败**：Planner完成，首次Reviewer适配器失败且用量未知，之后请求被阻止；共2次适配器尝试，无有效审查结论。旧版统一错误丢失具体原因，无法追溯判定是网络、usage还是输出截断；不把历史三文件成功冒充这轮通过。
- 针对此次暴露的问题，新增固定分类诊断、文件开头优先的有界上下文分配、上下文不足预览和分批建议。新版适配器/上下文策略使旧授权摘要失效。使用实际CLI和本地合成HTTP响应验证正常编排、限流、输出截断和usage缺失；本地合成验证不是第三方真实模型验收，也不证明自动修复能力。

## 8. 尚未完成

正式计费模式的可信价格/完整 token 上界、模型准确率实验、用户真实目标上的模型补丁与组合修复效果仍待验收。2026-11-06 已完成 `repair-run` 实际 CLI＋真实 Docker＋本地合成 Responses 的接线验证，详见[后端指南](backend-guide.md)；这不等同于用户目标修复成功。commit/push进一步集成和前端最后处理。历史436项验收的源码ZIP不含之后统一配置和真实开发接入改动；交付前必须重新打包和独立安装验证。
