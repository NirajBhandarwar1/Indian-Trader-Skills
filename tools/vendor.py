#!/usr/bin/env python3
"""Give every skill its own copy of the shared code it uses, so each one works on its own.

Run from the repository root after changing anything in nsetools/:

    python tools/vendor.py          # write the copies
    python tools/vendor.py --check  # exit 1 if any copy is missing or out of date

WHY. A skill is installed by copying its folder. If the folder only works with a sibling nsetools/
next to it, then copying nse-entry/ on its own fails at the first line with ModuleNotFoundError --
which is exactly what it did before this existed. The US pack avoids that by repeating the same
helpers inside every script by hand. That works, and it is also how eleven copies of one function
drift apart.

So there is ONE source, nsetools/, and this script copies into each skill the modules that skill
needs and nothing else. tests/test_repo.py fails if a copy differs from the source by a single
byte, and tests/test_standalone.py copies each skill alone into an empty folder and runs it.

WHAT A SKILL NEEDS is worked out by reading the code, not from a list someone has to remember to
update: every `from nsetools...` in the script, including ones inside functions, and then every
module those modules import, until nothing new turns up.

Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "nsetools"
PACKAGE = "nsetools"


def skills() -> list[Path]:
    return sorted(p for p in ROOT.glob("nse-*") if p.is_dir() and (p / "SKILL.md").exists())


def script_of(folder: Path) -> Path:
    scripts = [p for p in folder.glob("*.py")]
    if len(scripts) != 1:
        raise SystemExit(f"{folder.name}: expected one script, found {len(scripts)}")
    return scripts[0]


def imported_by_script(path: Path) -> set[str]:
    """The nsetools modules a skill script names, anywhere in it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == PACKAGE:
                found.update(alias.name for alias in node.names)
            elif node.module.startswith(PACKAGE + "."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PACKAGE + "."):
                    found.add(alias.name.split(".")[1])
    return {name for name in found if (SOURCE / f"{name}.py").exists()}


def imported_by_module(name: str) -> set[str]:
    """The sibling modules one nsetools module imports, relatively, anywhere in it."""
    tree = ast.parse((SOURCE / f"{name}.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                found.update(alias.name for alias in node.names)
    return {n for n in found if (SOURCE / f"{n}.py").exists()}


def needed(folder: Path) -> list[str]:
    """Everything a skill needs, followed through until nothing new turns up."""
    todo = sorted(imported_by_script(script_of(folder)))
    have: set[str] = set()
    while todo:
        name = todo.pop()
        if name in have:
            continue
        have.add(name)
        todo.extend(sorted(imported_by_module(name) - have))
    return sorted(have)


def files_for(folder: Path) -> list[str]:
    return ["__init__.py"] + [f"{name}.py" for name in needed(folder)]


def out_of_date(folder: Path) -> list[str]:
    """What is missing, different from the source, or there but no longer needed."""
    target = folder / PACKAGE
    want = set(files_for(folder))
    problems = []
    for name in sorted(want):
        copy = target / name
        if not copy.exists():
            problems.append(f"{folder.name}: {PACKAGE}/{name} is missing")
        elif not filecmp.cmp(copy, SOURCE / name, shallow=False):
            problems.append(f"{folder.name}: {PACKAGE}/{name} differs from the source")
    if target.exists():
        for extra in sorted(p.name for p in target.glob("*.py")):
            if extra not in want:
                problems.append(f"{folder.name}: {PACKAGE}/{extra} is not needed")
    return problems


def write(folder: Path) -> list[str]:
    target = folder / PACKAGE
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    names = files_for(folder)
    for name in names:
        shutil.copy2(SOURCE / name, target / name)
    return names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report, change nothing")
    args = ap.parse_args(argv)

    if args.check:
        problems = [p for folder in skills() for p in out_of_date(folder)]
        for line in problems:
            print(line)
        print("all copies match the source" if not problems
              else f"\n{len(problems)} problem(s). Run: python tools/vendor.py")
        return 1 if problems else 0

    for folder in skills():
        names = write(folder)
        print(f"{folder.name:<16} {', '.join(n[:-3] for n in names if n != '__init__.py')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
