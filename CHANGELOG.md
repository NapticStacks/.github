# Changelog

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
