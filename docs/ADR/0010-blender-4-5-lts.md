# ADR 0010: Pin Blender 4.5 LTS

**Status:** Accepted

## Context
- Blender's animation API changed significantly in 4.4 with slotted Actions.
- Blender 5.0 removes the legacy `action.fcurves` path.
- Golden results are only reproducible on a pinned version.

## Decision
- **Target 4.5 LTS.** Kinesis targets Blender **4.5 LTS**, pinned to an exact patch version in `scripts/tasks.py` and `.github/workflows/blender.yml`.
- **Portable install.** `uv run task setup-blender` downloads the portable build into `.tools/`, so it can sit next to any other Blender installation.
- **Slotted-action API.** Worker code uses the slotted-action API wherever it can. Moving to 5.x should then mean re-pinning and re-checking the golden results, not a rewrite.

## Consequences
- Developers who only have Blender 5.x installed need the portable 4.5 build to run integration tests.
- The unit tests and golden numpy tests do not need Blender at all.
