"""Go detector - standard testing package, Testify, and Gomega/Ginkgo.

Go test files end in `_test.go` and contain functions like `func TestXxx(t *testing.T)`.

Key anti-patterns handled:
- `TestXxx` functions that execute code with no `t.Error`, `t.Fatal`, or `assert`/`require` calls
- Tautological assertions like `assert.Equal(t, 1, 1)` or `assert.True(t, true)`
- `time.Sleep(...)` calls used instead of channel/sync polling or context timeouts
- Unconditional `t.Skip(...)` disabling tests
- Assertion-free helper functions
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

        # `raw string literal`
        if ch == "`":
            out[i] = " "
            i += 1
            while i < n and src[i] != "`":
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                i += 1
            continue

        # "interpreted string" or 'rune'
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

#: func TestXxx(t *testing.T) or func BenchmarkXxx(b *testing.B)
FUNC_TEST_RE = re.compile(
    r"\bfunc\s+(?P<name>(?:Test|Benchmark|Example)[A-Za-z0-9_]*)\s*\(\s*\w+\s+\*(?:testing\.)?(?:T|B|M)"
)

#: Standard testing calls and popular assertion libraries
ASSERTION_RE = re.compile(
    r"\b(?:"
    r"\w+\s*\.\s*(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\("   # t.Error, t.Fatal, etc.
    r"|assert\s*\.\s*\w+\s*\("                                     # testify/assert: assert.Equal(t, ...)
    r"|require\s*\.\s*\w+\s*\("                                    # testify/require: require.NoError(t, ...)
    r"|is\s*\.\s*(?:Equal|True|NoErr|Nil|OK)\s*\("                   # matryer/is
    r"|Expect\s*\([^\)]*\)\s*\.\s*(?:To|ToNot|NotTo)\s*\("       # gomega: Expect(x).To(Equal(y))
    r"|panic\s*\("
    r")"
)

#: Fixed sleep: time.Sleep
SLEEP_RE = re.compile(r"\btime\s*\.\s*Sleep\s*\(")

#: Disabled test: t.Skip / t.Skipf / t.SkipNow
SKIP_RE = re.compile(r"\b\w+\s*\.\s*Skip(?:f|Now)?\s*\(")


def _unconditional_skip(body_code: str) -> int:
    """Index of a Skip call made directly in the test body, or -1.

    "Directly in the body" means at brace depth 0 relative to body_code's own
    opening `{` - not inside an if/for/switch/select or a nested func literal.
    That distinction is the whole fix: `if testing.Short() { t.Skip(...) }` and
    `if os.Getenv("INTEGRATION") == "" { t.Skip(...) }` are the two most
    idiomatic skip patterns in Go, used throughout the standard library, and
    both are conditional - the test runs fully, assertions and all, on every
    ordinary `go test`. Treating them as an unconditional disable discards real
    assertions and reports a healthy test as one that "unconditionally skips
    execution", which is simply false.

    The whole body is scanned, not a fixed prefix: a real unconditional skip is
    not always the literal first statement (setup can precede it), and a
    fixed-length window either matches a guarded skip inside it or misses an
    unconditional one just past its end - this detector used to do both.
    """
    # body_code starts at the function's own opening `{` (that is how the
    # caller finds it), so that first brace has to be counted before depth 0
    # means "top level of this body" - starting at -1 makes it so.
    depth = -1
    pos = 0
    for m in SKIP_RE.finditer(body_code):
        for ch in body_code[pos:m.start()]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        pos = m.start()
        if depth == 0:
            return m.start()
    return -1

#: Tautological assertions in Go
TAUTOLOGY_RE = re.compile(
    r"\b(?:"
    r"assert\s*\.\s*True\s*\(\s*\w+\s*,\s*true\s*\)"
    r"|assert\s*\.\s*False\s*\(\s*\w+\s*,\s*false\s*\)"
    r"|assert\s*\.\s*Equal\s*\(\s*\w+\s*,\s*([a-zA-Z0-9_]+)\s*,\s*\1\s*\)"
    r"|require\s*\.\s*True\s*\(\s*\w+\s*,\s*true\s*\)"
    r"|require\s*\.\s*False\s*\(\s*\w+\s*,\s*false\s*\)"
    r"|require\s*\.\s*Equal\s*\(\s*\w+\s*,\s*([a-zA-Z0-9_]+)\s*,\s*\1\s*\)"
    r")"
)


def scan_golang_file(path: Path, result: ScanResult) -> None:
    """Scan a Go (*_test.go / .go) file for test suite anti-patterns."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        result.errors.append(f"{path}: {err}")
        return

    result.files_scanned += 1
    code = blank_noncode(src)

    for match in FUNC_TEST_RE.finditer(code):
        fn_name = match.group("name")
        fn_start = match.start()
        line_num = line_of(src, fn_start)

        # Find opening {
        body_open = code.find("{", match.end())
        if body_open == -1 or body_open - match.end() > 80:
            continue

        body_end = match_block(code, body_open)
        body_code = code[body_open:body_end]
        body_src = src[body_open:body_end]

        # Check for an unconditional skip - one made directly in the body, not
        # guarded by an if/for/switch/select. A guarded skip falls through to
        # the normal checks below instead of continuing past them: it is not a
        # disabled test, and its real assertions, tautologies and sleeps still
        # need to be judged.
        skip_at = _unconditional_skip(body_code)
        if skip_at != -1:
            skip_line = line_of(src, body_open + skip_at)
            result.tests.append(
                TestCase(
                    name=fn_name,
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
                    line=skip_line,
                    test_name=fn_name,
                    detail=f"Test '{fn_name}' unconditionally skips execution via t.Skip().",
                    snippet=body_src[max(0, skip_at - 20):skip_at + 60].strip(),
                    suggested_fix="Enable test or track skip condition to prevent permanent suite degradation.",
                )
            )
            continue

        # Check for tautological assertions
        for t_match in TAUTOLOGY_RE.finditer(body_code):
            t_line = line_of(src, body_open + t_match.start())
            result.findings.append(
                Finding(
                    rule=Rule.TAUTOLOGICAL_ASSERTION,
                    severity=Severity.HIGH,
                    file=path,
                    line=t_line,
                    test_name=fn_name,
                    detail="Assertion tests a constant value against itself.",
                    snippet=body_src[t_match.start() : t_match.end()].strip(),
                    suggested_fix="Assert actual variable output against expected value.",
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
                    test_name=fn_name,
                    detail="Fixed `time.Sleep(...)` used instead of channel/sync polling or context timeout.",
                    snippet=body_src[s_match.start() : min(len(body_src), s_match.end() + 30)].strip(),
                    suggested_fix="Replace with select channels or polling with timeout.",
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
                    test_name=fn_name,
                    detail=f"Test function '{fn_name}' executes with no assertions or failure checks.",
                    snippet=src[fn_start : min(len(src), body_open + 50)].strip(),
                    suggested_fix="Add `require.NoError(t, err)`, `assert.Equal(...)`, or `if err != nil { t.Fatal(err) }`.",
                )
            )

        result.tests.append(
            TestCase(
                name=fn_name,
                file=path,
                line=line_num,
                effective_assertions=effective_assertions,
                is_disabled=False,
            )
        )
