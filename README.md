# ReviewFlow

[English](README.en.md)

ReviewFlow 是一套已经完成的发布前审查系统，为 COMP5584 英文课程交付。它把仓库盘点、五角色大模型审查、人工编号确认和代码写回连成一条完整流水线。Python 与 Java 项目都可以直接审查。本地目录按你选定的编号写回；Git 仓库在独立工作区中完成补丁、容器测试和写回，原有提交保持完整。

终端进度、清单和报告全部使用英文，适合课堂演示与录屏。

## 已交付能力

ReviewFlow 已经把这条链路做完，并在真实 Java 项目上跑通。

| 能力 | 成果 |
| --- | --- |
| 仓库盘点 | 一次看清项目结构、纳入文件、体积和快照 |
| 五角色审查 | Planner、Correctness、Security、Verifier、Patch 顺序协作，实时打印当前步骤 |
| 证据化结论 | 每条建议都带标题、说明和具体改动，方便当场讲解 |
| 人工确认 | 按编号采纳、按区间采纳，或排除指定编号 |
| 本地写回 | 确认后直接更新目标项目中的 `.py` 与 `.java` 文件 |
| Git 工作区修复 | 在独立工作区生成补丁，用 Docker 运行测试，通过后写回选定源码 |
| 跨平台入口 | Windows 使用 `.\review`，macOS / Linux 使用 `review` |

一次完整的本地审查会分批阅读源码。每一批都先规划，再分别做正确性审查和安全审查，随后逐条复核，最后只为成立的问题生成修改。你始终掌握最终写回权。

## 五个角色

五个角色各有独立系统提示词：共享契约 `SOUL.md`、角色说明 `AGENT.md`，以及一项专业技能。提示词随请求发送，审查范围、调用次数和写回动作由程序掌控。

| 角色 | 职责 |
| --- | --- |
| Planner | 统筹本批文件，拆成正确性任务和安全性任务 |
| Correctness | 专注功能缺陷：错误结果、状态流转、异常处理和丢失的数据 |
| Security | 专注安全缺陷：输入、权限边界和危险操作 |
| Verifier | 对照源码证据独立裁决，接受、拒绝或搁置 |
| Patch | 为已接受的问题撰写完整文件和清晰说明 |

Correctness 与 Security 互相独立，互不参考对方的结论。Verifier 是质量关口，Patch 只处理已经接受的发现。单角色基线由 `--mode single` 启动，使用 `general` 在同一次审查中覆盖正确性与安全性。

## 环境与安装

需要 Python 3.11 或更高版本。审查流程使用 Pydantic 2 与 LangGraph 1。Git 修复使用本机 Git 和已准备好的 Docker 镜像。

在 ReviewFlow 目录执行。

Windows：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,review]"
.\review --help
```

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,review]'
.venv/bin/review --help
```

Windows 启动器是仓库根目录的 `review.cmd`，它调用本目录虚拟环境中的 Python。

## 模型配置

在 ReviewFlow 目录创建两个私有文件。`.gitignore` 已经排除它们，密钥留在本机。

`config.local.toml`：

```toml
secrets_file = "secrets.local.toml"
```

`secrets.local.toml`：

```toml
provider = "openai"
api_type = "responses"
model = "your-model-id"
base_url = "https://your-gateway.example/v1"
api_key = "your-key"
```

`api_type` 使用 `responses`。`base_url` 写到 `/v1`，程序会请求 `/responses`。`local-fix` 自动读取这份配置。

```powershell
.\review config-check --config config.local.toml
```

## 命令

示例在 ReviewFlow 目录中运行。Windows 使用 `.\review`，macOS / Linux 使用 `review` 或 `.venv/bin/review`。

### 1. 展示项目盘点

```powershell
.\review -d E:\path\to\project
.\review -d E:\path\to\project --source directory --verbose
.\review -d E:\path\to\project --format json
```

Git 仓库默认展示已提交的 `HEAD`。`--source directory` 展示当前工作区文件。这一步适合作为演示开场，让观众先看到项目规模。

### 2. 离线走通角色流程

```powershell
.\review --demo -d examples\demo-project --source directory
.\review --demo --mode single -d examples\demo-project --source directory
```

样例项目会完整走出审查、复核和报告。默认模式是 `multi`。

### 3. 正式本地审查

这条命令调用五个角色，分批审查项目中的 Python 与 Java 源码，并把建议保存为带编号的预览。终端持续显示批次、角色和模型等待状态。

```powershell
.\review local-fix -d E:\path\to\project
```

审查结束后，终端给出每一条建议的编号、标题、文件、说明和改动。重新展示同一份结果：

```powershell
.\review local-fix -d E:\path\to\project --list
```

### 4. 按编号写回

PowerShell 中请给编号加上引号。

```powershell
.\review local-fix -d E:\path\to\project --apply "1,3,5"
.\review local-fix -d E:\path\to\project --apply "4-6"
.\review local-fix -d E:\path\to\project --apply "except 4"
.\review local-fix -d E:\path\to\project --apply
```

| 命令 | 效果 |
| --- | --- |
| `--apply "1,3,5"` | 采纳指定编号 |
| `--apply "4-6"` | 采纳一段连续编号 |
| `--apply "except 4"` | 采纳其余编号 |
| `--apply` | 采纳当前预览中的全部可写入项 |

每次命令都基于预览中尚未写入的编号。可以先采纳一部分，再对剩余项继续选择。全部采纳完成后，本次预览结束。再次执行不带 `--list` 或 `--apply` 的 `local-fix`，会开始一轮新的五角色审查。

### 5. Git 仓库的专业修复

对于已有提交的仓库，ReviewFlow 在独立工作区中完成修复。先生成预览，再使用打印出的摘要批准执行：

```powershell
.\review repair-run --development --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:你的镜像摘要 --output E:\path\to\new-report
.\review repair-run --development --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:你的镜像摘要 --output E:\path\to\new-report --approve 预览摘要
```

`--include` 可以重复，用来指定多个相对路径。`--output` 是仓库外部的新目录。批准后的运行在容器中执行测试，并把通过验证的修改写回选定源码。报告目录保存 `report.json`、`report.md` 和 `candidate.diff`，适合作为修复过程的交付材料。

已提交版本的容器测试：

```powershell
.\review repair-run --development --tests-only --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:你的镜像摘要 --output E:\path\to\new-test-report
```

先查看预览，再追加 `--approve`。

## 推荐演示顺序

1. 用盘点命令展示目标项目的规模。
2. 用一分钟说明五个角色的分工和独立复核。
3. 展示一次 `local-fix` 的实时进度，或直接 `--list` 展示已经生成的编号清单。
4. 挑选几条清晰的逻辑修改，用 `--apply "编号"` 写回，并打开文件指出变化。
5. 口头介绍 Git 工作区路径：原提交保留，补丁在独立工作区中测试后再写回。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

私有模型配置和审查预览保留在本机，正式提交只包含 ReviewFlow 本身的源码、测试和说明。
