# ReviewFlow

[English](README.en.md)

ReviewFlow 是面向发布前检查的多智能体审查工具，用于 COMP5584。它先盘点仓库，再由五个角色阅读 Python 与 Java 源码、核实问题并给出修改。你按编号确认之后，修改才会写入文件。

本地目录直接写回你确认的文件。Git 仓库则在独立工作区里生成补丁，并在容器中跑测试，通过后再写回选定源码。

## 功能

- **仓库盘点。** 列出纳入与跳过的文件、体积和快照，不调用模型，也不运行项目测试。
- **五角色审查。** Planner 分派任务，Correctness 与 Security 分别查找功能问题和安全问题，Verifier 只保留成立的问题，Patch 生成完整替换文本。
- **本地确认写入。** `local-fix` 把建议存成预览。`--list` 查看编号，`--apply` 按编号写入，这一步不再调用模型。
- **Git 工作区修复。** `repair-run` 先给出预览摘要。批准后在工作区中测试候选补丁，通过门禁后写回选定源码，并留下报告与 diff。

## 环境

- Python 3.11 或更高版本
- 审查与修复需要可选依赖：Pydantic 2、LangGraph 1
- Git 仓库修复需要本机 Git，以及一个已经准备好的 Docker 镜像
- 终端进度、清单和报告使用英文

## 安装

在 ReviewFlow 目录中执行。

Windows：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,review]"
.\review --help
```

`review.cmd` 会调用当前目录虚拟环境里的 Python。

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,review]'
.venv/bin/review --help
```

## 配置

在 ReviewFlow 目录放置两个私有文件。它们已被 `.gitignore` 排除，不要提交密钥。

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

`api_type` 保持 `responses`。`base_url` 写到 `/v1`，程序会自行请求 `/responses`。`local-fix` 默认读取这份配置，命令里不必再写 `--config`。

检查配置是否可被读取：

```powershell
.\review config-check --config config.local.toml
```

## 命令

以下示例在 ReviewFlow 目录中运行。Windows 使用 `.\review`，macOS / Linux 使用 `review` 或 `.venv/bin/review`。

### 盘点

```powershell
.\review
.\review -d E:\path\to\project
.\review -d E:\path\to\project --source directory --verbose
.\review -d E:\path\to\project --format json
```

`--source auto` 在 Git 仓库中读取已提交的 `HEAD`。要连同当前未提交文件一起盘点，使用 `--source directory`。

### 离线演示

不调用模型，用自带样例走完角色流程：

```powershell
.\review --demo -d examples\demo-project --source directory
.\review --demo --mode single -d examples\demo-project --source directory
```

`multi` 是默认模式。`single` 使用综合审查角色。`multi-no-verifier` 省去复核。

### 本地审查与写入

第一次命令调用五个角色，把建议写入 `local-fix.preview.json`，不改目标文件。审查 Python 与 Java 源码，并跳过测试文件。运行时终端会显示当前批次和正在等待的角色。

```powershell
.\review local-fix -d E:\path\to\project
```

再次查看同一份预览，不调用模型：

```powershell
.\review local-fix -d E:\path\to\project --list
```

按编号写入。在 PowerShell 中给编号加上引号：

```powershell
.\review local-fix -d E:\path\to\project --apply "1,3,5"
.\review local-fix -d E:\path\to\project --apply "4-6"
.\review local-fix -d E:\path\to\project --apply "except 4"
.\review local-fix -d E:\path\to\project --apply
```

| 写法 | 结果 |
| --- | --- |
| `--apply "1,3,5"` | 只写入这几个编号 |
| `--apply "4-6"` | 写入连续编号 |
| `--apply "except 4"` | 写入其余编号，并跳过会清空原文件的项 |
| `--apply` | 写入所有不会清空原文件的项 |

每一条命令只处理预览里还剩下的编号。写完最后一项后，预览文件会被删除。下一次不带 `--list` 或 `--apply` 的 `local-fix` 会重新调用模型，并覆盖现有预览。

### Git 仓库修复

目标需要是已有提交的 Git 仓库。先预览，再把打印出的摘要原样加回命令：

```powershell
.\review repair-run --development --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:你的镜像摘要 --output E:\path\to\new-report
.\review repair-run --development --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:你的镜像摘要 --output E:\path\to\new-report --approve 预览摘要
```

`--include` 可重复，用来指定相对路径。`--output` 必须是目标仓库之外、尚不存在的新目录。批准后的运行会在工作区中执行容器测试，并把通过门禁的修改写回选定源码。报告目录中保留 `report.json`、`report.md` 和 `candidate.diff`。

只跑已提交版本的测试、不调用模型：

```powershell
.\review repair-run --development --tests-only --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:你的镜像摘要 --output E:\path\to\new-test-report
```

同样先看预览，再追加 `--approve`。

## 五个角色

角色由 `src/reviewflow/agents/SOUL.md`、各自的 `AGENT.md` 和一项技能组成。审查、预算和写回由程序控制。

| 角色 | 作用 |
| --- | --- |
| Planner | 把本批文件分成正确性任务和安全性任务，不判断代码对错 |
| Correctness | 查找功能缺陷，看不到 Security 的结论 |
| Security | 查找输入、权限和危险操作问题，看不到 Correctness 的结论 |
| Verifier | 根据证据接受、拒绝或搁置发现；只有接受的发现才会进入修改 |
| Patch | 为接受的发现生成完整文件和说明，不直接写入磁盘 |

`general` 只用于 `--mode single`，把正确性与安全性放在同一次审查里。

## 开发检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

不要提交 API Key、`secrets.local.toml`、`config.local.toml` 或 `local-fix.preview.json`。
