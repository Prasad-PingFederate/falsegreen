"""Command line interface."""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from pathlib import Path
from typing import List

from . import report as report_mod
from .detectors.python_ast import scan_python_file
from .models import ScanResult, Severity
from .score import compute

DEFAULT_INCLUDES = [
    "test_*.py", "*_test.py", "*Test*.py", "*_Playwright.py",
    "*.spec.py", "tests/**/*.py",
]

DEFAULT_EXCLUDES = [
    "*/node_modules/*", "*/.venv/*", "*/venv/*", "*/__pycache__/*",
    "*/.git/*", "*/build/*", "*/dist/*", "*/site-packages/*",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="falsegreen",
        description="Find tests that cannot fail.",
        epilog=(
            "A green suite is only meaningful if its tests were capable of going red. "
            "falsegreen finds the ones that were not."
        ),
    )
    p.add_argument("path", nargs="?", default=".", help="File or directory to scan (default: .)")
    p.add_argument(
        "--format", "-f",
        choices=["terminal", "markdown", "json", "sarif"],
        default="terminal",
    )
    p.add_argument("--output", "-o", type=Path, help="Write the report to a file instead of stdout")
    p.add_argument(
        "--fail-under",
        type=int,
        default=None,
        metavar="N",
        help="Exit non-zero if the Trust Score is below N. Use in CI.",
    )
    p.add_argument(
        "--fail-on",
        choices=["critical", "high", "medium", "low", "never"],
        default="never",
        help="Exit non-zero if any finding at or above this severity exists.",
    )
    p.add_argument("--include", action="append", default=None, metavar="GLOB")
    p.add_argument("--exclude", action="append", default=None, metavar="GLOB")
    p.add_argument("--limit", type=int, default=25, help="Findings shown in terminal output")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--quiet", "-q", action="store_true", help="Only print the score line")
    return p


def _matches(path: Path, patterns: List[str], root: Path) -> bool:
    name = path.name
    try:
        rel = str(path.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        rel = str(path).replace(os.sep, "/")

    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
        if "**" in pattern and fnmatch.fnmatch(rel, pattern.replace("**/", "*")):
            return True
    return False


def collect_files(root: Path, includes: List[str], excludes: List[str]) -> List[Path]:
    if root.is_file():
        return [root]

    found: List[Path] = []
    for path in root.rglob("*.py"):
        full = str(path).replace(os.sep, "/")
        if any(fnmatch.fnmatch(full, pattern) for pattern in excludes):
            continue
        if _matches(path, includes, root):
            found.append(path)
    return sorted(found)


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.path).resolve()
    if not root.exists():
        sys.stderr.write(f"falsegreen: path not found: {root}\n")
        return 2

    includes = args.include or DEFAULT_INCLUDES
    excludes = (args.exclude or []) + DEFAULT_EXCLUDES

    scan_root = root if root.is_dir() else root.parent
    files = collect_files(root, includes, excludes)

    if not files:
        sys.stderr.write(
            f"falsegreen: no test files matched under {root}.\n"
            f"  Looked for: {', '.join(includes)}\n"
            f"  Use --include to widen the search.\n"
        )
        return 2

    result = ScanResult(root=scan_root)
    for path in files:
        scan_python_file(path, result)

    score = compute(result)

    if args.quiet:
        text = f"Trust Score {score.value}/100 ({score.grade}) - {score.headline}"
    elif args.format == "markdown":
        text = report_mod.markdown(result, score)
    elif args.format == "json":
        text = report_mod.as_json(result, score)
    elif args.format == "sarif":
        text = report_mod.sarif(result, score)
    else:
        color = not args.no_color and sys.stdout.isatty() and os.getenv("NO_COLOR") is None
        text = report_mod.terminal(result, score, color=color, limit=args.limit)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        sys.stdout.write(f"Report written to {args.output}\n")
    else:
        sys.stdout.write(text + "\n")

    # Also publish to the GitHub Actions run summary when present.
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary and args.format != "terminal":
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(report_mod.markdown(result, score) + "\n")
        except OSError:
            pass

    if args.fail_under is not None and score.value < args.fail_under:
        sys.stderr.write(
            f"\nfalsegreen: Trust Score {score.value} is below the required {args.fail_under}.\n"
        )
        return 1

    if args.fail_on != "never":
        threshold = Severity(args.fail_on).rank
        offending = [f for f in result.findings if f.severity.rank <= threshold]
        if offending:
            sys.stderr.write(
                f"\nfalsegreen: {len(offending)} finding(s) at or above '{args.fail_on}'.\n"
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
