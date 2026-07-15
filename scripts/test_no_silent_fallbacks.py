#!/usr/bin/env python3
"""Regression test for two recurring bug classes documented across many past
incidents (X handle -> hardcoded "m13v_" impersonation, DEFAULT_ACCOUNTS,
bare "python3" reply/post subprocess call sites that lack Playwright on a
fresh install): a missing config value silently substituting a plausible
(but wrong, or crash-prone) default instead of failing loudly.

Two checks:

1. check_no_bare_playwright_subprocess() -- static scan of scripts/*.py and
   seo/*.py for `subprocess.run/check_output/check_call/Popen(["python3", ...])`
   call sites whose target script imports playwright. A script that needs
   Playwright must be launched via the pinned interpreter
   (`PYTHON = os.environ.get("S4L_PYTHON") or sys.executable`, see
   scripts/twitter_post_plan.py:131), never the literal "python3" -- bare
   "python3" resolves to the caller's system Python on PATH, which has no
   Playwright on a fresh install, and the subprocess dies silently
   (no_reply_json / no_such_module errors surfaced only in production,
   see bug_twitter_reply_bare_python3_no_playwright.md).

2. check_account_resolver_hard_fails() -- account_resolver.resolve()/require()
   must return None / raise on missing config, never fall back to a real
   person's handle (the "m13v_" / DEFAULT_ACCOUNTS impersonation bug, see
   bug_twitter_handle_scrape_brittle_m13v_fallback.md and
   bug_multitenant_no_install_scoping_account_fallback.md).

Run:
    python3 scripts/test_no_silent_fallbacks.py
Exit 0 = all pass; non-zero with FAIL lines otherwise.
"""
from __future__ import annotations

import ast
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = [os.path.join(REPO_ROOT, "scripts"), os.path.join(REPO_ROOT, "seo")]
SUBPROCESS_FUNCS = {"run", "check_output", "check_call", "Popen", "call"}

# file:line entries explicitly reviewed and confirmed safe to leave as bare
# "python3" (target does not import playwright, or the target path could not
# be resolved statically and was hand-verified). Every entry must carry a
# one-line reason -- this is a reviewed exception list, not a way to silence
# noise.
ALLOWLIST = {
    # e.g. "scripts/some_script.py:42": "targets log_run.py, no playwright import",
}

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# Check 1: bare "python3" spawning a playwright-dependent script
# ---------------------------------------------------------------------------

def _imports_playwright(tree):
    """True iff the AST contains a real `import playwright[...]` or
    `from playwright[...] import ...` statement, anywhere in the file
    (module-level or nested inside a function). Deliberately AST-based, not
    a substring search on the source text -- several scripts (engage_reddit.py,
    post_reddit.py, twitter_post_plan.py) contain the STRING "import
    playwright" as a `python -c "import playwright"` preflight-check
    argument without ever importing it in their own process; a text search
    misclassifies those as playwright-dependent."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "playwright" or alias.name.startswith("playwright.") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "playwright" or node.module.startswith("playwright.")):
                return True
    return False


def _find_playwright_dependent_scripts():
    """Basename -> True for every scanned .py file that actually imports
    playwright (module-level or inside a function -- either way the
    interpreter running that file needs Playwright installed)."""
    dependent = set()
    for d in SCAN_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".py"):
                continue
            path = os.path.join(d, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    src = f.read()
                tree = ast.parse(src, filename=path)
            except (OSError, SyntaxError):
                continue
            if _imports_playwright(tree):
                dependent.add(name)
    return dependent


def _const_str(node, literals):
    """Best-effort static string resolution: literals, known module-level
    names, and os.path.join(...)/os.path.expanduser(...) of resolvable
    parts. Not full data-flow analysis -- anything else resolves to None
    and is reported separately, never silently treated as safe."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return literals.get(node.id)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "join":
            parts = [_const_str(a, literals) for a in node.args]
            if all(p is not None for p in parts):
                return os.path.join(*parts)
        if isinstance(func, ast.Attribute) and func.attr == "expanduser":
            if node.args:
                inner = _const_str(node.args[0], literals)
                if inner is not None:
                    return os.path.expanduser(inner)
    return None


def _resolve_module_literals(tree):
    literals = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            val = _const_str(node.value, literals)
            if val is not None:
                literals[node.targets[0].id] = val
    return literals


def _scan_file(path, playwright_scripts, fails, notes):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        return
    literals = _resolve_module_literals(tree)
    rel = os.path.relpath(path, REPO_ROOT)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_subprocess_call = (
            isinstance(func, ast.Attribute)
            and func.attr in SUBPROCESS_FUNCS
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )
        if not is_subprocess_call or not node.args:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, ast.List) or not first_arg.elts:
            continue
        interp = first_arg.elts[0]
        if not (isinstance(interp, ast.Constant) and interp.value in ("python3", "python")):
            continue
        if len(first_arg.elts) < 2:
            continue

        key = f"{rel}:{node.lineno}"
        if key in ALLOWLIST:
            continue

        target = _const_str(first_arg.elts[1], literals)
        if target is None:
            notes.append((key, "target script path not statically resolvable -- review manually"))
            continue

        basename = os.path.basename(target)
        if basename in playwright_scripts:
            fails.append((key, f'spawns {basename} (imports playwright) via bare "{interp.value}"'))


def check_no_bare_playwright_subprocess():
    playwright_scripts = _find_playwright_dependent_scripts()
    fails, notes = [], []
    for d in SCAN_DIRS:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                _scan_file(os.path.join(d, name), playwright_scripts, fails, notes)

    print(
        f"  scanned {sum(1 for d in SCAN_DIRS for _ in os.listdir(d) if _.endswith('.py')) if all(os.path.isdir(d) for d in SCAN_DIRS) else '?'} "
        f"files; {len(playwright_scripts)} playwright-dependent scripts: {', '.join(sorted(playwright_scripts))}"
    )
    for key, reason in notes:
        print(f"  NOTE  {key}  -- {reason}")

    check("no bare-python3 subprocess call targets a playwright-dependent script", not fails,
          f"{len(fails)} violation(s)" if fails else "")
    for key, reason in fails:
        print(f"        FAIL  {key}  -- {reason}")
    if fails:
        print(
            '        fix: route through PYTHON = os.environ.get("S4L_PYTHON") or sys.executable '
            "instead of the literal \"python3\" (pattern: scripts/twitter_post_plan.py:131)"
        )


# ---------------------------------------------------------------------------
# Check 2: account_resolver must hard-fail, never impersonate
# ---------------------------------------------------------------------------

def check_account_resolver_hard_fails():
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    import account_resolver  # noqa: E402

    saved_load_config = account_resolver._load_config
    saved_env = dict(os.environ)
    try:
        # Strip every account-related env var + simulate an empty config.json
        # (no accounts section at all -- the "fresh install, nothing
        # configured yet" state).
        for k in list(os.environ):
            if k.startswith("AUTOPOSTER_"):
                del os.environ[k]
        account_resolver._load_config = lambda: {}

        for platform in ("twitter", "reddit", "linkedin", "github", "moltbook"):
            result = account_resolver.resolve(platform)
            check(f'resolve("{platform}") with no config returns None (not a hardcoded fallback)',
                  result is None, f"got {result!r}")

        raised = False
        try:
            account_resolver.require("twitter")
        except RuntimeError:
            raised = True
        check('require("twitter") with no config raises RuntimeError (hard-fail, never impersonate)', raised)

        # Positive case: a real configured value still resolves correctly,
        # so this test can't be satisfied by breaking resolve() outright.
        account_resolver._load_config = lambda: {"accounts": {"twitter": {"handle": "@some_handle"}}}
        result = account_resolver.resolve("twitter")
        check('resolve("twitter") returns the configured handle when config IS present',
              result == "some_handle", f"got {result!r}")
    finally:
        account_resolver._load_config = saved_load_config
        os.environ.clear()
        os.environ.update(saved_env)


def main():
    print("-- check 1: no bare-python3 subprocess spawning a playwright-dependent script --")
    check_no_bare_playwright_subprocess()
    print("-- check 2: account_resolver hard-fails instead of impersonating --")
    check_account_resolver_hard_fails()

    if FAILS:
        print(f"\n{len(FAILS)} FAILURE(S):")
        for name in FAILS:
            print(f"  - {name}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
