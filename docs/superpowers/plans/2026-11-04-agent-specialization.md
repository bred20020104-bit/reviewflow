# Agent Specialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. User has approved the described role/skill direction and requested implementation; do not ask for approval again. No commits or pushes.

**Goal:** Distinct, packaged role instructions and skills actually used by the review and offline patch request builders.

**Architecture:** Fixed trusted resource manifest, immutable per-run PromptBundle, role-specific system composition. Live preview binds the same bundle used by the graph.

**Tech Stack:** Existing Python, importlib.resources, dataclasses, LangGraph, pytest, setuptools.

**Spec:** docs/superpowers/specs/2026-11-04-agent-specialization.md

## Global Constraints

- No target mutations/execution, real external model requests, private config changes, frontend, commit/push or publication.
- Retain body<=65536 bytes, output<=2048 tokens, timeout<=60s, no automatic retries, context<=32768 bytes in live mode.
- Fixed role/skill mappings only; never load target AGENT.md/SOUL.md as trusted instructions.
- Existing saved session/RunReport schema unchanged; patch remains trusted offline backend only.

## Review Focus

1. Skill edit after preview must invalidate old approval before requests.
2. Skill edit during approved run must not change the frozen prompt snapshot.
3. Verifier must select verification profile regardless of task.role; unknown reviewer roles fail closed.
4. Target same-named Markdown files must remain untrusted source, not system configuration.
5. Installed wheel must contain all resources, without needing checkout/cwd lookup.

## Tasks

### Task 1: Packaged immutable profile bundle

**Files:** src/reviewflow/agents/{__init__.py,profiles.py,SOUL.md,roles/*/AGENT.md,skills/*/SKILL.md}; pyproject.toml; tests/test_agent_profiles.py.

**Interfaces:** load_profiles() -> PromptBundle; bundle.system_for(stage, role=None) -> str; bundle.manifest() -> dict. Fixed six profiles, five skills; loader reads package resources with bounded bytes and returns frozen values. Manifest is JSON-compatible with bundle digest, shared digest, role instruction digest and bound skill identifiers/digests.

- [x] Write failing tests for distinct profiles, general baseline, verifier routing, unknown roles, target-cwd isolation, fail-closed missing/invalid resources, immutable bundle and hash changes.
- [x] Run focused tests; confirm RED is missing functionality, not unrelated environment failure.
- [x] Implement fixed resources and stdlib loader; add setuptools package-data.
- [x] Run focused tests GREEN.

### Task 2: Wire profiles into actual requests and approvals

**Files:** src/reviewflow/review/{workflow.py,interactive.py}; src/reviewflow/backend/{live_review.py,patch_agent.py}; tests/test_agent_profiles.py, tests/test_live_review.py, tests/test_backend_cli.py.

**Interfaces:** _Run / run_interactive_review accept optional trusted profiles bundle; live controller loads once and passes it both to make_preview and graph; make_preview adds agent_profiles manifest and binds its digest plus tool instructions. propose_patch uses patch profile. Existing external default calls remain valid.

- [x] Add RED tests for real graph role-specific requests, offline patch profile, resource edit invalidating CLI approval, in-run resource edit not changing messages.
- [x] Replace shared reviewer prompt composition; ensure verifier/profile resolution is stage-aware.
- [x] Bind/forward one live bundle and retain sanitized errors, schema, budgets and saved sessions.
- [x] Run focused integration tests GREEN, then full suite.

### Task 3: User docs and installed/CLI verification

**Files:** docs/agent-profiles.md, README.md, docs/model-setup.md; external validation artifacts (no secrets).

- [x] Document exact file locations, role/skill mapping, editing and reapproval semantics, general baseline and offline-only patch boundary.
- [x] Build a local wheel in scratch with no network; verify profile loading from installed/wheel resources outside checkout.
- [x] Launch actual review CLI against a loopback synthetic Responses server; assert transmitted instructions differ by role and contain the intended skills. No target repository review or real model acceptance claims.
- [x] Run full pytest and Ruff src/tests; one narrow independent review, reproduce/fix important findings with tests, report exact results and absolute artifact path.

## Execution ledger

Pre-flight: workflow consumes PromptBundle; live preview and runtime must share the exact instance. Patch uses a distinct stage, never task.role. No other shared mutable interfaces.
Ruling: current feature branch has no commits, so no new worktree/commits; use the existing feature workspace without touching user target branches.
