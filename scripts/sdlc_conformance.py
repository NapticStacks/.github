#!/usr/bin/env python3
"""SDLC conformance gate for NapticStacks pull requests.

The reusable `sdlc-conformance` workflow is a thin caller around this module so
the logic is unit-testable off CI (same shape as test_count.py). It checks a PR
against docs/REVIEW.md's definition of done in NapticStacks/project-manager and
the naptic-sdlc gate catalog:

    evaluate(ctx, opts, issue_lookup, read_file) -> [Finding]   pure
    exit_code(findings, opts)                     -> 0 | 1
    gather(api, repo, number)                     -> ctx         network
    main(argv)                                    -> 0 | 1 | 2

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
             "unf-phase", "changelog", "version-bump", "phase-09c")

TEMPLATE_URL = "https://github.com/NapticStacks/.github/blob/main/.github/pull_request_template.md"
SECTIONS = ("Issue", "Tests named in the issue", "Verification evidence", "/review findings")
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


@dataclass(frozen=True)
class Finding:
    check: str
    level: str
    problem: str
    cause: str
    fix: str

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
    lines = (body or "").splitlines()
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


def check_template(repo, number, body, files) -> list[Finding]:
    found = sections(body)
    edit = f"gh pr edit {number} -R {repo}"
    out = []
    for name in SECTIONS:
        key = name.lower()
        if key not in found:
            out.append(Finding("template", VIOLATION, f"Missing section `## {name}`.",
                               f"REVIEW.md's definition of done needs all four sections of {TEMPLATE_URL}.",
                               f"Add the section (or `N/A because ...`): {edit}"))
            continue
        content = clean(found[key])
        if not content:
            out.append(Finding("template", VIOLATION, f"Section `## {name}` is empty.",
                               "A heading with only the template comment isn't evidence.",
                               f"Fill it in, or write `N/A because <reason>`: {edit}"))
        elif NA_RE.match(content) and not NA_REASON_RE.match(content):
            out.append(Finding("template", VIOLATION, f"Section `## {name}` says N/A with no reason.",
                               "REVIEW.md: every N/A needs a reason.",
                               f"Write `N/A because <reason>`: {edit}"))
        elif key == TESTS_SECTION and NA_DOCS_ONLY_RE.match(content) and not docs_only(files):
            out.append(Finding("docs-only", VIOLATION, "`N/A: docs-only` on a diff that changes code.",
                               "Only a diff that touches docs paths alone can skip the tests item.",
                               f"Paste the tests named in the issue and their output: {edit}"))
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
        out += check_template(repo, number, body, files)
        out += check_size(repo, number, pr, body)
    out += check_docs_only_label(repo, pr, files)
    out += check_review(repo, number, ctx["reviews"], pr["head"]["sha"], opts,
                        (pr.get("user") or {}).get("login"))
    out += check_options(repo, pr, files, opts, read_file, ctx.get("present"))
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


# --- network --------------------------------------------------------------------------

class ApiError(CheckerError):
    def __init__(self, status: int, url: str):
        super().__init__(f"GitHub API returned HTTP {status} for {url}")
        self.status = status


def _urllib_fetch(url: str, headers: dict):
    req = urllib.request.Request(url, headers=headers)
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

    def get(self, path: str, params: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return self.fetch(url, {"Authorization": f"Bearer {self.token}",
                                "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28",
                                "User-Agent": "naptic-sdlc-conformance"})

    def paged(self, path: str) -> list:
        items, page = [], 1
        while True:
            batch = self.get(path, {"per_page": PER_PAGE, "page": page})
            items.extend(batch)
            if len(batch) < PER_PAGE:
                return items
            page += 1


def gather(api: Api, repo: str, number: int) -> dict:
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
    return {"repo": repo, "pr": pr, "files": files, "present": present, "reviews": reviews}


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
                   phase_09c_on_infra=args.phase_09c_on_infra)
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    try:
        if args.pr:
            repo, number = parse_pr_ref(args.pr)
        else:
            repo, number = os.environ["SDLC_REPO"], int(os.environ["SDLC_PR"])
        api = Api(_token())
        ctx = gather(api, repo, number)
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
