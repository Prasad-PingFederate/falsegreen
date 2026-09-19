"""Kotlin detector - JUnit 5, Kotest, AssertJ, Kotlin test, and MockK.

Kotlin tests commonly appear in Android and JVM server applications utilizing
either JUnit 5 `@Test fun` methods, backtick-named functions, or Kotest
specification blocks (`test("...") { ... }`, `should("...") { ... }`).

Key anti-patterns handled:
- Test methods/blocks containing zero assertions or MockK verifications
- Swallowed assertions in catch blocks (e.g. `catch (e: Throwable) { }`)
- Kotest and JUnit tautological assertions (e.g. `true shouldBe true`, `assertEquals(1, 1)`)
- `Thread.sleep(...)` fixed waits
- Disabled tests via `@Disabled` or `@Ignore`
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

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

        # /* block comment (nested support in Kotlin) */
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            depth = 1
            i += 2
            while i < n and depth > 0:
                if src[i] == "/" and i + 1 < n and src[i + 1] == "*":
                    depth += 1
                    i += 2
                    continue
                elif src[i] == "*" and i + 1 < n and src[i + 1] == "/":
                    depth -= 1
                    i += 2
                    continue
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            continue

        # """triple-quoted raw string"""
        if src.startswith('"""', i):
            for _ in range(3):
                if i < n:
                    out[i] = " "
                    i += 1
            while i < n and not src.startswith('"""', i):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            for _ in range(3):
                if i < n:
                    out[i] = " "
                    i += 1
            continue

        # "regular string" or 'char'
        if ch in ('"', "'"):
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

#: @Test, @ParameterizedTest, @RepeatedTest
TEST_ANNOTATION_RE = re.compile(r"@(?:[\w.]+\.)?(Test|ParameterizedTest|RepeatedTest)\b")

#: Method signature following @Test: fun name() or fun `name with spaces`()
METHOD_SIG_RE = re.compile(
    r"(?:(?:public|protected|private|internal|inline|suspend|open|override|final)\s+)*"
    r"fun\s+(?:`(?P<bname>[^`]+)`|(?P<name>\w+))\s*\("
)

#: Kotest test blocks in code with blanked strings: test(...) {, should(...) {, it(...) {
KOTEST_BLOCK_RE = re.compile(
    r"\b(?:test|should|it|describe|context|feature|scenario)\s*\([^\)]*\)\s*\{"
)

#: Standard JUnit, Kotest, AssertJ, and MockK assertions
ASSERTION_RE = re.compile(
    r"\b(?:"
    r"assert[A-Z_]\w*"                                # assertEquals, assertTrue, assertThat, assertNotNull
    r"|Assert(?:ions)?\s*\.\s*\w+"                 # Assert.assertEquals, Assertions.assertThat
    r"|assertThat"
    r"|shouldBe\b|shouldNotBe\b|shouldEqual\b"      # Kotest matchers
    r"|shouldContain\b|shouldHaveSize\b|shouldThrow\b"
    r"|\.\s*shouldBe\w*\s*\("
    r"|verify\s*\{"                                  # MockK: verify { mock.call() }
    r"|verifySequence\s*\{|verifyOrder\s*\{"
    r"|confirmVerified\s*\("
    r"|fail\s*\("
    r")"
)

#: Catch clause in Kotlin
CATCH_RE = re.compile(r"\bcatch\s*\(\s*\w+\s*:\s*(?P<type>[\w.]+)\s*\)\s*\{")

#: Rethrow or fail call
RETHROW_RE = re.compile(r"\b(?:throw\b|fail\s*\(|Assert(?:ions)?\s*\.\s*fail)")

#: Fixed sleep
SLEEP_RE = re.compile(r"\b(?:Thread\s*\.\s*sleep|delay)\s*\(")

#: Disabled annotations
DISABLED_RE = re.compile(r"@(?:[\w.]+\.)?(?:Disabled|Ignore)\b")

#: Tautological assertions in Kotlin
TAUTOLOGY_RE = re.compile(
    r"\b(?:"
    r"assertEquals\s*\(\s*(\w+)\s*,\s*\1\s*\)"
    r"|assertTrue\s*\(\s*true\s*\)"
    r"|assertFalse\s*\(\s*false\s*\)"
    r"|true\s+shouldBe\s+true"
    r"|false\s+shouldBe\s+false"
    r")"
)


def scan_kotlin_file(path: Path, result: ScanResult) -> None:
    """Scan a Kotlin (.kt, .kts) file for test suite anti-patterns."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        result.errors.append(f"{path}: {err}")
        return

    result.files_scanned += 1
    code = blank_noncode(src)

    # 1. Scan for JUnit style tests
    for match in TEST_ANNOTATION_RE.finditer(code):
        anno_start = match.start()
        anno_end = match.end()

        sig_match = METHOD_SIG_RE.search(src[anno_end : anno_end + 200])
        if not sig_match:
            continue

        method_name = sig_match.group("bname") or sig_match.group("name")
        sig_start = anno_end + sig_match.start()
        line_num = line_of(src, sig_start)

        body_open = code.find("{", anno_end + sig_match.end())
        if body_open == -1 or body_open - (anno_end + sig_match.end()) > 80:
            continue

        body_end = match_block(code, body_open)
        body_code = code[body_open:body_end]
        body_src = src[body_open:body_end]

        # Check disabled
        anno_block = src[max(0, anno_start - 60) : anno_end]
        is_disabled = bool(DISABLED_RE.search(anno_block))
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
                    detail=f"Test '{method_name}' is disabled via @Disabled / @Ignore.",
                    snippet=src[anno_start : min(len(src), body_open + 20)].strip(),
                    suggested_fix="Enable test or track skip condition.",
                )
            )
            continue

        _analyze_body(result, path, method_name, line_num, body_code, body_src, sig_start, body_open, src)

    # 2. Scan for Kotest style blocks
    for match in KOTEST_BLOCK_RE.finditer(code):
        line_num = line_of(src, match.start())
        # extract block name from original src
        raw_header = src[match.start() : match.end()]
        name_match = re.search(r'["\']([^"\']+)["\']', raw_header)
        block_name = name_match.group(1) if name_match else "kotest_block"

        body_open = match.end() - 1
        body_end = match_block(code, body_open)
        body_code = code[body_open:body_end]
        body_src = src[body_open:body_end]

        _analyze_body(result, path, block_name, line_num, body_code, body_src, match.start(), body_open, src)


def _analyze_body(
    result: ScanResult,
    path: Path,
    name: str,
    line_num: int,
    body_code: str,
    body_src: str,
    sig_start: int,
    body_open: int,
    src: str,
) -> None:
    # Check tautological assertions
    for t_match in TAUTOLOGY_RE.finditer(body_code):
        t_line = line_of(src, body_open + t_match.start())
        result.findings.append(
            Finding(
                rule=Rule.TAUTOLOGICAL_ASSERTION,
                severity=Severity.HIGH,
                file=path,
                line=t_line,
                test_name=name,
                detail="Assertion tests a constant condition that is always true.",
                snippet=body_src[t_match.start() : t_match.end()].strip(),
                suggested_fix="Assert dynamic actual value against expected outcome.",
            )
        )

    # Check fixed sleep
    for s_match in SLEEP_RE.finditer(body_code):
        s_line = line_of(src, body_open + s_match.start())
        result.findings.append(
            Finding(
                rule=Rule.SLEEP_INSTEAD_OF_WAIT,
                severity=Severity.MEDIUM,
                file=path,
                line=s_line,
                test_name=name,
                detail="Fixed sleep used instead of polling / coroutine wait condition.",
                snippet=body_src[s_match.start() : min(len(body_src), s_match.end() + 30)].strip(),
                suggested_fix="Replace with coroutine polling or explicit await condition.",
            )
        )

    # Check swallowed exceptions
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
                        test_name=name,
                        detail="Catch block suppresses test failure without rethrowing or failing.",
                        snippet=body_src[c_match.start() : min(len(body_src), catch_end)].strip(),
                        suggested_fix="Rethrow exception or call fail(e.message).",
                    )
                )

    assertions = list(ASSERTION_RE.finditer(body_code))
    effective_assertions = len(assertions)

    if effective_assertions == 0:
        result.findings.append(
            Finding(
                rule=Rule.NO_ASSERTIONS,
                severity=Severity.CRITICAL,
                file=path,
                line=line_num,
                test_name=name,
                detail=f"Test '{name}' executes with zero assertions.",
                snippet=src[sig_start : min(len(src), body_open + 50)].strip(),
                suggested_fix="Add `shouldBe`, `assertEquals`, or MockK `verify` block.",
            )
        )

    result.tests.append(
        TestCase(
            name=name,
            file=path,
            line=line_num,
            effective_assertions=effective_assertions,
            is_disabled=False,
        )
    )
