# Repository Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**执行状态（2026-11-02）：** 五项任务已完成；独立审查的重要问题已按回归测试红→绿修复，完整套件 158 passed，Ruff 检查及格式检查通过。所有“提交检查点”仅完成检查，按授权边界跳过 commit/push；勾选不表示已创建提交。保留一个轻微待办：根路径权限拒绝可能误用退出码 2，详见 README。Git 不可用时 bare 布局识别仍属保守启发式。其余后续阶段未实施。

**Goal:** 实现只读、离线的 `reviewflow inspect PATH`，产出可复现文件清单与明确标注限制的覆盖报告。

**Architecture:** 使用标准库 CLI、统一的只读 SourceReader 协议、Git HEAD 与目录两个来源适配器，以及共享策略/快照/报告模块。文件采集不执行目标代码，报告只陈述清单事实，不冒充 LLM 审查。

**Tech Stack:** Python >= 3.11（本机使用 3.12）、stdlib、setuptools；pytest、Ruff 为开发依赖。本阶段无 LangGraph/LLM/Docker/UI。

**Spec:** `docs/superpowers/specs/2026-11-02-repository-intake-design.md`；同时阅读父设计 `docs/superpowers/specs/2026-11-02-reviewflow-design.md`。

## Global Constraints

- Python >= 3.11；本地开发优先 Python 3.12。
- 第一阶段运行时仅使用 Python 标准库；开发依赖仅 pytest、Ruff，构建后端使用 setuptools。
- 所有被检查项目均只读；不调用网络、不调用模型、不运行目标代码、不写入目标目录。
- 默认限制：单文件 262144 字节、最多 200 个纳入文件、纳入内容总计 8388608 字节。
- 使用 UTF-8 严格解码；包含 NUL 或无法 UTF-8 解码的内容不得纳入。
- 不跟随扫描条目中的软链接、不读取 Git 子模块内容、不自动改变 Git 状态。
- 限制和过滤必须进入报告；文件清单不等于语义审查覆盖。
- 未经用户明确授权，不创建本项目的 commit 或 push。每项“提交检查点”只检查差异；用户授权提交时才 commit，并添加规定的 Co-Authored-By 行。
- 下面测试中的 Git 提交仅发生在 pytest 的临时 fixture 仓库，使用测试身份且不访问远程，不是提交本项目或用户仓库。

## Review Focus

- Git 工作树 dirty 或用户输入子目录：HEAD 内容保持固定，忽略的本地修改和扩大的根目录必须可见（Task 3）。
- 软链接、敏感文件和超大内容：不得先读取再过滤，目录剪枝不能伪装成只跳过单文件（Tasks 1、2、4）。
- Git 未安装、命令超时、空仓库、bare 仓库：按规格分类错误，不静默转为另一输入来源（Tasks 3、5）。
- 文件名含空格、Unicode、换行以及路径中的非安全片段：保留合法名称，拒绝越界；JSON/Markdown 不误解析（Tasks 2、3、5）。
- 输入在读取中变化、工作区状态变化或预算不足：失败或报告真实范围，绝不输出虚假的完成审查结论（Tasks 2、4、5）。

---

## 文件与接口地图

```text
pyproject.toml                      打包、console script、pytest/Ruff 配置
src/reviewflow/__init__.py           包元数据，不导入目标项目
src/reviewflow/__main__.py           python -m reviewflow 入口
src/reviewflow/errors.py             稳定的输入/采集/工具错误类型
src/reviewflow/models.py             不含内容的 dataclass/字面量类型与 SourceReader 协议
src/reviewflow/policy.py             路径过滤、类型分类、规则常量
src/reviewflow/sources/__init__.py    来源接口导出
src/reviewflow/sources/directory.py  只读目录枚举和有界读取
src/reviewflow/sources/git.py        固定 Git HEAD 来源与受限命令调用
src/reviewflow/snapshot.py           排序、预算、内容哈希、覆盖统计
src/reviewflow/report.py             JSON/Markdown 渲染
src/reviewflow/cli.py                参数、来源选择、退出码与输出通道
tests/conftest.py                    临时目录/Git 仓库 fixture
tests/test_models_policy.py
tests/test_directory_source.py
tests/test_git_source.py
tests/test_snapshot.py
tests/test_cli_report.py
```

测试位于本项目独立的 `tests/` 目录，不将测试放入受检项目。

所有公开数据类型集中于 Task 1，后续任务不得创造不兼容的同名类型。CLI Task 5 完成前不宣称已有可用命令。

## Task 1: 建立可测试的包、清单模型与路径策略

**Files:** 创建 `pyproject.toml`、`src/reviewflow/{__init__,errors,models,policy}.py`、`tests/test_models_policy.py`。

**Interfaces:**
- `SourceMode = Literal["head", "directory"]`，`EntryType = Literal["file", "directory", "symlink", "submodule"]`。
- `ScanPolicy(max_files: int = 200, max_file_bytes: int = 262144, max_total_bytes: int = 8388608)`：frozen dataclass，非正整数/布尔值抛 ValueError。
- `SourceInfo(mode: SourceMode, root: Path, commit: str | None, branch: str | None, dirty: bool | None, warnings: tuple[str, ...])`；只读属性 `git_repair_eligible: bool` 返回 `mode == "head" and commit is not None`，JSON/Markdown 显式渲染，加入 head/directory 的真假测试。
- `SourceEntry(path: str, entry_type: EntryType, size_bytes: int | None, object_id: str | None = None)`：相对 POSIX 路径，不含根目录；目录记录路径以 `/` 结尾。
- `FileRecord(path: str, status: Literal["included", "skipped"], reason: str | None, size_bytes: int | None, sha256: str | None, kind: Literal["source", "test", "documentation", "config", "other"])`。
- `Coverage(included_files: int, included_bytes: int, skipped_files: int, excluded_directories: int, skip_counts: dict[str, int], budget_limited: bool, semantic_review_performed: bool = False, target_tests_executed: bool = False)`。
- `Snapshot(schema_version: int, source: SourceInfo, policy: ScanPolicy, records: tuple[FileRecord, ...], snapshot_id: str, warnings: tuple[str, ...])`；`Inventory(snapshot: Snapshot, coverage: Coverage)`。
- `SourceReader` Protocol：`info: SourceInfo`、`entries() -> Iterable[SourceEntry]`、`read_bytes(entry: SourceEntry, *, max_bytes: int) -> bytes`。
- `InvalidInputError`、`CollectionError` 继承 Exception；`GitUnavailableError`、`GitCommandError` 继承 CollectionError。
- `path_reason(path: str, entry_type: EntryType) -> str | None`：按规格优先级返回路径级 skip reason，不打开文件。
- `classify_kind(path: str) -> Literal["source", "test", "documentation", "config", "other"]`。
- `is_excluded_directory(name: str) -> bool`：集中提供目录剪枝规则。

- [x] **Step 1: 写失败测试。** 测试默认值与非法值；对规格全部排除名称、允许后缀、大小写及分类做参数化测试。核心断言：

```python
def test_policy_defaults():
    assert ScanPolicy() == ScanPolicy(200, 262144, 8388608)

@pytest.mark.parametrize("name", [".env", ".env.example", "KEY.PEM", "id_ed25519"])
def test_sensitive_paths(name):
    assert path_reason(name, "file") == "sensitive_path"

def test_link_precedes_sensitive_name():
    assert path_reason(".env", "symlink") == "symlink"
```

- [x] **Step 2: 建立专用 `.venv` 并运行测试确认失败。** 使用 `/opt/homebrew/bin/python3.12 -m venv .venv`（其他机器使用 Python >=3.11），在该环境安装 pytest/Ruff；运行 `.venv/bin/python -m pytest tests/test_models_policy.py -q`，预期因 `reviewflow` 尚未实现而失败。只从本项目目录启动，不从目标样例目录启动。
- [x] **Step 3: 实现接口与打包。** setuptools 的 src layout，`requires-python = ">=3.11"`，运行时 dependencies 为空，dev extras 为 pytest/Ruff；pytest testpaths 为 tests，Ruff target-version 为 py311。先不注册未实现的 CLI 入口。路径敏感规则只判断名字，不读取内容。完整 skip reason 集合固定为规格中的 10 项。
- [x] **Step 4: 安装并验证。** `.venv/bin/python -m pip install -e '.[dev]'`；重跑该测试文件和 `.venv/bin/python -m ruff check src tests`，应全部通过。
- [x] **Step 5: 提交检查点。** 检查上述文件差异和无凭据情况；只有得到授权才暂存明确路径并创建 `feat: define inventory models and path policy` 提交，附规定 attribution。

## Task 2: 实现普通目录的只读来源

**Files:** 创建 `src/reviewflow/sources/{__init__,directory}.py`、`tests/test_directory_source.py`；复用 Task 1 模型/策略。

**Interfaces:**
- `DirectorySource(root: Path)` 实现 SourceReader；`info.mode == "directory"`，commit/branch/dirty 均为 None。
- entries 只返回元数据，不读取文件正文；排除目录作为单条 directory 条目保留并剪枝。
- read_bytes 只接受已枚举的普通文件和相对路径，拒绝绝对路径/`..`/NUL/软链接，使用不跟随链接的有界读取；读取前后变化或无法读取则抛 CollectionError。

- [x] **Step 1: 写失败测试。** fixture 含 `src/main.py`、`tests/test_main.py`、`.env`、`node_modules/a.js`、软链接和 Unicode 路径。断言：

```python
def test_directory_prunes_and_does_not_follow_links(sample_tree):
    source = DirectorySource(sample_tree)
    paths = {e.path for e in source.entries()}
    assert "node_modules/" in paths
    assert "node_modules/a.js" not in paths
    assert "outside_link" in paths
    assert source.info.mode == "directory"
```

还需 `test_root_symlink_rejected`、`test_non_directory_rejected`、`test_spaces_unicode_and_newline_names`、`test_read_rejects_escape`、`test_read_rejects_symlink_replacement`、`test_deleted_or_changed_file_fails`、`test_read_is_bounded`，分别断言 InvalidInputError/CollectionError 与严格 max_bytes 行为。

- [x] **Step 2: 运行 `.venv/bin/python -m pytest tests/test_directory_source.py -q`，确认因来源类尚未实现失败。**
- [x] **Step 3: 实现 DirectorySource。** 使用 os.scandir/lstat 和有界二进制读取；不要 os.chdir，不导入目标模块，不动态加载配置。只对纳入时请求的文件调用 read_bytes；记录元数据不等于打开内容。
- [x] **Step 4: 验证该测试文件和已有测试通过。** 对安全 fixture 比较运行前后的文件内容，确保未修改输入。文件变化测试使用 monkeypatch 控制时机，不能靠 sleep 制造随机竞争。
- [x] **Step 5: 提交检查点。** 检查改动；获批时提交 `feat: add read-only directory source`，附 attribution。

## Task 3: 固定 Git HEAD 的来源与来源选择

**Files:** 创建 `src/reviewflow/sources/git.py`、`tests/conftest.py`、`tests/test_git_source.py`。

**Interfaces:**
- `GitRunner.run(root: Path, args: tuple[str, ...]) -> bytes`：只支持本任务固定只读命令，10 秒超时、无 shell、非交互，失败抛 GitCommandError，缺 Git 抛 GitUnavailableError。
- `GitHeadSource(root: Path, *, runner: GitRunner | None = None)` 实现 SourceReader，固定 commit 并持有按 NUL 解析的 blob/模式清单。
- `open_source(path: Path, mode: Literal["auto", "head", "directory"]) -> SourceReader`：按规格选择，供 Task 5 使用。

命令限定为 `rev-parse --is-inside-work-tree`、`rev-parse --show-toplevel`、`rev-parse --verify HEAD^{commit}`、`symbolic-ref --quiet --short HEAD`、`status --porcelain=v1 -z --untracked-files=normal --ignore-submodules=all`、`ls-tree -r -l -z --full-tree <fixed-commit>`、`cat-file blob <enumerated-oid>`。fixed commit / object ID 必须来自 Git 返回值并校验十六进制格式，不能拼接用户 revision。`symbolic-ref` 的 detached HEAD 返回码按预期处理；“非 Git 目录”的探测失败只在确定属于此情况时允许 auto 选择 DirectorySource，其他错误不能吞掉。

每次 Git 执行设置规格要求的环境，额外禁用分页、颜色、untracked cache、hooks；隔离全局/系统配置并清除 inherited Git overrides，但保留必要 PATH。不写用户 Git 配置。对本阶段未使用的 Git 执行入口不声称已有安全保证。

- [x] **Step 1: 写失败测试。** 临时仓库内提交 `a.py = b"old\n"` 后修改为 `b"new\n"`、暂存另一个文件、新增未跟踪文件。核心断言：

```python
def test_head_ignores_working_changes(dirty_repo):
    source = GitHeadSource(dirty_repo)
    entry = next(e for e in source.entries() if e.path == "a.py")
    assert source.read_bytes(entry, max_bytes=262145) == b"old\n"
    assert source.info.dirty is True
    assert source.info.commit
    assert "untracked.py" not in {e.path for e in source.entries()}
```

还需 `test_head_stays_fixed_after_branch_moves`、`test_subdirectory_expands_with_warning`、`test_detached_head`、`test_empty_repo_head_fails`、`test_empty_repo_explicit_directory_works`、`test_bare_repo_rejected`、`test_missing_git_plain_directory_works`、`test_missing_git_git_input_fails`、`test_git_timeout_not_directory_fallback`、`test_nul_delimited_names`、`test_submodule_and_symlink_not_read`、`test_inherited_git_environment_does_not_redirect_repo`。

- [x] **Step 2: 运行 `.venv/bin/python -m pytest tests/test_git_source.py -q`，确认因实现缺失而失败。** Git fixture 使用临时 user.name/user.email 和禁用 hooks 的配置，不读取或改变全局身份，不创建远程。
- [x] **Step 3: 实现 runner、来源和选择。** `ls-tree -l` 的大小可用于读取前限制；只读取策略纳入的 blob。Git dirty 警告只保存事实，不把未跟踪敏感文件正文读入。异常转换保留有用诊断但不打印环境变量/凭据。
- [x] **Step 4: 重跑所有来源测试。** 比较受检 fixture 的 HEAD、index 内容和工作区文件前后未变；自身 fixture 设置产生的提交不计为工具修改。清晰区分“不存在 HEAD”的输入错误与 Git 工具故障。
- [x] **Step 5: 提交检查点。** 获批时提交 `feat: inspect immutable Git HEAD snapshots`，附 attribution。

## Task 4: 构建预算受控的快照与覆盖统计

**Files:** 创建 `src/reviewflow/snapshot.py`、`tests/test_snapshot.py`。

**Interfaces:**
- `build_inventory(source: SourceReader, policy: ScanPolicy) -> Inventory`。
- `compute_snapshot_id(source: SourceInfo, policy: ScanPolicy, records: tuple[FileRecord, ...]) -> str`：规范化 JSON 使用 sort_keys=True、separators=(",", ":")、ensure_ascii=False，UTF-8 后 SHA-256；只纳入 mode/commit/policy/records。
- records 按 path 排序；path_reason 在任何正文读取前执行。included 记录内容 SHA-256，不保留正文；各跳过原因来自规格固定枚举。

- [x] **Step 1: 写失败测试。** 使用内存 FakeSource 实现 Task 1 协议并记录 read_bytes 调用，确保过滤和预算不是“读完再丢弃”。

```python
def test_sensitive_and_large_files_not_read(fake_source):
    inventory = build_inventory(fake_source, ScanPolicy(max_file_bytes=10))
    assert ".env" not in fake_source.read_paths
    assert "large.py" not in fake_source.read_paths
    assert inventory.coverage.semantic_review_performed is False
    assert inventory.coverage.target_tests_executed is False
```

增加 `test_sorted_deterministic_selection`、`test_count_limit_only_counts_included`、`test_total_budget_allows_later_smaller_file`、`test_exact_limit_inclusive`、`test_binary_and_invalid_utf8`、`test_excluded_directory_count_not_file_count`、`test_empty_inventory`、`test_same_contents_different_roots_same_id`、`test_changed_included_content_changes_id`、`test_read_error_aborts_without_partial_inventory`。参数化精确边界，不能只测“某个大文件”。

- [x] **Step 2: 运行 `.venv/bin/python -m pytest tests/test_snapshot.py -q`，确认实现缺失而失败。**
- [x] **Step 3: 实现排序、预算与哈希。** 按规格顺序判断；文件数/字节仅在 UTF-8/NUL 检查通过后累加。skipped_files 不含 excluded_directory 条目，excluded_directories 单独统计；skip_counts 计数所有跳过记录。budget_limited 仅由 file_limit/total_bytes_limit 触发；file_too_large 属文件策略限制，仍显示独立原因。warnings 明确提示所有未检查范围。
- [x] **Step 4: 运行模型、来源和快照测试，并对相同 fixture 重复采集比较 snapshot_id。** 数据结构中不能出现源码正文、API 凭据或动态时间戳。
- [x] **Step 5: 提交检查点。** 获批时提交 `feat: build deterministic inventory and coverage`，附 attribution。

## Task 5: CLI、报告与端到端验收

**Files:** 创建 `src/reviewflow/{cli,__main__,report}.py`、`tests/test_cli_report.py`；修改 `pyproject.toml`、`README.md`、`CONTRIBUTING.md`。

**Interfaces:**
- `render_json(inventory: Inventory) -> str`：一个 JSON 对象，schema 保持规格定义，将 Path 转为字符串；有效 Unicode，不含日志前缀。
- `render_markdown(inventory: Inventory) -> str`：摘要、来源、警告、纳入表、跳过表和固定免责声明；转义 Markdown 的管道、反引号及换行文件名，避免条目污染表格。
- `main(argv: Sequence[str] | None = None) -> int`：argparse，stdout 输出报告，stderr 输出错误；返回规格约定 0/1/2。
- console script `reviewflow = "reviewflow.cli:main"`；`python -m reviewflow` 调用同一个入口。

- [x] **Step 1: 写失败测试。** CLI 不存在时帮助/集成测试失败。对两种格式检查固定声明：

```python
def test_json_is_inventory_not_review(tmp_path, capsys):
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    assert main(["inspect", str(tmp_path), "--format", "json"]) == 0
    out = capsys.readouterr()
    data = json.loads(out.out)
    assert out.err == ""
    assert data["coverage"]["semantic_review_performed"] is False
    assert data["coverage"]["target_tests_executed"] is False
    assert data["notice"] == "Inventory only: no semantic code review or target tests have been performed."
```

增加 `test_help_without_model_credentials`、`test_invalid_limits_exit_2`、`test_missing_path_exit_2`、`test_empty_head_exit_2`、`test_collection_error_exit_1_no_report`、`test_markdown_escapes_paths`、`test_json_stable_except_root_metadata`、`test_console_script_and_module_entrypoint`。用 subprocess 从工具目录调用真实 CLI；目标 fixture 放置一旦导入就创建 marker 的 `sitecustomize.py`，运行后断言 marker 不存在。不要通过实际运行恶意代码来设置预期。

- [x] **Step 2: 运行 `.venv/bin/python -m pytest tests/test_cli_report.py -q`，确认 CLI/报告未实现导致失败。**
- [x] **Step 3: 实现报告与 CLI。** 不自动保存报告、不写目标目录、不启动 Web 服务。Git 子目录扩展、dirty 排除和预算限制清晰呈现，零纳入文件也不宣称安全。JSON 顶层为 snapshot/coverage/notice。
- [x] **Step 4: 完整离线验证。** 重新 editable install 注册入口，运行 `.venv/bin/python -m pytest -q`、`.venv/bin/python -m ruff check src tests`、`.venv/bin/python -m ruff format --check src tests`、`.venv/bin/reviewflow --help`、`.venv/bin/python -m reviewflow --help`。使用团队自建普通目录和临时 Git fixture 做两次 CLI 演示并核对输入未变；不得直接执行目标测试。
- [x] **Step 5: 更新文档为真实状态。** README 区分“已实现的 inventory”与“计划中的多 Agent 审查”，加入经过实际验证的 venv/pip/inspect/test 命令。明确当前开发仓库没有首次提交时 auto/head 无法检查它，可用显式 directory 模式测试。记录测试失败或跳过项，不捏造通过数。
- [x] **Step 6: 提交检查点。** 检查 diff、忽略规则和敏感内容；获批时提交 `feat: expose repository inventory CLI and reports`，附 attribution。不 push，除非得到单独明确授权。

## 自检与交付

- 本计划实现第一阶段规格，不提前声称覆盖完整多 Agent 设计；后续阶段见父设计。
- 五个 Review Focus 已分配到具体任务测试。
- SourceReader/Inventory/错误类型由 Task 1 定义，Tasks 2–5 只消费同一接口。
- 每项测试先红后绿；安装工具只安装进本项目 `.venv`，不更改系统 Python。
- 用户审阅规格与计划后再实现。推荐当前会话直接执行（Native），因为任务接口顺序依赖强、首阶段无模型及发布副作用；实现完成后再安排独立分支审查。
