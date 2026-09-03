---
name: Feature / Task
about: Use this template for all sprint work. Engineers can paste this directly into a Claude Code session.
title: ''
labels: needs-estimate
assignees: ''
---

<!--
MIRROR. This is the NapticStacks org default issue template. Every repo without its own
.github/ISSUE_TEMPLATE picks it up. The canonical copy lives in the project-manager repo at
.github/ISSUE_TEMPLATE/feature-task.md. Edit it there first, then mirror the change here.

How to fill this in:
- The `pm:` HTML comments are the contract. Heading text is yours to reword. Do not delete a marker.
- Anything in [square brackets] counts as EMPTY to the readiness check. Replace the whole bracket.
- Set the Target date issue field before you assign this. A missing date blocks the brief.
-->

<!-- pm:outcome -->
## Outcome
[One sentence naming the result that exists once this ships.]

<!-- pm:why -->
## Why this matters
<!-- Board: the My work view on the Naptic Engineering org board, https://github.com/orgs/NapticStacks/projects/<N> (N is the Naptic Engineering board number). -->
- **Advances:** [which KPI row, for which engagement]
- **Parent:** [#N of the epic, or "none yet"]
- **Approver:** [@login, backup @login; escalates after 48 hours]
- **Board:** [link to your My work view]

<!-- pm:scope -->
## Scope
- **Files:** [paths this work may change]
- **Do NOT touch:** [paths, files, or services this work must leave alone]
- **Existing patterns to follow:** [link an example file, or name the pattern]

<!-- pm:acceptance -->
## Acceptance
<!-- pm:test-command -->
- Test: `[exact command to run]`
- Behavior: [observable change a reviewer can verify]
- No regressions: `[test suite command]`

<!-- pm:run-it -->
## Run it
Three phases, in order. Do not merge them together.

**Prepare**
- Open with `/context-restore`, or `/picking-up-work` if this is a fresh session.
- From the project-manager repo, run `python3 scripts/github/delegation_readiness.py --issue <this issue url>`.
- Any gap means the brief is not ready. Comment `needs-brief: <gap>`, reassign to the person who assigned it, and stop.
- Turn on Plan Mode (Shift+Tab), then run `superpowers:writing-plans`.

**Request approval (gate #1)**
- Post the plan as an issue comment. Line 1 is `## Plan`. Line 2 is `entry: board|query|notification`.
- Do not build until the Nap comment on this issue reads READY TO BUILD.
- A `return: <reason>` comment means revise the plan and post it again.
- Waiting is never idle. Start your NEXT item while this gate is open.

**Execute**
- `/clear`
- `superpowers:executing-plans <plan>`
- `superpowers:verification-before-completion`
- `/review`
- `superpowers:requesting-code-review`
- `/ship`
- Write an inventor-notebook entry at each decision along the way.

## Claude Code Instructions
<!-- Engineers: paste this issue into Claude Code with "Use Plan Mode first" -->
- Use Plan Mode before writing any code (Shift+Tab in Claude Code)
- Estimated size: [ ] XS (~50 lines) [ ] S (~150 lines) [ ] M (~400 lines) [ ] L (~800 lines)
- Security constraints: <!-- Any PII, auth, secrets, or sensitive data involved? -->
- Worktree suggestion: `git worktree add ../<branch-name> feature/<branch-name>`

## AI PM Fields
<!-- These are filled in by /backlog-groom. Do not edit them by hand. -->
<!-- Story Points: -->
<!-- Lines Estimate: -->
<!-- Priority: -->
<!-- Epic: -->
