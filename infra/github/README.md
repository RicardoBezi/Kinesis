# GitHub branch protection for `main`

[`ruleset-main.json`](ruleset-main.json) implements the owner's decision of 2026-10-09:
- work arrives through PRs with 0 required approvals;
- these status checks are required:
  - `python (format, lint, types, tests)`
  - `openapi drift`
  - `kotlin client (build + test)`
  - `backend image`
  - `blender-gate`
- the branch is **not** required to be up to date before merging;
- force pushes and deletions are blocked;
- history stays linear, because only squash merges are allowed;
- the bypass list is empty.

`blender-gate` always runs, and passes when the path-filtered `blender` job succeeded or was skipped. This avoids the classic deadlock of requiring a path-filtered check. The nightly `container` parity job is deliberately left out of the gate: it opens an issue when it fails.

Apply these (owner-approved) from the repo root:

```bash
gh api repos/RicardoBezi/Kinesis/rulesets --method POST --input infra/github/ruleset-main.json
gh api repos/RicardoBezi/Kinesis --method PATCH \
  -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false \
  -F allow_auto_merge=true -F delete_branch_on_merge=true
```

The workflow from then on:
1. `git switch -c <branch>`, then commit.
2. `gh pr create`.
3. `gh pr merge --auto --squash`. The PR merges by itself once the five checks pass.
