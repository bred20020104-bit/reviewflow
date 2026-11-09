# 第一阶段：只读仓库清单与覆盖报告

日期：2026-11-02。状态：第一阶段实现及独立审查修复完成；158 项测试通过。保留根路径权限错误码的轻微待办，见 README；未提交或推送。

父设计：[ReviewFlow 系统设计](2026-11-02-reviewflow-design.md)。本文件细化第一阶段，不改变最终产品的多 Agent 目标。

## 目标与非目标

交付可在本地运行的 `reviewflow inspect PATH`，识别输入、固定来源、生成确定性的文件清单和覆盖报告，为后续 Planner 提供输入。

这是 **intake inventory**，不是代码审查。报告必须明确“尚未进行语义审查或执行目标测试”，不得把空 findings 表述为无问题。

本阶段无 LangGraph、LLM、API Key、Docker 执行、静态分析进程、补丁、Git 写入或 UI。只运行本项目自己的离线测试；不执行输入项目中的 Python、配置、hooks、测试、安装或构建脚本。

## 全局约束

- Python >= 3.11；本地开发优先 Python 3.12。
- 第一阶段运行时仅使用 Python 标准库；开发依赖仅 pytest、Ruff，构建后端使用 setuptools。
- 所有被检查项目均只读；不调用网络、不调用模型、不运行目标代码、不写入目标目录。
- 默认限制：单文件 262144 字节、最多 200 个纳入文件、纳入内容总计 8388608 字节。
- 使用 UTF-8 严格解码；包含 NUL 或无法 UTF-8 解码的内容不得纳入。
- 不跟随扫描条目中的软链接、不读取 Git 子模块内容、不自动改变 Git 状态。
- 限制和过滤必须进入报告；文件清单不等于语义审查覆盖。

以上数值是为可控 MVP 提出的默认值，作为本阶段验收标准；后续可依据试跑调整，但调整必须更新规格与测试。

## CLI 合约

计划命令（尚未实现）：

```text
reviewflow inspect PATH [--source auto|head|directory] [--format json|markdown]
                      [--max-files N] [--max-file-bytes N] [--max-total-bytes N]
```

- `--source` 默认 `auto`：处于 Git 工作树内时读取该仓库根目录的 HEAD，否则读取指定普通目录。
- Git 仓库的子目录输入归一到仓库根目录，并在 warnings 中说明扩大的范围。
- `--source head` 强制要求 Git 工作树和有效 HEAD；空仓库、bare 仓库等报错，不静默改为目录扫描。
- `--source directory` 显式读取传入目录当前文件；即使位于 Git 工作树，也不扩为仓库根、不保证工作区无修改。报告标记 `git_repair_eligible=false`。
- `--format` 默认 `markdown`；JSON 输出有效的单个 JSON 对象，无前置日志污染。
- 所有限制必须为正整数；非法参数、无效路径和输入错误退出码为 2，诊断写 stderr。
- 正常生成清单退出码为 0，即使存在有明确原因的跳过项或预算截断。
- 工具故障、权限拒绝、文件读取中消失/改变等输入采集失败退出码为 1；不输出貌似完整的报告。
- 输出到 stdout，不提供自动写报告文件的选项，不提示输入凭据。

## 数据结构

使用 `dataclasses` 和 `Literal` 字面量类型约束，准确签名由实施计划锁定。报告 schema_version 为 `1`。

`ScanPolicy`：max_files、max_file_bytes、max_total_bytes，及构建时参数校验。

`SourceInfo`：mode（head/directory）、root、commit（可空）、branch（可空）、dirty（目录模式为 null）、warnings；提供只读派生属性 git_repair_eligible，仅当 mode=head 且 commit 非空时为 true，报告中显式输出。

`FileRecord`：path（仓库或目录根相对 POSIX 路径）、status（included/skipped）、reason（纳入时 null）、size_bytes（未知时 null）、sha256（仅纳入文件）、kind（source/test/documentation/config/other）。

`Snapshot`：schema_version、source、policy、按相对路径排序的 records、snapshot_id、warnings。不存储源码全文或凭据。snapshot_id 用规范化 JSON 的 SHA-256 计算，仅覆盖 mode、commit、policy、记录；排除绝对路径、分支名、dirty 和时钟，因此相同目录内容移动位置后保持相同 ID。它标识本次清单中的受检范围，不是整个文件系统的完整镜像。

`Coverage`：included_files、included_bytes、skipped_files、excluded_directories、skip_counts、budget_limited、semantic_review_performed=false、target_tests_executed=false。

报告顶层包含 snapshot 与 coverage，并包含固定声明：`Inventory only: no semantic code review or target tests have been performed.`

## 采集方式

### Git HEAD

用参数数组执行 Git，不使用 shell；禁止终端认证提示、交互分页和 fsmonitor。Git 命令不访问远程、不调用 checkout/diff/filters/hooks，也不递归读取子模块。

固定 `rev-parse --verify HEAD^{commit}` 结果后，使用该 commit 的 `ls-tree -r -z --full-tree` 枚举条目，通过固定提交关联的对象 ID 获取 blob；后续 HEAD 移动不会改变本次来源。

`status --porcelain=v1 -z --untracked-files=normal --ignore-submodules=all` 仅用于提示是否 dirty，不读取文件内容进行审查；不得以 index 或工作区文件替代 HEAD blob。读取文件大小后再应用单文件限制，防止先把大 blob 全部读入内存。

命令执行统一限时 10 秒，设置 `GIT_TERMINAL_PROMPT=0`、`GIT_OPTIONAL_LOCKS=0`，清除继承的 `GIT_DIR`、`GIT_WORK_TREE`、`GIT_INDEX_FILE`、`GIT_COMMON_DIR` 和 `GIT_CONFIG_*` 覆盖变量，避免读取错误仓库。只允许本阶段实现中固定的只读子命令，禁用可触发外部程序的功能；具体命令列表在实施计划中定义。

bare 仓库不接受；Git 未安装时普通目录仍可用，但存在 `.git` 标记或显式 head 输入时必须报工具不可用。Git 输出损坏、超时或权限错误不得静默当成普通目录。

### 普通目录

规范化所选根目录，拒绝直接作为输入的软链接根路径和非目录；子条目用不跟随软链接的方式检查。普通文件内容读取有界；读取前后元数据若改变则失败，避免生成不一致清单。不是对敌对并发文件系统的安全隔离保证，后续动态操作仍使用真正快照。

明确剪枝目录，不运行 Git 子仓库，不导入目标 Python 模块。相同排序和策略应产生相同纳入集合与 snapshot_id。

### 路径分类

文件名及目录排除匹配不区分大小写。后缀匹配使用小写形式。

剪枝目录名：`.git`、`.venv`、`venv`、`env`、`__pycache__`、`.pytest_cache`、`.ruff_cache`、`.mypy_cache`、`node_modules`、`dist`、`build`、`.idea`、`.vscode`、`.reviewflow`、`.worktrees`、`workspaces`、`target-repos`。

目录模式记录被剪枝的目录根为 skipped/excluded_directory，不枚举其内部文件；Git HEAD 清单中的同类路径按单个 tracked 文件排除。报告分开统计目录排除和文件排除，不能把“跳过一个目录”谎称“只漏一个文件”。

不读取或哈希以下文件内容：`.env`、`.env.*`（包括 `.env.example`）、`*.pem`、`*.key`、`*.p12`、`*.pfx`、`id_rsa`、`id_ed25519`、`credentials.json`。记录 skipped/sensitive_path。

注意：项目自身 `.gitignore` 允许提交占位 `.env.example`，不代表扫描器会读取目标项目中的同名文件。这是两个不同策略。

允许文本后缀：`.py`、`.pyi`、`.md`、`.rst`、`.txt`、`.toml`、`.yaml`、`.yml`、`.json`、`.ini`、`.cfg`、`.sql`。允许的无后缀/点文件名称：`Dockerfile`、`Makefile`、`.gitignore`。其他条目记录 skipped/unsupported_type。

分类顺序：Python 文件路径含 `tests` 目录或 basename 以 `test_` 开头则 test；其余 `.py/.pyi` 为 source；`.md/.rst/.txt` 为 documentation；其余允许文件为 config。其余为 other。

排除优先级：软链接或子模块 → 排除目录 → 敏感路径 → 不支持类型 → 单文件大小 → 文件数上限 → 总字节上限 → 二进制/编码检查。二进制/编码失败不消耗纳入预算；预算不足跳过的条目不必为判断编码而读取。

预算按相对路径字典序遍历；超出某项限制的文件记录原因后继续检查后续条目，不能截断报告。大文件不阻止后续更小文件被纳入。文件数限制只统计 included。

skip reason 枚举：symlink、submodule、excluded_directory、sensitive_path、unsupported_type、file_too_large、file_limit、total_bytes_limit、binary、invalid_utf8。

这些过滤规则并非完整秘密扫描。第一阶段无远程上传；模型接入前需另行设计本地敏感信息检测、脱敏与发送授权。

## 验收

- 普通目录含 Python、测试、配置和说明时，输出排序稳定、计数正确、可重复的 JSON/Markdown。
- `.env`、私钥、排除目录和范围外链接从不被读取为上下文；跳过记录可见。
- 支持含空格、Unicode 的文件名；Git NUL 分隔保证换行文件名不会被拆分。
- 未提交修改、暂存修改、未跟踪文件不会污染 HEAD 清单，dirty 提示可见，原工作区/index/HEAD 不变。
- 强制 head 的空仓库明确失败，不悄悄审查工作区；显式 directory 模式可用于其当前文件。
- 单文件、数量、总量、空目录、二进制、编码、软链接、读取失败、Git 超时都有测试。
- 报告永远说明没有进行语义审查和目标测试，没有“零漏洞”或“可上线”文案。
- 仓库中放置恶意 `sitecustomize.py` 或同名模块不会被执行；本工具不从目标目录启动解释器。
- 所有项目测试无需网络、真实 API、GitHub 凭据、Docker 或运行目标代码。

## 后续衔接

本阶段纳入文件的哈希和清单作为 Planner 上下文选择的依据；阶段二读取实际内容时必须验证仍匹配快照，不得仅复用旧文件路径读取已变化内容。

阶段二再实现符号索引、源码检索、真正 findings、模型接口和 LangGraph。阶段三再实现执行隔离、审批与 Git 写操作。第一阶段不提前创建空 Agent 桩类冒充功能。
