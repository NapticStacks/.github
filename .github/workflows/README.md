# NapticStacks reusable CI workflows (SOC 2 control plane)

Single source of truth for the SOC 2 Type 2-aligned CI control set across
NapticStacks repos. Each repo calls these via a thin `.github/workflows/ci.yml`.
Pin callers to the `@v1` release tag (not `@main`) so a change here is rolled out
deliberately, not implicitly.

## Emitted check contexts

Use the caller **job-ids `security` and `ci`** verbatim — the required-status-check
names in `project-manager` → `config/repo_review_policy.json` are
`<caller-job-id> / <reusable-job-name>` and must match exactly:

| Context | Source | Gate? |
|---|---|---|
| `security / Secrets (gitleaks)` | reusable-security | HARD (required) |
| `security / IaC (checkov)` | reusable-security | evidence (soft-fail) |
| `security / Trivy` | reusable-security | evidence (soft-fail) |
| `ci / lint` | reusable-ci-python/node | typically required |
| `ci / unit` | reusable-ci-python/node | typically required |
| `ci / typecheck` | reusable-ci-python/node | opt-in |
| `ci / SAST (bandit)` | reusable-ci-python | evidence |
| `ci / SCA (pip-audit)` | reusable-ci-python | evidence |
| `ci / SCA (npm-audit)` | reusable-ci-node | gate (Node) |

GHAS code-scanning is not enabled on these repos, so scanners gate via exit code
and retain SARIF/JSON as 90-day artifacts (SOC 2 evidence) rather than uploading
to code-scanning.

## Caller example — Python (uv)

```yaml
name: CI
on:
  push: { branches: [main] }
  pull_request: { branches: [main] }
permissions: { contents: read }
jobs:
  security:
    uses: NapticStacks/.github/.github/workflows/reusable-security.yml@v1
  ci:
    uses: NapticStacks/.github/.github/workflows/reusable-ci-python.yml@v1
    with:
      dependency_install: uv
      install_command: "uv sync --extra dev"
      run_typecheck: true
```

## Caller example — Python (pip)

```yaml
  ci:
    uses: NapticStacks/.github/.github/workflows/reusable-ci-python.yml@v1
    with:
      dependency_install: pip
      install_command: "pip install -r requirements.txt"
```

## Caller example — Node (audit-only)

```yaml
  ci:
    uses: NapticStacks/.github/.github/workflows/reusable-ci-node.yml@v1
    # run_sca defaults true; enable run_lint/run_unit once the repo wires them.
```

## Rollout discipline

Observe a green run and read the EXACT emitted context names
(`gh api repos/NapticStacks/<r>/commits/<sha>/check-runs --jq '.check_runs[].name'`)
BEFORE adding a repo to `required_status_checks_repos` (or an override) in the
policy. A required context that never reports hangs every PR.
