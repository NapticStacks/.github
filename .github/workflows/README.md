# NapticStacks reusable CI workflows (SOC 2 control plane)

Single source of truth for the SOC 2 Type 2-aligned CI control set across
NapticStacks repos. Each repo calls these via a thin `.github/workflows/ci.yml`.
Pin callers to the `@v1` release tag (not `@main`) so a change here is rolled out
deliberately, not implicitly.

## Emitted check contexts

Use the caller **job-ids `security`, `ci` and `test-count`** verbatim — the required-status-check
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
| `test-count / test-count guard` | reusable-test-count-guard | gate (PR-only) |

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

## Caller example — test-count guard

```yaml
  test-count:
    uses: NapticStacks/.github/.github/workflows/reusable-test-count-guard.yml@v1
    # Add `with: { install_command: "pip install -e ." }` when collection needs deps.
```

Fails a PR when the count of collectible tests drops against the base branch.
Runs on `pull_request` only (a push has no base to compare against), so it
green-skips elsewhere and must not be marked required on `push`.

- **Detection.** pytest when `pytest.ini` / `setup.cfg` / `tox.ini` / a
  pytest-mentioning `pyproject.toml` / any `test_*.py` or `*_test.py` exists;
  jest when `package.json` names jest. Both present ⇒ counts are summed.
  Neither ⇒ verdict `noop` and a notice, never a failure.
- **Escape hatch.** Label the PR `tests-reduced-ok` when the reduction is
  intended. Read from the `github` context, so no token is required.
- **A broken collector is never reported as a test-count drop.** Collection
  failures (pytest exit 2/3/4, non-zero jest) surface as `collection_error` and
  echo the collector's own output. pytest exit 5 is a real count of zero, not an
  error. The override label waives a reduction; it never waives a broken
  collector.
- **No cloud credentials, deliberately.** Collection imports test modules, which
  runs module-level code — in some consumer repos that can reach real
  Slack/email/AWS if credentials are in the environment. The workflow declares no
  `AWS_*` env, calls no `configure-aws-credentials`, and requests no secrets. Do
  not call it with `secrets: inherit`.

The logic lives in `scripts/test_count.py` (unit-tested by
`scripts/test_test_count.py` against fixtures in `scripts/fixtures/`); the
workflow is a thin caller. `guard_ref` selects which ref of this repo the script
is loaded from and defaults to `v1` — keep it in step with the caller's `@ref`.

## Rollout discipline

Observe a green run and read the EXACT emitted context names
(`gh api repos/NapticStacks/<r>/commits/<sha>/check-runs --jq '.check_runs[].name'`)
BEFORE adding a repo to `required_status_checks_repos` (or an override) in the
policy. A required context that never reports hangs every PR.
