#!/usr/bin/env python3
"""SDLC conformance gate for NapticStacks pull requests.

The reusable `sdlc-conformance` workflow is a thin caller around this module so
the logic is unit-testable off CI (same shape as test_count.py). It checks a PR
against docs/REVIEW.md's definition of done in NapticStacks/project-manager and
the naptic-sdlc gate catalog:

    evaluate(ctx, opts, issue_lookup, read_file) -> [Finding]   pure
    exit_code(findings, opts)                     -> 0 | 1
    gather(api, repo, number)                     -> ctx         network
    readiness(ctx, opts)                          -> (state, description)  pure
    main(argv)                                    -> 0 | 1 | 2

v3 adds the SDLC V1 checks `tier`, `builder-of-record` and `self-review`, plus
the V1 template sections. They apply only to PRs created at or after the cutover
(CUTOVER, or `--cutover`); older PRs finish under v2 rules.

`--post-ready` (v2) turns the run into the readiness job: it posts the
`sdlc/ready` commit status on the head SHA and always exits 0 unless the gate
itself breaks (2). Without `statuses: write`, or on a fork PR, it says
"not posted" and still exits 0.

Exit codes (E1): 0 pass (warn mode may still annotate), 1 violations in block
mode, 2 checker error -- red in any mode, and always says "gate bug, not your PR".

The PR title and body are read from the API here, never passed through a shell.
This repo is PUBLIC: keep fixtures made-up. Stdlib only, Python 3.11+.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

DOCS = "https://github.com/NapticStacks/naptic-sdlc/blob/main/docs/gates.md"
GATE_BUG_URL = "https://github.com/NapticStacks/.github/issues/new?title=sdlc-conformance+gate+bug"
API = "https://api.github.com"
ORG = "NapticStacks"
SIZE_LIMIT = 800
PER_PAGE = 100

EXIT_OK, EXIT_VIOLATIONS, EXIT_CHECKER_ERROR = 0, 1, 2
VIOLATION, PENDING, NOTE = "violation", "pending", "note"

# Every id is a heading `#### check: <id>` in naptic-sdlc docs/gates.md, which
# GitHub slugs to `#check-<id>`. Adding an id here means adding that heading.
CHECK_IDS = ("draft", "issue-link", "template", "size", "review", "docs-only",
             "unf-phase", "changelog", "version-bump", "phase-09c",
             "tier", "builder-of-record", "self-review")

TEMPLATE_URL = "https://github.com/NapticStacks/.github/blob/main/.github/pull_request_template.md"
SECTIONS = ("Issue", "Tests named in the issue", "Verification evidence", "/review findings")
SECTIONS_V1 = ("Builder", "Workstation", "Session", "Tier")  # required only after CUTOVER
TESTS_SECTION = "tests named in the issue"
DOCS_ONLY_LABEL = "docs-only"
DOC_SUFFIXES = (".md", ".mdx", ".rst")  # .txt only under docs/: requirements.txt is code
DEPENDABOT_LOGINS = ("dependabot[bot]", "app/dependabot")

COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
EMPTY_FENCE_RE = re.compile(r"```[^\n]*\n\s*```")
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
NA_RE = re.compile(r"^n/?a\b", re.I)
NA_REASON_RE = re.compile(r"^n/?a\s*(?::|because\b)\s*\S", re.I)
NA_DOCS_ONLY_RE = re.compile(r"^n/?a\s*:\s*docs[- ]only", re.I)
REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|part of)\s*:?\s+"
    r"(?:https://github\.com/(?P<uo>[\w.-]+)/(?P<ur>[\w.-]+)/issues/(?P<un>\d+)"
    r"|(?:(?P<o>[\w.-]+)/(?P<r>[\w.-]+))?#(?P<n>\d+))(?![\w/])", re.I)
FENCE_LINE_RE = re.compile(r"^(?:\s*>)*\s*(`{3,}|~{3,})(.*)$")  # blockquote/list-nested fences too
SIZE_JUSTIFICATION_RE = re.compile(r"^size justification:\s*\S", re.I | re.M)
PHASE_RE = re.compile(r"Phase\s+[0-9]{1,2}[a-z]?")
INFRA_RE = re.compile(r"^(infra|terraform|cdk)/")
IAM_RE = re.compile(r"(^|[/_.-])iam([_./-]|$)", re.I)
READINESS_FILE_RE = re.compile(r"(^|/)09c-readiness[^/]*\.md$")

# Readiness v2. The marker is the heading /naptic-review opens every review with
# (napticstack-skills naptic-review/lenses/synthesis.md). Only the PR author or a
# trusted reviewer can supply it, and only on the current head commit.
READY_CONTEXT = "sdlc/ready"
READY_SUCCESS, READY_PENDING = "success", "pending"
STATUS_DESC_MAX = 140  # GitHub truncates commit-status descriptions past this
MARKER_RE = re.compile(r"^##\s+Three-lens review\b")
MARKER_STATES = ("APPROVED", "COMMENTED", "CHANGES_REQUESTED")

# SDLC V1 (v3). PRs created before CUTOVER finish under v2 rules: the three checks
# below and the SECTIONS_V1 template sections skip them.
CUTOVER = "2026-10-16T00:00:00Z"
TIERS = ("low", "medium", "high")
TIER_LABEL_RE = re.compile(r"^tier:(low|medium|high)$", re.I)
BUILDER_RE = re.compile(r"^[ \t>*-]*(?:\*\*)?builder(?:\*\*)?[ \t]*:(?:\*\*)?[ \t]*@?(app/[\w-]+|[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?)",
                        re.I | re.M)
SELF_REVIEW_MARKER = "<!-- sdlc:self-review v1 -->"
SECURITY_SIGNOFF_MARKER = "<!-- sdlc:security-signoff v1 -->"
SHA_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")


@dataclass(frozen=True)
class Finding:
    check: str
    level: str
    problem: str
    cause: str
    fix: str
    # Short structured text for the sdlc/ready description (template findings only), so readiness never rewrites
    # another check's English.
    gap: str = ""

    @property
    def anchor(self) -> str:
        return f"{DOCS}#check-{self.check}"


@dataclass(frozen=True)
class Options:
    mode: str = "warn"
    require_approval: bool = False
    trusted_reviewers: tuple = ("maydaycyber",)
    bot_authors: tuple = ()
    unf_phase: bool = False
    changelog: bool = False
    version_bump: bool = False
    phase_09c_on_infra: bool = False
    # v3 (SDLC V1). `builders`: the human engineer roster (empty = not checked against a roster).
    # `security_signers`: Security Ops first, then the backup who signs when the first one built it.
    cutover: str = CUTOVER
    builders: tuple = ()
    security_signers: tuple = ()


class CheckerError(Exception):
    """The gate could not read what it needs. Always exit 2, never blame the PR."""


# --- small pure helpers ------------------------------------------------------------

def author_kind(login: str | None, bot_authors=()) -> str:
    """Same rules as project-manager scripts/github/project_23_sync.py classify_author()."""
    lowered = (login or "").strip().lower()
    if not lowered:
        return "Human"
    if lowered in DEPENDABOT_LOGINS:
        return "Dependabot"
    if lowered.endswith("[bot]") or lowered.startswith("app/"):
        return "Fleet bot"
    if lowered in {b.lower() for b in bot_authors}:
        return "Fleet bot"
    return "Human"


def is_docs_path(path: str) -> bool:
    return path.startswith("docs/") or path.lower().endswith(DOC_SUFFIXES)


def docs_only(files: list[str]) -> bool:
    return bool(files) and all(is_docs_path(p) for p in files)


def fence_mask(lines: list[str]) -> list[bool]:
    """True for each line that opens, sits inside, or closes a fenced code block.

    CommonMark rules that matter here: a fence is 3+ backticks or tildes; a backtick
    fence's info string can't contain a backtick (so ```x``` is an inline span); it
    closes only on a line of the same char, at least as long, with nothing after it.
    """
    out, fence = [], None
    for line in lines:
        m = FENCE_LINE_RE.match(line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
                out.append(True)
            else:
                out.append(False)
        else:
            out.append(True)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
                fence = None
    return out


def sections(body: str | None) -> dict[str, str]:
    """{lowercased `## heading`: raw content up to the next `## `}."""
    out: dict[str, str] = {}
    current, buf = None, []
    lines = COMMENT_RE.sub("", body or "").splitlines()  # a heading inside <!-- --> isn't one
    for line, fenced in zip(lines, fence_mask(lines)):
        m = None if fenced else HEADING_RE.match(line)
        if m:
            if current is not None:
                out[current] = "\n".join(buf)
            current, buf = m.group(1).strip().lower(), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf)
    return out


def clean(text: str) -> str:
    return EMPTY_FENCE_RE.sub("", COMMENT_RE.sub("", text or "")).strip()


def issue_refs(body: str | None) -> list[tuple[str, str, int]]:
    """(owner, repo, number) for each closing/part-of ref; owner/repo '' = same repo."""
    refs = []
    lines = COMMENT_RE.sub("", body or "").splitlines()
    prose = "\n".join(l for l, fenced in zip(lines, fence_mask(lines)) if not fenced)
    for m in REF_RE.finditer(prose):
        if m.group("un"):
            refs.append((m.group("uo"), m.group("ur"), int(m.group("un"))))
        else:
            refs.append((m.group("o") or "", m.group("r") or "", int(m.group("n"))))
    return refs


def review_state(reviews: list[dict], head_sha: str, trusted) -> str:
    """valid | stale | changes_requested | none, from each trusted reviewer's latest
    decisive review (APPROVED, CHANGES_REQUESTED or DISMISSED; COMMENTED is ignored)."""
    trusted_l = {t.strip().lower() for t in trusted if t.strip()}
    latest: dict[str, dict] = {}
    for r in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        login = ((r.get("user") or {}).get("login") or "").lower()
        if login in trusted_l and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = r
    if any(r["state"] == "CHANGES_REQUESTED" for r in latest.values()):
        return "changes_requested"
    approvals = [r for r in latest.values() if r["state"] == "APPROVED"]
    if any(r.get("commit_id") == head_sha for r in approvals):
        return "valid"
    return "stale" if approvals else "none"


def parse_time(value: str) -> datetime:
    """An ISO 8601 UTC timestamp like GitHub's `2026-10-16T00:00:00Z`. Raises ValueError."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no timezone")
    return dt


def is_v1(pr: dict, opts: "Options") -> bool:
    """True when the V1 checks apply: the PR was created at or after the cutover.
    No `created_at` (GitHub always sends one) counts as post-cutover, so the checks fail closed."""
    created = pr.get("created_at")
    return not created or parse_time(created) >= parse_time(opts.cutover)


def prose(body: str | None) -> str:
    """The body without HTML comments and fenced code."""
    lines = COMMENT_RE.sub("", body or "").splitlines()
    return "\n".join(l for l, fenced in zip(lines, fence_mask(lines)) if not fenced)


def builder_of_record(body: str | None) -> str | None:
    """The login on the first `Builder:` line outside comments and fences, or None."""
    m = BUILDER_RE.search(prose(body))
    return m.group(1) if m else None


def label_names(labels) -> list[str]:
    return [(l.get("name") if isinstance(l, dict) else l) or "" for l in labels or []]


def effective_tier(labels) -> str | None:
    """The highest `tier:*` among the labels, or None when there is none."""
    found = [TIER_LABEL_RE.match(l.strip()) for l in label_names(labels)]
    tiers = {m.group(1).lower() for m in found if m}
    return max(tiers, key=TIERS.index) if tiers else None


def _login(item: dict) -> str:
    return ((item.get("user") or {}).get("login") or "").lower()


# --- the checks ----------------------------------------------------------------------

def check_issue_link(repo, number, body, issue_lookup) -> list[Finding]:
    refs = issue_refs(body)
    edit = f"gh pr edit {number} -R {repo}"
    if not refs:
        return [Finding("issue-link", VIOLATION, "No issue link in the PR body.",
                        "REVIEW.md: no issue link means no scope to review against, and the board can't close the card.",
                        f"Add `Closes #<issue>` (or `Part of #<issue>`) under ## Issue: {edit}")]
    owner, name = repo.split("/")
    out = []
    for o, r, n in refs:
        same = not o or (o.lower() == owner.lower() and r.lower() == name.lower())
        if same:
            kind = issue_lookup(n)
            if kind is None:
                out.append(Finding("issue-link", VIOLATION, f"#{n} does not exist in {repo}.",
                                   "The link points at an issue number that isn't there.",
                                   f"Fix the number: {edit}"))
            elif kind == "pr":
                out.append(Finding("issue-link", VIOLATION, f"#{n} is a pull request, not an issue.",
                                   "Closes/Part of must point at the issue that scopes this work.",
                                   f"Link the issue instead: {edit}"))
        elif o.lower() == ORG.lower():
            out.append(Finding("issue-link", NOTE, f"{o}/{r}#{n} is cross-repo, not verified.",
                               "The caller's token can only read issues in this repo.",
                               "Nothing to do if the link is right."))
        else:
            out.append(Finding("issue-link", VIOLATION, f"{o}/{r}#{n} is outside {ORG}.",
                               "Work is scoped by a NapticStacks issue.",
                               f"Link a NapticStacks issue: {edit}"))
    return out


def check_template(repo, number, body, files, v1=False) -> list[Finding]:
    found = sections(body)
    edit = f"gh pr edit {number} -R {repo}"
    out = []
    for name in SECTIONS + (SECTIONS_V1 if v1 else ()):
        key = name.lower()
        if key not in found:
            out.append(Finding("template", VIOLATION, f"Missing section `## {name}`.",
                               (f"SDLC V1 PRs carry the Builder, Workstation, Session and Tier sections of {TEMPLATE_URL}."
                                if name in SECTIONS_V1 else
                                f"REVIEW.md's definition of done needs all four sections of {TEMPLATE_URL}."),
                               f"Add the section (or `N/A because ...`): {edit}", gap=f"missing {name}"))
            continue
        content = clean(found[key])
        if not content:
            out.append(Finding("template", VIOLATION, f"Section `## {name}` is empty.",
                               "A heading with only the template comment isn't evidence.",
                               f"Fill it in, or write `N/A because <reason>`: {edit}", gap=f"{name} is empty"))
        elif NA_RE.match(content) and not NA_REASON_RE.match(content):
            out.append(Finding("template", VIOLATION, f"Section `## {name}` says N/A with no reason.",
                               "REVIEW.md: every N/A needs a reason.",
                               f"Write `N/A because <reason>`: {edit}", gap=f"{name} N/A without a reason"))
        elif key == TESTS_SECTION and NA_DOCS_ONLY_RE.match(content) and not docs_only(files):
            out.append(Finding("docs-only", VIOLATION, "`N/A: docs-only` on a diff that changes code.",
                               "Only a diff that touches docs paths alone can skip the tests item.",
                               f"Paste the tests named in the issue and their output: {edit}",
                               gap=f"{name} says docs-only on a code diff"))
    return out


def check_size(repo, number, pr, body) -> list[Finding]:
    lines = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    if lines <= SIZE_LIMIT or SIZE_JUSTIFICATION_RE.search(clean(body)):
        return []
    return [Finding("size", VIOLATION, f"{lines} lines changed (limit {SIZE_LIMIT}).",
                    "Big PRs get shallow reviews. CLAUDE.md caps PRs at 800 lines unless justified.",
                    f"Split the PR, or add a line `Size justification: <why>` to the body: gh pr edit {number} -R {repo}")]


def check_review(repo, number, reviews, head_sha, opts, author_login=None) -> list[Finding]:
    state = review_state(reviews, head_sha, opts.trusted_reviewers)
    if state == "valid":
        return []
    level = VIOLATION if (opts.mode == "block" and opts.require_approval) else PENDING
    who = ",".join(opts.trusted_reviewers)
    author = (author_login or "").lower()
    others = [t for t in opts.trusted_reviewers if t.lower() != author]
    if not others:
        return [Finding("review", level, "Pending review: the author is the only trusted reviewer.",
                        f"You are the only trusted reviewer ({who}), and GitHub won't let you approve your own PR.",
                        "Add another reviewer to `trusted_reviewers` in this repo's sdlc.yml, or ask one to review.")]
    problem, cause, fix = {
        "none": ("Pending review: no approval from a trusted reviewer yet.",
                 f"A valid review is an APPROVE from one of: {who}.",
                 f"Request it: gh pr edit {number} -R {repo} --add-reviewer {others[0]}"),
        "stale": ("Pending review: the approval is on an older commit.",
                  "Approvals count only on the current head; a push after approval makes it stale.",
                  f"Re-request review: gh pr edit {number} -R {repo} --add-reviewer {others[0]}"),
        "changes_requested": ("Pending review: changes were requested.",
                              "A trusted reviewer's latest review asks for changes.",
                              f"Address them, push, then re-request: gh pr edit {number} -R {repo} --add-reviewer {others[0]}"),
    }[state]
    return [Finding("review", level, problem, cause, fix)]


def check_docs_only_label(repo, pr, files) -> list[Finding]:
    labels = {(l.get("name") or "").lower() for l in pr.get("labels") or []}
    if DOCS_ONLY_LABEL in labels and not docs_only(files):
        code = [p for p in files if not is_docs_path(p)][:3]
        return [Finding("docs-only", VIOLATION, "`docs-only` label ignored: the diff changes code.",
                        f"Non-docs paths in the diff, e.g. {', '.join(code)}. The label is rechecked on every push.",
                        f"Remove the label: gh pr edit {pr['number']} -R {repo} --remove-label docs-only")]
    return []


def check_options(repo, pr, files, opts, read_file, present=None) -> list[Finding]:
    present = files if present is None else present
    out, n, body = [], pr["number"], pr.get("body") or ""
    if opts.unf_phase and not (PHASE_RE.search(pr.get("title") or "") or PHASE_RE.search(body)):
        out.append(Finding("unf-phase", VIOLATION, "No UNF phase reference in the title or body.",
                           "This repo requires a phase tag such as `Phase 04` or `Phase 09c`.",
                           f"Add it to the title: gh pr edit {n} -R {repo} --title \"<title> (Phase NN)\""))
    if opts.changelog and "CHANGELOG.md" not in files:
        out.append(Finding("changelog", VIOLATION, "CHANGELOG.md was not changed.",
                           "This repo records every change in CHANGELOG.md.",
                           "Run gstack /ship (it bumps CHANGELOG and VERSION together), or add an entry by hand."))
    if opts.version_bump:
        # Base branch tip, not pr.base.sha: matches engineer-pipeline's origin/${BASE_REF}.
        base_v = read_file("VERSION", pr["base"].get("ref") or pr["base"]["sha"])
        head_v = read_file("VERSION", pr["head"]["sha"])
        base_s, head_s = (base_v or "").strip(), (head_v or "").strip()
        if head_v is None:
            out.append(Finding("version-bump", VIOLATION, "There is no VERSION file at the repo root.",
                               "The version_bump option is on for this repo, but it has no VERSION file.",
                               "Add a VERSION file, or turn off `version_bump` in the caller's sdlc.yml."))
        elif "VERSION" not in files or base_s == head_s:
            out.append(Finding("version-bump", VIOLATION, f"VERSION is not bumped (still `{head_s[:40]}`).",
                               "This repo bumps VERSION in every PR.",
                               "Run gstack /ship, or bump VERSION by hand and push."))
    if opts.phase_09c_on_infra and any(INFRA_RE.search(p) or IAM_RE.search(p) for p in files):
        # Evidence must exist at head: a deleted or renamed-away readiness file doesn't count.
        if not (any(READINESS_FILE_RE.search(p) for p in present) or "Phase 09c readiness:" in body):
            out.append(Finding("phase-09c", VIOLATION, "Infra or IAM change without Phase 09c readiness.",
                               "Changes under infra/, terraform/, cdk/ or IAM paths need a readiness checklist.",
                               "Add a `**/09c-readiness*.md` file, or a `Phase 09c readiness:` header with a checklist in the body."))
    return out


def check_tier(repo, pr, ctx, opts, builder) -> list[Finding]:
    n = pr["number"]
    issues = ctx.get("linked_issues") or {}
    labels = label_names(pr.get("labels")) + [l for i in issues.values() for l in label_names(i.get("labels"))]
    tier = effective_tier(labels)
    label = f"gh pr edit {n} -R {repo} --add-label tier:<low|medium|high>"
    if tier is None:
        return [Finding("tier", NOTE, "No tier label: this PR counts as medium.",
                        "The tier comes from a tier:* label on the PR or its linked issue. Untagged work is medium.",
                        f"Label it: {label}")]
    if tier != "high":
        return []
    signers = [s.lower() for s in opts.security_signers]
    if not signers:
        return [Finding("tier", VIOLATION, "tier:high without a security sign-off.",
                        "This caller sets no `security_signers`, so no sign-off can count. High tier fails closed.",
                        "Set `security_signers:` in the repo's .github/workflows/sdlc.yml (Security Ops first, then the backup).")]
    # The first signer signs; a later one only when the first one built it. Nobody signs off their own work.
    b = (builder or "").lower()
    allowed = {signers[0]} if signers[0] != b else set(signers[1:])
    allowed.discard(b)
    comments = list(ctx.get("comments") or []) + [c for i in issues.values() for c in i.get("comments") or []]
    if any(_login(c) in allowed and SECURITY_SIGNOFF_MARKER in (c.get("body") or "") for c in comments):
        return []
    who = ", ".join(sorted(allowed)) or "nobody (every signer is the builder)"
    return [Finding("tier", VIOLATION, "tier:high without a security sign-off.",
                    f"High tier needs a Security Ops comment with `{SECURITY_SIGNOFF_MARKER}` on the linked issue "
                    f"(or this PR) from: {who}.",
                    "Ask Security Ops to review the plan and post the sign-off comment on the issue, then re-run the check.")]


def check_builder(repo, pr, ctx, opts, builder, kind) -> list[Finding]:
    n = pr["number"]
    fix = f"Add `Builder: @<your login>` under the PR body's ## Builder section: gh pr edit {n} -R {repo} --body-file <file>"
    level = NOTE if kind == "Dependabot" else VIOLATION
    if builder is None:
        cause = ("Dependabot can't carry a Builder line; whoever approves the bump answers for it."
                 if kind == "Dependabot" else
                 "Every PR, swarm and bot PRs included, names the human who answers for it.")
        return [Finding("builder-of-record", level, "No `Builder:` line in the PR body.", cause, fix)]
    if author_kind(builder, opts.bot_authors) != "Human":
        return [Finding("builder-of-record", level, f"Builder `{builder}` is a bot.",
                        "The swarm builds, but the builder of record is always a human.", fix)]
    if "builder_account" in ctx:
        account = ctx["builder_account"]
        if account is None:
            return [Finding("builder-of-record", VIOLATION, f"Builder `{builder}` does not exist on GitHub.",
                            "The Builder line must name a real GitHub login. Check the spelling.", fix)]
        if account.get("type") != "User":
            return [Finding("builder-of-record", VIOLATION,
                            f"Builder `{builder}` is a GitHub {account.get('type') or 'non-user'} account, not a person.",
                            "The builder of record is always a human.", fix)]
    if not opts.builders:
        return [Finding("builder-of-record", NOTE, f"Builder `{builder}` was not checked against a roster.",
                        "This caller sets no `builders` roster, so the gate only checked the login is a human account.",
                        "Set `builders:` in the repo's .github/workflows/sdlc.yml to the engineer roster.")]
    if builder.lower() not in {b.lower() for b in opts.builders}:
        return [Finding("builder-of-record", VIOLATION, f"Builder `{builder}` is not on the engineer roster.",
                        f"The builder must be one of this repo's `builders`: {', '.join(opts.builders)}.", fix)]
    return []


def check_self_review(repo, pr, ctx, builder, kind) -> list[Finding]:
    head = pr["head"]["sha"]
    level = NOTE if kind == "Dependabot" else VIOLATION
    fix = "Run /closer on the branch: it runs the reviews and /sdlc-handoff, which posts a fresh marker. It never merges."
    if builder is None:
        return [Finding("self-review", level, "No self-review: there is no builder of record.",
                        "The marker counts only from the builder of record. Fix builder-of-record first.", fix)]
    b = builder.lower()
    marks = [c for c in ctx.get("comments") or [] if SELF_REVIEW_MARKER in (c.get("body") or "")]
    marks += [r for r in ctx.get("reviews") or [] if SELF_REVIEW_MARKER in (r.get("body") or "")
              and r.get("state") != "DISMISSED"]
    mine = [m for m in marks if _login(m) == b]
    # A comment names the head SHA in full; a review is bound to its commit_id by GitHub.
    on_head = [m for m in mine
               if m.get("commit_id") == head or ("commit_id" not in m and head in SHA_RE.findall(m.get("body") or ""))]
    if on_head:
        return []
    if mine:
        return [Finding("self-review", level, f"The self-review marker is on an older commit, not head {head[:7]}.",
                        "A push after the marker makes it stale. The marker must name the current head SHA.", fix)]
    if marks:
        return [Finding("self-review", level, f"The self-review marker isn't from the builder `{builder}`.",
                        "Only the builder of record's own marker counts.", fix)]
    return [Finding("self-review", level, "No self-review marker on this PR.",
                    f"The builder posts `{SELF_REVIEW_MARKER}` with the head SHA after /naptic-review "
                    "(and /cso for medium and high).", fix)]


def check_v1(repo, pr, ctx, opts, kind) -> list[Finding]:
    builder = builder_of_record(pr.get("body"))
    return (check_tier(repo, pr, ctx, opts, builder)
            + check_builder(repo, pr, ctx, opts, builder, kind)
            + check_self_review(repo, pr, ctx, builder, kind))


def evaluate(ctx: dict, opts: Options, issue_lookup, read_file) -> list[Finding]:
    """All findings for one PR. `issue_lookup(n)` -> 'issue' | 'pr' | None;
    `read_file(path, ref)` -> str | None. Both are injected so this stays pure."""
    repo, pr, files = ctx["repo"], ctx["pr"], ctx["files"]
    number, body = pr["number"], pr.get("body") or ""
    if pr.get("draft"):
        return [Finding("draft", NOTE, "Draft PR: checks skipped.",
                        "Drafts aren't ready for review.",
                        f"Mark it ready when it is: gh pr ready {number} -R {repo}")]
    kind = author_kind((pr.get("user") or {}).get("login"), opts.bot_authors)
    human = kind == "Human"
    v1 = is_v1(pr, opts)
    out = check_issue_link(repo, number, body, issue_lookup)
    issue_section = clean(sections(body).get("issue", ""))
    if NA_REASON_RE.match(issue_section):
        # REVIEW.md: every item, the issue link included, accepts `N/A because ...`.
        out = [Finding(f.check, NOTE, f.problem, f"The Issue section says: {issue_section[:80]}", f.fix)
               if f.check == "issue-link" else f for f in out]
    if kind == "Dependabot":
        # Dependabot can't link an issue; pm-dependabot-auto-merge owns those PRs.
        out = [Finding(f.check, NOTE, f.problem, "Dependabot PRs don't carry issue links.", f.fix)
               if f.check == "issue-link" else f for f in out]
    if human:
        out += check_template(repo, number, body, files, v1)
        out += check_size(repo, number, pr, body)
    out += check_docs_only_label(repo, pr, files)
    out += check_review(repo, number, ctx["reviews"], pr["head"]["sha"], opts,
                        (pr.get("user") or {}).get("login"))
    out += check_options(repo, pr, files, opts, read_file, ctx.get("present"))
    if v1:
        out += check_v1(repo, pr, ctx, opts, kind)
    return out


def verdict(findings: list[Finding], opts: Options) -> str:
    """fail | warnings | pending | pass -- the one word the summary line leads with."""
    if exit_code(findings, opts):
        return "fail"
    if any(f.level == VIOLATION for f in findings):
        return "warnings"
    return "pending" if any(f.level == PENDING for f in findings) else "pass"


def exit_code(findings: list[Finding], opts: Options) -> int:
    if opts.mode == "block" and any(f.level == VIOLATION for f in findings):
        return EXIT_VIOLATIONS
    return EXIT_OK


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotation(f: Finding, mode: str) -> str:
    kind = "notice" if f.level != VIOLATION else ("error" if mode == "block" else "warning")
    msg = f"{f.problem} Cause: {f.cause} Fix: {f.fix} Docs: {f.anchor}"
    return f"::{kind} title=sdlc {f.check}::{_escape(msg)}"


# --- readiness (v2) -----------------------------------------------------------------

def is_marker(review: dict) -> bool:
    return review.get("state") in MARKER_STATES and bool(MARKER_RE.match((review.get("body") or "").lstrip()))


def asks_changes(review: dict) -> bool:
    """A marker that asks for changes: GitHub's state, or the verdict on its first line. A self-review is posted as a
    COMMENT (GitHub refuses request-changes on your own PR), so its heading carries the real verdict."""
    first = (review.get("body") or "").lstrip().split("\n", 1)[0]
    return review.get("state") == "CHANGES_REQUESTED" or "REQUEST CHANGES" in first.upper()


def marker_state(reviews: list[dict], head_sha: str, allowed) -> str:
    """ok | changes | stale | none, from /naptic-review marker reviews by `allowed` logins.

    On the head commit, each login's newest marker stands; if any of them asks for changes, the PR is not ready.
    So an author's later marker never clears a trusted reviewer's change request; only that reviewer's own later
    marker does. Markers on older commits are stale. A dismissed review is not a marker."""
    allowed_l = {a.strip().lower() for a in allowed if a and a.strip()}
    marks = sorted((r for r in reviews if is_marker(r)
                    and ((r.get("user") or {}).get("login") or "").lower() in allowed_l),
                   key=lambda r: r.get("submitted_at") or "")
    latest: dict[str, dict] = {}
    for r in marks:
        if r.get("commit_id") == head_sha:
            latest[((r.get("user") or {}).get("login") or "").lower()] = r
    if latest:
        return "changes" if any(asks_changes(r) for r in latest.values()) else "ok"
    return "stale" if marks else "none"


def _fit(items: list[str], prefix: str, suffix: str) -> str:
    """prefix + as many items as fit + suffix, within STATUS_DESC_MAX."""
    for keep in range(len(items), 0, -1):
        more = f" (+{len(items) - keep} more)" if keep < len(items) else ""
        text = prefix + "; ".join(items[:keep]) + more + suffix
        if len(text) <= STATUS_DESC_MAX:
            return text
    return (prefix + suffix)[:STATUS_DESC_MAX]


def readiness(ctx: dict, opts: Options) -> tuple[str, str]:
    """(success | pending, description) for the sdlc/ready status. Pure.

    Ready means: not a draft, the four template sections pass (humans only, same
    as the conformance run), and a /naptic-review marker review on the head SHA."""
    repo, pr = ctx["repo"], ctx["pr"]
    number, head = pr["number"], pr["head"]["sha"]
    short = head[:7]
    if pr.get("draft"):
        return READY_PENDING, f"Draft PR. Mark it ready when it is: gh pr ready {number}"
    author = (pr.get("user") or {}).get("login") or ""
    if author_kind(author, opts.bot_authors) == "Human":
        gaps = [f.gap or f.problem.rstrip(".")
                for f in check_template(repo, number, pr.get("body") or "", ctx["files"], is_v1(pr, opts))]
        if gaps:
            return READY_PENDING, _fit(gaps, "PR body: ", f". Fix: gh pr edit {number}")
    state = marker_state(ctx["reviews"], head, (author, *opts.trusted_reviewers))
    if state == "ok":
        return READY_SUCCESS, f"Ready: template complete, /naptic-review on {short}."
    if state == "changes":
        return READY_PENDING, f"/naptic-review asked for changes on {short}. Fix, push, run /naptic-review {number}"
    older = " (last one is on an older commit)" if state == "stale" else ""
    return READY_PENDING, f"No /naptic-review on head {short}{older}. Run /naptic-review {number}"


def is_fork(repo: str, pr: dict) -> bool:
    head = pr.get("head") or {}
    # No repo key at all (a trimmed payload): assume same-repo; a fork would 403 into the "not posted" path anyway.
    # A null repo is a deleted fork, and counts as one.
    if "repo" not in head:
        return False
    return ((head.get("repo") or {}).get("full_name") or "").lower() != repo.lower()


def post_ready(api: "Api", ctx: dict, opts: Options, in_actions: bool) -> int:
    """Post sdlc/ready on the head SHA. Advisory: exits 0 whatever the verdict."""
    repo, pr = ctx["repo"], ctx["pr"]
    state, desc = readiness(ctx, opts)
    head = pr["head"]["sha"]
    if is_fork(repo, pr):
        return _ready_report(f"sdlc/ready not posted: fork PR (the token is read-only). Would be {state}: {desc}",
                             in_actions, warn=True)
    payload = {"state": state, "context": READY_CONTEXT, "description": desc}
    server, run_id = os.environ.get("GITHUB_SERVER_URL"), os.environ.get("GITHUB_RUN_ID")
    if server and run_id:
        payload["target_url"] = f"{server}/{repo}/actions/runs/{run_id}"
    try:
        api.post(f"/repos/{repo}/statuses/{head}", payload)
    except ApiError as e:
        if e.status != 403:
            raise
        return _ready_report("sdlc/ready not posted: the caller's token lacks `statuses: write`. "
                             "Add it to the caller's permissions (see the v2 caller in .github/workflows/README.md). "
                             f"Would be {state}: {desc}", in_actions, warn=True)
    return _ready_report(f"sdlc/ready {state} on {head[:7]}: {desc}", in_actions, warn=False)


def _ready_report(line: str, in_actions: bool, warn: bool) -> int:
    if in_actions:
        print(f"::{'warning' if warn else 'notice'} title=sdlc ready::{_escape(line)}")
    else:
        print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(f"### {line}\n")
    return EXIT_OK


# --- network --------------------------------------------------------------------------

class ApiError(CheckerError):
    def __init__(self, status: int, url: str):
        super().__init__(f"GitHub API returned HTTP {status} for {url}")
        self.status = status


def _urllib_fetch(url: str, headers: dict, data: bytes | None = None):
    req = urllib.request.Request(url, headers=headers, data=data,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, url) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise CheckerError(f"could not reach the GitHub API ({url}): {e}") from e


class Api:
    """Read-only GitHub REST client. `fetch(url, headers)` is injectable for tests."""

    def __init__(self, token: str, fetch=None):
        self.token, self.fetch = token, fetch or _urllib_fetch

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "naptic-sdlc-conformance"}

    def get(self, path: str, params: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return self.fetch(url, self._headers())

    def post(self, path: str, payload: dict):
        return self.fetch(API + path, {**self._headers(), "Content-Type": "application/json"}, json.dumps(payload).encode())

    def paged(self, path: str) -> list:
        items, page = [], 1
        while True:
            batch = self.get(path, {"per_page": PER_PAGE, "page": page})
            items.extend(batch)
            if len(batch) < PER_PAGE:
                return items
            page += 1


def gather(api: Api, repo: str, number: int, cutover: str = CUTOVER) -> dict:
    pr = api.get(f"/repos/{repo}/pulls/{number}")
    entries = api.paged(f"/repos/{repo}/pulls/{number}/files")
    if len(entries) != pr.get("changed_files"):
        raise CheckerError(f"the file listing returned {len(entries)} of {pr.get('changed_files')} "
                           "changed files (truncated), so docs-only can't be decided")
    # A rename touches both paths: terraform/x.tf -> docs/x.tf is not a docs-only change.
    files, present = [], []
    for e in entries:
        if e.get("status") != "removed":
            present.append(e["filename"])
        files.append(e["filename"])
        if e.get("previous_filename") and e["previous_filename"] != e["filename"]:
            files.append(e["previous_filename"])
    reviews = api.paged(f"/repos/{repo}/pulls/{number}/reviews")
    ctx = {"repo": repo, "pr": pr, "files": files, "present": present, "reviews": reviews}
    if is_v1(pr, Options(cutover=cutover)):
        gather_v1(api, ctx)
    return ctx


def gather_v1(api: Api, ctx: dict) -> None:
    """The extra reads the V1 checks need: PR comments, same-repo linked issues
    (labels; comments only for tier:high), and the builder's account type.
    Cross-repo issues are skipped: the caller's token can't read them."""
    repo, pr = ctx["repo"], ctx["pr"]
    owner, name = repo.split("/")
    ctx["comments"] = api.paged(f"/repos/{repo}/issues/{pr['number']}/comments")
    issues = {}
    for o, r, n in issue_refs(pr.get("body")):
        if (o and (o.lower(), r.lower()) != (owner.lower(), name.lower())) or n in issues:
            continue
        try:
            issue = api.get(f"/repos/{repo}/issues/{n}")
        except ApiError as e:
            if e.status == 404:
                continue
            raise
        if "pull_request" not in issue:
            issues[n] = {"labels": label_names(issue.get("labels")), "comments": []}
    ctx["linked_issues"] = issues
    labels = label_names(pr.get("labels")) + [l for i in issues.values() for l in i["labels"]]
    if effective_tier(labels) == "high":
        for n, issue in issues.items():
            issue["comments"] = api.paged(f"/repos/{repo}/issues/{n}/comments")
    builder = builder_of_record(pr.get("body"))
    if builder and author_kind(builder) == "Human":
        try:
            user = api.get(f"/users/{urllib.parse.quote(builder)}")
            ctx["builder_account"] = {"login": user.get("login"), "type": user.get("type")}
        except ApiError as e:
            if e.status != 404:
                raise
            ctx["builder_account"] = None


def issue_lookup_for(api: Api, repo: str):
    def lookup(n: int):
        try:
            issue = api.get(f"/repos/{repo}/issues/{n}")
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        return "pr" if "pull_request" in issue else "issue"
    return lookup


def read_file_for(api: Api, repo: str):
    def read(path: str, ref: str):
        try:
            data = api.get(f"/repos/{repo}/contents/{path}", {"ref": ref})
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        if data.get("encoding") != "base64":
            raise CheckerError(f"{path} at {ref} came back without inline content "
                               f"(encoding {data.get('encoding')!r}, size {data.get('size')})")
        return base64.b64decode(data.get("content") or "").decode("utf-8", "replace")
    return read


# --- CLI -------------------------------------------------------------------------------

def parse_pr_ref(ref: str) -> tuple[str, int]:
    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", ref.strip())
    if not m:
        raise ValueError(f"--pr must look like owner/repo#N, got {ref!r}")
    return m.group(1), int(m.group(2))


def _token() -> str:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise CheckerError("no token: set GH_TOKEN or run `gh auth login`") from e


def _csv(value: str) -> tuple:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Check a PR against the NapticStacks SDLC gate.")
    p.add_argument("--pr", help="owner/repo#N (local mode). In Actions, SDLC_REPO + SDLC_PR are used.")
    p.add_argument("--mode", choices=("warn", "block"), default="warn")
    p.add_argument("--require-approval", action="store_true")
    p.add_argument("--trusted-reviewers", default="maydaycyber")
    p.add_argument("--bot-authors", default="")
    p.add_argument("--cutover", default=CUTOVER,
                   help="v3: PRs created before this UTC time skip the V1 checks (default %(default)s).")
    p.add_argument("--builders", default="", help="v3: comma-separated human engineer roster for builder-of-record.")
    p.add_argument("--security-signers", default="",
                   help="v3: comma-separated Security Ops signers, primary first, then the backup.")
    p.add_argument("--post-ready", action="store_true",
                   help="v2 readiness job: post the sdlc/ready commit status instead of conformance findings.")
    for flag in ("unf-phase", "changelog", "version-bump", "phase-09c-on-infra"):
        p.add_argument(f"--{flag}", action="store_true")
    return p


def _print(findings: list[Finding], opts: Options, in_actions: bool) -> None:
    for f in findings:
        if in_actions:
            print(annotation(f, opts.mode))
        else:
            print(f"[{f.level}] {f.check}: {f.problem}\n  cause: {f.cause}\n  fix:   {f.fix}\n  docs:  {f.anchor}")
    violations = sum(f.level == VIOLATION for f in findings)
    summary = f"sdlc-conformance ({opts.mode}): {verdict(findings, opts)}, {violations} violation(s)."
    print(summary)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(f"### {summary}\n\n" + "".join(
                f"- **{f.check}** ({f.level}): {f.problem} Fix: {f.fix} [docs]({f.anchor})\n" for f in findings))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    opts = Options(mode=args.mode, require_approval=args.require_approval,
                   trusted_reviewers=_csv(args.trusted_reviewers) or ("maydaycyber",),
                   bot_authors=_csv(args.bot_authors), unf_phase=args.unf_phase,
                   changelog=args.changelog, version_bump=args.version_bump,
                   phase_09c_on_infra=args.phase_09c_on_infra, cutover=args.cutover,
                   builders=_csv(args.builders), security_signers=_csv(args.security_signers))
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    try:
        if args.pr:
            repo, number = parse_pr_ref(args.pr)
        else:
            repo, number = os.environ["SDLC_REPO"], int(os.environ["SDLC_PR"])
        parse_time(opts.cutover)  # a bad --cutover is a checker error, never a silent skip
        api = Api(_token())
        ctx = gather(api, repo, number, opts.cutover)
        if args.post_ready:
            return post_ready(api, ctx, opts, in_actions)
        findings = evaluate(ctx, opts, issue_lookup_for(api, repo), read_file_for(api, repo))
        _print(findings, opts, in_actions)
    except Exception as e:  # noqa: BLE001 -- any failure here is the gate's, never the PR's (E1: exit 2)
        msg = (f"Checker error, gate bug, not your PR: {e}. Re-run the job; if it fails "
               f"again, file it: {GATE_BUG_URL} Docs: {DOCS}#checker-error")
        print(f"::error title=sdlc checker error::{_escape(msg)}" if in_actions else msg)
        return EXIT_CHECKER_ERROR
    return exit_code(findings, opts)


if __name__ == "__main__":
    sys.exit(main())
