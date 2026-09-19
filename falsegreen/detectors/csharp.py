"""C# (.NET) detector - NUnit, xUnit, MSTest, SpecFlow, FluentAssertions, and Moq.

C# test suites across enterprise .NET projects rely on NUnit ([Test]),
xUnit ([Fact], [Theory]), MSTest ([TestMethod]), and BDD tools like SpecFlow
([Given], [When], [Then]).

Key anti-patterns handled:
- Methods decorated with test attributes containing zero assertions
- Swallowed exceptions in `try { ... } catch (Exception) { }`
- Dangling FluentAssertions calls (`result.Should();` with no matcher)
- Tautological assertions (e.g. `Assert.True(true)`, `Assert.Equal(1, 1)`, `true.Should().BeTrue()`)
- Thread.Sleep / Task.Delay fixed sleeps
- Disabled tests ([Ignore], [Fact(Skip = "...")], [Test(Explicit = true)])
- Assertion-free verify/check helpers
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from ..models import Finding, Rule, ScanResult, Severity, TestCase

# --------------------------------------------------------------------------
# Source normalisation
# --------------------------------------------------------------------------


def blank_noncode(src: str) -> str:
    """Replace comments and string literals with spaces, preserving offsets and newlines."""
    out = list(src)
    i, n = 0, len(src)

    while i < n:
        ch = src[i]

        # // line comment
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue

        # /* block comment */
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            for _ in range(2):
                if i < n:
                    out[i] = " "
                    i += 1
            continue

        # @"" or $@" verbatim / raw string literals
        if (ch == "@" and i + 1 < n and src[i + 1] == '"') or (src.startswith('$@"', i) or src.startswith('@$"', i)):
            offset = 3 if (src.startswith('$@"', i) or src.startswith('@$"', i)) else 2
            for _ in range(offset):
                if i < n:
                    out[i] = " "
                    i += 1
            while i < n:
                if src[i] == '"':
                    if i + 1 < n and src[i + 1] == '"': # escaped quote in verbatim
                        out[i] = " "
                        out[i + 1] = " "
                        i += 2
                        continue
                    else:
                        out[i] = " "
                        i += 1
                        break
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            continue

        # "string" or 'char'
        if ch in '"\'':
            quote = ch
            out[i] = " "
            i += 1
            while i < n and src[i] != quote:
                if src[i] == "\\":
                    out[i] = " "
                    i += 1
                if i < n:
                    if src[i] != "\n":
                        out[i] = " "
                    i += 1
            if i < n:
                out[i] = " "
                i += 1
            continue

        i += 1

    return "".join(out)


def match_block(code: str, open_idx: int) -> int:
    """Index just past the `}` closing the block that opens at `open_idx`."""
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(code)


def line_of(src: str, index: int) -> int:
    return src.count("\n", 0, index) + 1


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

#: [Test], [TestCase], [Fact], [Theory], [TestMethod], [DataTestMethod], [Given], [When], [Then], [StepDefinition]
TEST_ATTR_RE = re.compile(
    r"\[\s*(?:(?:NUnit\.Framework\.|Xunit\.|Microsoft\.VisualStudio\.TestTools\.UnitTesting\.|TechTalk\.SpecFlow\.|Reqnroll\.)?"
    r"(?:Test|TestCase|TestCaseSource|Fact|Theory|TestMethod|DataTestMethod|Given|When|Then|StepDefinition))"
    r"(?:\([^\)]*\))?\s*\]"
)

#: Method declaration signature following attributes
METHOD_SIG_RE = re.compile(
    r"(?:(?:public|protected|private|internal|static|async|virtual|override|abstract)\s+)*"
    r"(?:(?:async\s+)?(?:Task(?:<[^>]+>)?|void|[\w.<>\[\],\s?]+))\s+"
    r"(?P<name>\w+)\s*\("
)

#: Standard and Fluent / Mock Assertions
ASSERTION_RE = re.compile(
    r"\b(?:"
    r"Assert\s*\.\s*\w+"                           # Assert.That, Assert.AreEqual, Assert.True, Assert.Multiple
    r"|StringAssert\s*\.\s*\w+"
    r"|CollectionAssert\s*\.\s*\w+"
    r"|FileAssert\s*\.\s*\w+"
    r"|DirectoryAssert\s*\.\s*\w+"
    r"|\.Should\s*\(\s*\)\s*\.\s*\w+"         # FluentAssertions: .Should().Be(...)
    r"|\.ShouldBe\w*\s*\("                        # Shouldly: .ShouldBe(x)
    r"|\.ShouldNotBe\w*\s*\("
    r"|\.ShouldThrow\w*\s*\("
    r"|\.Verify\s*\("                              # Moq: mock.Verify(x => ...)
    r"|\.VerifyAll\s*\("
    r"|\.VerifyNoOtherCalls\s*\("
    r"|\.Received\s*(?:<[^>]+>)?\s*\("             # NSubstitute: sub.Received().Method()
    r"|\.DidNotReceive\s*(?:<[^>]+>)?\s*\("
    r"|A\.CallTo\s*\("                             # FakeItEasy: A.CallTo(...)
    r"|\.MustHaveHappened\w*\s*\("
    r")"
)

#: Dangling .Should() call without an assertion matcher
DANGLING_SHOULD_RE = re.compile(r"\.Should\s*\(\s*\)\s*;")

#: Catch clause in C#
CATCH_RE = re.compile(r"\bcatch\s*(?:\(\s*(?P<type>[\w.]+)(?:\s+\w+)?\s*\))?\s*\{")

#: Rethrow or explicit assertion failure
RETHROW_RE = re.compile(r"\b(?:throw\b|Assert\s*\.\s*Fail|Assert\s*\.\s*Inconclusive)")

#: Fixed wait: Thread.Sleep or Task.Delay
SLEEP_RE = re.compile(r"\b(?:Thread\s*\.\s*Sleep|Task\s*\.\s*Delay)\s*\(")

#: Disabled attributes: [Ignore], [Fact(Skip = "...")], [Theory(Skip = "...")], [Test(Explicit = true)]
DISABLED_ATTR_RE = re.compile(
    r"\[\s*(?:Ignore|Fact\s*\([^\)]*Skip|Theory\s*\([^\)]*Skip|Test\s*\([^\)]*Explicit\s*=\s*true)"
)

#: Tautological assertions: Assert.True(true), Assert.That(true, Is.True), Assert.Equal(1, 1), etc.
TAUTOLOGY_RE = re.compile(
    r"\b(?:"
    r"Assert\s*\.\s*(?:True|IsTrue)\s*\(\s*true\s*\)"
    r"|Assert\s*\.\s*(?:False|IsFalse)\s*\(\s*false\s*\)"
    r"|Assert\s*\.\s*That\s*\(\s*true\s*,\s*Is\s*\.\s*True\s*\)"
    r"|Assert\s*\.\s*That\s*\(\s*false\s*,\s*Is\s*\.\s*False\s*\)"
    r"|Assert\s*\.\s*(?:Equal|AreEqual)\s*\(\s*(\w+)\s*,\s*\1\s*\)"
    r"|true\s*\.\s*Should\s*\(\s*\)\s*\.\s*BeTrue\s*\(\s*\)"
    r"|false\s*\.\s*Should\s*\(\s*\)\s*\.\s*BeFalse\s*\(\s*\)"
    r")"
)

#: If condition without else
IF_RE = re.compile(r"\bif\s*\(")

#: Helper method name pattern
HELPER_CHECK_NAME_RE = re.compile(r"^(?:Verify|Check|Assert|Validate|Ensure|Confirm)[A-Z_]")


def scan_csharp_file(path: Path, result: ScanResult) -> None:
    """Scan a C# (.cs) test file for test suite anti-patterns."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        result.errors.append(f"{path}: {err}")
        return

    result.files_scanned += 1
    code = blank_noncode(src)

    # 1. Scan for test methods via attributes
    for match in TEST_ATTR_RE.finditer(code):
        attr_start = match.start()
        attr_end = match.end()

        # Look ahead for method signature
        sig_match = METHOD_SIG_RE.search(code[attr_end : attr_end + 300])
        if not sig_match:
            continue

        method_name = sig_match.group("name")
        method_sig_start = attr_end + sig_match.start()
        line_num = line_of(src, method_sig_start)

        # Find body opening brace
        body_open = code.find("{", attr_end + sig_match.end())
        if body_open == -1 or body_open - (attr_end + sig_match.end()) > 100:
            continue

        body_end = match_block(code, body_open)
        body_code = code[body_open:body_end]
        body_src = src[body_open:body_end]

        # Check if disabled
        attr_block = src[max(0, attr_start - 100) : attr_end]
        is_disabled = bool(DISABLED_ATTR_RE.search(attr_block))
        if is_disabled:
            result.tests.append(
                TestCase(
                    name=method_name,
                    file=path,
                    line=line_num,
                    effective_assertions=0,
                    is_disabled=True,
                )
            )
            result.findings.append(
                Finding(
                    rule=Rule.DISABLED_TEST,
                    severity=Severity.LOW,
                    file=path,
                    line=line_num,
                    test_name=method_name,
                    detail=f"Test '{method_name}' is disabled or skipped via attribute.",
                    snippet=src[attr_start : min(len(src), body_open + 20)].strip(),
                    suggested_fix="Enable test or track skip reason to avoid permanent test rot.",
                )
            )
            continue

        # Check for dangling .Should()
        for d_match in DANGLING_SHOULD_RE.finditer(body_code):
            d_line = line_of(src, body_open + d_match.start())
            result.findings.append(
                Finding(
                    rule=Rule.DANGLING_EXPECT,
                    severity=Severity.CRITICAL,
                    file=path,
                    line=d_line,
                    test_name=method_name,
                    detail="Dangling `.Should();` call without assertion matcher.",
                    snippet=body_src[max(0, d_match.start() - 20) : d_match.end() + 20].strip(),
                    suggested_fix="Append a matcher, e.g. `.Should().Be(expected);` or `.Should().NotBeNull();`.",
                )
            )

        # Check for tautological assertions
        for t_match in TAUTOLOGY_RE.finditer(body_code):
            t_line = line_of(src, body_open + t_match.start())
            result.findings.append(
                Finding(
                    rule=Rule.TAUTOLOGICAL_ASSERTION,
                    severity=Severity.HIGH,
                    file=path,
                    line=t_line,
                    test_name=method_name,
                    detail="Assertion tests a constant condition that is always true.",
                    snippet=body_src[t_match.start() : t_match.end()].strip(),
                    suggested_fix="Assert against actual dynamic test state instead of constant values.",
                )
            )

        # Check for fixed sleep
        for s_match in SLEEP_RE.finditer(body_code):
            s_line = line_of(src, body_open + s_match.start())
            result.findings.append(
                Finding(
                    rule=Rule.SLEEP_INSTEAD_OF_WAIT,
                    severity=Severity.MEDIUM,
                    file=path,
                    line=s_line,
                    test_name=method_name,
                    detail="Fixed sleep used instead of polling / wait condition.",
                    snippet=body_src[s_match.start() : min(len(body_src), s_match.end() + 30)].strip(),
                    suggested_fix="Replace with explicit wait or polling retry condition.",
                )
            )

        # Check for swallowed exceptions
        for c_match in CATCH_RE.finditer(body_code):
            catch_open = body_code.find("{", c_match.start())
            if catch_open != -1:
                catch_end = match_block(body_code, catch_open)
                catch_handler = body_code[catch_open:catch_end]
                if not RETHROW_RE.search(catch_handler):
                    c_line = line_of(src, body_open + c_match.start())
                    result.findings.append(
                        Finding(
                            rule=Rule.SWALLOWED_ASSERTION,
                            severity=Severity.CRITICAL,
                            file=path,
                            line=c_line,
                            test_name=method_name,
                            detail="Catch block suppresses test failure without rethrowing or failing.",
                            snippet=body_src[c_match.start() : min(len(body_src), catch_end)].strip(),
                            suggested_fix="Rethrow exception or call Assert.Fail(ex.Message).",
                        )
                    )

        # Count assertions
        assertions = list(ASSERTION_RE.finditer(body_code))
        effective_assertions = len(assertions)

        if effective_assertions == 0:
            result.findings.append(
                Finding(
                    rule=Rule.NO_ASSERTIONS,
                    severity=Severity.CRITICAL,
                    file=path,
                    line=line_num,
                    test_name=method_name,
                    detail=f"Test method '{method_name}' contains zero assertions.",
                    snippet=src[method_sig_start : min(len(src), body_open + 50)].strip(),
                    suggested_fix="Add Assert.That(...), Assert.Equal(...), or .Should() verification.",
                )
            )

        result.tests.append(
            TestCase(
                name=method_name,
                file=path,
                line=line_num,
                effective_assertions=effective_assertions,
                is_disabled=False,
            )
        )
