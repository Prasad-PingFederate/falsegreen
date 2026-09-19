"""Trust Score.

One number, defined narrowly enough to defend: the share of tests that can
actually fail. It is not a quality score and does not pretend to be - a suite of
shallow-but-honest tests scores 100, and that is correct. The claim is only
"these tests report real results", which is the precondition for every other
claim a suite makes about itself.

Defensibility matters more than sophistication here. A score someone can argue
with in a code review is worth more than a weighted index nobody can explain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .models import ScanResult, Severity


@dataclass
class Score:
    value: int              # 0-100
    grade: str              # A-F
    trustworthy: int
    total: int
    headline: str
    detail: str

    @property
    def is_failing(self) -> bool:
        return self.value < 70


def compute(result: ScanResult) -> Score:
    total = result.total_tests

    if total == 0:
        return Score(
            value=100,
            grade="-",
            trustworthy=0,
            total=0,
            headline="No tests found",
            detail=(
                "Nothing was scanned. Point falsegreen at a directory containing test "
                "files, or widen --include."
            ),
        )

    trustworthy = sum(1 for t in result.tests if t.is_trustworthy)
    value = round(100 * trustworthy / total)

    criticals = len(result.by_severity(Severity.CRITICAL))
    highs = len(result.by_severity(Severity.HIGH))

    if value >= 95 and criticals == 0:
        grade = "A"
    elif value >= 85:
        grade = "B"
    elif value >= 70:
        grade = "C"
    elif value >= 50:
        grade = "D"
    else:
        grade = "F"

    broken = total - trustworthy
    if broken == 0:
        headline = f"All {total} tests can fail."
        detail = "Every test has at least one assertion that is allowed to propagate."
    else:
        pct = round(100 * broken / total)
        headline = f"{broken} of {total} tests ({pct}%) cannot fail."
        detail = (
            f"{criticals} critical and {highs} high-severity findings. Those tests "
            f"contribute green ticks without checking anything, so the suite reports "
            f"a pass rate it has not earned."
        )

    return Score(
        value=value,
        grade=grade,
        trustworthy=trustworthy,
        total=total,
        headline=headline,
        detail=detail,
    )
