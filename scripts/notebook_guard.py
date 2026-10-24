"""Inventor-notebook guard (NapticStacks/project-manager#311).

An inventor notebook (`JORDAN-CONFIDENTIAL/` by default; more folders through
`--folders`, e.g. `JORDAN-CONFIDENTIAL,BEN-CONFIDENTIAL`) is local-only: it is the dated record of human conception for any
future patent, and it must not land on a remote. This guard fails a change in
which any commit adds, edits, copies or renames a file into such a folder,
anywhere in the tree. A commit that only removes one (untracking it) passes.

Every commit in the range is checked, plus the net diff (git log prints no paths
for merge commits), so a file added and deleted again inside one PR fails, and so
does one added while resolving a merge.

A repo that still tracks the folder from before the guard only gets a warning,
so unrelated work keeps moving while its untrack PR lands. History before the
range is never checked: old commits are kept on purpose as timestamp evidence.

Exit codes: 0 pass (warnings allowed), 1 a change touches a notebook,
2 the guard itself failed (git error).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field

DEFAULT_FOLDERS = "JORDAN-CONFIDENTIAL"
ZERO = "0" * 40


def parse_folders(spec: str) -> frozenset[str]:
    return frozenset(f.strip().strip("/").lower() for f in spec.split(",") if f.strip())


@dataclass
class Result:
    violations: list[str] = field(default_factory=list)
    still_tracked: list[str] = field(default_factory=list)
    base_missing: bool = False


def is_notebook_path(path: str, folders: frozenset[str] | None = None) -> bool:
    """True when any directory in the path is a notebook folder (any case)."""
    folders = folders or parse_folders(DEFAULT_FOLDERS)
    return any(part.lower() in folders for part in path.split("/")[:-1])


def escape_property(s: str) -> str:
    """Escape a value for a workflow-command property (file=...)."""
    return (s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            .replace(":", "%3A").replace(",", "%2C"))


def _git(repo: str, *args: str) -> str:
    # quotePath off here, and every caller passes -z: a quoted path ("JORDAN-CONFIDENTIAL/caf\303\251.md")
    # would never match the folder name.
    return subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false", *args],
                          check=True, capture_output=True, text=True).stdout


def _has_commit(repo: str, sha: str) -> bool:
    return subprocess.run(["git", "-C", repo, "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def _parse_name_status(out: str) -> list[str]:
    """New-side paths of every non-delete record in -z --name-status output."""
    fields = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        status = fields[i].strip("\n")
        if not status:
            i += 1
            continue
        if status[0] in "RC":  # rename or copy: old path, then new path
            paths.append(fields[i + 2])
            i += 3
        else:
            if status[0] != "D":
                paths.append(fields[i + 1])
            i += 2
    return paths


def _changed_paths(repo: str, base: str, head: str) -> list[str]:
    """Paths any commit in base..head adds, edits, copies or renames into place, plus the net diff."""
    per_commit = _parse_name_status(_git(repo, "log", "-z", "-M", "--name-status", "--format=", f"{base}..{head}"))
    net = _parse_name_status(_git(repo, "diff", "-z", "-M", "--name-status", base, head))
    return per_commit + net


def check(repo: str, base: str | None, head: str, folders: frozenset[str] | None = None) -> Result:
    folders = folders or parse_folders(DEFAULT_FOLDERS)
    r = Result()
    if base and base != ZERO:
        if _has_commit(repo, base):
            for p in _changed_paths(repo, base, head):
                if is_notebook_path(p, folders) and p not in r.violations:
                    r.violations.append(p)
        else:
            r.base_missing = True  # e.g. a force-push: `before` is gone; fall back to the tree
    tracked = _git(repo, "ls-tree", "-r", "-z", "--name-only", head).split("\0")
    r.still_tracked = [p for p in tracked if p and is_notebook_path(p, folders) and p not in r.violations]
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default="", help="base SHA; empty or all zeros checks the tree only")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--folders", default=DEFAULT_FOLDERS, help="comma-separated notebook folder names")
    ap.add_argument("--strict-base", action="store_true",
                    help="a base missing from the clone is an error (pull_request, merge_group)")
    a = ap.parse_args(argv)
    try:
        r = check(a.repo, a.base or None, a.head, parse_folders(a.folders))
    except subprocess.CalledProcessError as e:
        print(f"::error::notebook guard could not read git: {(e.stderr or str(e)).strip()}")
        return 2
    if r.base_missing and a.strict_base:
        print("::error::Base commit is not in this clone, so the change can't be checked.")
        return 2
    if r.base_missing:
        print("::warning::Base commit is not in this clone (force-push?). Checked the tree only.")
    for p in r.violations:
        print(f"::error file={escape_property(p)}::A commit in this change adds or edits a local-only "
              "inventor notebook. Remove it from the commits and keep the folder in .gitignore (project-manager#311).")
    if r.still_tracked:
        print(f"::warning::{len(r.still_tracked)} notebook file(s) are still tracked. "
              "Merge this repo's untrack PR (project-manager#311).")
    if r.violations:
        print(f"Notebook guard: FAIL, {len(r.violations)} notebook file(s) in this change.")
        return 1
    print("Notebook guard: pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
