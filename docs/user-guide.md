# ReviewFlow 用户指南

## 当前能做什么

默认命令提供**离线、只读的项目文件盘点**：选择目录、固定输入版本、过滤文件、生成快照与覆盖报告。不调用模型、不上传代码、不执行目标代码或测试、不修改目标文件。显式会话命令另写外部报告；`repair-apply` 只写候选worktree，`repair-run` 则可在单独批准并通过最终门禁后写回原项目选定源码。默认盘点不会触发这些动作。

CLI后端已提供结构化证据校验、LangGraph多角色流程、真实Responses开发审查，以及 `repair-run` 的补丁→Docker测试→独立复审→受保护写回。`repair-run --tests-only` 只做隔离测试，模型调用为0且不写回；支持分项目测试环境和审批绑定的1..300秒测试期限。详见[模型配置](model-setup.md)及[后端指南](backend-guide.md)。默认 `review` 仍只是盘点，`--demo` 的候选和判定仍是预设数据。实际Docker与合成模型接线已验证，不代表任意仓库修复成功；真实目标模型修复验收、UI和远程发布仍未完成。

## 1. 一次性安装

### 环境要求

- Python >=3.11，推荐 Python 3.12。
- 当前在 macOS / Python 3.12.16 / Git 2.50.1 上验证。文件读取和启动器依赖 POSIX，暂不支持 Windows；Linux 尚未实机验证。
- Git 模式建议 Git 2.50.1 或更新版本，旧版本未验证。普通目录模式无需 Git。
- 默认盘点无需 API Key、Docker；真实审查需要私有模型配置，隔离测试/自动修复需要已准备的固定镜像及Docker。安装依赖可能需要网络，盘点离线运行。

**在可信的 ReviewFlow 工具目录安装，不在待检查的陌生项目中安装或运行其脚本：**

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

仅盘点没有第三方运行时依赖。开发者运行全部测试时改装 `'.[dev,review]'`，其中 `review` 提供 Pydantic 2 和 LangGraph；新增后端命令也使用该可选依赖组。

### 安装短命令

以下是由用户选择执行的一次性安装：

```bash
.venv/bin/python -I -m reviewflow.install --bin-dir "$HOME/.local/bin"
```

启动器绑定安装时的 ReviewFlow 虚拟环境；以后不用每天激活虚拟环境。安装器不使用 sudo、不修改系统 Python、不写 shell 配置；发现目标已有 `review` 文件、目录或软链接会拒绝覆盖。

若安装目录不在 PATH，在当前终端执行：

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v review
review --help
```

需要新终端也生效时，**检查自己的 shell 配置后手动添加**上述 PATH 行（zsh 通常为 `~/.zshrc`）。不要反复追加相同配置。若已有别的同名工具，先检查 `command -v review`；也可用 `~/.local/bin/review` 的完整路径明确选择本工具。

### 本次开发环境的实际状态

本次仅在 ReviewFlow 的 `.venv/bin/review` 创建并验证了启动器，**未安装到 `~/.local/bin`，未修改 PATH 或 shell 配置**。

在这台机器上可用以下一次性、仅当前终端的设置：

```bash
export PATH="/Users/bred/PycharmProjects/project/reviewflow/.venv/bin:$PATH"
command -v review
review --help
```

之后切换到任何项目目录即可使用短命令。这个绝对路径只是当前开发位置，不是其他机器的安装要求。若终端已激活本项目 `.venv`，通常已在 PATH 中，仍建议用 `command -v review` 确认。

### 本机 libexpat 问题

本机 Homebrew Python 曾因加载系统旧版 libexpat 出现 `pyexpat` 缺失符号错误。仅遇到同类问题时，在安装前使用：

```bash
export DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib
```

如需把修复仅绑定到新启动器，在**尚未存在该启动器**时执行：

```bash
.venv/bin/python -I -m reviewflow.install \
  --bin-dir "$HOME/.local/bin" \
  --library-path /opt/homebrew/opt/expat/lib
```

本项目 `.venv/bin/review` 已包含显式传入的本机库路径，不需要重复安装。该路径不是通用默认值，也不应照搬到其他机器；无需关闭 TLS 校验。

## 2. 日常使用

```bash
# 在待检查项目目录中：默认简洁摘要
review

# 指定目录；含空格的路径加引号
review -d "/path/to/my project"
review --directory /path/to/project

# 检查磁盘上的当前文件，包括未提交修改
review --source directory

# 显示每个纳入和跳过条目
review --verbose

# 完整机器报告 / Markdown 报告
review --format json
review --format markdown

review --help
review --version
```

正确参数是 `-d` 或 `--directory`，不是 `-direcory`。不接受参数缩写。`--verbose` 只与默认 text 格式搭配，不能与 JSON / Markdown 搭配；后二者本身就是完整清单。

### “哪个目录”与“哪个版本”

| 选项 | 含义 |
| --- | --- |
| `-d PATH` / `--directory PATH` | 选择项目位置；相对路径基于调用时所在目录 |
| `--source auto`（默认） | Git 工作树读取固定的已提交 HEAD；普通目录读取当前文件 |
| `--source head` | 强制读取 Git HEAD，需要有效提交 |
| `--source directory` | 读取指定目录的当前文件，不扩展到 Git 根目录 |

**Git 默认不包含 staged、unstaged、untracked 修改。** 从 Git 子目录使用 auto/head 会扩展到仓库根目录，并显示警告。想看当前正在开发的内容，明确加 `--source directory`。

空 Git 仓库没有 HEAD，auto/head 会报错而不是偷偷换版本。本项目尚未首次提交时使用：

```bash
review --source directory
```

不需要先 commit 才能使用目录盘点。目录模式不具备后续 Git 自动修复资格，不代表文件不能被读取。

## 3. 如何理解输出

默认摘要显示项目、来源、纳入量、跳过量、预算和完整快照 ID，不再刷屏输出所有文件哈希；普通下划线直接可读。控制字符及双向格式字符会转为可见转义，避免终端操作注入。

- **盘点完成不等于代码审查通过。** 没有找出问题列表，不代表代码没有问题。
- 纳入数量仅表示读取并记录了内容哈希，不是已审查的文件数。
- 被剪枝目录的内部文件数未知；不能把它记成零遗漏。
- 预算未耗尽也可能有过滤、二进制、非 UTF-8 等跳过。
- 快照 ID 覆盖输入模式、固定提交、策略和清单记录；不包含绝对根目录、分支或 dirty 状态，也不包含被跳过文件的完整内容。它不是来源签名或整个仓库的完整性证明。
- `git_repair_eligible` 仅是 Git 修复的输入前提，不代表内容已验证、补丁已批准或当前仓库满足全部修复限制。

每份报告包含：

```text
Inventory only: no semantic code review or target tests have been performed.
```

默认盘点不会自动保存报告（`session-*` 的显式保存另见第8节）。需要保存盘点时用终端重定向，建议输出到目标目录之外：

```bash
review -d /path/to/project --format json > /path/outside-project/inventory.json
```

重定向由 shell 创建/覆盖文件，不属于只读扫描；先确认输出路径不存在重要文件。不要将报告写进被扫描目录，否则它可能被本次或后续扫描纳入。失败时检查退出码和 stderr，不要把空输出文件当成功报告。

## 4. 预算与过滤

默认最多 200 个纳入文件、单文件 262144 字节（256 KiB）、总纳入 8388608 字节（8 MiB）。可显式设置正整数：

```bash
review --max-files 100 --max-file-bytes 131072 --max-total-bytes 4194304
```

路径按确定性顺序处理，超限有跳过原因。这些是纳入内容限制，**不是整个目录枚举、Git 元数据或未来多 Agent 运行的总资源上限**。避免扫描巨大或持续变化的目录。

主要过滤规则：

- 排除 `.git`、虚拟环境、缓存、构建输出、`node_modules`、临时工作区等。
- 排除 `.superpowers`、`.claude`、`.eggs` 和以 `.egg-info` 结尾的目录，大小写不敏感。
- 不读取 `.env` / `.env.*`（包含 `.env.example`）、指定凭据名称及密钥后缀。
- 不跟随软链接，不递归读取子模块；不读取特殊设备文件。
- 只纳入受支持的源码/文本/配置类型；跳过二进制、非 UTF-8 内容。

这不是完整 `.gitignore` 解释器；Git ignore 与 ReviewFlow 过滤不是同一件事。文件名过滤无法发现藏在普通 `.py` 中的所有秘密，**不构成代码上传授权**。

## 5. 安全边界与兼容入口

新启动器在解释器启动时使用 `-I`，不从目标当前目录或 `PYTHONPATH` 导入 `sitecustomize.py`、同名标准库或 ReviewFlow 模块；只按受控读取接口读目标文件。它不是操作系统沙箱：ReviewFlow 本身的安装、解释器、依赖和安装目的目录必须可信。

保留旧入口 `reviewflow inspect PATH`，默认 Markdown；新 `review` 默认 text。JSON schema 1 保持兼容。从陌生目标目录运行时应使用新安全启动器；诊断时也可用可信解释器的绝对路径：

```bash
/absolute/path/to/reviewflow/.venv/bin/python -I -m reviewflow.quick -d /path/to/project
/absolute/path/to/reviewflow/.venv/bin/python -I -m reviewflow inspect /path/to/project
```

普通 `reviewflow` console script 没有新启动器的启动前隔离保证，不推荐从不可信目标目录直接启动它。目录读取不是原子文件系统快照，发现变化会失败，需要重新盘点。

2A 的 `ReviewContext` 只读取快照中纳入的路径，每次检查大小、SHA-256、UTF-8 和行范围；单片段最多 200 行、32768 UTF-8 字节，不静默截断。行号按 LF 划分、保留 CRLF。候选证据必须属于任务范围；Verifier 的反证可来自其他纳入文件。结构和来源校验不能证明模型的语义判断正确，也不能证明测试已通过。

## 6. 故障排查、迁移和卸载

| 现象 | 处理 |
| --- | --- |
| `command not found: review` | 检查安装位置和 PATH；运行 `command -v review`，或先用启动器绝对路径 |
| 运行的是其他同名工具 | 检查 PATH 优先级；明确使用本工具启动器路径，不覆盖其他工具 |
| 安装提示已存在 | 先查看该文件内容和归属，确认是否本工具的旧启动器；不要盲目删除或覆盖 |
| 提示无 HEAD | 使用 `--source directory` 检查当前文件 |
| 文件在读取期间变化 | 停止后台写入，重新运行；不要把变化后的文件套用到旧快照 |
| 权限或 Git 失败 | 查看 stderr；不要使用 sudo 扩大目标读取权限来掩盖问题 |
| 项目移动或 `.venv` 重建后不能启动 | 启动器绑定旧绝对路径；检查旧启动器，确认归属后移除并从新环境重新安装 |

退出码：`0` 盘点完成（可能跳过文件）；`2` 参数/输入错误；`1` 采集或工具失败。stdout 只包含最终报告，stderr 包含错误。

已知限制：

- Markdown 渲染器可能将 `__init__.py` / `_private_.py` 中的下划线解析为强调标记，显示时丢失下划线。该非阻塞显示问题尚未修复；需要精确文件名时使用默认 text 或 JSON。
- 根目录解析遇到权限拒绝时可能返回 `2` 而非 `1`，但不会报告成功。
- 没有 Git 时，普通目录若同时包含 `HEAD`、`objects/`、`refs/` 可能被保守拒绝，可显式指定 directory。

卸载短命令：先查看启动器路径和内容，确认它由 ReviewFlow 安装，再只删除该 `review` 文件；如曾手动添加专用 PATH 配置，检查后移除对应行。不要删除别的同名命令，不需要删除目标项目或系统 Python。安装器不自动覆盖或自动清理旧文件。

## 7. 后续阶段

LangGraph 模拟流程与发送审批/费用门禁保留。现新增 `review --review --development`：使用 Responses HTTP 适配器，读取明确的可信工具配置，先预览范围与运行摘要，追加 `--approve 摘要` 后才联网。最多8次请求、正文64 KiB、输出2048 tokens、单次60秒、无重试/重定向；金额未知，不宣称美元硬预算。HTTP 仅在此显式开发模式允许，普通配置仍要求 HTTPS，正式计费模式未启用。完整命令见 [模型配置指南](model-setup.md)。默认 `review` 和 `--demo` 不会因 Key 文件存在而自动联网；真实 partial/failed 保留外部报告并退出1。报告不会授权修复，也不进入模拟 session-resume。

详细设计：[CLI 体验](superpowers/specs/2026-11-02-cli-experience-design.md)、[结构化审查](superpowers/specs/2026-11-02-structured-review-design.md)。

## 8. 保存、查看与重新运行离线会话

已安装短命令时，在任意可信调用位置使用以下命令；否则在工具根目录将 `review` 换成 `.venv/bin/review`。每次都明确指定同一来源/目录，不从保存的 JSON 自动选目标。

```bash
# 1. 创建：output 必须是目标之外、父目录已存在的全新目录
review session-start --demo -d /path/to/project --source directory \
  --output /outside/session-001

# 2. 查看：只校验快照/证据并读取报告，不运行模型或补丁
review session-show -d /path/to/project --source directory \
  --session /outside/session-001

# 3. 选择报告中的 finding_id，只导出候选与任务，不自动修复
review session-export -d /path/to/project --source directory \
  --session /outside/session-001 --finding FINDING_ID --output /outside/selected-001

# 4. 在同一来源上重新完整执行一次模拟审查，追加 run-0002.json
review session-resume --demo -d /path/to/project --source directory \
  --session /outside/session-001
```

`session-start` 另支持 `--mode single|multi|multi-no-verifier`。创建/重新运行未写 `--demo` 时拒绝，不静默联网。源码变化或换了根目录后不能套用旧会话，应创建新会话；相同内容位于不同根目录也会拒绝。

会话包含 `run-0001.json` 等递增报告（最多100版），目录权限700，报告文件600，已有报告不覆盖。报告为 JSON，包含覆盖、候选、复核和分组建议、检索证据、模拟计量；不包含凭据或批准令牌。`session-export` 写 `task.json` 与 `finding.json` 到全新外部目录。**导出结果不是已验证缺陷，更不是应用补丁许可。**

`session-start/resume` 完整模拟退出0，partial/failed 退出1但保留报告，输入错误退出2。`session-show/export` 成功执行退出0；查看时仍应检查 `report.status`，不能仅凭退出码判断审查完成。

`resume` 并非从中断的 LangGraph 节点继续：它重新校验输入，使用新的进程内许可和新的有界模拟计量，从头运行。没有实时费用累计恢复、修复工作区恢复或跨进程发布权限。磁盘报告不具备真实性签名，允许编辑文件的人可能伪造文字内容；程序只检查结构/来源，**不能将其当作语义或执行证明**。

## 9. 专用配置文件

本机统一模型配置位置：`/Users/bred/PycharmProjects/project/reviewflow/secrets.local.toml`。将 `provider`、`api_type`、`model`、`base_url`、`api_key` 五项都写在这个文件的顶层，不加 `[model]`。`config.local.toml` 只保留 `secrets_file = "secrets.local.toml"`，不需要填写 budget/privacy。

程序默认每轮$1、每次请求$0.25、最多12次调用、60秒超时、输入16000/输出4000 token、0自动重试。真实单价由未来适配器核实，未知价格会阻止发送，不按免费处理。候选范围从 `-d` 和盘点纳入文件生成；上传与遥测仍关闭，选择目录不等于批准上传。

不要求使用环境变量。两者已 Git 忽略，秘密文件权限600；不要上传真实 Key，即使之后删除也不能消除 Git 历史。源码包不含这两个文件，新机器按 [模型配置指南](model-setup.md) 创建。可显式执行 `review credentials-check --file /absolute/tool/secrets.local.toml`，只检查本地格式，不发送、不打印 Key。

**默认盘点、demo、session 均不读取真实 Key；填完 Key 也不会自动接通模型。** 简化配置的显式 `config-check/prepare` 会读取私有文件获取模型元数据，因此报告 `credentials_read=true`，但不打印/发送 Key。服务商 API 尚待确定并适配，真实调用和执行仍需单独批准。
