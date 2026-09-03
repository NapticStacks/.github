"""Tests for the test-count guard.

Fixture matrix (scripts/fixtures/):
  pytest_project      -> 2 collectible tests, exit 0
  jest_project        -> 1 test file (skipped locally when jest is not installed)
  empty_project       -> no runner at all -> noop
  pytest_import_error -> import error at collection time -> collection_error
"""

import subprocess
from pathlib import Path

import pytest

import test_count as tc

FIXTURES = Path(__file__).parent / "fixtures"
PYTEST_PROJECT = FIXTURES / "pytest_project"
JEST_PROJECT = FIXTURES / "jest_project"
EMPTY_PROJECT = FIXTURES / "empty_project"
IMPORT_ERROR_PROJECT = FIXTURES / "pytest_import_error"


def jest_available() -> bool:
    """True when `npx jest` can run offline. CI installs it; dev boxes may not."""
    try:
        proc = subprocess.run(
            ["npx", "--no-install", "jest", "--version"],
            cwd=JEST_PROJECT, capture_output=True, timeout=60, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_jest = pytest.mark.skipif(
    not jest_available(),
    reason="jest is not installed in this fixture (run `npm install` in scripts/fixtures/jest_project); CI installs it",
)


# --------------------------- detect_runner ---------------------------

def test_detect_runner_finds_pytest():
    assert tc.detect_runner(PYTEST_PROJECT) == ["pytest"]


def test_detect_runner_finds_jest():
    assert tc.detect_runner(JEST_PROJECT) == ["jest"]


def test_detect_runner_finds_nothing_in_empty_dir():
    assert tc.detect_runner(EMPTY_PROJECT) == []


def test_detect_runner_finds_both_when_both_present(tmp_path):
    (tmp_path / "test_thing.py").write_text("def test_x():\n    assert True\n")
    (tmp_path / "package.json").write_text('{"devDependencies": {"jest": "^29"}}')
    assert tc.detect_runner(tmp_path) == ["jest", "pytest"]


def test_detect_runner_ignores_vendored_test_files(tmp_path):
    # A jest-only repo whose node_modules happens to vendor a Python test file
    # must NOT be treated as having a pytest suite too, or the guard would run
    # pytest over node_modules and compare a meaningless number.
    (tmp_path / "package.json").write_text('{"devDependencies": {"jest": "^29"}}')
    vendored = tmp_path / "node_modules" / "some-package"
    vendored.mkdir(parents=True)
    (vendored / "test_vendored.py").write_text("def test_x():\n    assert True\n")
    assert tc.detect_runner(tmp_path) == ["jest"]


def test_detect_runner_ignores_package_json_without_jest(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"express": "^4"}}')
    assert tc.detect_runner(tmp_path) == []


# ------------------------------ count ------------------------------

def test_count_pytest_project():
    n, code, err = tc.count(PYTEST_PROJECT)
    assert (n, code) == (2, 0), err


@requires_jest
def test_count_jest_project():
    n, code, err = tc.count(JEST_PROJECT)
    assert (n, code) == (1, 0), err


def test_count_empty_project_is_noop():
    n, code, err = tc.count(EMPTY_PROJECT)
    assert n is None
    assert code == 0
    assert err == ""


def test_count_import_error_reports_nonzero_exit_and_diagnostic():
    n, code, err = tc.count(IMPORT_ERROR_PROJECT)
    assert code == tc.PYTEST_INTERRUPTED
    assert code in tc.PYTEST_COLLECTION_ERROR_CODES
    # pytest prints collection tracebacks on stdout; count() must still surface them.
    assert "a_module_that_does_not_exist_anywhere" in err


def test_count_no_tests_collected_is_zero_not_an_error(tmp_path):
    # A pytest project marker with no test bodies: pytest exits 5. That is a
    # count of zero, NOT a collection error.
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_empty_suite.py").write_text("# no tests here\n")
    n, code, err = tc.count(tmp_path)
    assert (n, code) == (0, 0), err


# ----------------------------- verdict -----------------------------

def r(n, code=0, err=""):
    """Shorthand for a count() result tuple."""
    return (n, code, err)


def test_verdict_pass_when_count_holds():
    assert tc.verdict(r(5), r(5), False) == tc.VERDICT_PASS


def test_verdict_pass_when_count_rises():
    assert tc.verdict(r(5), r(9), False) == tc.VERDICT_PASS


def test_verdict_dropped_when_count_falls():
    assert tc.verdict(r(5), r(4), False) == tc.VERDICT_DROPPED


def test_verdict_label_overrides_a_drop():
    assert tc.verdict(r(5), r(4), True) == tc.VERDICT_PASS


def test_verdict_noop_when_neither_side_has_a_runner():
    assert tc.verdict(r(None), r(None), False) == tc.VERDICT_NOOP


def test_verdict_treats_a_vanished_suite_as_a_drop():
    # Base had tests, head has no runner at all -> the suite was deleted.
    assert tc.verdict(r(5), r(None), False) == tc.VERDICT_DROPPED


def test_verdict_pass_when_a_new_suite_appears():
    assert tc.verdict(r(None), r(3), False) == tc.VERDICT_PASS


def test_verdict_collection_error_on_base():
    assert tc.verdict(r(0, 2, "boom"), r(5), False) == tc.VERDICT_COLLECTION_ERROR


def test_verdict_collection_error_beats_the_label():
    # The label waives a deliberate reduction. It must NOT waive a broken collector.
    assert tc.verdict(r(0, 2, "boom"), r(5), True) == tc.VERDICT_COLLECTION_ERROR


def test_collection_error_named_not_count_drop():
    """HOSTILE CASE: an import error on head must never read as `dropped`.

    A broken collector reports zero tests. Naively comparing counts would call
    that a test reduction and tell the author to add tests back, hiding the real
    failure. It must surface as `collection_error`, end to end from count().
    """
    base = tc.count(PYTEST_PROJECT)
    head = tc.count(IMPORT_ERROR_PROJECT)

    assert base[0] == 2
    assert head[0] == 0, "a broken collector yields zero tests -- the trap this test guards"

    result = tc.verdict(base, head, label_present=False)
    assert result == tc.VERDICT_COLLECTION_ERROR
    assert result != tc.VERDICT_DROPPED
    # Not even the override label may disguise it.
    assert tc.verdict(base, head, label_present=True) == tc.VERDICT_COLLECTION_ERROR


# ----------------------- GITHUB_OUTPUT contract -----------------------
# verdict / base_count / head_count are declared workflow_call outputs, so their
# rendering is a caller-visible contract.

def read_outputs(tmp_path, monkeypatch, base, head, *extra):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    code = tc.main(["--base", str(base), "--head", str(head), *extra])
    parsed = dict(
        line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
    )
    return code, parsed


def test_main_emits_outputs_on_pass(tmp_path, monkeypatch):
    code, out = read_outputs(tmp_path, monkeypatch, PYTEST_PROJECT, PYTEST_PROJECT)
    assert code == 0
    assert out == {"verdict": "pass", "base_count": "2", "head_count": "2"}


def test_main_emits_empty_count_not_the_string_none(tmp_path, monkeypatch):
    # A missing runner must render as "" for callers, never the literal "None".
    code, out = read_outputs(tmp_path, monkeypatch, EMPTY_PROJECT, EMPTY_PROJECT)
    assert code == 0
    assert out["verdict"] == tc.VERDICT_NOOP
    assert out["base_count"] == ""
    assert out["head_count"] == ""
    assert "None" not in out.values()


def test_main_emits_outputs_and_fails_on_collection_error(tmp_path, monkeypatch):
    code, out = read_outputs(tmp_path, monkeypatch, PYTEST_PROJECT, IMPORT_ERROR_PROJECT)
    assert code == 1
    assert out["verdict"] == tc.VERDICT_COLLECTION_ERROR
