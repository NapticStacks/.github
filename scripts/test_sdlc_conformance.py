"""Unit tests for sdlc_conformance.py. All data is made up: this repo is public."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import sdlc_conformance as sc

FIXTURES = Path(__file__).parent / "fixtures" / "sdlc"
HEAD = "a" * 40
REPO = "NapticStacks/example"


@pytest.fixture(autouse=True)
def _not_in_actions(monkeypatch):
    """CI sets GITHUB_ACTIONS/GITHUB_STEP_SUMMARY; tests assert the local output format."""
    for var in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "SDLC_REPO", "SDLC_PR", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def make_ctx(**pr_overrides):
    pr = json.loads((FIXTURES / "pr_good.json").read_text())
    pr.update(pr_overrides)
    return {"repo": REPO, "pr": pr,
            "files": ["src/widget.py", "tests/test_widget.py"],
            "reviews": [approve("maydaycyber", HEAD)]}


def approve(login, sha, state="APPROVED", at="2026-10-05T12:00:00Z"):
    return {"user": {"login": login}, "state": state, "commit_id": sha, "submitted_at": at}


def lookup_ok(number):
    return "issue"


def no_file(path, ref):
    return None


def run(ctx, opts=None, issue_lookup=lookup_ok, read_file=no_file):
    return sc.evaluate(ctx, opts or sc.Options(), issue_lookup, read_file)


def checks(findings, level=None):
    return sorted(f.check for f in findings if level is None or f.level == level)


# --- the happy path -------------------------------------------------------------

def test_good_pr_has_no_findings():
    assert run(make_ctx()) == []


def test_draft_is_skipped_with_one_note():
    findings = run(make_ctx(draft=True, body=""))
    assert [(f.check, f.level) for f in findings] == [("draft", sc.NOTE)]


# --- issue link (X4, E-1) --------------------------------------------------------

def test_missing_issue_link_is_a_violation():
    body = make_ctx()["pr"]["body"].replace("Closes #7", "No link here")
    assert "issue-link" in checks(run(make_ctx(body=body)), sc.VIOLATION)


def test_issue_ref_inside_html_comment_does_not_count():
    body = make_ctx()["pr"]["body"].replace("Closes #7", "<!-- Closes #7 -->")
    assert "issue-link" in checks(run(make_ctx(body=body)), sc.VIOLATION)


@pytest.mark.parametrize("ref", ["Fixes #7", "Part of #7", "closes NapticStacks/example#7",
                                 "Resolves https://github.com/NapticStacks/example/issues/7"])
def test_same_repo_ref_forms_are_verified(ref):
    seen = []
    body = make_ctx()["pr"]["body"].replace("Closes #7", ref)
    findings = run(make_ctx(body=body), issue_lookup=lambda n: seen.append(n) or "issue")
    assert seen == [7]
    assert "issue-link" not in checks(findings)


def test_same_repo_ref_to_missing_issue_is_a_violation():
    findings = run(make_ctx(), issue_lookup=lambda n: None)
    assert "issue-link" in checks(findings, sc.VIOLATION)


def test_same_repo_ref_to_a_pull_request_is_a_violation():
    findings = run(make_ctx(), issue_lookup=lambda n: "pr")
    assert "issue-link" in checks(findings, sc.VIOLATION)


def test_cross_repo_napticstacks_ref_is_a_note_not_verified():
    body = make_ctx()["pr"]["body"].replace("Closes #7", "Part of NapticStacks/other-repo#3")
    findings = run(make_ctx(body=body), issue_lookup=lambda n: pytest.fail("must not look up"))
    assert [(f.check, f.level) for f in findings] == [("issue-link", sc.NOTE)]
    assert "not verified" in findings[0].problem


def test_ref_outside_the_org_is_a_violation():
    body = make_ctx()["pr"]["body"].replace("Closes #7", "Closes someone-else/repo#3")
    assert "issue-link" in checks(run(make_ctx(body=body)), sc.VIOLATION)


# --- template sections (REVIEW.md definition of done) ----------------------------

def test_missing_section_is_a_violation():
    body = make_ctx()["pr"]["body"].replace("## Verification evidence", "## Something else")
    findings = run(make_ctx(body=body))
    assert any(f.check == "template" and "Verification evidence" in f.problem for f in findings)


def test_section_with_only_comment_and_empty_fence_is_empty():
    body = make_ctx()["pr"]["body"].replace(
        "```\n$ curl -s localhost:8000/widget\n{\"ok\": true}\n```",
        "<!-- paste output -->\n\n```\n```")
    assert "template" in checks(run(make_ctx(body=body)), sc.VIOLATION)


def test_na_with_reason_is_accepted():
    body = make_ctx()["pr"]["body"].replace("No findings.", "N/A because the branch is one typo fix")
    assert run(make_ctx(body=body)) == []


def test_bare_na_is_a_violation():
    body = make_ctx()["pr"]["body"].replace("No findings.", "N/A")
    assert "template" in checks(run(make_ctx(body=body)), sc.VIOLATION)


def test_bots_skip_template_and_size():
    ctx = make_ctx(body="Closes #7", additions=5000, user={"login": "naptic-hp-agent[bot]"})
    assert checks(run(ctx)) == []


# --- size -------------------------------------------------------------------------

def test_over_800_lines_without_justification_is_a_violation():
    assert "size" in checks(run(make_ctx(additions=700, deletions=101)), sc.VIOLATION)


def test_over_800_lines_with_justification_passes():
    body = make_ctx()["pr"]["body"] + "\nSize justification: generated migration, reviewed by diff stat\n"
    assert run(make_ctx(additions=900, deletions=0, body=body)) == []


# --- valid review (X2, C-3) -------------------------------------------------------

@pytest.mark.parametrize("reviews,expected_state", [
    ([], "none"),
    ([approve("maydaycyber", "c" * 40)], "stale"),
    ([approve("maydaycyber", HEAD, "CHANGES_REQUESTED")], "changes_requested"),
    ([approve("maydaycyber", HEAD), approve("maydaycyber", HEAD, "DISMISSED", "2026-10-05T13:00:00Z")], "none"),
    ([approve("someone-untrusted", HEAD)], "none"),
    ([approve("maydaycyber", HEAD, "COMMENTED")], "none"),
    ([approve("maydaycyber", "c" * 40), approve("maydaycyber", HEAD, at="2026-10-05T14:00:00Z")], "valid"),
])
def test_review_state(reviews, expected_state):
    assert sc.review_state(reviews, HEAD, ("maydaycyber",)) == expected_state


def test_pending_review_is_pending_in_warn_mode():
    ctx = make_ctx(); ctx["reviews"] = []
    assert [(f.check, f.level) for f in run(ctx)] == [("review", sc.PENDING)]


def test_block_mode_with_require_approval_fails_on_stale_review():
    ctx = make_ctx(); ctx["reviews"] = [approve("maydaycyber", "c" * 40)]
    findings = run(ctx, sc.Options(mode="block", require_approval=True))
    assert [(f.check, f.level) for f in findings] == [("review", sc.VIOLATION)]
    assert sc.exit_code(findings, sc.Options(mode="block")) == sc.EXIT_VIOLATIONS


def test_trusted_reviewers_are_configurable():
    ctx = make_ctx(); ctx["reviews"] = [approve("BenBlanke", HEAD)]
    assert run(ctx, sc.Options(trusted_reviewers=("maydaycyber", "BenBlanke"))) == []


# --- author kind (mirrors project-manager classify_author) ------------------------

@pytest.mark.parametrize("login,kind", [
    ("dependabot[bot]", "Dependabot"), ("app/dependabot", "Dependabot"),
    ("naptic-hp-agent[bot]", "Fleet bot"), ("app/some-app", "Fleet bot"),
    ("listed-bot", "Fleet bot"), ("example-dev", "Human"), ("", "Human"),
])
def test_author_kind(login, kind):
    assert sc.author_kind(login, ("listed-bot",)) == kind


# --- docs-only (X3) ---------------------------------------------------------------

def test_docs_only_label_on_code_diff_is_a_violation():
    ctx = make_ctx(labels=[{"name": "docs-only"}])
    assert "docs-only" in checks(run(ctx), sc.VIOLATION)


def test_docs_only_label_on_docs_diff_is_honored():
    ctx = make_ctx(labels=[{"name": "docs-only"}])
    ctx["files"] = ["docs/guide.md", "README.md"]
    assert run(ctx) == []


def test_na_docs_only_tests_claim_on_code_diff_is_a_violation():
    body = make_ctx()["pr"]["body"].replace(
        "```\npytest tests/test_widget.py -q\n3 passed in 0.12s\n```", "N/A: docs-only")
    assert "docs-only" in checks(run(make_ctx(body=body)), sc.VIOLATION)


def test_docs_only_label_fix_names_the_repo():
    ctx = make_ctx(labels=[{"name": "docs-only"}])
    [f] = [f for f in run(ctx) if f.check == "docs-only"]
    assert f"-R {REPO}" in f.fix


def test_is_docs_path():
    assert sc.is_docs_path("docs/a/b.png")
    assert sc.is_docs_path("README.md")
    assert not sc.is_docs_path("src/app.py")


# --- migration options (C-4) ------------------------------------------------------

def test_unf_phase_option():
    opts = sc.Options(unf_phase=True)
    assert "unf-phase" in checks(run(make_ctx(), opts), sc.VIOLATION)
    assert run(make_ctx(title="feat: widget (Phase 04)"), opts) == []


def test_changelog_option():
    opts = sc.Options(changelog=True)
    ctx = make_ctx()
    assert "changelog" in checks(run(ctx, opts), sc.VIOLATION)
    ctx["files"].append("CHANGELOG.md")
    assert run(ctx, opts) == []


def test_version_bump_option():
    opts = sc.Options(version_bump=True)
    ctx = make_ctx(); ctx["files"].append("VERSION")
    versions = {("VERSION", "main"): "1.0.0\n", ("VERSION", HEAD): "1.0.1\n"}
    assert run(ctx, opts, read_file=lambda p, r: versions.get((p, r))) == []
    same = {("VERSION", "main"): "1.0.0\n", ("VERSION", HEAD): "1.0.0\n"}
    assert "version-bump" in checks(run(ctx, opts, read_file=lambda p, r: same.get((p, r))), sc.VIOLATION)
    ctx["files"].remove("VERSION")
    assert "version-bump" in checks(run(ctx, opts, read_file=lambda p, r: versions.get((p, r))), sc.VIOLATION)


def test_version_bump_ignores_trailing_whitespace_only_changes():
    opts = sc.Options(version_bump=True)
    ctx = make_ctx(); ctx["files"].append("VERSION")
    ws = {("VERSION", "main"): "1.0.0\n", ("VERSION", HEAD): "1.0.0"}
    assert "version-bump" in checks(run(ctx, opts, read_file=lambda p, r: ws.get((p, r))), sc.VIOLATION)


def test_phase_09c_option():
    opts = sc.Options(phase_09c_on_infra=True)
    ctx = make_ctx(); ctx["files"].append("cdk/stack.py")
    assert "phase-09c" in checks(run(ctx, opts), sc.VIOLATION)
    ctx["files"].append("docs/09c-readiness-widget.md")
    assert run(ctx, opts) == []
    iam = make_ctx(); iam["files"].append("policies/iam_role.json")
    assert "phase-09c" in checks(run(iam, opts), sc.VIOLATION)
    claims = make_ctx(); claims["files"].append("src/claims/handler.py")
    assert "phase-09c" not in checks(run(claims, opts))


# --- verdict + rendering (E1, DX2) ------------------------------------------------

def test_exit_codes():
    v = sc.Finding("size", sc.VIOLATION, "p", "c", "f")
    p = sc.Finding("review", sc.PENDING, "p", "c", "f")
    assert sc.exit_code([v], sc.Options(mode="warn")) == sc.EXIT_OK
    assert sc.exit_code([v], sc.Options(mode="block")) == sc.EXIT_VIOLATIONS
    assert sc.exit_code([p], sc.Options(mode="block")) == sc.EXIT_OK


def every_finding():
    """One finding of every kind the checks can emit."""
    out = []
    ctx = make_ctx(draft=True); out += run(ctx)
    ctx = make_ctx(body="", labels=[{"name": "docs-only"}], additions=900); ctx["reviews"] = []
    ctx["files"].append("cdk/x.py")
    out += run(ctx, sc.Options(unf_phase=True, changelog=True, version_bump=True, phase_09c_on_infra=True),
               issue_lookup=lambda n: None)
    # v3: a post-cutover PR with no Builder line, a tier:high label and no markers.
    out += run(v1_ctx(labels=("tier:high",), comments=[], body="## Issue\nCloses #7\n"), V1_OPTS)
    return out


def test_every_finding_has_four_parts_and_a_known_anchor():
    for f in every_finding():
        assert f.problem and f.cause and f.fix, f
        assert f.check in sc.CHECK_IDS, f.check
        assert f.anchor == f"{sc.DOCS}#check-{f.check}"


def test_every_check_id_is_emitted_somewhere():
    emitted = {f.check for f in every_finding()}
    assert emitted == set(sc.CHECK_IDS)


def test_annotation_escapes_newlines_and_names_all_four_parts():
    f = sc.Finding("size", sc.VIOLATION, "Too big\nreally", "c%", "fix it")
    line = sc.annotation(f, "warn")
    assert line.startswith("::warning title=sdlc size::")
    assert "\n" not in line and "%0A" in line and "%25" in line
    for part in ("Cause:", "Fix:", "Docs:"):
        assert part in line
    assert sc.annotation(f, "block").startswith("::error ")
    assert sc.annotation(sc.Finding("review", sc.PENDING, "p", "c", "f"), "block").startswith("::notice ")


def test_injection_body_is_inert(tmp_path):
    canary = tmp_path / "pwned"
    body = make_ctx()["pr"]["body"] + f"\n$(touch {canary}) `touch {canary}`\n"
    run(make_ctx(body=body))
    assert not canary.exists()


# --- API + gather (E-3/E-4, error rows) --------------------------------------------

class FakeTransport:
    """url -> payload, or an int status to raise. Records every URL fetched."""
    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def __call__(self, url, headers):
        self.seen.append(url)
        assert url.startswith("https://"), url
        assert headers["Authorization"] == "Bearer t0ken"
        for prefix, payload in self.routes.items():
            if url.startswith(sc.API + prefix):
                if isinstance(payload, int):
                    raise sc.ApiError(payload, url)
                return payload(url) if callable(payload) else payload
        raise sc.ApiError(404, url)


def paged(items):
    def handler(url):
        q = dict(p.split("=") for p in url.split("?", 1)[1].split("&"))
        page, per = int(q["page"]), int(q["per_page"])
        return items[(page - 1) * per: page * per]
    return handler


def pr_routes(n_files=2, pr_extra=None):
    pr = json.loads((FIXTURES / "pr_good.json").read_text())
    pr.update(changed_files=n_files, **(pr_extra or {}))
    files = [{"filename": f"docs/page{i}.md"} for i in range(n_files)]
    return {"/repos/NapticStacks/example/pulls/42/files": paged(files),
            "/repos/NapticStacks/example/pulls/42/reviews": paged([approve("maydaycyber", HEAD)]),
            "/repos/NapticStacks/example/pulls/42": pr}


def test_gather_pages_through_150_files():
    transport = FakeTransport(pr_routes(n_files=150))
    ctx = sc.gather(sc.Api("t0ken", transport), REPO, 42)
    assert len(ctx["files"]) == 150
    assert sum("/files?" in u for u in transport.seen) == 2


def test_truncated_listing_is_a_checker_error():
    routes = pr_routes(n_files=150)
    routes["/repos/NapticStacks/example/pulls/42"] = dict(routes["/repos/NapticStacks/example/pulls/42"], changed_files=3001)
    with pytest.raises(sc.CheckerError, match="truncated"):
        sc.gather(sc.Api("t0ken", FakeTransport(routes)), REPO, 42)


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_api_failure_reading_the_pr_is_a_checker_error(status):
    api = sc.Api("t0ken", FakeTransport({"/repos/NapticStacks/example/pulls/42": status}))
    with pytest.raises(sc.CheckerError):
        sc.gather(api, REPO, 42)


def test_issue_lookup_maps_404_to_none_and_prs_to_pr():
    api = sc.Api("t0ken", FakeTransport({
        "/repos/NapticStacks/example/issues/7": {"number": 7},
        "/repos/NapticStacks/example/issues/8": {"number": 8, "pull_request": {}},
    }))
    lookup = sc.issue_lookup_for(api, REPO)
    assert (lookup(7), lookup(8), lookup(9)) == ("issue", "pr", None)


def test_issue_lookup_403_is_a_checker_error():
    api = sc.Api("t0ken", FakeTransport({"/repos/NapticStacks/example/issues/7": 403}))
    with pytest.raises(sc.CheckerError):
        sc.issue_lookup_for(api, REPO)(7)


def test_parse_pr_ref():
    assert sc.parse_pr_ref("NapticStacks/slack-bot#12") == ("NapticStacks/slack-bot", 12)
    with pytest.raises(ValueError):
        sc.parse_pr_ref("slack-bot 12")


def test_main_exit_2_on_checker_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise sc.CheckerError("could not read the PR (HTTP 403)")
    monkeypatch.setattr(sc, "gather", boom)
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    assert sc.main(["--pr", "NapticStacks/example#42", "--mode", "warn"]) == sc.EXIT_CHECKER_ERROR
    out = capsys.readouterr().out
    assert "gate bug, not your PR" in out and sc.GATE_BUG_URL in out


def test_main_block_mode_returns_1(monkeypatch, capsys):
    ctx = make_ctx(additions=5000)
    monkeypatch.setattr(sc, "gather", lambda api, repo, n, *_, **__: ctx)
    monkeypatch.setattr(sc, "issue_lookup_for", lambda api, repo, *_: lookup_ok)
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    assert sc.main(["--pr", "NapticStacks/example#42", "--mode", "block"]) == sc.EXIT_VIOLATIONS
    assert sc.main(["--pr", "NapticStacks/example#42", "--mode", "warn"]) == sc.EXIT_OK


def test_main_reads_repo_and_number_from_env(monkeypatch):
    seen = {}
    monkeypatch.setattr(sc, "gather", lambda api, repo, n, *_, **__: seen.update(repo=repo, n=n) or make_ctx())
    monkeypatch.setattr(sc, "issue_lookup_for", lambda api, repo, *_: lookup_ok)
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    monkeypatch.setenv("SDLC_REPO", "NapticStacks/example")
    monkeypatch.setenv("SDLC_PR", "42")
    assert sc.main([]) == sc.EXIT_OK
    assert seen == {"repo": "NapticStacks/example", "n": 42}


def test_main_unexpected_exception_is_exit_2_not_1(monkeypatch, capsys):
    def boom(*a, **k):
        raise TypeError("unexpected payload shape")
    monkeypatch.setattr(sc, "gather", boom)
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    assert sc.main(["--pr", "NapticStacks/example#42", "--mode", "block"]) == sc.EXIT_CHECKER_ERROR
    assert "gate bug, not your PR" in capsys.readouterr().out


# --- workflow contract (S-base, C-1, X5) --------------------------------------------

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "reusable-sdlc-conformance.yml"


def test_workflow_never_reads_pr_text_from_the_event():
    text = WORKFLOW.read_text()
    for banned in ("pull_request.body", "pull_request.title", "pull_request_target", "secrets."):
        assert banned not in text, banned


def test_workflow_permissions_are_read_only():
    text = WORKFLOW.read_text()
    assert "contents: read" in text and "pull-requests: read" in text and "issues: read" in text
    assert ": write" not in text


def test_workflow_pins_every_action_to_a_sha():
    for line in WORKFLOW.read_text().splitlines():
        if "uses:" in line and not line.strip().startswith("#"):
            ref = line.split("@", 1)[1].split()[0]
            assert re.fullmatch(r"[0-9a-f]{40}", ref), line


def test_workflow_defaults_to_its_own_tag_and_job_name():
    text = WORKFLOW.read_text()
    assert "default: sdlc-conformance-v3" in text
    assert "name: sdlc-conformance" in text


# --- review follow-ups ------------------------------------------------------------

def test_dependabot_missing_issue_link_is_a_note_not_a_violation():
    ctx = make_ctx(body="Bumps foo from 1.0 to 1.1.", user={"login": "dependabot[bot]"})
    findings = run(ctx, sc.Options(mode="block"))
    assert [(f.check, f.level) for f in findings] == [("issue-link", sc.NOTE)]
    assert sc.exit_code(findings, sc.Options(mode="block")) == sc.EXIT_OK


def test_fleet_bot_still_needs_an_issue_link():
    ctx = make_ctx(body="no link", user={"login": "naptic-hp-agent[bot]"})
    assert "issue-link" in checks(run(ctx), sc.VIOLATION)


@pytest.mark.parametrize("mode,levels,expected", [
    ("warn", [sc.VIOLATION, sc.PENDING], "warnings"),
    ("block", [sc.VIOLATION], "fail"),
    ("warn", [sc.PENDING], "pending"),
    ("warn", [sc.NOTE], "pass"),
    ("warn", [], "pass"),
])
def test_verdict_words(mode, levels, expected):
    findings = [sc.Finding("size", lvl, "p", "c", "f") for lvl in levels]
    assert sc.verdict(findings, sc.Options(mode=mode)) == expected


# --- three-lens review fixes (.github#7) -------------------------------------------

def test_txt_manifests_are_not_docs():
    assert not sc.is_docs_path("requirements.txt")
    assert not sc.is_docs_path("CMakeLists.txt")
    assert not sc.docs_only(["requirements.txt"])
    assert sc.is_docs_path("docs/notes.txt")


def test_bad_pr_ref_exits_2_not_1(capsys):
    assert sc.main(["--pr", "slack-bot 12"]) == sc.EXIT_CHECKER_ERROR


def test_review_fix_never_suggests_the_author():
    ctx = make_ctx(user={"login": "maydaycyber"}); ctx["reviews"] = []
    f = run(ctx, sc.Options(trusted_reviewers=("maydaycyber", "BenBlanke")))[0]
    assert "--add-reviewer BenBlanke" in f.fix and "maydaycyber" not in f.fix


def test_review_when_author_is_the_only_trusted_reviewer():
    ctx = make_ctx(user={"login": "maydaycyber"}); ctx["reviews"] = []
    f = run(ctx)[0]
    assert "--add-reviewer" not in f.fix
    assert "only trusted reviewer" in f.cause


def test_version_bump_message_is_plain_text():
    ctx = make_ctx()
    f = [x for x in run(ctx, sc.Options(version_bump=True)) if x.check == "version-bump"][0]
    assert "None" not in f.problem and "\\n" not in f.problem
    assert "no VERSION file" in f.problem
    long = {("VERSION", "main"): "1.0.0\n", ("VERSION", "a" * 40): "x" * 500}
    ctx["files"].append("VERSION")
    f = [x for x in run(ctx, sc.Options(version_bump=True), read_file=lambda p, r: long.get((p, r))) if x.check == "version-bump"]
    assert f == [] or len(f[0].problem) < 200


def test_missing_section_cause_names_the_org_template():
    body = make_ctx()["pr"]["body"].replace("## Issue", "## What")
    f = [x for x in run(make_ctx(body=body)) if x.check == "template"][0]
    assert "pull_request_template.md" in f.cause


def test_step_summary_carries_the_fix(tmp_path, monkeypatch):
    out = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(out))
    sc._print([sc.Finding("size", sc.VIOLATION, "p", "c", "run this")], sc.Options(), False)
    assert "run this" in out.read_text()


def test_gate_ref_must_be_a_gate_tag_or_sha():
    text = WORKFLOW.read_text()
    assert "sdlc-conformance-v" in text and "GATE_REF" in text


# --- codex round 1 (.github#7) ------------------------------------------------------

def test_version_file_without_inline_content_is_a_checker_error():
    api = sc.Api("t0ken", FakeTransport({
        "/repos/NapticStacks/example/contents/VERSION": {"encoding": "none", "content": "", "size": 2_000_000}}))
    with pytest.raises(sc.CheckerError):
        sc.read_file_for(api, REPO)("VERSION", "a" * 40)


@pytest.mark.parametrize("ref", ["Closes https://github.com/NapticStacks/example/issues/7oops",
                                 "Closes #7oops", "Closes NapticStacks/example#7x"])
def test_malformed_refs_are_not_links(ref):
    assert sc.issue_refs(ref) == []


def test_reporting_failure_is_exit_2(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "gather", lambda api, repo, n, *_, **__: make_ctx())
    monkeypatch.setattr(sc, "issue_lookup_for", lambda api, repo, *_: lookup_ok)
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "missing-dir" / "summary.md"))
    assert sc.main(["--pr", "NapticStacks/example#42"]) == sc.EXIT_CHECKER_ERROR



# --- functionality lens (.github#7) -------------------------------------------------

def test_issue_na_with_reason_is_a_note():
    body = make_ctx()["pr"]["body"].replace("Closes #7", "N/A because this is a spike with no issue")
    findings = run(make_ctx(body=body))
    assert [(f.check, f.level) for f in findings] == [("issue-link", sc.NOTE)]


def test_heading_inside_a_code_fence_does_not_split_sections():
    body = make_ctx()["pr"]["body"].replace(
        "3 passed in 0.12s", "3 passed in 0.12s\n## Verification evidence\nfake")
    found = sc.sections(body)
    assert "3 passed" in found["tests named in the issue"] and "fake" in found["tests named in the issue"]


def test_ref_inside_a_code_fence_is_ignored():
    body = make_ctx()["pr"]["body"].replace("3 passed in 0.12s", "abc123 fixes #3 in the parser")
    seen = []
    run(make_ctx(body=body), issue_lookup=lambda n: seen.append(n) or "issue")
    assert seen == [7]


# --- codex round 2 (.github#7) ------------------------------------------------------

def test_inline_triple_backtick_span_does_not_open_a_fence():
    body = make_ctx()["pr"]["body"].replace(
        "```\npytest tests/test_widget.py -q\n3 passed in 0.12s\n```", "```pytest tests/test_widget.py``` 3 passed")
    assert run(make_ctx(body=body)) == []


def test_four_backtick_fence_holding_three_backticks():
    body = "````markdown\n```\n````\n" + make_ctx()["pr"]["body"]
    assert [n for _, _, n in sc.issue_refs(body)] == [7]
    assert "verification evidence" in sc.sections(body)


def test_version_compare_uses_full_values():
    ctx = make_ctx(); ctx["files"].append("VERSION")
    v = {("VERSION", "main"): "1.0.0+" + "a" * 34 + "1", ("VERSION", "a" * 40): "1.0.0+" + "a" * 34 + "2"}
    assert run(ctx, sc.Options(version_bump=True), read_file=lambda p, r: v.get((p, r))) == []


@pytest.mark.parametrize("block", ["> ```text\n> Closes #999\n> ```", "- step\n    ```text\n    Closes #999\n    ```"])
def test_fences_in_blockquotes_and_lists_hide_refs(block):
    assert [n for _, _, n in sc.issue_refs(block + "\n\nCloses #7\n")] == [7]


def test_renames_count_both_paths():
    routes = pr_routes(n_files=1)
    routes["/repos/NapticStacks/example/pulls/42/files"] = paged(
        [{"filename": "docs/main.tf", "previous_filename": "terraform/main.tf", "status": "renamed"}])
    ctx = sc.gather(sc.Api("t0ken", FakeTransport(routes)), REPO, 42)
    assert sorted(ctx["files"]) == ["docs/main.tf", "terraform/main.tf"]
    assert not sc.docs_only(ctx["files"])


def test_deleted_readiness_file_is_not_evidence():
    opts = sc.Options(phase_09c_on_infra=True)
    ctx = make_ctx(); ctx["files"] += ["terraform/main.tf", "docs/09c-readiness-old.md"]
    ctx["present"] = [p for p in ctx["files"] if p != "docs/09c-readiness-old.md"]
    assert "phase-09c" in checks(run(ctx, opts), sc.VIOLATION)


def test_gather_marks_removed_files_absent():
    routes = pr_routes(n_files=2)
    routes["/repos/NapticStacks/example/pulls/42/files"] = paged(
        [{"filename": "terraform/main.tf", "status": "modified"},
         {"filename": "docs/09c-readiness-old.md", "status": "removed"}])
    ctx = sc.gather(sc.Api("t0ken", FakeTransport(routes)), REPO, 42)
    assert ctx["present"] == ["terraform/main.tf"]


def test_headings_hidden_in_comments_do_not_count():
    body = "## Issue\nCloses #7\n" + "".join(
        f"<!--\n## {name}\n-->\n" for name in sc.SECTIONS[1:])
    assert "template" in checks(run(make_ctx(body=body)), sc.VIOLATION)


# --- readiness v2: the sdlc/ready commit status ----------------------------------------

MARKER_BODY = "## Three-lens review — COMMENT\n\n**ROI:** n/a\n"


def marker(login, sha, state="COMMENTED", body=MARKER_BODY, at="2026-10-13T12:00:00Z"):
    return {"user": {"login": login}, "state": state, "commit_id": sha, "submitted_at": at, "body": body}


def ready_ctx(reviews=None, **pr_overrides):
    ctx = make_ctx(**pr_overrides)
    ctx["reviews"] = [marker("example-dev", HEAD)] if reviews is None else reviews
    return ctx


def test_ready_when_template_complete_and_marker_on_head():
    state, desc = sc.readiness(ready_ctx(), sc.Options())
    assert state == sc.READY_SUCCESS
    assert HEAD[:7] in desc


def test_ready_marker_by_trusted_reviewer_counts():
    state, _ = sc.readiness(ready_ctx([marker("maydaycyber", HEAD, state="APPROVED")]), sc.Options())
    assert state == sc.READY_SUCCESS


def test_ready_no_marker_is_pending_and_names_the_fix_command():
    state, desc = sc.readiness(ready_ctx([]), sc.Options())
    assert state == sc.READY_PENDING
    assert "/naptic-review 42" in desc and HEAD[:7] in desc


def test_ready_stale_marker_on_an_older_commit_is_pending():
    state, desc = sc.readiness(ready_ctx([marker("example-dev", "c" * 40)]), sc.Options())
    assert state == sc.READY_PENDING and "/naptic-review" in desc


@pytest.mark.parametrize("review", [
    marker("drive-by-user", HEAD),                                   # not the author, not trusted
    marker("example-dev", HEAD, body="LGTM\n## Three-lens review"),  # heading not first
    marker("example-dev", HEAD, body="## Two-lens review — COMMENT"),
    marker("example-dev", HEAD, state="DISMISSED"),
])
def test_ready_forged_or_malformed_marker_does_not_count(review):
    state, _ = sc.readiness(ready_ctx([review]), sc.Options())
    assert state == sc.READY_PENDING


def test_ready_latest_marker_requesting_changes_is_pending():
    reviews = [marker("maydaycyber", HEAD, at="2026-10-13T10:00:00Z"),
               marker("maydaycyber", HEAD, state="CHANGES_REQUESTED", at="2026-10-13T11:00:00Z")]
    state, desc = sc.readiness(ready_ctx(reviews), sc.Options())
    assert state == sc.READY_PENDING and "changes" in desc.lower()


def test_ready_missing_section_is_pending_and_names_it():
    body = make_ctx()["pr"]["body"].split("## /review findings")[0]
    state, desc = sc.readiness(ready_ctx(body=body), sc.Options())
    assert state == sc.READY_PENDING
    assert "/review findings" in desc and "gh pr edit 42" in desc


def test_ready_review_findings_na_because_is_ready():
    body = make_ctx()["pr"]["body"].replace("No findings.", "N/A because nothing was found")
    assert sc.readiness(ready_ctx(body=body), sc.Options())[0] == sc.READY_SUCCESS


def test_ready_edited_body_flips_pending_to_success():
    broken = make_ctx()["pr"]["body"].replace("No findings.", "")
    assert sc.readiness(ready_ctx(body=broken), sc.Options())[0] == sc.READY_PENDING
    fixed = broken + "One nit, fixed in the last commit.\n"
    assert sc.readiness(ready_ctx(body=fixed), sc.Options())[0] == sc.READY_SUCCESS


def test_ready_draft_is_pending():
    state, desc = sc.readiness(ready_ctx(draft=True), sc.Options())
    assert state == sc.READY_PENDING and "draft" in desc.lower()


def test_ready_description_fits_the_status_limit():
    body = "## Issue\nCloses #7\n"  # three sections missing
    _, desc = sc.readiness(ready_ctx([], body=body), sc.Options())
    assert len(desc) <= sc.STATUS_DESC_MAX


class PostTransport(FakeTransport):
    """FakeTransport plus POSTs: records (url, payload); `post_status` int raises."""
    def __init__(self, routes, post_status=None):
        super().__init__(routes)
        self.posts, self.post_status = [], post_status

    def __call__(self, url, headers, data=None):
        if data is None:
            return super().__call__(url, headers)
        if self.post_status:
            raise sc.ApiError(self.post_status, url)
        self.posts.append((url, json.loads(data)))
        return {}


def ready_routes(pr_extra=None, reviews=None):
    routes = pr_routes(pr_extra=pr_extra)
    routes["/repos/NapticStacks/example/pulls/42/files"] = paged(
        [{"filename": "src/widget.py"}, {"filename": "tests/test_widget.py"}])
    routes["/repos/NapticStacks/example/pulls/42/reviews"] = paged(
        [marker("example-dev", HEAD)] if reviews is None else reviews)
    return routes


def main_ready(monkeypatch, transport, extra=()):
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    monkeypatch.setattr(sc, "_urllib_fetch", transport)
    return sc.main(["--pr", "NapticStacks/example#42", "--post-ready", *extra])


def test_post_ready_posts_success_to_the_head_sha(monkeypatch):
    transport = PostTransport(ready_routes())
    assert main_ready(monkeypatch, transport) == sc.EXIT_OK
    [(url, payload)] = transport.posts
    assert url == f"{sc.API}/repos/NapticStacks/example/statuses/{HEAD}"
    assert payload["context"] == "sdlc/ready" and payload["state"] == "success"


def test_post_ready_without_statuses_permission_warns_and_exits_0(monkeypatch, capsys, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    transport = PostTransport(ready_routes(), post_status=403)
    assert main_ready(monkeypatch, transport) == sc.EXIT_OK
    out = capsys.readouterr().out
    assert "::warning" in out and "statuses" in out
    assert "not posted" in summary.read_text()


def test_post_ready_on_a_fork_pr_is_not_posted(monkeypatch, capsys):
    fork = {"head": {"sha": HEAD, "repo": {"full_name": "someone/example"}}}
    transport = PostTransport(ready_routes(pr_extra=fork))
    assert main_ready(monkeypatch, transport) == sc.EXIT_OK
    assert transport.posts == []
    assert "not posted" in capsys.readouterr().out.lower()


def test_post_ready_other_api_error_is_a_checker_error(monkeypatch):
    transport = PostTransport(ready_routes(), post_status=500)
    assert main_ready(monkeypatch, transport) == sc.EXIT_CHECKER_ERROR


def test_ready_without_post_ready_nothing_is_posted(monkeypatch):
    """The v1 path: the conformance run never writes."""
    transport = PostTransport(ready_routes())
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    monkeypatch.setattr(sc, "_urllib_fetch", transport)
    sc.main(["--pr", "NapticStacks/example#42"])
    assert transport.posts == []


def test_post_ready_ignores_conformance_violations(monkeypatch):
    """Readiness is advisory: a huge PR still exits 0 from the ready job, even in block mode."""
    transport = PostTransport(ready_routes(pr_extra={"additions": 5000}))
    assert main_ready(monkeypatch, transport, ["--mode", "block"]) == sc.EXIT_OK
    assert transport.posts[0][1]["state"] == "success"


def test_ready_workflow_v2_has_a_ready_job_that_inherits_caller_permissions():
    text = WORKFLOW.read_text()
    assert "name: sdlc-ready" in text and "--post-ready" in text
    top = text.split("\njobs:", 1)[0]
    assert "\npermissions:" not in top  # a top-level block would cap the ready job at read-only


def test_ready_workflow_v1_inputs_still_accepted():
    text = WORKFLOW.read_text()
    for name in ("mode", "require_approval", "trusted_reviewers", "bot_authors", "unf_phase",
                 "changelog", "version_bump", "phase_09c_on_infra", "gate_ref"):
        assert f"\n      {name}:" in text, name


README = Path(__file__).parent.parent / ".github" / "workflows" / "README.md"


def readme_v2_caller() -> str:
    """The v2 caller YAML block in the README, or a clear failure naming what moved."""
    text = README.read_text()
    assert "(v3, current):" in text, "README lost its '(v3, current):' caller heading"
    after = text.split("(v3, current):", 1)[1]
    assert "```yaml" in after, "no ```yaml block after the v2 caller heading"
    return after.split("```yaml", 1)[1].split("```", 1)[0]


def test_ready_v2_caller_reruns_on_every_event_that_can_flip_readiness():
    """codex 3a: back-to-draft and an edited review must re-post, or a stale success lingers."""
    caller = readme_v2_caller()
    pr_types = caller.split("pull_request:", 1)[1].split("\n", 2)[1]
    review_types = caller.split("pull_request_review:", 1)[1].split("\n", 2)[1]
    for t in ("opened", "edited", "synchronize", "reopened", "ready_for_review", "converted_to_draft"):
        assert t in pr_types, t
    for t in ("submitted", "edited", "dismissed"):
        assert t in review_types, t
    assert "statuses: write" in caller and "@sdlc-conformance-v3" in caller
    assert "review_requested" in pr_types  # /closer requests the reviewer right after the self-review marker
    # ${{ }} inside a {...} flow mapping is invalid YAML: concurrency must be block style.
    assert "{group:" not in caller


# --- /naptic-review round (.github#9, napticstack-skills#384) ---------------------------

def test_ready_author_cannot_clear_a_trusted_reviewers_change_request():
    reviews = [marker("maydaycyber", HEAD, state="CHANGES_REQUESTED", at="2026-10-13T10:00:00Z"),
               marker("example-dev", HEAD, at="2026-10-13T11:00:00Z")]
    state, desc = sc.readiness(ready_ctx(reviews), sc.Options())
    assert state == sc.READY_PENDING and "changes" in desc.lower()


def test_ready_a_later_trusted_marker_clears_its_own_change_request():
    reviews = [marker("maydaycyber", HEAD, state="CHANGES_REQUESTED", at="2026-10-13T10:00:00Z"),
               marker("maydaycyber", HEAD, state="APPROVED", at="2026-10-13T11:00:00Z")]
    assert sc.readiness(ready_ctx(reviews), sc.Options())[0] == sc.READY_SUCCESS


def test_ready_self_review_posted_as_comment_with_request_changes_verdict_is_pending():
    body = "## Three-lens review — REQUEST CHANGES (posted as a comment: own PR)\n\n- [MAJOR] ..."
    state, _ = sc.readiness(ready_ctx([marker("example-dev", HEAD, body=body)]), sc.Options())
    assert state == sc.READY_PENDING


def test_ready_a_dismissed_change_request_falls_back_to_the_earlier_marker():
    reviews = [marker("maydaycyber", HEAD, at="2026-10-13T10:00:00Z"),
               marker("maydaycyber", HEAD, state="DISMISSED", at="2026-10-13T11:00:00Z")]
    assert sc.readiness(ready_ctx(reviews), sc.Options())[0] == sc.READY_SUCCESS


def test_ready_bot_pr_skips_the_template_but_still_needs_the_marker():
    ctx = ready_ctx(body="Bumps foo.", user={"login": "naptic-hp-agent[bot]"})
    opts = sc.Options(bot_authors=("naptic-hp-agent[bot]",), trusted_reviewers=("maydaycyber",))
    assert sc.readiness(ctx, opts)[0] == sc.READY_PENDING  # no marker yet: the template skip doesn't make it ready
    ctx = ready_ctx([marker("maydaycyber", HEAD)], body="Bumps foo.", user={"login": "naptic-hp-agent[bot]"})
    assert sc.readiness(ctx, opts)[0] == sc.READY_SUCCESS


def test_ready_deleted_fork_counts_as_a_fork():
    assert sc.is_fork("NapticStacks/example", {"head": {"sha": HEAD, "repo": None}}) is True
    assert sc.is_fork("NapticStacks/example", {"head": {"sha": HEAD}}) is False


def test_ready_gap_text_comes_from_the_finding_not_its_wording():
    findings = sc.check_template(REPO, 42, "## Issue\nCloses #7\n", [])
    assert [f.gap for f in findings] == ["missing Tests named in the issue", "missing Verification evidence",
                                         "missing /review findings"]


def test_ready_job_guards_gate_ref_like_the_conformance_job():
    """The ready job copies three setup steps; the gate_ref guard and the pinned checkout must stay identical."""
    jobs = WORKFLOW.read_text().split("\n  sdlc-ready:", 1)
    conformance, ready = jobs[0].split("\n  sdlc-conformance:", 1)[1], jobs[1]
    pick = lambda block: [l.strip() for l in block.splitlines() if "GATE_REF\" =~" in l or "actions/checkout@" in l or "actions/setup-python@" in l]
    assert pick(conformance) and pick(conformance) == pick(ready)


# --- v3: V1 checks (tier, builder-of-record, self-review) --------------------------------
# All logins below are made up. The real roster lives in each private caller's `builders:`.

V1_CREATED = "2026-10-20T09:00:00Z"
BUILDER = "example-dev"
SIGNER, BACKUP_SIGNER = "example-secops", "example-lead"
V1_SECTIONS = ("\n## Builder\nBuilder: @example-dev\n\n## Workstation\nexample-box\n\n"
               "## Session\nhttps://claude.ai/code/session_example\n\n## Tier\ntier:low\n")


def self_review(login=BUILDER, sha=HEAD, body=None, at="2026-10-20T10:00:00Z", edited_by_other=False):
    text = body if body is not None else f"{sc.SELF_REVIEW_MARKER} {sha}"  # what /closer posts
    return {"user": {"login": login}, "body": text, "created_at": at, "node_id": f"IC_{login}_{at}",
            "edited_by_other": edited_by_other}


def signoff(login=SIGNER, at="2026-10-19T10:00:00Z", sha=None, edited_by_other=False):
    text = f"{sc.SECURITY_SIGNOFF_MARKER}" + (f" {sha}" if sha else "") + "\nSigned off after reading the plan."
    return {"user": {"login": login}, "body": text, "created_at": at, "node_id": f"IC_so_{login}_{at}",
            "edited_by_other": edited_by_other}


HIGH_SIGNED = {7: {"labels": ["tier:high"], "comments": []}}


def v1_ctx(labels=("tier:low",), comments=None, issues=None, builder_account="User", closing=(7,), **pr_overrides):
    """A post-cutover PR that passes every v1 check."""
    ctx = make_ctx(**pr_overrides)
    pr = ctx["pr"]
    if "created_at" not in pr_overrides:
        pr["created_at"] = V1_CREATED
    if "body" not in pr_overrides:
        pr["body"] = pr["body"].replace("Closes #7\n", "Closes #7\n" + V1_SECTIONS)
    if "labels" not in pr_overrides:
        pr["labels"] = [{"name": l} for l in labels]
    ctx["comments"] = [self_review()] if comments is None else comments
    ctx["linked_issues"] = {7: {"labels": [], "comments": []}} if issues is None else issues
    ctx["closing_issues"] = list(closing)  # GitHub's closingIssuesReferences
    m = sc.BUILDER_RE.search(sc.COMMENT_RE.sub("", pr["body"]))
    if m and builder_account is not None:
        ctx["builder_account"] = {"login": m.group(1), "type": builder_account}
    elif m:
        ctx["builder_account"] = None
    return ctx


V1_OPTS = sc.Options(builders=(BUILDER, "example-other"), security_signers=(SIGNER, BACKUP_SIGNER))


def v1run(ctx, opts=V1_OPTS, **kw):
    return run(ctx, opts, **kw)


def test_v1_good_pr_has_no_findings():
    assert v1run(v1_ctx()) == []


def test_v1_checks_are_new_check_ids():
    for cid in ("tier", "builder-of-record", "self-review"):
        assert cid in sc.CHECK_IDS


def test_v1_cutover_default_is_2026_10_16_utc():
    assert sc.CUTOVER == "2026-10-16T00:00:00Z"
    assert sc.Options().cutover == sc.CUTOVER


@pytest.mark.parametrize("created", ["2026-10-15T23:59:59Z", "2026-10-01T00:00:00Z"])
def test_v1_checks_skip_prs_created_before_cutover(created):
    ctx = v1_ctx(created_at=created, labels=(), comments=[],
                 body=make_ctx()["pr"]["body"])  # no Builder line, no tier, no marker, no new sections
    assert v1run(ctx) == []


def test_v1_checks_run_on_a_pr_created_exactly_at_cutover():
    ctx = v1_ctx(created_at="2026-10-16T00:00:00Z", comments=[])
    assert "self-review" in checks(v1run(ctx), sc.VIOLATION)


def test_v1_missing_created_at_counts_as_post_cutover():
    ctx = v1_ctx(comments=[])
    del ctx["pr"]["created_at"]
    assert "self-review" in checks(v1run(ctx), sc.VIOLATION)


def test_v1_cutover_is_configurable():
    ctx = v1_ctx(comments=[])
    late = sc.Options(builders=V1_OPTS.builders, security_signers=V1_OPTS.security_signers,
                      cutover="2026-11-01T00:00:00Z")
    assert v1run(ctx, late) == []


def test_v1_bad_cutover_is_a_checker_error(monkeypatch, capsys):
    monkeypatch.setattr(sc, "gather", lambda *a, **k: make_ctx())
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    assert sc.main(["--pr", "NapticStacks/example#42", "--cutover", "next tuesday"]) == sc.EXIT_CHECKER_ERROR


# tier

def test_v1_no_tier_label_is_a_medium_note():
    findings = v1run(v1_ctx(labels=()))
    assert [(f.check, f.level) for f in findings] == [("tier", sc.NOTE)]
    assert "medium" in findings[0].problem


def test_v1_tier_from_the_linked_issue_counts():
    ctx = v1_ctx(labels=(), issues={7: {"labels": ["tier:low"], "comments": []}})
    assert v1run(ctx) == []


def test_v1_highest_tier_wins_across_pr_and_issue():
    ctx = v1_ctx(labels=("tier:low",), issues={7: {"labels": ["tier:high"], "comments": []}})
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)


def test_v1_effective_tier():
    assert sc.effective_tier(["tier:low", "tier:medium"]) == "medium"
    assert sc.effective_tier(["bug", "TIER:HIGH"]) == "high"
    assert sc.effective_tier([]) is None


def test_v1_high_without_signoff_fails():
    findings = v1run(v1_ctx(labels=("tier:high",)))
    [f] = [f for f in findings if f.check == "tier"]
    assert f.level == sc.VIOLATION and "sign-off" in f.problem


def test_v1_high_with_signoff_on_the_issue_passes():
    ctx = v1_ctx(labels=("tier:high",), issues={7: {"labels": ["tier:high"], "comments": [signoff()]}})
    assert v1run(ctx) == []


def test_v1_high_with_signoff_on_the_pr_passes():
    ctx = v1_ctx(labels=("tier:high",), comments=[self_review(), signoff(sha=HEAD)])
    assert v1run(ctx) == []


def test_v1_pr_level_signoff_must_name_the_head_sha():
    ctx = v1_ctx(labels=("tier:high",), comments=[self_review(), signoff()])
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)
    ctx = v1_ctx(labels=("tier:high",), comments=[self_review(), signoff(sha="c" * 40)])
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)


def test_v1_borrowed_signoff_from_another_linked_issue_does_not_count():
    """F1: #7 is tier:high and unsigned; #5 was signed for other work. Linking both must not pass."""
    body = v1_ctx()["pr"]["body"].replace("Closes #7\n", "Closes #7\nPart of #5\n")
    ctx = v1_ctx(labels=(), body=body, issues={7: {"labels": ["tier:high"], "comments": []},
                                               5: {"labels": ["tier:low"], "comments": [signoff()]}})
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)


def test_v1_signoff_on_an_issue_without_tier_high_does_not_count_for_a_high_pr_label():
    ctx = v1_ctx(labels=("tier:high",), issues={7: {"labels": [], "comments": [signoff()]}})
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)


@pytest.mark.parametrize("where", ["issue", "pr"])
def test_v1_signoff_edited_by_someone_else_does_not_count(where):
    """F2: an engineer appends the hidden marker to an old Security Ops comment."""
    forged = signoff(sha=HEAD, edited_by_other=True)
    if where == "issue":
        ctx = v1_ctx(labels=("tier:high",), issues={7: {"labels": ["tier:high"], "comments": [forged]}})
    else:
        ctx = v1_ctx(labels=("tier:high",), comments=[self_review(), forged])
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)


def test_v1_marker_comment_without_edit_info_does_not_count():
    """Fail closed: a marker comment the gate couldn't check for edits is ignored."""
    c = signoff(); del c["edited_by_other"]
    ctx = v1_ctx(labels=("tier:high",), issues={7: {"labels": ["tier:high"], "comments": [c]}})
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)
    c = self_review(); del c["edited_by_other"]
    assert "self-review" in checks(v1run(v1_ctx(comments=[c])), sc.VIOLATION)


def test_v1_self_review_edited_by_someone_else_does_not_count():
    assert "self-review" in checks(v1run(v1_ctx(comments=[self_review(edited_by_other=True)])), sc.VIOLATION)


def test_v1_pr_author_cannot_sign_off_even_when_builder_is_someone_else():
    """F3: a signer authors the PR and names a teammate as Builder."""
    ctx = v1_ctx(labels=("tier:high",), user={"login": SIGNER},
                 issues={7: {"labels": ["tier:high"], "comments": [signoff()]}})
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)


def test_v1_missing_linked_issue_means_tier_unknown():
    """F5: a 404 on a linked issue is never a silent medium."""
    ctx = v1_ctx(labels=(), issues={7: None})
    [f] = [f for f in v1run(ctx) if f.check == "tier"]
    assert f.level == sc.VIOLATION and "#7" in f.problem


def test_v1_only_cross_repo_issues_and_no_pr_tier_label_is_a_violation():
    """F6: the tier may live on an issue the token can't read."""
    body = v1_ctx()["pr"]["body"].replace("Closes #7\n", "Part of NapticStacks/other#3\n")
    [f] = [f for f in v1run(v1_ctx(labels=(), body=body, issues={})) if f.check == "tier"]
    assert f.level == sc.VIOLATION and "tier label to the PR" in f.fix


def test_v1_cross_repo_issue_with_a_pr_tier_label_passes():
    body = v1_ctx()["pr"]["body"].replace("Closes #7\n", "Part of NapticStacks/other#3\n")
    assert [(f.check, f.level) for f in v1run(v1_ctx(labels=("tier:low",), body=body, issues={}))] == [
        ("issue-link", sc.NOTE)]


@pytest.mark.parametrize("comment", [
    signoff(login="drive-by-user"),                                       # not a signer
    {"user": {"login": SIGNER}, "body": "LGTM, signed off", "created_at": "2026-10-19T10:00:00Z",
     "edited_by_other": False},                                           # no marker
    signoff(login=BACKUP_SIGNER),                                         # backup only when the primary built it
])
def test_v1_high_signoff_that_does_not_count(comment):
    ctx = v1_ctx(labels=("tier:high",), issues={7: {"labels": ["tier:high"], "comments": [comment]}})
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)


def test_v1_backup_signer_counts_when_the_primary_built_it():
    body = make_ctx()["pr"]["body"].replace("Closes #7\n", "Closes #7\n" + V1_SECTIONS.replace(BUILDER, SIGNER))
    ctx = v1_ctx(labels=("tier:high",), body=body, comments=[self_review(login=SIGNER)],
                 issues={7: {"labels": ["tier:high"], "comments": [signoff(login=BACKUP_SIGNER)]}})
    opts = sc.Options(builders=(SIGNER,), security_signers=(SIGNER, BACKUP_SIGNER))
    assert v1run(ctx, opts) == []


def test_v1_builder_cannot_sign_off_their_own_high_tier_work():
    body = make_ctx()["pr"]["body"].replace("Closes #7\n", "Closes #7\n" + V1_SECTIONS.replace(BUILDER, SIGNER))
    ctx = v1_ctx(labels=("tier:high",), body=body, comments=[self_review(login=SIGNER)],
                 issues={7: {"labels": ["tier:high"], "comments": [signoff(login=SIGNER)]}})
    opts = sc.Options(builders=(SIGNER,), security_signers=(SIGNER, BACKUP_SIGNER))
    assert "tier" in checks(v1run(ctx, opts), sc.VIOLATION)


# NapticStacks/.github#15: the first signer may sign their own work only with a /cso evidence marker
# (`<!-- sdlc:cso v1 --> <sha>` plus a link to the /cso run or report comment) in the same place the sign-off counts.

CSO_LINK = "https://github.com/NapticStacks/example/pull/42#issuecomment-1"
REVIEWED = "3f9c2a7e1b4d6058a9c7e2f13b5d8a6c4e0f9b21"  # a plan or trunk SHA that exists in the repo


def cso(login=SIGNER, sha=HEAD, link=CSO_LINK, at="2026-10-19T09:00:00Z", edited_by_other=False, body=None,
        sha_exists=None):
    if body is None:
        body = f"{sc.CSO_MARKER} {sha}"
        if link:
            body += f"\n/cso report: {link}"
    c = {"user": {"login": login}, "body": body, "created_at": at, "node_id": f"IC_cso_{login}_{at}",
         "edited_by_other": edited_by_other}
    if sha_exists is not None:
        c["sha_exists"] = sha_exists
    return c


def issue_cso(**kw):
    """An issue-level marker: the reviewed SHA, which gather proved exists in the repo."""
    return cso(**{"sha": REVIEWED, "sha_exists": True, **kw})


def own_work(pr_comments=(), issue_comments=(), builder=SIGNER, author=None, issues=None, closing=(7,)):
    """A tier:high PR whose builder of record (and, if `author`, PR author) is a security signer."""
    body = make_ctx()["pr"]["body"].replace("Closes #7\n", "Closes #7\n" + V1_SECTIONS.replace(BUILDER, builder))
    extra = {"user": {"login": author}} if author else {}
    if issues is None:
        issues = {7: {"labels": ["tier:high"], "comments": list(issue_comments)}}
    ctx = v1_ctx(labels=("tier:high",), body=body, comments=[self_review(login=builder), *pr_comments],
                 issues=issues, closing=closing, **extra)
    opts = sc.Options(builders=(builder,), security_signers=(SIGNER, BACKUP_SIGNER))
    return ctx, opts


def tier_levels(ctx, opts):
    return [f.level for f in v1run(ctx, opts) if f.check == "tier"]


def test_cso_first_signer_own_work_with_pr_marker_passes():
    ctx, opts = own_work(pr_comments=[cso(), signoff(sha=HEAD)])
    assert tier_levels(ctx, opts) == []


def test_cso_first_signer_own_work_with_issue_marker_passes():
    ctx, opts = own_work(issue_comments=[issue_cso(), signoff()])
    assert tier_levels(ctx, opts) == []


def test_cso_first_signer_as_pr_author_with_marker_passes():
    ctx, opts = own_work(pr_comments=[cso(), signoff(sha=HEAD)], builder=BUILDER, author=SIGNER)
    assert tier_levels(ctx, opts) == []


def test_cso_marker_posted_by_a_backup_signer_counts():
    # Pinned: any security signer may post the /cso evidence (a backup running /cso is more independent, not less).
    ctx, opts = own_work(pr_comments=[cso(login=BACKUP_SIGNER), signoff(sha=HEAD)])
    assert tier_levels(ctx, opts) == []


def test_cso_own_work_without_marker_fails_with_a_hint_naming_the_marker():
    ctx, opts = own_work(pr_comments=[signoff(sha=HEAD)], issue_comments=[signoff()])
    [f] = [f for f in v1run(ctx, opts) if f.check == "tier"]
    assert f.level == sc.VIOLATION
    assert SIGNER in f.cause and sc.CSO_MARKER in f.cause
    assert f"{sc.CSO_MARKER} {HEAD}" in f.fix  # the line to paste, with the full head so a copy works
    assert sc.CSO_SPEC in f.fix  # the README holds the full rule; the finding doesn't restate it
    assert sc.CSO_SPEC.endswith("README.md#what-v3-adds-sdlc-v1")
    assert len(f.cause) < 300


def test_cso_stale_sha_marker_on_the_pr_fails():
    ctx, opts = own_work(pr_comments=[cso(sha="c" * 40), signoff(sha=HEAD)])
    assert tier_levels(ctx, opts) == [sc.VIOLATION]


def test_cso_issue_marker_on_the_pr_with_issue_signoff_fails():
    ctx, opts = own_work(pr_comments=[cso()], issue_comments=[signoff()])
    assert tier_levels(ctx, opts) == [sc.VIOLATION]


def test_cso_pr_signoff_with_only_an_issue_marker_fails():
    ctx, opts = own_work(pr_comments=[signoff(sha=HEAD)], issue_comments=[issue_cso()])
    assert tier_levels(ctx, opts) == [sc.VIOLATION]


def test_cso_one_marker_never_covers_two_closed_high_issues():
    issues = {7: {"labels": ["tier:high"], "comments": [issue_cso(), signoff()]},
              5: {"labels": ["tier:high"], "comments": [signoff()]}}
    ctx, opts = own_work(issues=issues, closing=(7, 5))
    ctx["pr"]["body"] = ctx["pr"]["body"].replace("Closes #7\n", "Closes #7\nCloses #5\n")
    assert tier_levels(ctx, opts) == [sc.VIOLATION]
    issues[5]["comments"].insert(0, issue_cso(at="2026-10-19T09:30:00Z"))
    assert tier_levels(ctx, opts) == []


@pytest.mark.parametrize("comments", [
    [signoff(login=BACKUP_SIGNER, sha=HEAD)],
    [cso(login=BACKUP_SIGNER), signoff(login=BACKUP_SIGNER, sha=HEAD)],
    [cso(), signoff(login=BACKUP_SIGNER, sha=HEAD)],
])
def test_cso_non_first_signer_own_work_fails_marker_or_not(comments):
    ctx, opts = own_work(pr_comments=comments, builder=BACKUP_SIGNER)
    assert tier_levels(ctx, opts) == [sc.VIOLATION]


@pytest.mark.parametrize("link", [
    "https://github.com/NapticStacks/example/pull/42#issuecomment-123",
    "https://github.com/NapticStacks/example/issues/7#issuecomment-123",
    "https://github.com/NapticStacks/example/pull/42#pullrequestreview-123",
    "https://github.com/NapticStacks/example/actions/runs/123",
    "https://github.com/NapticStacks/example/actions/runs/123/job/456",
    "https://github.com/napticstacks/other-repo/pull/9#issuecomment-1",
])
def test_cso_report_link_forms_that_count(link):
    ctx, opts = own_work(pr_comments=[cso(link=f"[report]({link})"), signoff(sha=HEAD)])
    assert tier_levels(ctx, opts) == []


@pytest.mark.parametrize("link", [
    "https://example.com/cso-report",                                   # not GitHub
    "https://github.com/someone-else/example/pull/42#issuecomment-1",   # outside the org
    "https://github.com/NapticStacks/example/pull/42",                  # not a comment, review or run
    "http://github.com/NapticStacks/example/actions/runs/123",          # not https
    "https://github.com.evil.io/NapticStacks/example/actions/runs/1",   # look-alike host
    "https://github.com/NapticStacks/example/actions/runs/",            # no run id
])
def test_cso_report_link_forms_that_do_not_count(link):
    ctx, opts = own_work(pr_comments=[cso(link=link), signoff(sha=HEAD)])
    assert tier_levels(ctx, opts) == [sc.VIOLATION]


@pytest.mark.parametrize("marker", [
    issue_cso(sha="0" * 40),               # placeholder: all one character
    issue_cso(sha="f" * 40),
    issue_cso(sha_exists=False),           # gather found no such commit in the repo
    cso(sha=REVIEWED),                     # existence never checked: fails closed
])
def test_cso_issue_marker_needs_a_real_reviewed_sha(marker):
    ctx, opts = own_work(issue_comments=[marker, signoff()])
    assert tier_levels(ctx, opts) == [sc.VIOLATION]


@pytest.mark.parametrize("marker", [
    cso(link=None),                                     # no link to the /cso run or report
    cso(login="drive-by-user"),                         # not a security signer
    cso(edited_by_other=True),                          # someone else edited it
    cso(body=f"see report\n{sc.CSO_MARKER} {HEAD}\n{CSO_LINK}"),  # marker not on the first line
    cso(body=f"{sc.CSO_MARKER}\n{CSO_LINK}"),           # no SHA
    cso(body=f"{sc.CSO_MARKER} {HEAD[:7]}\n{CSO_LINK}"),  # short SHA
    cso(body=f"{sc.CSO_MARKER} {HEAD.upper()}\n{CSO_LINK}"),  # not lowercase
    cso(body=f"`{sc.CSO_MARKER} {HEAD}`\n{CSO_LINK}"),  # quoted, not the exact line
    cso(body=f"{sc.CSO_MARKER} {HEAD} {CSO_LINK}"),     # link on the marker line, not below it
])
def test_cso_marker_that_does_not_count(marker):
    ctx, opts = own_work(pr_comments=[marker, signoff(sha=HEAD)])
    assert tier_levels(ctx, opts) == [sc.VIOLATION]


def test_cso_marker_with_crlf_and_leading_blank_lines_counts():
    body = f"\r\n\r\n{sc.CSO_MARKER} {HEAD}\r\n/cso report: {CSO_LINK}\r\n"
    ctx, opts = own_work(pr_comments=[cso(body=body), signoff(sha=HEAD)])
    assert tier_levels(ctx, opts) == []


def test_marker_parts_returns_the_rest_of_the_body():
    assert sc.marker_parts(f"\n{sc.CSO_MARKER} {HEAD}\nline two\nline three", sc.CSO_LINE_RE) == (
        True, HEAD, "line two\nline three")
    assert sc.marker_parts("no marker", sc.CSO_LINE_RE) == (False, None, "")


def test_cso_marker_alone_is_not_a_signoff():
    ctx, opts = own_work(pr_comments=[cso()])
    assert tier_levels(ctx, opts) == [sc.VIOLATION]


def test_cso_is_not_needed_when_the_first_signer_did_not_build_it():
    ctx = v1_ctx(labels=("tier:high",), comments=[self_review(), signoff(sha=HEAD)])
    assert v1run(ctx) == []


@pytest.mark.parametrize("tier", ["tier:low", "tier:medium"])
def test_cso_marker_does_not_change_lower_tiers(tier):
    ctx = v1_ctx(labels=(tier,), comments=[self_review(), cso(sha="c" * 40)])
    assert v1run(ctx) == []


def test_cso_warn_mode_still_exits_0():
    ctx, opts = own_work(pr_comments=[signoff(sha=HEAD)])
    assert sc.exit_code(v1run(ctx, opts), opts) == 0


def test_cso_marker_edit_history_is_read():
    c = cso()
    del c["edited_by_other"]
    plain = {"user": {"login": SIGNER}, "body": "thanks", "node_id": "IC_plain"}

    class Api:
        def post(self, path, payload):
            assert payload["variables"]["ids"] == [c["node_id"]]
            return {"data": {"nodes": [gnode(SIGNER)]}}
    sc.mark_edits(Api(), [c, plain])
    assert c["edited_by_other"] is False
    assert "edited_by_other" not in plain


@pytest.mark.parametrize("route, expected", [({"sha": REVIEWED}, True), (404, False), (422, False)])
def test_cso_gather_checks_the_issue_marker_sha_exists(route, expected):
    c = issue_cso()
    del c["edited_by_other"], c["sha_exists"]
    routes = v1_routes({"body": "Closes #7\nBuilder: @example-dev"}, issue_labels=["tier:high"], issue_comments=[c])
    routes[f"/repos/NapticStacks/example/git/commits/{REVIEWED}"] = route
    transport = GraphTransport(routes, {c["node_id"]: gnode(SIGNER)})
    ctx = sc.gather(sc.Api("t0ken", transport), REPO, 42)
    assert ctx["linked_issues"][7]["comments"][0]["sha_exists"] is expected


def test_cso_gather_commit_lookup_error_is_a_checker_error():
    c = issue_cso()
    del c["edited_by_other"], c["sha_exists"]
    routes = v1_routes({"body": "Closes #7\nBuilder: @example-dev"}, issue_labels=["tier:high"], issue_comments=[c])
    routes[f"/repos/NapticStacks/example/git/commits/{REVIEWED}"] = 500
    transport = GraphTransport(routes, {c["node_id"]: gnode(SIGNER)})
    with pytest.raises(sc.CheckerError):
        sc.gather(sc.Api("t0ken", transport), REPO, 42)


def test_v1_high_with_no_signers_configured_fails_closed():
    ctx = v1_ctx(labels=("tier:high",), issues={7: {"labels": ["tier:high"], "comments": [signoff()]}})
    opts = sc.Options(builders=V1_OPTS.builders)
    [f] = [f for f in v1run(ctx, opts) if f.check == "tier"]
    assert f.level == sc.VIOLATION and "security_signers" in f.cause


# builder-of-record

def _body_with_builder(line):
    return make_ctx()["pr"]["body"].replace("Closes #7\n", "Closes #7\n" + V1_SECTIONS.replace(
        "Builder: @example-dev", line))


@pytest.mark.parametrize("line", ["Builder: @example-dev", "- Builder: @example-dev",
                                  "**Builder:** @example-dev", "builder: example-dev", "Builder: @Example-Dev"])
def test_v1_builder_line_forms(line):
    assert sc.builder_of_record(_body_with_builder(line)).lower() == BUILDER


@pytest.mark.parametrize("line", ["Builder: N/A because the swarm built it", "Builder: TBD", "Builder: none",
                                  "Builder: n/a", "Builder: @x,y", "Builder: @me/you", "Builder: unknown"])
def test_v1_placeholder_or_malformed_builder_is_no_builder(line):
    """F8: the login must end at whitespace or end of line, and placeholders aren't logins."""
    assert sc.builder_of_record(line) is None


def test_v1_builder_followed_by_a_note_still_parses():
    assert sc.builder_of_record("Builder: @example-dev (pairing with Ben)") == "example-dev"


def test_v1_builder_line_inside_a_comment_or_fence_does_not_count():
    assert sc.builder_of_record("## Issue\nCloses #7\n<!-- Builder: @example-dev -->") is None
    assert sc.builder_of_record("```\nBuilder: @example-dev\n```") is None


def test_v1_missing_builder_line_fails():
    ctx = v1_ctx(body=_body_with_builder("Builder: @"))
    assert "builder-of-record" in checks(v1run(ctx), sc.VIOLATION)


def test_v1_builder_not_on_the_roster_fails():
    ctx = v1_ctx(body=_body_with_builder("Builder: @stranger"), comments=[self_review(login="stranger")])
    [f] = [f for f in v1run(ctx) if f.check == "builder-of-record"]
    assert f.level == sc.VIOLATION and "roster" in f.problem


@pytest.mark.parametrize("login", ["naptic-hp-agent[bot]", "app/some-app", "dependabot[bot]"])
def test_v1_bot_builder_fails(login):
    ctx = v1_ctx(body=_body_with_builder(f"Builder: @{login}"), comments=[self_review(login=login)])
    opts = sc.Options(builders=(*V1_OPTS.builders, login), security_signers=V1_OPTS.security_signers,
                      bot_authors=("naptic-hp-agent[bot]",))
    assert "builder-of-record" in checks(v1run(ctx, opts), sc.VIOLATION)


def test_v1_builder_account_that_is_not_a_user_fails():
    assert "builder-of-record" in checks(v1run(v1_ctx(builder_account="Bot")), sc.VIOLATION)
    assert "builder-of-record" in checks(v1run(v1_ctx(builder_account="Organization")), sc.VIOLATION)


def test_v1_builder_login_that_does_not_exist_fails():
    [f] = [f for f in v1run(v1_ctx(builder_account=None)) if f.check == "builder-of-record"]
    assert f.level == sc.VIOLATION and "exist" in f.problem


def test_v1_no_roster_configured_is_a_note_for_a_real_user():
    findings = v1run(v1_ctx(), sc.Options(security_signers=V1_OPTS.security_signers))
    assert [(f.check, f.level) for f in findings] == [("builder-of-record", sc.NOTE)]


def test_v1_swarm_pr_needs_the_builder_line_too():
    ctx = v1_ctx(body="Closes #7", user={"login": "naptic-hp-agent[bot]"})
    opts = sc.Options(builders=V1_OPTS.builders, bot_authors=("naptic-hp-agent[bot]",))
    found = checks(v1run(ctx, opts), sc.VIOLATION)
    assert "builder-of-record" in found and "template" not in found


def test_v1_swarm_pr_with_a_human_builder_passes():
    ctx = v1_ctx(body="Closes #7\nBuilder: @example-dev", user={"login": "naptic-hp-agent[bot]"})
    opts = sc.Options(builders=V1_OPTS.builders, bot_authors=("naptic-hp-agent[bot]",))
    assert v1run(ctx, opts) == []


def test_v1_dependabot_builder_and_self_review_are_notes():
    ctx = v1_ctx(body="Bumps foo from 1.0 to 1.1.", user={"login": "dependabot[bot]"}, comments=[])
    findings = v1run(ctx, sc.Options(mode="block", builders=V1_OPTS.builders))
    levels = {f.check: f.level for f in findings}
    assert levels["builder-of-record"] == sc.NOTE and levels["self-review"] == sc.NOTE
    assert sc.exit_code(findings, sc.Options(mode="block")) == sc.EXIT_OK


# self-review

def test_v1_no_marker_fails_and_names_closer():
    [f] = [f for f in v1run(v1_ctx(comments=[])) if f.check == "self-review"]
    assert f.level == sc.VIOLATION and "/closer" in f.fix and "requests a reviewer" in f.fix
    for g in v1run(v1_ctx(comments=[])):
        assert "never merges" not in (g.cause + g.fix)
    assert "napticstack-skills 0.88.0" in f.cause


def test_v1_marker_on_an_older_head_is_stale():
    [f] = [f for f in v1run(v1_ctx(comments=[self_review(sha="c" * 40)])) if f.check == "self-review"]
    assert f.level == sc.VIOLATION and "older" in f.problem


def test_v1_marker_by_someone_else_does_not_count():
    [f] = [f for f in v1run(v1_ctx(comments=[self_review(login="example-other")])) if f.check == "self-review"]
    assert f.level == sc.VIOLATION and "builder" in f.problem


def test_v1_marker_without_the_head_sha_does_not_count():
    comment = self_review(body=f"{sc.SELF_REVIEW_MARKER}\nSelf-review done on {HEAD[:7]}.")
    assert "self-review" in checks(v1run(v1_ctx(comments=[comment])), sc.VIOLATION)


def test_v1_marker_login_match_ignores_case():
    assert v1run(v1_ctx(comments=[self_review(login="Example-Dev")])) == []


def test_v1_newest_marker_on_head_counts_even_after_a_stale_one():
    comments = [self_review(sha="c" * 40, at="2026-10-20T09:00:00Z"), self_review(at="2026-10-20T11:00:00Z")]
    assert v1run(v1_ctx(comments=comments)) == []


def test_v1_marker_in_a_review_on_head_counts():
    ctx = v1_ctx(comments=[])
    ctx["reviews"].append({"user": {"login": BUILDER}, "state": "COMMENTED", "commit_id": HEAD,
                           "submitted_at": "2026-10-20T10:00:00Z", "body": f"{sc.SELF_REVIEW_MARKER}\n## Three-lens review",
                           "edited_by_other": False})
    assert v1run(ctx) == []


def test_v1_marker_in_a_review_on_an_older_commit_is_stale():
    ctx = v1_ctx(comments=[])
    ctx["reviews"].append({"user": {"login": BUILDER}, "state": "COMMENTED", "commit_id": "c" * 40,
                           "submitted_at": "2026-10-20T10:00:00Z", "body": f"{sc.SELF_REVIEW_MARKER} {HEAD}"})
    assert "self-review" in checks(v1run(ctx), sc.VIOLATION)


def test_v1_no_builder_line_means_self_review_cannot_pass():
    [f] = [f for f in v1run(v1_ctx(body=_body_with_builder("Builder: @"))) if f.check == "self-review"]
    assert f.level == sc.VIOLATION and "builder-of-record" in f.cause


# template: the new V1 sections

@pytest.mark.parametrize("name", ["Builder", "Workstation", "Session", "Tier"])
def test_v1_template_needs_the_new_sections(name):
    body = v1_ctx()["pr"]["body"].replace(f"## {name}\n", "## Something else\n")
    found = [f for f in v1run(v1_ctx(body=body)) if f.check == "template"]
    assert any(name in f.problem for f in found)


def test_v1_template_sections_list():
    assert sc.SECTIONS_V1 == ("Builder", "Workstation", "Session", "Tier")
    assert sc.SECTIONS == ("Issue", "Tests named in the issue", "Verification evidence", "/review findings")


def test_v1_pre_cutover_pr_is_not_failed_for_missing_new_sections():
    assert run(make_ctx()) == []  # pr_good.json is pre-cutover and has none of the new sections


def test_v1_readiness_uses_the_v1_template_after_cutover():
    ctx = v1_ctx(body=make_ctx()["pr"]["body"] + "\nBuilder: @example-dev\n")
    ctx["reviews"] = [marker(BUILDER, HEAD)]
    state, desc = sc.readiness(ctx, V1_OPTS)
    assert state == sc.READY_PENDING and "Workstation" in desc
    ctx = v1_ctx(); ctx["reviews"] = [marker(BUILDER, HEAD)]
    assert sc.readiness(ctx, V1_OPTS)[0] == sc.READY_SUCCESS


# gather + CLI for v1

class GraphTransport(FakeTransport):
    """FakeTransport plus GraphQL POSTs. `graph` maps node id -> edit info, or is an int status to raise."""
    def __init__(self, routes, graph=None, closing=()):
        super().__init__(routes)
        self.graph, self.queries, self.closing = graph if graph is not None else {}, [], list(closing)

    def __call__(self, url, headers, data=None):
        if data is None:
            return super().__call__(url, headers)
        assert url == sc.API + "/graphql"
        q = json.loads(data)
        self.queries.append(q)
        if isinstance(self.graph, int):
            raise sc.ApiError(self.graph, url)
        if "closingIssuesReferences" in q["query"]:
            nodes = [{"number": n, "repository": {"nameWithOwner": r}} for r, n in self.closing]
            return {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
                "totalCount": len(nodes), "nodes": nodes}}}}}
        ids = q["variables"]["ids"]
        return {"data": {"nodes": [self.graph.get(i) for i in ids]}}


def gnode(author, editors=(), edited=False, total=None):
    return {"author": {"login": author}, "lastEditedAt": "2026-10-20T12:00:00Z" if edited else None,
            "editor": {"login": editors[-1]} if editors else None,
            "userContentEdits": {"totalCount": len(editors) if total is None else total,
                                 "nodes": [{"editor": {"login": e}} for e in editors]}}


def v1_routes(pr_extra=None, issue_labels=(), issue_comments=(), comments=(), user_type="User"):
    routes = pr_routes(pr_extra={"created_at": V1_CREATED, **(pr_extra or {})})
    routes = {"/repos/NapticStacks/example/issues/42/comments": paged(list(comments)),
              "/repos/NapticStacks/example/issues/7/comments": paged(list(issue_comments)),
              "/repos/NapticStacks/example/issues/7": {"number": 7, "labels": [{"name": l} for l in issue_labels]},
              "/users/example-dev": {"login": "example-dev", "type": user_type},
              **routes}
    return routes


def test_v1_gather_reads_comments_issue_labels_and_the_builder_account():
    body = make_ctx()["pr"]["body"].replace("Closes #7\n", "Closes #7\n" + V1_SECTIONS)
    rc = self_review(); del rc["edited_by_other"]
    transport = GraphTransport(v1_routes({"body": body}, issue_labels=["tier:medium"], comments=[rc]),
                               {rc["node_id"]: gnode(BUILDER)})
    ctx = sc.gather(sc.Api("t0ken", transport), REPO, 42)
    assert ctx["comments"][0]["user"]["login"] == BUILDER
    assert ctx["comments"][0]["edited_by_other"] is False
    assert ctx["linked_issues"][7]["labels"] == ["tier:medium"]
    assert ctx["linked_issues"][7]["comments"] == []  # comments are read only for tier:high
    assert ctx["builder_account"] == {"login": "example-dev", "type": "User"}


def test_v1_gather_reads_issue_comments_for_high_tier():
    so = signoff(); del so["edited_by_other"]
    transport = GraphTransport(v1_routes({"body": "Closes #7\nBuilder: @example-dev"},
                                         issue_labels=["tier:high"], issue_comments=[so]),
                               {so["node_id"]: gnode(SIGNER, editors=["example-dev"], edited=True)})
    ctx = sc.gather(sc.Api("t0ken", transport), REPO, 42)
    assert ctx["linked_issues"][7]["comments"][0]["user"]["login"] == SIGNER
    assert ctx["linked_issues"][7]["comments"][0]["edited_by_other"] is True


@pytest.mark.parametrize("node,expected", [
    (gnode("example-dev"), False),                                               # never edited
    (gnode("example-dev", editors=["example-dev"], edited=True), False),         # edited by the author
    (gnode("example-dev", editors=["Example-Dev", "mallory"], edited=True), True),  # someone else in the history
    (gnode("example-dev", editors=["mallory", "example-dev"], edited=True), True),  # author re-edit doesn't launder it
    (gnode("example-dev", editors=["example-dev"], edited=True, total=101), True),  # history too long to check
    ({**gnode("example-dev", edited=True), "editor": None}, True),               # edited by a deleted account
    (None, True),                                                                 # node not returned
])
def test_v1_edit_history_rules(node, expected):
    assert sc.edited_by_other(node, "example-dev") is expected


def test_v1_graphql_failure_is_a_checker_error():
    """F2: if the edit history can't be read, exit 2. Never trust the marker."""
    rc = self_review(); del rc["edited_by_other"]
    transport = GraphTransport(v1_routes({"body": "Closes #7\nBuilder: @example-dev"}, comments=[rc]), graph=502)
    with pytest.raises(sc.CheckerError):
        sc.gather(sc.Api("t0ken", transport), REPO, 42)


def test_v1_graphql_errors_payload_is_a_checker_error():
    rc = self_review(); del rc["edited_by_other"]
    routes = v1_routes({"body": "Closes #7\nBuilder: @example-dev"}, comments=[rc])
    class Errs(GraphTransport):
        def __call__(self, url, headers, data=None):
            if data is None:
                return super().__call__(url, headers)
            return {"errors": [{"message": "rate limited"}]}
    with pytest.raises(sc.CheckerError):
        sc.gather(sc.Api("t0ken", Errs(routes)), REPO, 42)


def test_v1_no_marker_comments_means_no_graphql_call():
    plain = {"user": {"login": "x"}, "body": "hi", "node_id": "IC_x"}
    transport = GraphTransport(v1_routes({"body": "Closes #7\nBuilder: @example-dev"}, comments=[plain]))
    sc.gather(sc.Api("t0ken", transport), REPO, 42)
    assert not [q for q in transport.queries if "ids" in q["variables"]]  # no edit-history read


def test_v1_gather_fetches_each_linked_issue_once_and_main_reuses_it(monkeypatch):
    """F5: check_issue_link reuses the issue gather_v1 already read."""
    transport = GraphTransport(v1_routes({"body": "Closes #7\nBuilder: @example-dev"}))
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    monkeypatch.setattr(sc, "_urllib_fetch", transport)
    sc.main(["--pr", "NapticStacks/example#42"])
    assert sum(u.endswith("/issues/7") for u in transport.seen) == 1


def test_v1_post_ready_skips_the_v1_reads(monkeypatch):
    """F12: readiness only needs the cutover decision."""
    transport = PostTransport(ready_routes(pr_extra={"created_at": V1_CREATED}))
    main_ready(monkeypatch, transport)
    assert not any("/comments" in u or "/users/" in u or u.endswith("/issues/7") for u in transport.seen)


def test_v1_gather_unknown_builder_is_none():
    routes = v1_routes({"body": "Closes #7\nBuilder: @example-dev"})
    routes["/users/example-dev"] = 404
    ctx = sc.gather(sc.Api("t0ken", GraphTransport(routes)), REPO, 42)
    assert ctx["builder_account"] is None


def test_v1_gather_skips_v1_reads_before_cutover():
    transport = FakeTransport(pr_routes())  # pr_good.json is pre-cutover
    ctx = sc.gather(sc.Api("t0ken", transport), REPO, 42)
    assert not any("/comments" in u or "/users/" in u for u in transport.seen)
    assert "comments" not in ctx


def test_v1_gather_skips_cross_repo_and_missing_issues():
    routes = v1_routes({"body": "Part of NapticStacks/other#3\nCloses #9\nBuilder: @example-dev"})
    ctx = sc.gather(sc.Api("t0ken", GraphTransport(routes)), REPO, 42)
    assert ctx["linked_issues"] == {9: None}  # cross-repo skipped; a 404 is recorded as unknown


def test_v1_main_passes_the_new_flags(monkeypatch):
    seen = {}
    def fake_eval(ctx, opts, lookup, read):
        seen["opts"] = opts
        return []
    monkeypatch.setattr(sc, "gather", lambda *a, **k: make_ctx())
    monkeypatch.setattr(sc, "evaluate", fake_eval)
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    assert sc.main(["--pr", "NapticStacks/example#42", "--builders", "a, b", "--security-signers", "s,j",
                    "--cutover", "2026-11-01T00:00:00Z"]) == sc.EXIT_OK
    assert seen["opts"].builders == ("a", "b") and seen["opts"].security_signers == ("s", "j")
    assert seen["opts"].cutover == "2026-11-01T00:00:00Z"


def test_v1_workflow_passes_the_new_inputs_through():
    text = WORKFLOW.read_text()
    for name in ("builders", "security_signers", "cutover"):
        assert f"\n      {name}:" in text, name
    run_steps = text.split("- name: Run the gate", 1)[1].split("\n  sdlc-ready:", 1)[0]
    for flag in ("--builders", "--security-signers", "--cutover"):
        assert flag in run_steps, flag
    ready = text.split("\n  sdlc-ready:", 1)[1]
    for flag in ("--builders", "--security-signers", "--cutover"):
        assert flag in ready, flag
    assert 'default: "2026-10-16T00:00:00Z"' in text


ORG_TEMPLATE = Path(__file__).parent.parent / ".github" / "pull_request_template.md"
CALLER_TEMPLATE = Path(__file__).parent.parent / "workflow-templates" / "sdlc.yml"


def test_v1_org_pr_template_carries_every_section():
    headings = [h.lower() for h in sc.sections(ORG_TEMPLATE.read_text())]
    for name in (*sc.SECTIONS, *sc.SECTIONS_V1):
        assert name.lower() in headings, name
    assert "Builder: @" in ORG_TEMPLATE.read_text()


def test_v1_caller_template_pins_v3_and_reruns_on_v1_events():
    text = CALLER_TEMPLATE.read_text()
    assert "reusable-sdlc-conformance.yml@sdlc-conformance-v3" in text
    pr_types = text.split("pull_request:", 1)[1].split("\n", 2)[1]
    for t in ("opened", "edited", "synchronize", "reopened", "ready_for_review", "converted_to_draft",
              "labeled", "unlabeled", "review_requested"):
        assert t in pr_types, t
    assert "statuses: write" in text and "pull_request_target" not in text and "secrets." not in text
    props = json.loads((CALLER_TEMPLATE.parent / "sdlc.properties.json").read_text())
    assert props["name"] and props["description"]

    assert '# cutover: "2026-10-16T00:00:00Z"' in text  # F11: set per repo at adoption


# --- codex rounds 2-4: closing issues come only from GitHub; one exact first-line marker -------

def _two_high(sign7, sign5, closing=(7, 5)):
    body = v1_ctx()["pr"]["body"].replace("Closes #7\n", "Closes #7\nCloses #5\n")
    return v1_ctx(labels=(), body=body, closing=closing, issues={
        7: {"labels": ["tier:high"], "comments": [signoff()] if sign7 else []},
        5: {"labels": ["tier:high"], "comments": [signoff()] if sign5 else []}})


def test_r2_every_closed_high_issue_needs_its_own_signoff():
    assert "tier" in checks(v1run(_two_high(False, True)), sc.VIOLATION)
    assert "tier" in checks(v1run(_two_high(True, False)), sc.VIOLATION)
    assert v1run(_two_high(True, True)) == []


def test_r2_a_head_bound_pr_signoff_covers_every_high_issue():
    ctx = _two_high(False, False)
    ctx["comments"].append(signoff(sha=HEAD))
    assert v1run(ctx) == []


def test_r3_part_of_issue_raises_tier_but_cannot_satisfy_signoff():
    body = v1_ctx()["pr"]["body"].replace("Closes #7\n", "Closes #7\nPart of #5\n")
    ctx = v1_ctx(labels=(), body=body, closing=(7,), issues={7: {"labels": [], "comments": []},
                                                             5: {"labels": ["tier:high"], "comments": [signoff()]}})
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)
    ctx["comments"].append(signoff(sha=HEAD))  # only a head-bound PR sign-off satisfies it
    assert v1run(ctx) == []


def test_r3_closed_issue_signed_and_part_of_issue_high_still_fails():
    ctx = _two_high(True, True, closing=(7,))
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)


def test_r4_closing_issues_come_only_from_github_not_the_body_text():
    """Round 4 P1: an indented or quoted `Closes #7` that GitHub doesn't treat as closing never counts."""
    for body_ref in ("> Closes #7\n", "    Closes #7\n", "Closes #7\n"):
        body = v1_ctx()["pr"]["body"].replace("Closes #7\n", body_ref)
        ctx = v1_ctx(labels=(), body=body, closing=(), issues={7: {"labels": ["tier:high"], "comments": [signoff()]}})
        assert "tier" in checks(v1run(ctx), sc.VIOLATION), body_ref
    assert not hasattr(sc, "closing_refs")


def test_r4_sidebar_linked_issue_counts_as_closing():
    body = v1_ctx()["pr"]["body"].replace("Closes #7\n", "Part of #7\n")
    ctx = v1_ctx(labels=(), body=body, closing=(7,), issues={7: {"labels": ["tier:high"], "comments": [signoff()]}})
    assert v1run(ctx) == []


def test_r3_gather_reads_closing_issue_references():
    transport = GraphTransport(v1_routes({"body": "Part of #7\nBuilder: @example-dev"}, issue_labels=["tier:high"]),
                               closing=[("NapticStacks/example", 7), ("NapticStacks/other", 3)])
    ctx = sc.gather(sc.Api("t0ken", transport), REPO, 42)
    assert ctx["closing_issues"] == [7]


def test_r3_closing_references_read_failure_is_a_checker_error():
    transport = GraphTransport(v1_routes({"body": "Closes #7\nBuilder: @example-dev"}), graph=502)
    with pytest.raises(sc.CheckerError):
        sc.gather(sc.Api("t0ken", transport), REPO, 42)


M, SO, S = sc.SELF_REVIEW_MARKER, sc.SECURITY_SIGNOFF_MARKER, HEAD


def test_r4_closer_one_line_comment_passes():
    """WS-2a's /closer posts exactly this one-line comment."""
    body = f"<!-- sdlc:self-review v1 --> {HEAD}"
    assert v1run(v1_ctx(comments=[self_review(body=body)])) == []


@pytest.mark.parametrize("body", [
    f"{M} {S}",
    f"{M} {S}   \r\n",                         # trailing whitespace / CR stripped
    f"\n\n{M} {S}\nanything after is ignored: ```\n<details>",
    f"{M} {S}\nuse `<form>` here",             # round 4: inline HTML later never masks it
])
def test_r4_accepted_self_review_forms(body):
    assert v1run(v1_ctx(comments=[self_review(body=body)])) == []


@pytest.mark.parametrize("body", [
    f"{M}\n{S}",                                # SHA on the next line: dropped
    f" {M} {S}",                                # not column 0
    f"{M}  {S}",                                # two spaces
    f"{M} {S} done",                            # trailing text
    f"{M} {S[:7]}",                             # short SHA
    f"{M} {S.upper()}",                         # uppercase hex
    f"Self-review:\n{M} {S}",                   # not the first non-empty line
    f"```\n{M} {S}\n```",                       # fenced
    f"> {M} {S}",                               # quoted
    f"<details>\n{M} {S}\n</details>",          # HTML block
    f"<div\nclass=\"x\">\n{M} {S}",             # round 4: multi-line HTML opener
    f"- x\n  ```\n{M} {S}\n  ```",              # list-nested fence
    f"`{M} {S}`",                               # inline code
    f"<!-- sdlc:handoff v1 --> {M} {S}",        # handoff packet
    f"{M} {S}\n<!-- sdlc:handoff v1 {{}} -->",  # handoff packet carrying a marker line
    f"{M}",                                     # comment marker without a SHA
])
def test_r4_rejected_self_review_forms(body):
    assert "self-review" in checks(v1run(v1_ctx(comments=[self_review(body=body)])), sc.VIOLATION)


def test_r4_self_review_on_an_older_sha_is_stale():
    [f] = [f for f in v1run(v1_ctx(comments=[self_review(body=f"{M} {'c' * 40}")])) if f.check == "self-review"]
    assert "older" in f.problem


@pytest.mark.parametrize("body,ok", [
    (f"{SO} {S}", True),
    (f"{SO}\n{S}", False),
    (f"{SO}", False),                           # a PR-level sign-off needs the SHA
    (f"Signed.\n{SO} {S}", False),
    (f"{SO} {'c' * 40}", False),
])
def test_r4_pr_level_signoff_format(body, ok):
    ctx = v1_ctx(labels=("tier:high",), closing=(), comments=[self_review(), dict(signoff(), body=body)])
    assert ("tier" in checks(v1run(ctx), sc.VIOLATION)) is (not ok)


@pytest.mark.parametrize("body,ok", [
    (f"{SO}", True),
    (f"{SO}\nRead the plan; fine.", True),
    (f"{SO} {S}", True),                        # SHA optional on an issue
    (f"{SO} {S[:7]}", False),
    (f"```\n{SO}\n```", False),
    (f"Looks fine.\n{SO}", False),
    (f"> {SO}", False),
])
def test_r4_issue_level_signoff_format(body, ok):
    ctx = v1_ctx(labels=(), issues={7: {"labels": ["tier:high"], "comments": [dict(signoff(), body=body)]}})
    assert ("tier" in checks(v1run(ctx), sc.VIOLATION)) is (not ok)


def _review(body, sha=HEAD):
    return {"user": {"login": BUILDER}, "state": "COMMENTED", "commit_id": sha,
            "submitted_at": "2026-10-20T10:00:00Z", "body": body, "edited_by_other": False}


@pytest.mark.parametrize("body,ok", [
    (f"{M}\n## Three-lens review", True),
    (f"{M} {S}\n## Three-lens review", True),
    (f"## Three-lens review\n{M}", False),
    (f"{M} {'c' * 40}", False),                 # a SHA, if given, must be the head
])
def test_r4_review_marker_first_line(body, ok):
    ctx = v1_ctx(comments=[])
    ctx["reviews"].append(_review(body))
    assert ("self-review" in checks(v1run(ctx), sc.VIOLATION)) is (not ok)


@pytest.mark.parametrize("line", ["> Builder: @example-dev", ">Builder: @example-dev"])
def test_r3_quoted_builder_line_does_not_count(line):
    assert sc.builder_of_record(line) is None


def test_r4_repeated_builder_sections_all_count():
    """Round 4 P2: a second `## Builder` section can't overwrite the first."""
    body = ("## Issue\nCloses #7\n\n## Builder\nBuilder: @example-dev\n\n## Tier\ntier:low\n\n"
            "## Builder\nBuilder: @example-other\n")
    assert sc.builder_logins(body) == ["example-dev", "example-other"]
    [f] = [f for f in v1run(v1_ctx(body=body)) if f.check == "builder-of-record"]
    assert f.level == sc.VIOLATION and "more than one" in f.problem.lower()


def test_r4_builder_line_outside_the_section_also_counts():
    body = "## Issue\nCloses #7\nBuilder: @someone-else\n\n## Builder\nBuilder: @example-dev\n"
    assert sc.builder_logins(body) == ["someone-else", "example-dev"]
    assert sc.builder_of_record(body) is None


def test_r3_multiple_distinct_builders_is_a_violation():
    body = _body_with_builder("Builder: @example-dev\nBuilder: @example-other")
    assert sc.builder_of_record(body) is None
    [f] = [f for f in v1run(v1_ctx(body=body)) if f.check == "builder-of-record"]
    assert f.level == sc.VIOLATION and "more than one" in f.problem.lower()


def test_r3_same_builder_twice_is_fine():
    body = _body_with_builder("Builder: @example-dev\nBuilder: @Example-Dev")
    assert sc.builder_of_record(body) == "example-dev"


def test_r3_no_ambiguous_builder_can_sign_off():
    body = _body_with_builder(f"Builder: @example-dev\nBuilder: @{SIGNER}")
    ctx = v1_ctx(labels=("tier:high",), body=body, issues={7: {"labels": ["tier:high"], "comments": [signoff()]}})
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)


# --- codex round 5 -----------------------------------------------------------------------

@pytest.mark.parametrize("node", [
    {"author": {"login": "example-dev"}, "lastEditedAt": "2026-10-20T12:00:00Z",
     "editor": {"login": "example-dev"}, "userContentEdits": None},
    {"author": {"login": "example-dev"}, "lastEditedAt": "2026-10-20T12:00:00Z", "editor": {"login": "example-dev"}},
    {"author": {"login": "example-dev"}, "lastEditedAt": "2026-10-20T12:00:00Z", "editor": {"login": "example-dev"},
     "userContentEdits": {"totalCount": 3, "nodes": [{"editor": {"login": "example-dev"}}]}},
    {"author": {"login": "example-dev"}, "lastEditedAt": None,
     "userContentEdits": {"totalCount": 2, "nodes": [{"editor": {"login": "example-dev"}}]}},
])
def test_r5_incomplete_edit_history_fails_closed(node):
    assert sc.edited_by_other(node, "example-dev") is True


def test_r5_unedited_comment_with_no_history_is_fine():
    node = {"author": {"login": "example-dev"}, "lastEditedAt": None, "editor": None,
            "userContentEdits": {"totalCount": 0, "nodes": []}}
    assert sc.edited_by_other(node, "example-dev") is False


@pytest.mark.parametrize("line", ["Builder: @example-dev @example-other", "Builder: @example-dev, @example-other",
                                  "Builder: @example-dev and @example-other", "Builder: example-dev @example-other"])
def test_r5_builder_line_with_two_logins_is_ambiguous(line):
    body = _body_with_builder(line)
    assert sc.builder_of_record(body) is None
    assert set(sc.builder_logins(body)) == {"example-dev", "example-other"}
    [f] = [f for f in v1run(v1_ctx(body=body)) if f.check == "builder-of-record"]
    assert f.level == sc.VIOLATION and "more than one" in f.problem.lower()


def test_r5_every_login_on_an_ambiguous_line_is_excluded_from_signing():
    body = _body_with_builder(f"Builder: @example-dev, @{SIGNER}")
    ctx = v1_ctx(labels=("tier:high",), body=body, issues={7: {"labels": ["tier:high"], "comments": [signoff()]}})
    assert "tier" in checks(v1run(ctx), sc.VIOLATION)


def test_r5_note_after_the_login_and_emails_are_not_logins():
    assert sc.builder_of_record("Builder: @example-dev (pairing with Ben)") == "example-dev"
    assert sc.builder_of_record("Builder: @example-dev ben@example.com") == "example-dev"


def test_r5_malformed_single_login_line_says_ambiguous():
    [f] = [f for f in v1run(v1_ctx(body=_body_with_builder("Builder: @example-dev,"))) if f.check == "builder-of-record"]
    assert f.level == sc.VIOLATION and "malformed" in f.problem
