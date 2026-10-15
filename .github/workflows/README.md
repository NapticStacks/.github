# NapticStacks reusable CI workflows (SOC 2 control plane)

Single source of truth for the SOC 2 Type 2-aligned CI control set across
NapticStacks repos. Each repo calls these via a thin `.github/workflows/ci.yml`.
Pin callers to the `@v1` release tag (not `@main`) so a change here is rolled out
deliberately, not implicitly.

## Emitted check contexts

Use the caller **job-ids `security`, `ci`, `test-count` and `sdlc`** verbatim — the required-status-check
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
| `sdlc / sdlc-conformance` | reusable-sdlc-conformance | warn by default; block per repo (pinned to `@sdlc-conformance-v1`, `-v2` or `-v3`) |
| `sdlc / sdlc-ready` + commit status `sdlc/ready` | reusable-sdlc-conformance (v2 and later) | advisory, never required; Claude Skins reads it |

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
  # Python repo whose test modules import the package under test:
  test-count:
    uses: NapticStacks/.github/.github/workflows/reusable-test-count-guard.yml@v1
    with:
      install_command: "pip install -e ."

  # Node repo — jest must be installed before it can list tests:
  test-count:
    uses: NapticStacks/.github/.github/workflows/reusable-test-count-guard.yml@v1
    with:
      install_command: "npm ci"
```

**A Node repo without `install_command` will report `collection_error` on its
first run.** The guard calls `npx --no-install jest --listTests`, which cannot
list anything until `node_modules` exists — so set `install_command` (`npm ci`,
or `npm install` where there is no lockfile) when wiring a Node repo. The same
applies to a Python repo whose test modules import the package under test:
without an install, collection fails on import and the guard says so rather than
guessing. `install_command` runs in BOTH checkouts; an asymmetric install would
make the comparison meaningless.

**Caveat — both installs share one environment, and the last one wins.** The job
installs base then head into the same runner-wide Python/Node environment, so
head's install is what remains when either side is collected. For a src-layout
package imported by its installed name, that means the base branch is collected
against *head's* installed package. It is fine when the two revisions' installed
packages are import-compatible, which is the common case; it can misreport when
a PR renames or moves modules that the base tests import. If that bites, treat
the resulting `collection_error` as accurate — the counts genuinely were not
comparable — and split the sides into separate virtualenvs in a follow-up.

Fails a PR when the count of collectible tests drops against the base branch.
Runs on `pull_request` only (a push has no base to compare against), so it
green-skips elsewhere and must not be marked required on `push`.

- **Detection.** pytest when `pytest.ini` / `setup.cfg` / `tox.ini` / a
  pytest-mentioning `pyproject.toml` / any `test_*.py` or `*_test.py` exists;
  jest when `package.json` names jest. Both present ⇒ counts are summed.
  Neither ⇒ verdict `noop` and a notice, never a failure.
- **The two runners count different units.** pytest counts individual test
  *cases* (`--collect-only` test ids); jest counts test **files**
  (`--listTests`), because listing files is all jest will do without executing
  the suite. So deleting cases from an existing jest file does not move the
  number — only deleting a whole file does. In a mixed repo the guard sums a
  case count and a file count into one total, which is a coarse number: it is
  a tripwire for "coverage left the repo", not a coverage metric.
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

Callers can read the result: the workflow exposes `verdict`
(`pass` / `dropped` / `collection_error` / `noop`), `base_count` and
`head_count` as `workflow_call` outputs. The two counts are empty strings when
no runner was detected on that side.

The logic lives in `scripts/test_count.py` (unit-tested by
`scripts/test_test_count.py` against fixtures in `scripts/fixtures/`); the
workflow is a thin caller. `guard_ref` selects which ref of this repo the script
is loaded from and defaults to `v1` — keep it in step with the caller's `@ref`.

## Caller example — sdlc conformance

The full org template is [`workflow-templates/sdlc.yml`](../../workflow-templates/sdlc.yml). GitHub offers it under
**Actions → New workflow** in every NapticStacks repo. WS-11 of the SDLC V1 rollout (naptic-sdlc#27) copies it
into each client repo as `.github/workflows/sdlc.yml` and fills in `builders` and `security_signers` there.
Those logins stay in the private client repo: this repo is public.

Copy as `.github/workflows/sdlc.yml` (v3, current):

```yaml
on:
  pull_request:
    types: [opened, edited, synchronize, reopened, ready_for_review, converted_to_draft, labeled, unlabeled, review_requested]
  pull_request_review:
    types: [submitted, edited, dismissed]
permissions: {contents: read, pull-requests: read, issues: read, statuses: write}
concurrency:
  group: sdlc-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  sdlc:
    uses: NapticStacks/.github/.github/workflows/reusable-sdlc-conformance.yml@sdlc-conformance-v3
    with:
      mode: warn
      builders: "<engineer roster logins>"
      security_signers: "<Security Ops login>,<backup login>"
```

### What v3 adds (SDLC V1)

All of it skips PRs created before `cutover` (default `2026-10-16T00:00:00Z`). Those finish under v2 rules.

- **`tier`:** the highest `tier:*` label on the PR or its same-repo linked issues. None is a note (counts as medium).
  - A linked issue that can't be read (404) means tier unknown: a violation, never a silent medium.
  - Only cross-repo issues linked and no `tier:*` on the PR: a violation. Add the label to the PR.
  - `tier:high` needs a comment carrying `<!-- sdlc:security-signoff v1 -->` from the first `security_signers`
    login (or a later one when the first one built it). It counts on a linked issue that itself carries
    `tier:high`, or on the PR when the comment also names the full head SHA. A sign-off on another linked issue
    is never borrowed.
  - Neither the `Builder:` login nor the PR author can sign off. No `security_signers` set means high always fails.
- **`builder-of-record`:** a `Builder: @login` line in the body (comments and code fences don't count). The login
  must be a real GitHub user account, not a bot, and on `builders`. With no `builders` set, that last part is a note.
  Bot and swarm PRs need the line too. Dependabot gets a note.
- **`self-review`:** a comment by the builder with `<!-- sdlc:self-review v1 -->` and the full 40-character head
  SHA, or a review by the builder carrying the marker on the head commit. A push makes it stale. `/closer` posts
  the marker and requests a reviewer (through `/sdlc-handoff`, being added in napticstack-skills 0.88.0).
- **Edited markers don't count.** A marker comment or review counts only if nobody but its author ever edited it.
  The gate reads each marker's edit history over GraphQL (`userContentEdits`, `editor`). If that read fails, the
  gate exits 2. It never trusts an unchecked marker.
- **Template:** post-cutover human PRs also need `## Builder`, `## Workstation`, `## Session` and `## Tier`
  (see the org [PR template](../pull_request_template.md)).
- **`review_requested`** is a new caller trigger: `/closer` requests the reviewer right after it posts the marker,
  so the gate re-runs and sees it.
- **Known gaps (stale green):** the caller has no `issue_comment` or issue-label trigger. So a green result stays
  green after a sign-off is deleted, or after a linked issue is raised to `tier:high`, until the next PR event or a
  manual re-run. A new comment doesn't re-run it either. Re-run the check before you rely on it.

### v1 / v2 / v3 compatibility

| | v1 caller (`@sdlc-conformance-v1`) | v2 caller (`@sdlc-conformance-v2`) | v3 caller (`@sdlc-conformance-v3`) |
|---|---|---|---|
| `sdlc / sdlc-conformance` | yes, unchanged | yes, same checks and exit codes | yes; adds `tier`, `builder-of-record`, `self-review` for post-cutover PRs |
| `sdlc / sdlc-ready` job | no | yes | yes; post-cutover PRs also need the V1 template sections |
| `sdlc/ready` commit status | no | yes, when the caller grants `statuses: write` | same as v2 |
| Inputs | all v1 inputs | all v1 inputs, same names and defaults; `gate_ref` defaults to `sdlc-conformance-v2` | all v2 inputs, plus `builders`, `security_signers`, `cutover`; `gate_ref` defaults to `sdlc-conformance-v3` |
| Without `statuses: write` | n/a | `sdlc-ready` warns "not posted" and stays green | same as v2 |
| Fork PR | n/a | "not posted" (the token is read-only) | same as v2 |

v1 and v2 are not deprecated. Their tags never move, so those callers keep working exactly as before.

### Migrating v2 → v3

1. Change `@sdlc-conformance-v2` to `@sdlc-conformance-v3` (once that tag exists: it is cut on the merge commit of the v3 PR).
2. Add `review_requested` to the `pull_request` types.
3. Set `builders` and `security_signers` under `with:`.
4. If the caller pins `gate_ref`, set it to `sdlc-conformance-v3` too (or drop it to take the default).
5. Update the repo's PR template to carry the Builder, Workstation, Session and Tier sections.

### Migrating v1 → v2

1. Change `@sdlc-conformance-v1` to `@sdlc-conformance-v2` (once that tag exists: it is cut on the merge commit of the v2 PR).
2. Add `converted_to_draft` to the `pull_request` types and `edited` to the `pull_request_review` types, so a PR turned back into a draft or a review edited to drop its marker re-posts `pending`.
3. Add `statuses: write` to the caller's `permissions:`.
4. If the caller pins `gate_ref`, set it to `sdlc-conformance-v2` too (or drop it to take the default).
5. Open a PR and check both `sdlc / sdlc-conformance` and `sdlc / sdlc-ready` report. Don't make `sdlc-ready` a required check: it is advisory.

### What "ready" means

`sdlc/ready` is `success` when all three hold, and `pending` otherwise. The description says what's missing and the command that fixes it.

- The PR is not a draft.
- The four template sections pass, under the same template rule `sdlc-conformance` applies (readiness does not re-check the issue link). `## /review findings` is non-empty or `N/A because <reason>`. Bot-authored PRs skip this, as in the conformance run.
- A `/naptic-review` **marker review** is on the head commit. The marker is a review whose body starts with `## Three-lens review` (the heading `/naptic-review` opens every review with). It counts only from the PR author or a `trusted_reviewers` login, and only when its `commit_id` is the head SHA. A push makes it stale. Each login's newest marker on head stands, and if any of them asks for changes (the review state, or `REQUEST CHANGES` in its heading, which is how a self-review posted as a comment says it), the PR is not ready. An author's marker never clears a trusted reviewer's change request.

**`sdlc/ready` is advisory, never an authorization.** On `pull_request`, GitHub runs the caller from the PR's own head, so a PR that edits its caller can post the status itself, as `github-actions[bot]` like any run. Readers take the newest status from that creator, but must never use it to gate a merge, an auto-merge or a deploy.

Pin `@sdlc-conformance-v3` (or `-v2`, `-v1`), not `@v1`: the gate has its own tag so it can move without moving the shared one.

**Local pre-push check:** from a clone of `NapticStacks/.github` (Python 3.11+, `gh auth login` done), run `python scripts/sdlc_conformance.py --pr <owner/repo#N>`. It uses the same code path and the same exit codes as CI.

**Before a repo turns on `mode: block`:** on `pull_request`, GitHub reads the caller file from the PR head, so a PR can edit its own `sdlc.yml` (reviewers, mode, builders, cutover). Block mode is only a control once that file is protected: CODEOWNERS `@maydaycyber` on `.github/workflows/**` with required code-owner review (or an org required-workflow ruleset), plus a tag-protection ruleset on `sdlc-conformance-v*` here. The gate already refuses a `gate_ref` that isn't an `sdlc-conformance-vN` tag or a full SHA.

## Rollout discipline

Observe a green run and read the EXACT emitted context names
(`gh api repos/NapticStacks/<r>/commits/<sha>/check-runs --jq '.check_runs[].name'`)
BEFORE adding a repo to `required_status_checks_repos` (or an override) in the
policy. A required context that never reports hangs every PR.
