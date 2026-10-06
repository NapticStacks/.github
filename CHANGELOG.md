# Changelog

## sdlc-conformance-v1 (2026-10-05)

- New reusable workflow `reusable-sdlc-conformance.yml` and `scripts/sdlc_conformance.py`.
- Checks REVIEW.md's definition of done: issue link, the four org-template sections, size,
  valid review on the current head, docs-only claims, and four opt-in checks ported from
  engineer-pipeline (`unf_phase`, `changelog`, `version_bump`, `phase_09c_on_infra`).
- Exit codes: 0 pass, 1 violations in block mode, 2 checker error (red in any mode).
- Before any v2: a deprecation notice ships in v1 first and is listed here.
