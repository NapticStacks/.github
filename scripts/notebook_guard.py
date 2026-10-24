"""Inventor-notebook guard (NapticStacks/project-manager#311).

The inventor notebook (`JORDAN-CONFIDENTIAL/`) is local-only: it is the dated
record of human conception for any future patent, and it must not land on a
remote. This guard fails a change that adds, edits or renames a file into a
`JORDAN-CONFIDENTIAL/` folder anywhere in the tree. Removing one (untracking it)
is allowed.

A repo that still tracks the folder from before the guard only gets a warning,
so unrelated work keeps moving while its untrack PR lands. History is never
checked: old commits are kept on purpose as timestamp evidence.

Exit codes: 0 pass (warnings allowed), 1 a change touches the notebook,
2 the guard itself failed (bad SHA, git error).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field

FOLDER = "jordan-confidential"
ZERO = "0" * 40


@dataclass
class Result:
    violations: list[str] = field(default_factory=list)
    still_tracked: list[str] = field(default_factory=list)


def is_notebook_path(path: str) -> bool:
    """True when any directory in the path is the notebook folder (any case)."""
    return any(part.lower() == FOLDER for part in path.split("/")[:-1])


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def check(repo: str, base: str | None, head: str) -> Result:
    r = Result()
    if base and base != ZERO:
        out = _git(repo, "diff", "--name-status", "-M", base, head)
        for line in out.splitlines():
            parts = line.split("\t")
            status, path = parts[0], parts[-1]  # a rename's last field is its new path
            if status.startswith("D"):
                continue
            if is_notebook_path(path):
                r.violations.append(path)
    tracked = _git(repo, "ls-tree", "-r", "--name-only", head).splitlines()
    r.still_tracked = [p for p in tracked if is_notebook_path(p) and p not in r.violations]
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default="", help="base SHA; empty or all zeros checks the tree only")
    ap.add_argument("--head", default="HEAD")
    a = ap.parse_args(argv)
    try:
        r = check(a.repo, a.base or None, a.head)
    except subprocess.CalledProcessError as e:
        print(f"::error::notebook guard could not read git: {e.stderr.strip() or e}")
        return 2
    for p in r.violations:
        print(f"::error file={p}::This change adds or edits the local-only inventor notebook. "
              "Remove it from the commit and keep JORDAN-CONFIDENTIAL/ in .gitignore (project-manager#311).")
    if r.still_tracked:
        print(f"::warning::{len(r.still_tracked)} file(s) under JORDAN-CONFIDENTIAL/ are still tracked. "
              "Merge this repo's untrack PR (project-manager#311).")
    if r.violations:
        print(f"Notebook guard: FAIL, {len(r.violations)} notebook file(s) in this change.")
        return 1
    print("Notebook guard: pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
