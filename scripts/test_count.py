#!/usr/bin/env python3
"""Count collectible tests in a directory and compare two revisions.

The reusable `test-count-guard` workflow is a thin caller around this module so
the interesting logic is unit-testable off CI. Three entry points:

    detect_runner(directory) -> ["jest", "pytest"] subset, sorted
    count(directory)         -> (n, exit_code, stderr)
    verdict(base, head, label_present) -> one of the VERDICT_* constants

`count()` returns n=None when no runner is detected, so "this repo has no suite"
stays distinguishable from "this repo has a suite with zero tests".

Stdlib only, Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# pytest's documented exit codes (docs.pytest.org -> "Usage and Invocations").
PYTEST_OK = 0
PYTEST_TESTS_FAILED = 1
PYTEST_INTERRUPTED = 2          # collection was interrupted -- e.g. an import error
PYTEST_INTERNAL_ERROR = 3
PYTEST_USAGE_ERROR = 4
PYTEST_NO_TESTS_COLLECTED = 5   # NOT an error: a real count of zero
PYTEST_COLLECTION_ERROR_CODES = (PYTEST_INTERRUPTED, PYTEST_INTERNAL_ERROR, PYTEST_USAGE_ERROR)

# Synthetic codes for a collector that never ran. Borrowed from the shell
# conventions (127 = command not found, 124 = timed out) so they read correctly
# in a CI log and can never collide with a real pytest exit code.
COLLECTOR_UNAVAILABLE = 127
COLLECTOR_TIMEOUT = 124
COLLECTOR_FAILURE_CODES = (COLLECTOR_UNAVAILABLE, COLLECTOR_TIMEOUT)

VERDICT_PASS = "pass"
VERDICT_DROPPED = "dropped"
VERDICT_COLLECTION_ERROR = "collection_error"
VERDICT_NOOP = "noop"

COLLECT_TIMEOUT_SECONDS = 600
PYTEST_MARKERS = ("pytest.ini", "setup.cfg", "tox.ini")
# Vendored trees are never the repo's own suite. Skipping them keeps the scan
# fast AND correct: a stray `test_*.py` shipped inside some npm package or venv
# would otherwise make a pure-jest repo look like it has a pytest suite too.
PRUNED_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", ".tox", ".nox", "dist", "build",
     "__pycache__", ".mypy_cache", ".pytest_cache", "site-packages"}
)


def _looks_like_a_test_file(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _has_pytest_marker(directory: Path) -> bool:
    for marker in PYTEST_MARKERS:
        if (directory / marker).is_file():
            return True
    pyproject = directory / "pyproject.toml"
    if pyproject.is_file() and "pytest" in pyproject.read_text(errors="replace"):
        return True
    for root, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if d not in PRUNED_DIRS and not d.startswith(".")]
        if any(_looks_like_a_test_file(name) for name in filenames):
            return True
    return False


def _has_jest_marker(directory: Path) -> bool:
    package_json = directory / "package.json"
    if not package_json.is_file():
        return False
    try:
        manifest = json.loads(package_json.read_text(errors="replace"))
    except (ValueError, OSError):
        return False
    if "jest" in manifest:
        return True
    for section in ("dependencies", "devDependencies"):
        if "jest" in (manifest.get(section) or {}):
            return True
    scripts = manifest.get("scripts") or {}
    return any("jest" in str(value) for value in scripts.values())


def detect_runner(directory) -> list[str]:
    """Return the sorted test runners that look usable in `directory`."""
    directory = Path(directory)
    runners = []
    if _has_jest_marker(directory):
        runners.append("jest")
    if _has_pytest_marker(directory):
        runners.append("pytest")
    return sorted(runners)


def _run(argv: list[str], directory: Path) -> tuple[int, str, str]:
    """Run a collector. Returns (returncode, stdout, stderr) and NEVER raises.

    A missing binary (no npx on the runner) or a hung collection would otherwise
    escape as an exception, skipping _emit() and leaving the workflow's declared
    outputs unset -- the job would fail with no verdict and no named reason. Both
    come back as a non-zero code instead, so they land in `collection_error`
    alongside every other collector failure.
    """
    try:
        proc = subprocess.run(
            argv, cwd=str(directory), capture_output=True, text=True,
            timeout=COLLECT_TIMEOUT_SECONDS, check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        return COLLECTOR_UNAVAILABLE, "", f"FileNotFoundError: {exc} (is `{argv[0]}` installed on the runner?)"
    except subprocess.TimeoutExpired:
        return COLLECTOR_TIMEOUT, "", (
            f"TimeoutExpired: `{argv[0]}` did not finish collecting within "
            f"{COLLECT_TIMEOUT_SECONDS}s"
        )
    except OSError as exc:
        return COLLECTOR_UNAVAILABLE, "", f"{type(exc).__name__}: {exc} (could not execute `{argv[0]}`)"


def _count_pytest(directory: Path) -> tuple[int, int, str]:
    code, out, err = _run([sys.executable, "-m", "pytest", "--collect-only", "-q", "."], directory)
    if code in PYTEST_COLLECTION_ERROR_CODES or code in COLLECTOR_FAILURE_CODES:
        # pytest prints collection tracebacks on STDOUT, so surface both streams
        # or the failure reason reaches the log empty.
        return 0, code, (out + err).strip()
    # Exit 5 (no tests collected) is a legitimate count of zero, not a failure.
    n = sum(1 for line in out.splitlines() if "::" in line)
    return n, 0, ""


def _count_jest(directory: Path) -> tuple[int, int, str]:
    code, out, err = _run(["npx", "--no-install", "jest", "--listTests"], directory)
    if code != 0:
        return 0, code, (err + out).strip()
    # `--listTests` prints one test FILE per line, so this counts files, not
    # individual test cases. See the README note.
    n = sum(1 for line in out.splitlines() if line.strip())
    return n, 0, ""


def count(directory) -> tuple[int | None, int, str]:
    """Count collectible tests in `directory`.

    Returns (n, exit_code, stderr). n is None when no runner is detected.
    exit_code is 0 when collection succeeded; otherwise it is the collector's
    own failing code and `stderr` carries its diagnostic output.
    """
    directory = Path(directory)
    runners = detect_runner(directory)
    if not runners:
        return None, 0, ""
    total = 0
    for runner in runners:
        n, code, err = _count_jest(directory) if runner == "jest" else _count_pytest(directory)
        if code != 0:
            return 0, code, f"[{runner}] {err}"
        total += n
    return total, 0, ""


def verdict(base, head, label_present: bool) -> str:
    """Compare two count() results.

    Order matters: a broken collector is checked BEFORE the counts are compared.
    A failed collection reports zero tests, which a naive comparison would call a
    test reduction -- blaming the author for deleting tests instead of naming the
    real breakage. The override label waives a deliberate reduction; it never
    waives a broken collector.
    """
    base_n, base_code, _ = base
    head_n, head_code, _ = head
    if base_code != 0 or head_code != 0:
        return VERDICT_COLLECTION_ERROR
    if base_n is None and head_n is None:
        return VERDICT_NOOP
    if (head_n or 0) < (base_n or 0) and not label_present:
        return VERDICT_DROPPED
    return VERDICT_PASS


def _emit(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare collectible test counts across two checkouts.")
    parser.add_argument("--base", required=True, help="Path to the base-branch checkout.")
    parser.add_argument("--head", required=True, help="Path to the PR-head checkout.")
    parser.add_argument("--label", default="tests-reduced-ok", help="Name of the override label.")
    parser.add_argument("--label-present", action="store_true", help="The override label is on the PR.")
    args = parser.parse_args(argv)

    base = count(args.base)
    head = count(args.head)
    result = verdict(base, head, args.label_present)

    print(f"base: n={base[0]} exit={base[1]}")
    print(f"head: n={head[0]} exit={head[1]}")
    print(f"verdict: {result}")
    # These three are declared workflow_call outputs, so the empty-string
    # rendering of "no runner detected" is a contract, not a detail. Never emit
    # the literal "None".
    _emit("verdict", result)
    _emit("base_count", "" if base[0] is None else str(base[0]))
    _emit("head_count", "" if head[0] is None else str(head[0]))

    if result == VERDICT_COLLECTION_ERROR:
        print("\n::error::Test collection FAILED. This is a broken collector, not a test-count drop.")
        for label, res in (("base", base), ("head", head)):
            if res[1] != 0:
                print(f"\n--- {label} collector output (exit {res[1]}) ---\n{res[2]}")
        return 1
    if result == VERDICT_DROPPED:
        # `or 0` because a vanished runner counts as None -- "2 -> None" reads as
        # a bug in the guard rather than a deleted suite.
        print(
            f"\n::error::Test count dropped {base[0] or 0} -> {head[0] or 0}. "
            f"Add the `{args.label}` label if the reduction is intentional."
        )
        return 1
    if result == VERDICT_NOOP:
        print("::notice::No test runner detected on either side -- guard is a no-op.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
