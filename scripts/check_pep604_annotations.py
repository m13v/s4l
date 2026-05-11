#!/usr/bin/env python3
"""
check_pep604_annotations.py — pre-commit guard.

Why this exists:
    The production launchd plists invoke `/usr/bin/python3` which is
    Python 3.9.6 on this Mac. PEP 604 union syntax (``str | None``) is
    syntactically valid in 3.9, but the annotation is EVALUATED at
    function/class def time, so any annotation containing ``X | Y``
    raises ``TypeError: unsupported operand type(s) for |: 'type' and
    'NoneType'`` on module import — taking the entire pipeline down.

    On 2026-05-11 commit 2ac6410 added ``str | None`` annotations to
    scripts/post_reddit.py without ``from __future__ import annotations``.
    Every launchd-fired reddit-search cycle from 09:43 through 10:00
    produced a one-line traceback log (zero iterations, zero searches,
    zero posts) before this guard existed.

How it works:
    For every staged .py file under scripts/ or seo/ (this repo's
    launchd-driven roots), parse with ast and walk every type-annotation
    site:
        - ast.AnnAssign.annotation
        - ast.arg.annotation
        - ast.FunctionDef.returns / AsyncFunctionDef.returns
    If ANY annotation contains a ``BinOp(op=BitOr())``, the file MUST
    have ``from __future__ import annotations`` at module level. The
    __future__ import makes all annotations lazy strings, so PEP 604
    syntax is purely cosmetic at runtime and 3.9 stays happy.

Override:
    git commit --no-verify   (do this only if you've confirmed the file
                              is never imported by Python 3.9, or you've
                              bumped launchd to 3.10+ yourself).
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOTS = ("scripts/", "seo/")  # only files under these directories run via launchd


def _has_future_annotations(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            for alias in node.names:
                if alias.name == "annotations":
                    return True
    return False


def _contains_pep604_union(node: ast.AST | None) -> bool:
    """Return True if any sub-node is a ``BinOp(op=BitOr())`` that's part of an
    annotation expression (e.g. ``str | None``, ``int | str | None``)."""
    if node is None:
        return False
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
            return True
    return False


def _collect_annotation_sites(tree: ast.Module):
    """Yield (lineno, col, snippet) for every annotation site that uses ``|``."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _contains_pep604_union(node.returns):
                yield (node.returns.lineno, node.returns.col_offset, f"-> {ast.unparse(node.returns)}")
            for arg in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs):
                if _contains_pep604_union(arg.annotation):
                    yield (arg.annotation.lineno, arg.annotation.col_offset,
                           f"{arg.arg}: {ast.unparse(arg.annotation)}")
        elif isinstance(node, ast.AnnAssign):
            if _contains_pep604_union(node.annotation):
                target_src = ast.unparse(node.target)
                yield (node.annotation.lineno, node.annotation.col_offset,
                       f"{target_src}: {ast.unparse(node.annotation)}")


def check_file(path: Path) -> list[str]:
    """Return a list of human-readable problems for the file (empty if clean)."""
    try:
        src = path.read_text()
    except OSError as exc:
        return [f"{path}: could not read: {exc}"]
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}: SyntaxError on line {exc.lineno}: {exc.msg}"]

    if _has_future_annotations(tree):
        return []  # safe — annotations are lazy strings

    problems = []
    for (lineno, col, snippet) in _collect_annotation_sites(tree):
        problems.append(f"{path}:{lineno}:{col}: PEP 604 union without `from __future__ import annotations`: {snippet}")
    return problems


def _staged_python_files() -> list[Path]:
    """Files staged for commit, scoped to REPO_ROOTS."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    files = []
    for line in out.splitlines():
        line = line.strip()
        if not line.endswith(".py"):
            continue
        if not any(line.startswith(root) for root in REPO_ROOTS):
            continue
        p = Path(line)
        if p.exists():
            files.append(p)
    return files


def main(argv: list[str]) -> int:
    # If args provided, treat them as explicit file paths (for manual runs / full audit).
    # Otherwise default to "all staged files" for the pre-commit hook.
    if len(argv) > 1:
        if argv[1] == "--all":
            files = []
            for root in REPO_ROOTS:
                files.extend(Path(root).rglob("*.py"))
        else:
            files = [Path(a) for a in argv[1:]]
    else:
        files = _staged_python_files()

    if not files:
        return 0

    all_problems = []
    for f in files:
        all_problems.extend(check_file(f))

    if not all_problems:
        return 0

    sys.stderr.write("REJECTED: PEP 604 union types found without `from __future__ import annotations`.\n")
    sys.stderr.write(
        "Launchd runs /usr/bin/python3 (Python 3.9.6) which evaluates annotations at def time.\n"
        "Add this line right after the module docstring:\n\n"
        "    from __future__ import annotations\n\n"
        "or remove the `|` unions in favor of Optional[...] / Union[...].\n\n"
    )
    for p in all_problems:
        sys.stderr.write(f"  {p}\n")
    sys.stderr.write("\nOverride: git commit --no-verify (only if you know what you're doing)\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
