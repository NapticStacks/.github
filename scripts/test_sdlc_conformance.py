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
    versions = {("VERSION", "b" * 40): "1.0.0\n", ("VERSION", HEAD): "1.0.1\n"}
    assert run(ctx, opts, read_file=lambda p, r: versions.get((p, r))) == []
    same = {("VERSION", "b" * 40): "1.0.0\n", ("VERSION", HEAD): "1.0.0\n"}
    assert "version-bump" in checks(run(ctx, opts, read_file=lambda p, r: same.get((p, r))), sc.VIOLATION)
    ctx["files"].remove("VERSION")
    assert "version-bump" in checks(run(ctx, opts, read_file=lambda p, r: versions.get((p, r))), sc.VIOLATION)


def test_version_bump_ignores_trailing_whitespace_only_changes():
    opts = sc.Options(version_bump=True)
    ctx = make_ctx(); ctx["files"].append("VERSION")
    ws = {("VERSION", "b" * 40): "1.0.0\n", ("VERSION", HEAD): "1.0.0"}
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
