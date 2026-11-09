# Offline Backend Completion Implementation Plan

**Authorization:** 用户要求除前端和需要真实 API Key 的内容外顺次完成，不再等待问答。既有安全约束不变；只在 pytest 临时仓库执行 Git 写操作，不提交/推送本项目或真实目标。

**Goal:** 完成可离线验证的配置/授权/预算、补丁/工作区/执行边界、评估与交付工具。未选提供方不猜测 SDK；真实语义能力与沙箱实机安全验收不能用模拟代替。

**Architecture:** 保持现有 inventory 与 simulated LangGraph；增加独立 backend 服务层和显式 CLI 子命令。授权由可信调用方的用户动作产生，绑定规范化操作摘要；外部 JSON 不能自行声明已经批准。修复使用结构化完整文件替换而非执行模型 shell/任意 diff；所有 Git 写入仅发生在专属工作区。目标测试仅允许固定 Docker 命令，不使用宿主机 fallback。

**Spec:** ../specs/2026-11-02-reviewflow-design.md；现有 CLI/2A 合约保持兼容。

## Global constraints and rulings

- Python >=3.11；基础 inventory 保持 stdlib-only；新模块可使用已安装 Pydantic。
- 所有文件、模型输出、报告均为不可信数据；明确 CLI 参数不等于允许其扩大文件范围。
- 不联网、不发现凭据、不执行目标代码、不全局安装、不修改本项目 Git。测试只能写 pytest 自有临时仓库。
- 未提供 SDK/API 类型：完成 transport-neutral 调用闸门及 fake transport 测试，live CLI 继续关闭。
- Docker 镜像必须固定 digest 且本机已存在；没有 Docker/镜像即 blocked。不自动 pull/install，不宣称已实机验证。
- 第一版修复单次只处理一个问题、最多两次；每次失败恢复自有文件基线，累积最终状态必须单独验证。未验证补丁不可提交。禁止删除/新增/改名/软链接/二进制/测试文件修改；这是有意收紧，代价是部分修复只能建议。
- 本轮不加入复杂语义去重、无限检索或猜测供应商 token 精确算法；保留候选来源与未知覆盖。上下文增强以离线文本分片索引服务实现，不冒称已接入模型工具循环。

## Review Focus

- 配置占位/未知字段/URL/布尔数字混用拒绝；不读取目标 dotenv，不泄漏输入或环境秘密。
- 授权绑定内容、操作、范围和预算；旧授权、修复授权不能用于 commit/push；费用预留在 transport 之前，失败不能免费重试。
- 补丁不能越界、覆盖变更、写 symlink/测试/配置；Git hooks/filters 不运行；原工作区和暂存区不变。
- 未验证/模拟/blocked 测试不能成为可提交修复；容器超时必须停止容器，无宿主执行 fallback。
- 评估只接受人工标注/根因匹配，不将模拟流程结果当实验准确率；交付包用白名单且拒绝 symlink/secrets/覆盖。

## Tasks (inline TDD; one independent final review)

- [x] 1. Config + approvals + budgets: strict TOML schema with Decimal prices; canonical request digests; process-local one-shot grants; guarded trusted transport with actual usage reconciliation and fail-closed budget. Tests invalid config, stale/mismatched/consumed approval, failed call reservation, unknown usage/overspend.
- [x] 2. Offline context and evaluation: bounded LF chunk index/search with snapshot-bound evidence; single/multi-no-verifier mode; evaluation manifests, grouped splits, human root-cause matches, verifier/repair metrics, simulated-result rejection. Tests no truncation claims, evidence, duplicate matching, clean cases and split leakage.
- [x] 3. Repair protocol + workspace: strict replacement proposals, selected finding/task provenance, protected paths, patch hash/diff; approved dedicated worktree creation/materialization without checkout hooks/filters; bounded attempt state. Tests real temporary Git worktrees and malicious inputs; unchanged original dirty tree/index.
- [x] 4. Validation + publication gates: digest-pinned Docker command builder/runner with limits, no network or credentials, bounded logs and timeout cleanup; fixed approved checks, baseline/candidate validation and independent re-review gates; separately authorized commit and HTTPS repair-branch push with no force. No actual Docker/network calls this session. Tests command shape, timeout/failure gates, exact staged changes, stale approval and destinations.
- [x] 5. CLI + runnable offline demo: explicit config-check, prepare, context, repair-demo, evaluate commands with safe JSON/text output and no automatic config discovery; repair demo only creates its own fixture under a new user-designated directory, clearly simulated and noncommittable. Integration tests actual isolated launcher.
- [x] 6. Evaluation fixtures + delivery: self-authored grouped defect/clean corpus and held-out annotations outside target trees; runbook, evaluation protocol, demo/slides/contribution templates; safe source archive command excluding credentials/artifacts. Full tests/Ruff, independent review, fix Critical/Important with TDD; preserve tree/ledger, no publish.

## Acceptance

Each task: first failing tests, minimal implementation, scoped and full tests. Final report distinguishes implemented services, CLI integrations, mocked boundary checks and unrun real provider/container/remote acceptance. Do not claim that all envisioned autonomous semantic behavior is validated.

## Completion evidence (2026-11-03)

- 386 tests passed in the working source and separately extracted source; the latter reused installed dependencies and an explicit source-level entrypoint harness, not a fresh pip installation.
- One independent final review found four Important issues; 11 failing regression cases reproduced them, followed by fixes and a green full suite. Application rollback, cleanup return codes, executable-entry rejection and sensitive archive filenames are covered.
- Executable Git entries are explicitly unsupported rather than losing permission semantics; only `100644` trees can enter automatic repair.
- Offline wheel build was attempted and failed because `setuptools.build_meta` is unavailable; no cached setuptools wheel and no network installation attempted. Clean build/dependency installation remains a release acceptance item.
- Real SDK/billing, Docker isolation/termination, remote publication and model effectiveness remain unverified, not implied by checked offline tasks. Full limitations: ../../backend-guide.md.
