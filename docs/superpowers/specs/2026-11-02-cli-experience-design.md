# ReviewFlow 简洁命令与终端体验设计

日期：2026-11-02。状态：已实现并通过离线测试、Ruff 与真实启动器验证；短命令仅安装在项目 `.venv/bin`，未进行用户级安装或 shell 配置修改。使用步骤见 [用户指南](../../user-guide.md)。

## 1. 目标与范围

用户在任意待检查项目目录打开终端，执行 `review` 即可开始；也可通过 `review -d PATH` 指定项目。不要求每次激活虚拟环境、输入 Python 模块路径或手工设置机器环境变量。

本次改进命令入口、报告可读性、开发辅助目录过滤和安装体验。不改变输入来源、安全约束及审批规则；没有真实模型配置时依然只是文件盘点，不能把命令名称当作已完成 AI 审查的证明。

## 2. 命令合约

| 命令 | 行为 |
| --- | --- |
| `review` | 对当前工作目录进行盘点，来源 auto，输出终端摘要 |
| `review -d PATH` | 对指定目录进行盘点；PATH 相对于调用者当前工作目录解析 |
| `review --directory PATH` | `-d` 的完整形式 |
| `review --source auto` | Git 工作树读取固定 HEAD；普通目录读取当前文件 |
| `review --source head` | 必须是有有效 HEAD 的 Git 工作树 |
| `review --source directory` | 读取当前文件，包括未提交修改，不扩展到 Git 根目录 |
| `review --verbose` | 摘要之后列出所有纳入及跳过条目 |
| `review --format text` | 默认，适合终端阅读的纯文本 |
| `review --format json` | 完整机器可读报告，不含摘要前缀 |
| `review --format markdown` | 完整 Markdown 报告 |
| `review --help` | 参数、默认行为与常见例子，无扫描、无模型调用 |
| `review --version` | 软件版本，无扫描 |

沿用 `--max-files`、`--max-file-bytes`、`--max-total-bytes` 及原默认值 200 / 262144 / 8388608。

`--verbose` 只用于 text；与 json/markdown 组合返回参数错误，避免用户不清楚它是否改变机器报告。参数不接受缩写，`-direcory` 等拼写错误应提示正确用法，不能静默当作路径。

`--directory` 选择“哪个项目”，`--source` 选择“哪个版本”，两者不可混淆。空 Git 仓库仍明确报错并建议 `--source directory`。Git 子目录扩大范围和未提交修改排除必须在摘要可见。

### 兼容已有调用

- 保留 `reviewflow inspect PATH` 和 `python -I -m reviewflow inspect PATH`。
- 旧入口默认 Markdown，避免改变已使用它的脚本；新 `review` 默认 text。
- JSON 顶层及 schema version 1 不变；新显示层不改变文件哈希算法。
- 新旧入口复用同一个参数校验、采集及退出码实现，不维护两套扫描逻辑。

## 3. 终端输出

示意（数据仅为示例）：

```text
ReviewFlow · 项目文件盘点

项目      /path/to/project
来源      当前文件（包括未提交修改；无 Git 修复资格）
结果      盘点完成，不代表代码审查通过

纳入      24 个文件 · 86.2 KiB
跳过      2 个文件/条目 · 6 个目录（目录内部文件数未知）
预算      未达到限制
上限      200 个文件 · 单文件 256.0 KiB · 总计 8.0 MiB
快照      <完整快照 ID>

注意
  · 尚未进行代码逻辑审查或执行目标测试
  · 其他来源、跳过或预算警告按实际状态列出

使用 --verbose 查看完整文件清单。
Inventory only: no semantic code review or target tests have been performed.
```

### 显示规则

- 默认不输出每个文件和每个 SHA-256，控制终端信息量；快照 ID 保留。
- verbose 按确定性路径顺序逐条显示路径、类型、字节数、跳过原因及纳入文件哈希。用缩进列表，不依赖中文字符宽度计算或 Markdown 表格。
- 终端不生成 HTML 实体；正常下划线直接显示。路径中的控制字符、换行、双向格式控制符必须转换为可见转义，禁止注入 ANSI 操作。
- Markdown 保留安全转义，但普通名称里的下划线不必全部转为 HTML 实体。表格管道、反引号、HTML、特殊换行仍须正确处理。
- stdout 只输出最终结果，stderr 只输出诊断；默认没有进度动画、颜色、交互问答或自动打开浏览器，重定向行为一致。
- JSON/Markdown 总是完整报告，摘要不改变覆盖统计。
- 退出码仍为 0 成功盘点（允许跳过）、2 输入/参数错误、1 采集/工具失败。不得输出部分报告后宣称成功。

## 4. 默认排除改进

在原规则基础上，大小写不敏感地排除 `.superpowers/`、`.claude/`、`.eggs/` 及目录名以 `.egg-info` 结尾的目录。在普通目录模式剪枝；在 HEAD 模式过滤其已跟踪文件。

- 仍记录跳过原因 excluded_directory，剪枝目录内部文件数未知。
- 不自动加载或执行目标的配置文件。
- 此次不实现完整 `.gitignore` 解释器，文档明确工具过滤与 Git 忽略不同。
- 过滤规则变化可能改变快照 ID 和文件计数，这是预期，不宣称前后同一快照。
- 文件名规则不是全面秘密检测，不因此授权上传源代码。

## 5. 一次性安装与启动隔离

### 选择

采用仓库内的安装器生成 POSIX shell 启动脚本，而不依赖 shell alias、目标目录中的脚本，或全局 pip 安装。依赖仍放在 ReviewFlow 自身虚拟环境。

一次性安装命令（已实现并验证；由用户选择执行）：

```bash
# 在可信的 ReviewFlow 工具目录执行
.venv/bin/python -I -m reviewflow.install --bin-dir "$HOME/.local/bin"
```

可选 `--library-path PATH` 将本机需要的动态库路径写入这个启动脚本，不修改 shell 的全局 DYLD_LIBRARY_PATH。本机可显式传 `/opt/homebrew/opt/expat/lib`，不得在通用程序里硬编码该机器路径。

### 启动脚本职责

1. 使用安装时固定的虚拟环境 Python 绝对路径，保留虚拟环境入口，不 resolve 到系统解释器。
2. 在 Python 启动前就传入 `-I`，再调用短命令模块。不能先运行普通 Python console script，再在程序内补隔离。
3. 使用正确 shell 引号传递解释器、库路径和全部用户参数（`"$@"`），不拼接 eval。
4. 不切换工作目录，从而让 `review` 的 `.` 始终表示用户所在项目。
5. 在目标目录设置恶意 `sitecustomize.py`、`json.py`、伪造 reviewflow 包、PYTHONPATH 时，也不能加载这些内容。
6. 检查目标启动文件：存在文件/目录/软链接时拒绝覆盖并给出建议，不能抢占同名命令。不使用 sudo，不改系统 Python。
7. 安装器只创建用户明确选择的目录及其中的 review 文件；不会自动修改 `.zshrc`、`.bashrc`、`.profile` 或其他 shell 文件。
8. 若安装目录不在 PATH，明确提示需由用户添加；不会谎称已经可以在新终端直接使用。已有同名命令也要提示用户检查 `command -v review`。
9. 项目移动、虚拟环境删除或重建后需要重新安装启动器。卸载只删除确认属于本工具的启动脚本，不清理项目、虚拟环境或用户文件。

开发中只在 pytest 临时目录和本项目 `.venv/bin` 验证安装器，不写用户级目录；实际安装到用户环境时单独确认。

## 6. 用户文档

新增 `docs/user-guide.md`，README 保留简短入口并链接到指南。指南包括：首次安装、环境修复、PATH 配置、当前目录/指定目录、HEAD 与当前文件区别、三种格式、详细清单、过滤规则、退出码、故障排查、迁移与卸载、安全边界。

明确区分“已实现且已验证的命令”和后续阶段计划。不再让用户把每次激活 venv 或输入长模块命令当作日常操作；保留长命令用于诊断和兼容。

## 7. 验收

- 无参数、-d、--directory、相对路径、空格和中文路径均正确。
- 默认摘要不含逐文件哈希与 HTML 实体；verbose 仍保留正常下划线，危险控制字符不可执行。
- JSON 可解析、无混入日志，旧接口和原有测试兼容。
- 短命令在临时目标目录启动，污染 PYTHONPATH 后仍不执行目标代码；前后文件字节保持不变。
- 安装器对现有文件/软链接拒绝覆盖；参数中空格/单引号无 shell 注入；库路径只作用于启动器。
- 新辅助目录先过滤后读取，Git/目录两种来源行为一致。
- 全量 pytest、Ruff 和真实启动器 smoke 通过后才能把文档状态改为已实现。
