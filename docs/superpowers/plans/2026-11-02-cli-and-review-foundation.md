# CLI Experience and Structured Review Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline, TDD per task, one independent final review. User requested implementation after the approved CLI interaction design; no repeat approval for those interactions.

**Execution status (2026-11-02):** Tasks 1–5 complete. Independent final review: no Critical/Important; Markdown underscore rendering retained as a documented non-blocking minor. Final full suite: 258 passed; Ruff check/format clean. No commits, user-level installation, model calls or target execution. Checkbox completion denotes implementation/verification and handoff, not a Git commit or approval of next-stage operations. Provider/model selection and separate upload/budget approval remain pending.

**Goal:** 交付简洁 `review` 启动器和终端报告，并开始第二阶段 2A 的离线审查协议与证据上下文。

**Architecture:** 新旧 CLI 共享采集核心；安全 shell 启动器在解释器启动前启用隔离；Pydantic 协议及上下文为独立可选模块。2B 模型适配/LangGraph 待提供方决定后另行细化，不能把 2A 当完整第二阶段。

**Tech Stack:** Python >=3.11，stdlib CLI，pytest/Ruff，Pydantic 2 可选依赖；本机 Python 3.12。

**Spec:** `docs/superpowers/specs/2026-11-02-cli-experience-design.md` 与 `docs/superpowers/specs/2026-11-02-structured-review-design.md`。

## Global Constraints

- 不修改目标项目；不调用模型、上传源码、执行目标代码或安装目标依赖。
- 不提交/推送；保留当前 unborn 功能分支和现有实现。
- 只在本项目虚拟环境安装开发/可选依赖；用户级启动器/PATH 修改另行确认。
- 新 `review` 默认 text；旧 `reviewflow inspect` 默认 Markdown；JSON schema 1 保持不变。
- 预算沿用 200 / 262144 / 8388608，免责声明不变。
- 2A 用 optional extra `review = ["pydantic>=2,<3"]`；基础 CLI 不导入该包。
- 环境 workaround 仅用于当前机器命令或用户明确传入的启动器选项，不硬编码到应用。

## Review Focus

- 在目标目录启动，PYTHONPATH/sitecustomize/json 同名模块污染不能执行目标代码（Task 2）。
- 新短参数、旧入口和机器输出不能互相混淆；stdout 不夹带日志（Task 1）。
- 辅助目录在 HEAD 与 directory 两种模式都先过滤后读取（Task 1）。
- 模型声明的快照、行号、任务路径与实际源文件不一致必须拒绝，不能伪造证据（Tasks 3–4）。
- 安装目标存在、软链接、路径空格/引号、输入期间变化必须失败闭合或安全处理（Tasks 2、4）。

## Task 1: 简洁 CLI、终端摘要与排除规则

Files: modify `src/reviewflow/{cli,report,policy}.py`; create `src/reviewflow/quick.py`, `tests/test_quick_cli.py`; extend policy tests.

Interfaces: `quick.main(argv: Sequence[str] | None = None) -> int`; `render_text(inventory, *, verbose=False) -> str`; shared `cli.add_scan_options(parser, *, default_format)` and `cli.run_inspection(args) -> int`. Keep `cli.main` legacy entry.

- [x] RED tests: no args uses cwd; -d/--directory handles relative and spaced paths; default text retains underscores, no HTML entities/record hashes, mandatory notice; verbose lists included/skipped entries; JSON unchanged; verbose+nontext rejected; unknown options/typos rejected; version/help no scanning; control chars escaped; legacy tests still run.
- [x] RED policy tests: .superpowers/.claude/.eggs/*.egg-info pruned, descendants excluded in HEAD records; innocent similar names allowed.
- [x] Implement shared options, plain-text rendering, short CLI, narrowly relaxed Markdown underscore rendering, exclusions.
- [x] GREEN full pytest, Ruff check/format. No unauthorized commits.

## Task 2: 一次性安全启动器

Files: create `src/reviewflow/install.py`, `tests/test_launcher.py`.

Interfaces: `install_launcher(bin_dir: Path, *, python: Path, library_path: Path | None = None) -> Path`; `install.main(argv=None) -> int`. Installer module CLI uses current sys.executable; args --bin-dir default ~/.local/bin and optional --library-path. Generated script execs absolute venv Python `-I -m reviewflow.quick "$@"`; preserve cwd.

- [x] RED tests: install temp launcher, execute from temp target with poisoned PYTHONPATH and sitecustomize/json/reviewflow.py tripwires; report correct root, marker absent, input unchanged; quoted/spaced paths; executable bit; pre-existing regular file/directory/dangling symlink refused without overwrite; optional library path handled without literal machine default.
- [x] Implement exclusive creation (no overwrite), shell quoting, bounded diagnostics and PATH guidance; no shell profile edits. Catch operational failure return 1; invalid input 2.
- [x] GREEN entire suite; create project-local `.venv/bin/review` only if absent, execute help and real safe temp target smoke. Do not claim user-global install complete.

## Task 3: 第二阶段结构化协议

Files: create `src/reviewflow/review/{__init__,contracts}.py`, `tests/test_review_contracts.py`; modify pyproject optional dependencies.

Interfaces: Pydantic `ReviewTask`, `EvidenceRef`, `Finding`, `VerificationResult` exactly per 2A spec. Common strict frozen base, forbid extra fields; tuple collections and bounds; reusable validated relative path and SHA types.

- [x] RED tests for missing protocols, followed by strict JSON roundtrip, bad paths, IDs/hashes, ranges, duplicate task paths, bounds, extra-field rejection, no mutable tuple payload, accepted/rejected evidence requirement.
- [x] Add optional dependency, install into .venv; implement schemas, no provider SDK.
- [x] GREEN entire suite; basic CLI source imports remain independent of Pydantic.

## Task 4: 快照绑定与证据校验

Files: create `src/reviewflow/review/context.py`, `tests/test_review_context.py`.

Interfaces: `ReviewContext(source: SourceReader, inventory: Inventory)`; `read_lines(path, start_line, end_line) -> str`; `validate_task(task) -> None`; `validate_finding(task, finding) -> None`; `validate_verification(task, finding, result) -> None`. Violations CollectionError with no content in diagnostics.

- [x] RED tests: allowed lines/LF/CRLF; sensitive/skipped paths rejected before reads; changed file and wrong source/snapshot rejected; out-of-range/over-200-lines/over-32768-byte excerpts rejected; task ID/path/snapshot/hash mismatches rejected; genuine verifier counterevidence elsewhere in included scope allowed; wrong finding ID rejected; Unicode/newline filename accepted; pinned Git HEAD unaffected by dirty file.
- [x] Implement local reads via same SourceReader, recheck digest/size/UTF8, canonical inventory digest and source identity, strict object relationship checks. No imports or execution from target.
- [x] GREEN full suite; report 2A only, never claim live Agent review.

## Task 5: 用户文档、验证与交付

Files: create `docs/user-guide.md`; update README/CONTRIBUTING and spec status with actual evidence; progress ledger under this plan's ignored scratch workspace.

- [x] Document real install/launch/help/version/format commands, PATH one-time setup, machine library option, conflicts/moving/uninstall, scope/version defaults, filters, permissions and known limitations. Link both designs.
- [x] Full pytest, Ruff check, format --check, install smoke from target cwd, metadata only self inventory. No target tests.
- [x] One fresh independent whole-change review; Important fixes RED→GREEN in one pass, minors ledger. Preserve scratch because no commits; no Git publish.
- [x] Summarize implemented CLI and 2A, explicitly ask provider/model choice for 2B and separately user-global install approval if still needed. No assumption that broad development authorization grants cloud spending or shell modifications.
