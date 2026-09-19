"""Java detector - JUnit 4/5, TestNG, and the Selenium suites built on them.

Java is where the biggest Selenium estates live, and where the assertion-free
test is easiest to write by accident: a method annotated `@Test` that drives a
WebDriver and returns is a complete, passing test. It goes green for as long as
no exception escapes, which a page object will usually make sure of.

Three Java-specific shapes matter beyond the usual ones:

- `catch (Exception e)` around an assertion. `AssertionError` is an Error, not
  an Exception, so `catch (Exception e)` does *not* swallow a JUnit assertion -
  but `catch (Throwable t)`, `catch (AssertionError e)` and the very common
  `catch (Exception e) { }` around a Selenium call plus assertion all do, in
  different ways. The distinction is worth getting right rather than flagging
  every try/catch.
- `@Test(expected = ...)` and `assertThrows` are assertions, even though no
  `assert` appears in the body.
- Mockito `verify(...)` is an assertion; `when(...)` is not.

Parsing is textual, like the JavaScript detector: no javac, no dependency, and
a file that does not compile still yields a useful answer.
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
    """Replace comments and string literals with spaces, preserving offsets.

    Brace matching is the backbone of this detector, so a `{` inside a string
    or a comment has to stop existing. Lengths and newlines are preserved so
    every index and line number computed on the result still refers to the
    original source.
    """
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

        # """text block""" (Java 15+)
        if src.startswith('"""', i):
            for _ in range(3):
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


def match_paren(code: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(code)


def line_of(src: str, index: int) -> int:
    return src.count("\n", 0, index) + 1


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

#: @Test, @org.junit.Test, @ParameterizedTest, @RepeatedTest
TEST_ANNOTATION_RE = re.compile(r"@(?:[\w.]+\.)?(Test|ParameterizedTest|RepeatedTest)\b")

#: The method signature that follows the annotations.
METHOD_SIG_RE = re.compile(
    r"(?:(?:public|protected|private|static|final|synchronized|abstract|default)\s+)*"
    r"(?:<[^>]+>\s*)?"                       # generics
    r"[\w.<>\[\],\s?]+\s+"                   # return type
    r"(?P<name>\w+)\s*\("
)

#: Anything that can fail a test on its own.
ASSERTION_RE = re.compile(
    r"\b(?:"
    r"assert[A-Z_]\w*"                       # assertEquals, assertTrue, assertThat, assert_
    r"|Assert(?:ions)?\s*\.\s*\w+"           # Assert.assertEquals, Assertions.assertAll
    r"|assertThat"                           # AssertJ / Hamcrest / Truth
    r"|verify\s*\("                          # Mockito verify
    r"|verifyNoMoreInteractions|verifyZeroInteractions"
    r"|fail\s*\("                            # explicit failure
    r"|shouldBe\w*|shouldHave\w*|shouldNotBe\w*"   # Kotest / AssertJ fluent
    r"|expect\s*\("
    r"|MatcherAssert\s*\.\s*assertThat"
    r")",
)

#: `assert x == y;` - the JVM keyword, only live under -ea. Deliberately not in
#: ASSERTION_RE above: it is a weaker signal and gets its own, lower verdict.
BARE_ASSERT_RE = re.compile(r"^\s*assert\s+[^;]+;", re.MULTILINE)

#: A catch clause, with the type it catches.
CATCH_RE = re.compile(r"\bcatch\s*\(\s*(?:final\s+)?(?P<types>[\w.|\s]+?)\s+\w+\s*\)")

#: Catching any of these swallows a JUnit/TestNG assertion failure.
#: AssertionError is an Error, so `catch (Exception e)` alone does NOT.
ASSERTION_SWALLOWING_TYPES = {"Throwable", "Error", "AssertionError"}

#: Something in the handler that makes the failure survive.
RETHROW_RE = re.compile(r"\b(?:throw\b|fail\s*\(|Assert(?:ions)?\s*\.\s*fail)")

#: Thread.sleep / TimeUnit.SECONDS.sleep - a fixed wait where a condition belongs.
SLEEP_RE = re.compile(r"\b(?:Thread\s*\.\s*sleep|TimeUnit\s*\.\s*\w+\s*\.\s*sleep)\s*\(")

#: An explicit wait is the correct alternative, and is itself an assertion:
#: it throws TimeoutException when the condition never holds.
EXPLICIT_WAIT_RE = re.compile(r"\b(?:WebDriverWait|FluentWait|\.until\s*\()")

#: @Disabled (JUnit 5), @Ignore (JUnit 4), @Test(enabled = false) (TestNG)
DISABLED_RE = re.compile(r"@(?:[\w.]+\.)?(?:Disabled|Ignore)\b")
TESTNG_DISABLED_RE = re.compile(r"enabled\s*=\s*false")

#: @Test(expected = X.class) and assertThrows are assertions without an assert.
EXPECTED_EXCEPTION_RE = re.compile(r"\b(?:expected|expectedExceptions)\s*=")
ASSERT_THROWS_RE = re.compile(r"\bassert(?:Throws|ThrowsExactly)\s*\(")

#: Helpers whose name promises a check.
CHECKING_NAME_RE = re.compile(r"^(?:verify|check|assert|validate|ensure|confirm)[A-Z_]")

#: `if (...) { ... }` - used to spot assertions that only run on one branch.
IF_RE = re.compile(r"\bif\s*\(")


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------


class _Scanner:
    def __init__(self, path: Path, src: str, result: ScanResult):
        self.path = path
        self.src = src
        self.code = blank_noncode(src)
        self.result = result
        self.lines = src.splitlines()

    def snippet(self, line: int) -> str:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1].strip()[:160]
        return ""

    def add(self, rule: Rule, severity: Severity, line: int, test: str, detail: str = "") -> None:
        self.result.findings.append(
            Finding(rule=rule, severity=severity, file=self.path, line=line,
                    test_name=test, detail=detail, snippet=self.snippet(line))
        )

    # -- locating methods -------------------------------------------------

    def _method_after(self, index: int) -> Optional[Tuple[str, int, int, int]]:
        """Find the method whose annotations end at `index`.

        Returns (name, signature_start, body_start, body_end).
        """
        code = self.code
        pos = index

        # Skip the annotation's own argument list, e.g. @Test(expected = X.class),
        # and any further annotations stacked on the method.
        while pos < len(code):
            while pos < len(code) and code[pos] in " \t\r\n":
                pos += 1
            if pos < len(code) and code[pos] == "(":
                pos = match_paren(code, pos)
                continue
            if pos < len(code) and code[pos] == "@":
                pos += 1
                while pos < len(code) and (code[pos].isalnum() or code[pos] in "._"):
                    pos += 1
                continue
            break

        match = METHOD_SIG_RE.match(code, pos)
        if not match:
            # Be forgiving about unusual modifiers or formatting.
            match = METHOD_SIG_RE.search(code, pos, pos + 400)
            if not match:
                return None

        paren_end = match_paren(code, match.end() - 1)
        brace = code.find("{", paren_end)
        if brace == -1:
            return None

        # An abstract or interface method has a `;` before any `{`.
        semi = code.find(";", paren_end)
        if semi != -1 and semi < brace:
            return None

        return match.group("name"), match.start(), brace, match_block(code, brace)

    # -- analysis ---------------------------------------------------------

    def run(self) -> None:
        code = self.code
        seen_bodies: set = set()

        for m in TEST_ANNOTATION_RE.finditer(code):
            found = self._method_after(m.end())
            if not found:
                continue
            name, sig_start, body_start, body_end = found
            if body_start in seen_bodies:
                continue
            seen_bodies.add(body_start)

            # The annotation block preceding the method, for @Disabled etc.
            head_start = max(0, code.rfind("\n\n", 0, m.start()))
            head = code[head_start:body_start]
            body = code[body_start:body_end]
            line = line_of(self.src, m.start())

            disabled = bool(DISABLED_RE.search(head)) or bool(TESTNG_DISABLED_RE.search(head))
            case = TestCase(name=name, file=self.path, line=line, is_disabled=disabled)
            self.result.tests.append(case)

            if disabled:
                self.add(Rule.DISABLED_TEST, Severity.LOW, line, name,
                         "annotated @Disabled/@Ignore, or enabled = false")
                continue

            self._check_body(case, name, head, body, body_start)

        self._check_helpers(seen_bodies)

    def _check_body(
        self, case: TestCase, name: str, head: str, body: str, body_start: int
    ) -> None:
        # `@Test(expected = ...)` asserts without an assert statement.
        declares_expected = bool(EXPECTED_EXCEPTION_RE.search(head))

        assertions = list(ASSERTION_RE.finditer(body))
        bare_asserts = list(BARE_ASSERT_RE.finditer(body))
        waits = list(EXPLICIT_WAIT_RE.finditer(body))

        effective = len(assertions) + len(bare_asserts) + len(waits)
        if declares_expected:
            effective += 1

        # Thread.sleep where a condition belongs.
        for m in SLEEP_RE.finditer(body):
            self.add(
                Rule.SLEEP_INSTEAD_OF_WAIT, Severity.MEDIUM,
                line_of(self.src, body_start + m.start()), name,
                "a fixed sleep races the application; WebDriverWait fails fast and "
                "does not depend on the machine being slow in the same way",
            )

        # Assertions swallowed by a catch that absorbs AssertionError.
        swallowed = self._swallowed_assertions(body, body_start, name)
        effective -= swallowed

        if effective <= 0:
            if swallowed:
                pass  # already reported, and more precisely
            else:
                self.add(
                    Rule.NO_ASSERTIONS, Severity.CRITICAL,
                    line_of(self.src, body_start), name,
                    "the method drives the system under test and returns; it passes "
                    "unless something throws, so it cannot tell correct behaviour "
                    "from wrong behaviour",
                )
            case.effective_assertions = 0
            return

        case.effective_assertions = effective

        # assertTrue(true), assertEquals(x, x)
        self._check_tautologies(body, body_start, name)

        # Every assertion inside an `if` with no failing alternative.
        if assertions and self._all_assertions_conditional(body, assertions):
            self.add(
                Rule.OPTIONAL_ASSERTION, Severity.HIGH,
                line_of(self.src, body_start + assertions[0].start()), name,
                "every assertion sits inside an if with no failing else, so when the "
                "condition is false the test passes having verified nothing",
            )

    def _swallowed_assertions(self, body: str, body_start: int, name: str) -> int:
        """Count assertions inside a catch that absorbs an assertion failure."""
        count = 0
        for m in CATCH_RE.finditer(body):
            types = {t.strip() for t in re.split(r"[|\s]+", m.group("types")) if t.strip()}
            types = {t.split(".")[-1] for t in types}
            if not (types & ASSERTION_SWALLOWING_TYPES):
                continue

            brace = body.find("{", m.end())
            if brace == -1:
                continue
            handler = body[brace:match_block(body, brace)]
            if RETHROW_RE.search(handler):
                continue  # the failure survives

            # The try block this catch belongs to.
            try_open = body.rfind("try", 0, m.start())
            if try_open == -1:
                continue
            try_brace = body.find("{", try_open)
            if try_brace == -1 or try_brace > m.start():
                continue
            try_body = body[try_brace:match_block(body, try_brace)]

            inner = list(ASSERTION_RE.finditer(try_body)) + list(BARE_ASSERT_RE.finditer(try_body))
            if not inner:
                continue

            count += len(inner)
            caught = "/".join(sorted(types & ASSERTION_SWALLOWING_TYPES))
            self.add(
                Rule.SWALLOWED_ASSERTION, Severity.CRITICAL,
                line_of(self.src, body_start + try_brace + inner[0].start()), name,
                f"the assertion raises AssertionError and `catch ({caught})` absorbs it "
                f"without re-throwing or failing, so the test reports success either way",
            )
        return count

    def _check_tautologies(self, body: str, body_start: int, name: str) -> None:
        for m in re.finditer(r"\bassert(Equals|True|False|Same)\s*\(", body):
            open_idx = m.end() - 1
            args_src = body[open_idx + 1:match_paren(body, open_idx) - 1]
            args = [a.strip() for a in _split_args(args_src)]
            kind = m.group(1)

            tautology = (
                (kind in ("Equals", "Same") and len(args) >= 2 and args[-1] == args[-2]
                 and args[-1] != "")
                or (kind == "True" and args[:1] == ["true"])
                or (kind == "False" and args[:1] == ["false"])
            )
            if tautology:
                self.add(
                    Rule.TAUTOLOGICAL_ASSERTION, Severity.HIGH,
                    line_of(self.src, body_start + m.start()), name,
                    f"assert{kind} holds by construction, whatever the code under test did",
                )

    def _all_assertions_conditional(self, body: str, assertions: List[re.Match]) -> bool:
        """True when every assertion is inside an `if` and no `else` can fail."""
        if "else" in body:
            return False
        spans = []
        for m in IF_RE.finditer(body):
            paren_end = match_paren(body, m.end() - 1)
            brace = body.find("{", paren_end)
            if brace == -1 or brace > paren_end + 40:
                continue
            spans.append((brace, match_block(body, brace)))
        if not spans:
            return False
        return all(any(s <= a.start() < e for s, e in spans) for a in assertions)

    def _check_helpers(self, test_bodies: set) -> None:
        """A method named verifyX/checkX that never asserts makes callers lie."""
        code = self.code
        for m in METHOD_SIG_RE.finditer(code):
            name = m.group("name")
            if not CHECKING_NAME_RE.match(name):
                continue

            paren_end = match_paren(code, m.end() - 1)
            brace = code.find("{", paren_end)
            if brace == -1 or brace in test_bodies:
                continue
            semi = code.find(";", paren_end)
            if semi != -1 and semi < brace:
                continue

            body = code[brace:match_block(code, brace)]
            if ASSERTION_RE.search(body) or BARE_ASSERT_RE.search(body):
                continue
            if EXPLICIT_WAIT_RE.search(body):
                continue
            # A helper that only delegates is judged at its own call sites.
            if re.search(r"\b(?:verify|check|assert|validate|ensure|confirm)[A-Z_]\w*\s*\(", body):
                continue

            self.add(
                Rule.ASSERTION_FREE_HELPER, Severity.HIGH,
                line_of(self.src, m.start()), name,
                f"'{name}' reads as a verification at every call site but asserts nothing",
            )


def _split_args(src: str) -> List[str]:
    """Split an argument list on top-level commas."""
    args, depth, current = [], 0, []
    for ch in src:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        args.append("".join(current))
    return args


def scan_java_file(path: Path, result: ScanResult) -> None:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        result.errors.append(f"{path}: could not read ({err})")
        return

    try:
        _Scanner(path, src, result).run()
    except Exception as err:  # one odd file must not kill the run
        result.errors.append(f"{path}: {type(err).__name__}: {err}")
        return

    result.files_scanned += 1
