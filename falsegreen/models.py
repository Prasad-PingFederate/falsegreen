"""Core types.

A "finding" here is always the same claim: this test cannot fail, or cannot
fail for the reason it was written. That is a narrower and far more defensible
claim than "this test is bad", and it is the whole product. Everything that
cannot be stated that way belongs in a linter, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


class Severity(str, Enum):
    CRITICAL = "critical"   # the test provably cannot fail
    HIGH = "high"           # the test can fail, but not for its stated reason
    MEDIUM = "medium"       # weakens the signal; real bugs can slip through
    LOW = "low"             # smell; worth knowing, not worth blocking a build

    @property
    def rank(self) -> int:
        return {"critical": 0, "high": 1, "medium": 2, "low": 3}[self.value]


class Rule(str, Enum):
    NO_ASSERTIONS = "no-assertions"
    SWALLOWED_ASSERTION = "swallowed-assertion"
    BLANKET_TRY_EXCEPT = "blanket-try-except"
    TAUTOLOGICAL_ASSERTION = "tautological-assertion"
    DANGLING_EXPECT = "dangling-expect"
    UNAWAITED_EXPECT = "unawaited-expect"
    ASSERTION_FREE_HELPER = "assertion-free-helper"
    SLEEP_INSTEAD_OF_WAIT = "sleep-instead-of-wait"
    DISABLED_TEST = "disabled-test"
    FORCED_INTERACTION = "forced-interaction"
    SELF_GUARDED_ASSERTION = "self-guarded-assertion"
    OPTIONAL_ASSERTION = "optional-assertion"
    MOCK_ASSERTION_TYPO = "mock-assertion-typo"
    CONSTANT_CONDITION_TRAP = "constant-condition-trap"
    BROAD_RAISES = "broad-raises"
    LOOP_ONLY_ASSERTION = "loop-only-assertion"
    IGNORED_PREDICATE_CALL = "ignored-predicate-call"
    UNFINALIZED_SOFT_ASSERT = "unfinalized-soft-assert"
    EMPTY_STRING_ASSERTION = "empty-string-assertion"
    UNAWAITED_ASYNC_CALL = "unawaited-async-call"


RULE_TITLES = {
    Rule.NO_ASSERTIONS: "Test contains no assertions",
    Rule.SWALLOWED_ASSERTION: "Assertion is swallowed by except",
    Rule.BLANKET_TRY_EXCEPT: "Whole test body wrapped in a swallowing try/except",
    Rule.TAUTOLOGICAL_ASSERTION: "Assertion is always true",
    Rule.DANGLING_EXPECT: "expect(...) with no matcher - asserts nothing",
    Rule.UNAWAITED_EXPECT: "Async expect(...) is never awaited",
    Rule.ASSERTION_FREE_HELPER: "Named like a check but never asserts",
    Rule.SLEEP_INSTEAD_OF_WAIT: "Fixed sleep used instead of a wait condition",
    Rule.DISABLED_TEST: "Test is skipped or disabled",
    Rule.FORCED_INTERACTION: "Forced interaction with no verification after it",
    Rule.SELF_GUARDED_ASSERTION: "Assertion guarded by the condition it asserts",
    Rule.OPTIONAL_ASSERTION: "Assertion runs only on one branch; the other passes silently",
    Rule.MOCK_ASSERTION_TYPO: "Mock assertion typo or missing parentheses",
    Rule.CONSTANT_CONDITION_TRAP: "Assertion condition contains truthy constant that is always true",
    Rule.BROAD_RAISES: "pytest.raises catches broad Exception or BaseException",
    Rule.LOOP_ONLY_ASSERTION: "Assertion only inside loop; passes silently if collection is empty",
    Rule.IGNORED_PREDICATE_CALL: "Boolean predicate helper called without assert (result discarded)",
    Rule.UNFINALIZED_SOFT_ASSERT: "Soft assertions collected but never finalized with assert_all()",
    Rule.EMPTY_STRING_ASSERTION: "Empty string substring assertion is tautological (always true)",
}

RULE_EXPLANATIONS = {
    Rule.NO_ASSERTIONS: (
        "The test runs code and then ends. It passes as long as nothing throws, so it "
        "cannot distinguish correct behaviour from wrong behaviour - only from a crash."
    ),
    Rule.SWALLOWED_ASSERTION: (
        "The assertion raises AssertionError, and an enclosing except catches it and "
        "does not re-raise. The failure the assertion exists to report is discarded, so "
        "the test reports success either way."
    ),
    Rule.BLANKET_TRY_EXCEPT: (
        "Every statement in the test sits inside a try whose except swallows. Nothing "
        "in the body can fail the test - not the assertions, not even a crash."
    ),
    Rule.TAUTOLOGICAL_ASSERTION: (
        "The condition is true by construction, so the assertion holds regardless of "
        "what the code under test did."
    ),
    Rule.DANGLING_EXPECT: (
        "expect(x) on its own builds an assertion object and discards it. Without a "
        "matcher call nothing is ever checked."
    ),
    Rule.UNAWAITED_EXPECT: (
        "The matcher returns a promise that is never awaited. The assertion resolves "
        "after the test has already finished, so a failure cannot fail the test."
    ),
    Rule.ASSERTION_FREE_HELPER: (
        "A helper named assert_/verify_/check_/expect_ that never asserts. Every caller "
        "reads as verified while nothing is verified."
    ),
    Rule.SLEEP_INSTEAD_OF_WAIT: (
        "A fixed sleep substitutes for a real wait condition. It is the dominant source "
        "of flakes, and when it is too short the assertion after it races the app."
    ),
    Rule.DISABLED_TEST: (
        "The test is skipped, so it contributes a green tick while covering nothing. "
        "Worth tracking so skips do not quietly become permanent."
    ),
    Rule.FORCED_INTERACTION: (
        "A forced click bypasses the checks that prove the element was actually "
        "interactive, and nothing after it verifies the click landed. A no-op click and "
        "a successful one are indistinguishable."
    ),
    Rule.SELF_GUARDED_ASSERTION: (
        "The assertion only runs when its own condition is already true - "
        "`if is_visible(x): expect(x).to_be_visible()`. It is a tautology spread over "
        "two lines: true branch asserts something already established, false branch "
        "skips silently. The element going missing is the bug it was written to catch, "
        "and that is exactly the case it ignores."
    ),
    Rule.OPTIONAL_ASSERTION: (
        "Every assertion in this test sits inside a conditional with no failing "
        "alternative. When the condition is false the test reaches the end and passes "
        "having verified nothing, which is indistinguishable from a real pass in CI."
    ),
    Rule.MOCK_ASSERTION_TYPO: (
        "A mock assertion was accessed as an attribute without parentheses (e.g. "
        "`mock.assert_called_once`), or a misspelled assertion method was called "
        "(e.g. `mock.assert_called_with_once`). On standard Mock objects, this "
        "evaluates as a truthy mock attribute and never raises an error, leaving the "
        "test permanently green without verifying the mock."
    ),
    Rule.CONSTANT_CONDITION_TRAP: (
        "The assertion condition uses `or` with a truthy constant (e.g. "
        "`assert status == 200 or 201`). In Python, truthy constants like non-zero "
        "numbers or non-empty strings make the entire expression always true, so "
        "the assertion can never fail."
    ),
    Rule.BROAD_RAISES: (
        "pytest.raises(Exception) or pytest.raises(BaseException) catches every possible "
        "failure, including syntax errors, KeyError, AttributeError, or unrelated crashes "
        "in the test harness rather than the specific exception expected."
    ),
    Rule.LOOP_ONLY_ASSERTION: (
        "All assertions in this test sit exclusively inside a for-loop body. If the "
        "collection is empty, the loop body never executes, zero assertions are run, "
        "and the test passes green."
    ),
    Rule.IGNORED_PREDICATE_CALL: (
        "A predicate helper method (such as `is_visible()`, `has_row()`, or `is_logged_in()`) "
        "was invoked as a standalone statement without `assert`. The method returned a boolean "
        "which Python discarded, so the test passes regardless of whether the check was True or False."
    ),
    Rule.UNFINALIZED_SOFT_ASSERT: (
        "Soft assertions (such as `check.equal()` or `soft_asserts.append()`) were performed, "
        "but the test exited without calling `assert_all()` or `verify_all()`. Any failures "
        "recorded during the test are silently ignored."
    ),
    Rule.UNAWAITED_ASYNC_CALL: (
        "In an async test, calling an async Playwright or coroutine method (like goto, click, fill) without await creates an unexecuted coroutine, so the intended action never runs while the test passes silently."
    ),
    Rule.EMPTY_STRING_ASSERTION: (
        "The assertion checks `assert '' in string` or `assert expected in string` where `expected` "
        "is empty. In Python and JavaScript, an empty string is present in every string, making "
        "the assertion tautological and unable to fail."
    ),
}


@dataclass
class Finding:
    rule: Rule
    severity: Severity
    file: Path
    line: int
    test_name: str
    detail: str = ""
    snippet: str = ""
    suggested_fix: str = ""

    @property
    def title(self) -> str:
        return RULE_TITLES[self.rule]

    @property
    def explanation(self) -> str:
        return RULE_EXPLANATIONS[self.rule]

    def location(self, root: Optional[Path] = None) -> str:
        path = self.file
        if root:
            try:
                path = self.file.relative_to(root)
            except ValueError:
                pass
        return f"{path}:{self.line}"


@dataclass
class TestCase:
    name: str
    file: Path
    line: int
    effective_assertions: int = 0
    is_disabled: bool = False

    @property
    def is_trustworthy(self) -> bool:
        return self.effective_assertions > 0 and not self.is_disabled


@dataclass
class ScanResult:
    root: Path
    tests: List[TestCase] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    files_scanned: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def total_tests(self) -> int:
        return len(self.tests)

    @property
    def untrustworthy_tests(self) -> int:
        return sum(1 for t in self.tests if not t.is_trustworthy)

    def by_severity(self, severity: Severity) -> List[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def sorted_findings(self) -> List[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (f.severity.rank, str(f.file), f.line),
        )
