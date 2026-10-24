# Changelog

## notebook-guard-v1 (unreleased; tag cut on merge, then v1 moves)

- `reusable-security.yml`: the required `Secrets (gitleaks)` job now runs `scripts/notebook_guard.py` first.
  Any commit in the change (merge commits included, via the net diff) that adds, edits, copies or renames a file into
  a notebook folder fails. Folders come from the new input `notebook_folders` (default `JORDAN-CONFIDENTIAL`, Jordan's
  scope decision 2026-10-24; widen per repo when Ben or Darien opt in). Untracking passes. Paths are read with `-z`, so unusual names
  can't slip past. On push, a force-pushed `before` missing from the clone falls back to a tree check with a warning; on
  `pull_request` and `merge_group` a missing base is an error (exit 2).
  gitleaks still runs when the guard fails. `merge_group` uses its base SHA. `notebook_guard_ref` is validated.
  A repo that still tracks the folder from before gets a warning only. History is not checked, on purpose.
- New input `notebook_guard_ref` (default `notebook-guard-v1`). Callers on `@v1` get the guard when `v1` moves.
- Rollback: move `v1` back to `a67d6a0` (its target before this release).
- Moving `v1` also brings `reusable-test-count-guard.yml` (new file, opt-in, no effect on existing callers).

## sdlc-conformance-v3 (unreleased; tag cut on merge)

- SDLC V1 checks, for PRs created at or after the cutover (`2026-10-16T00:00:00Z`, input `cutover`):
  `tier` (highest `tier:*` label on the PR or its same-repo issues; none is a medium note; `tier:high` needs a
  Security Ops sign-off comment `<!-- sdlc:security-signoff v1 -->`), `builder-of-record` (a `Builder: @login`
  line naming a human on `builders`) and `self-review` (`<!-- sdlc:self-review v1 -->` by the builder on the
  current head SHA).
- Template: post-cutover human PRs also need `## Builder`, `## Workstation`, `## Session`, `## Tier`. The org PR
  template carries them.
- New inputs `builders`, `security_signers`, `cutover` (and CLI flags). `gate_ref` defaults to `sdlc-conformance-v3`.
- Org caller template `workflow-templates/sdlc.yml` (adds the `review_requested` trigger).
- Markers count only as the comment's exact first non-empty line (`<!-- sdlc:self-review v1 --> <head sha>`;
  sign-offs alike), from the right login, never edited by anyone else (GraphQL edit history; exit 2 if unreadable).
- `tier:high` sign-off: on the PR with the head SHA, or on every `tier:high` issue the PR closes per GitHub's
  `closingIssuesReferences`. `Part of` issues raise the tier but never satisfy it. One `Builder:` login only.
- Pre-cutover PRs get exactly the v2 checks. v1 and v2 tags don't move.

## sdlc-conformance-v2 (2026-10-13)

- New job `sdlc / sdlc-ready` posts the `sdlc/ready` commit status (`success` or `pending`) on the PR head.
  Ready = not a draft, the four template sections pass, and a `/naptic-review` marker review
  (body starts `## Three-lens review`) by the author or a trusted reviewer on the head SHA.
- New CLI flag `--post-ready`. Advisory: exits 0 whatever the verdict; exit 2 still means a gate bug.
- Without `statuses: write` in the caller, or on a fork PR, it warns "not posted" and stays green.
- The workflow-level `permissions:` block moved to the `sdlc-conformance` job (same read-only scopes),
  so `sdlc-ready` can inherit the caller's grant.
- v1 is not deprecated and its tag does not move, so no v1 deprecation notice is needed.

## sdlc-conformance-v1 (2026-10-05)

- New reusable workflow `reusable-sdlc-conformance.yml` and `scripts/sdlc_conformance.py`.
- Checks REVIEW.md's definition of done: issue link, the four org-template sections, size,
  valid review on the current head, docs-only claims, and four opt-in checks ported from
  engineer-pipeline (`unf_phase`, `changelog`, `version_bump`, `phase_09c_on_infra`).
- Exit codes: 0 pass, 1 violations in block mode, 2 checker error (red in any mode).
- Before any v2: a deprecation notice ships in v1 first and is listed here.
