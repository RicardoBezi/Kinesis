# ADR 0011: Python task runner instead of make

**Status:** Accepted

## Context
The main development machine runs Windows and does not have GNU Make. CI runs on Linux.

## Decision
- **Runner:** `scripts/tasks.py`, written with the standard library only.
- **Command execution:** argparse plus `subprocess.run` with list arguments, never `shell=True`.
- **Entry point:** a console script named `task`. `uv run task <name>` then behaves the same in PowerShell, Git Bash, macOS and Linux CI.

| Group | Tasks |
|---|---|
| Setup | `setup`, `setup-blender` |
| Tests | `test`, `test-unit`, `test-integration`, `test-blender`, `test-golden`, `live-nebius-test` |
| Code quality | `lint`, `fmt`, `typecheck` |
| Run and build | `run`, `fixture`, `openapi`, `models`, `client` |

## Consequences
- Developers install one tool (uv).
- CI runs the same commands that developers run.
