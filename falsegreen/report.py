"""Output renderers: terminal, markdown, JSON, SARIF.

The terminal view is the demo. Someone runs this on their own repo for the first
time and either gets a jolt of recognition or closes the tab - so it leads with
the number and the worst offenders, not with configuration or methodology.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .models import Finding, Rule, ScanResult, Severity
from .score import Score

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
CYAN = "\033[36m"

SEV_COLOR = {
    Severity.CRITICAL: RED,
    Severity.HIGH: YELLOW,
    Severity.MEDIUM: CYAN,
    Severity.LOW: DIM,
}


def _c(text: str, color: str, enabled: bool) -> str:
    return f"{color}{text}{RESET}" if enabled else text


def terminal(result: ScanResult, score: Score, color: bool = True, limit: int = 25) -> str:
    out: List[str] = []
    add = out.append

    add("")
    add(_c("  falsegreen", BOLD, color) + _c("  tests that cannot fail", DIM, color))
    add("")

    grade_color = GREEN if score.value >= 85 else (YELLOW if score.value >= 70 else RED)
    add(f"  Trust Score  {_c(str(score.value) + '/100', BOLD + grade_color, color)}   grade {score.grade}")
    add(f"  {score.headline}")
    add("")

    if not result.findings:
        add(_c("  Nothing found. Every test has an assertion that can propagate.", GREEN, color))
        add("")
        return "\n".join(out)

    counts = {s: len(result.by_severity(s)) for s in Severity}
    summary = "  ".join(
        _c(f"{counts[s]} {s.value}", SEV_COLOR[s], color)
        for s in Severity
        if counts[s]
    )
    add(f"  {summary}")
    add("")

    shown = result.sorted_findings()[:limit]
    for f in shown:
        sev = _c(f.severity.value.upper().ljust(8), SEV_COLOR[f.severity], color)
        loc = _c(f.location(result.root), CYAN, color)
        add(f"  {sev} {f.title}")
        add(f"           {loc}  in {_c(f.test_name, BOLD, color)}")
        if f.detail:
            add(f"           {_c(f.detail, DIM, color)}")
        if f.snippet:
            add(f"           {_c('| ' + f.snippet, DIM, color)}")
        if getattr(f, "suggested_fix", ""):
            add(f"           {_c('+ fix: ' + f.suggested_fix, GREEN, color)}")
        add("")

    remaining = len(result.findings) - len(shown)
    if remaining > 0:
        add(_c(f"  ... and {remaining} more. Use --format markdown for the full list.", DIM, color))
        add("")

    if result.errors:
        add(_c(f"  {len(result.errors)} file(s) could not be parsed.", YELLOW, color))
        add("")

    return "\n".join(out)


def markdown(result: ScanResult, score: Score) -> str:
    out: List[str] = []
    add = out.append

    add("# falsegreen report")
    add("")
    add(f"**Trust Score: {score.value}/100 (grade {score.grade})**")
    add("")
    add(score.headline)
    add("")
    add(score.detail)
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| Files scanned | {result.files_scanned} |")
    add(f"| Tests found | {result.total_tests} |")
    add(f"| Tests that can fail | {score.trustworthy} |")
    add(f"| Tests that cannot fail | {result.untrustworthy_tests} |")
    add(f"| Findings | {len(result.findings)} |")
    add("")

    if not result.findings:
        add("No findings.")
        return "\n".join(out)

    for severity in Severity:
        items = [f for f in result.sorted_findings() if f.severity == severity]
        if not items:
            continue

        add(f"## {severity.value.capitalize()} ({len(items)})")
        add("")
        add("| Location | Test | Problem |")
        add("|---|---|---|")
        for f in items:
            add(
                f"| `{f.location(result.root)}` "
                f"| `{f.test_name}` "
                f"| {f.title}{(' - ' + f.detail) if f.detail else ''} |"
            )
        add("")

    add("## What these mean")
    add("")
    seen = {f.rule for f in result.findings}
    for rule in Rule:
        if rule in seen:
            from .models import RULE_TITLES, RULE_EXPLANATIONS
            add(f"**{RULE_TITLES[rule]}** - {RULE_EXPLANATIONS[rule]}")
            add("")

    return "\n".join(out)


def as_json(result: ScanResult, score: Score) -> str:
    payload = {
        "trust_score": score.value,
        "grade": score.grade,
        "summary": {
            "files_scanned": result.files_scanned,
            "tests_found": result.total_tests,
            "tests_that_can_fail": score.trustworthy,
            "tests_that_cannot_fail": result.untrustworthy_tests,
            "findings": len(result.findings),
        },
        "findings": [
            {
                "rule": f.rule.value,
                "severity": f.severity.value,
                "file": str(f.file.relative_to(result.root)) if _under(f.file, result.root) else str(f.file),
                "line": f.line,
                "test": f.test_name,
                "title": f.title,
                "detail": f.detail,
                "snippet": f.snippet,
                "suggested_fix": getattr(f, "suggested_fix", ""),
            }
            for f in result.sorted_findings()
        ],
        "errors": result.errors,
    }
    return json.dumps(payload, indent=2)


def sarif(result: ScanResult, score: Score) -> str:
    """SARIF so findings surface inline in the GitHub PR diff."""
    rules = {}
    for f in result.findings:
        rules.setdefault(
            f.rule.value,
            {
                "id": f.rule.value,
                "name": f.rule.value.replace("-", " ").title().replace(" ", ""),
                "shortDescription": {"text": f.title},
                "fullDescription": {"text": f.explanation},
                "defaultConfiguration": {
                    "level": "error" if f.severity in (Severity.CRITICAL, Severity.HIGH) else "warning"
                },
            },
        )

    results_list = []
    for f in result.sorted_findings():
        uri = (
            str(f.file.relative_to(result.root)).replace("\\", "/")
            if _under(f.file, result.root)
            else str(f.file).replace("\\", "/")
        )
        entry = {
            "ruleId": f.rule.value,
            "level": "error" if f.severity in (Severity.CRITICAL, Severity.HIGH) else "warning",
            "message": {"text": f"{f.title} in {f.test_name}. {f.detail}".strip()},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": uri},
                        "region": {
                            "startLine": max(1, f.line),
                            **({"snippet": {"text": f.snippet}} if f.snippet else {}),
                        },
                    }
                }
            ],
        }
        if getattr(f, "suggested_fix", ""):
            entry["fixes"] = [
                {
                    "description": {"text": f"Suggested fix: {f.suggested_fix}"},
                    "fileChanges": [
                        {
                            "artifactLocation": {"uri": uri},
                            "replacements": [
                                {
                                    "deletedRegion": {"startLine": max(1, f.line)},
                                    "insertedContent": {"text": f.suggested_fix},
                                }
                            ],
                        }
                    ],
                }
            ]
        results_list.append(entry)

    payload = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "falsegreen",
                        "informationUri": "https://github.com/Prasad-PingFederate/falsegreen",
                        "rules": list(rules.values()),
                    }
                },
                "results": results_list,
            }
        ],
    }
    return json.dumps(payload, indent=2)


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
