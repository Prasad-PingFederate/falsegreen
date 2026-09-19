"""JavaScript / TypeScript detection for Playwright, Jest and Vitest.

WHY THERE IS NO PARSER DEPENDENCY

falsegreen installs with zero dependencies, which is most of why anyone is
willing to put it in their CI. Pulling in a JS parser to keep that promise
would cost more than it buys, so this module tokenizes enough JavaScript to be
correct about the two things the rules actually need: where the code is (as
opposed to strings, comments, template literals and regex literals), and where
each test's body begins and ends.

Everything is done on a "blanked" copy of the source in which every non-code
region is replaced by spaces of the same length. Offsets therefore still map
exactly onto the original text, so line numbers and snippets stay accurate
while `expect(` inside a comment or a string cannot produce a finding.

THE RULE THAT JUSTIFIES THE MODULE

Playwright's web-first assertions are asynchronous:

    await expect(page.locator("#row")).toBeVisible();   // correct
    expect(page.locator("#row")).toBeVisible();         // never fails the test

The second form returns a promise nobody waits on. The assertion resolves after
the test has already finished, so a failure surfaces as an unhandled rejection
at best and as nothing at all at worst. It is the exact JS counterpart of the
swallowed assertion this tool was built for, and it is everywhere.

Jest's matchers are synchronous, so the same shape is correct there. The rule
only fires for matchers that are actually async, and `.resolves` / `.rejects`,
which need awaiting in both frameworks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from ..models import Finding, Rule, ScanResult, Severity, TestCase

# Playwright web-first assertions. Each returns a promise.
ASYNC_MATCHERS = {
    "toBeAttached", "toBeChecked", "toBeDisabled", "toBeEditable", "toBeEmpty",
    "toBeEnabled", "toBeFocused", "toBeHidden", "toBeInViewport", "toBeVisible",
    "toContainText", "toHaveAccessibleDescription", "toHaveAccessibleName",
    "toHaveAttribute", "toHaveClass", "toHaveCount", "toHaveCSS", "toHaveId",
    "toHaveJSProperty", "toHaveRole", "toHaveScreenshot", "toHaveText",
    "toHaveTitle", "toHaveURL", "toHaveValue", "toHaveValues", "toPass",
    "toBeOK", "toMatchAriaSnapshot",
}

# Any matcher at all — used to decide whether a test asserts anything.
SYNC_MATCHER_HINT = re.compile(r"\.(to[A-Z]\w*|toBe|toEqual|toThrow)\s*\(")

# Calls that THROW when the expectation is not met. These are assertions even
# though no `expect` appears, and a whole idiom depends on them:
#
#     screen.getByText('cached value')        // throws if absent
#     await screen.findByRole('button')       // throws if it never appears
#     await page.waitForSelector('#row')      // throws on timeout
#
# Treating these as non-assertions produced 170 false positives on a single
# real repository, which is the kind of result that gets a tool uninstalled.
# queryBy*/queryAllBy* are deliberately absent: they return null rather than
# throwing, so on their own they assert nothing.
THROWING_ASSERTION = re.compile(
    r"\b(?:get|find)(?:All)?By[A-Z]\w*\s*\("          # Testing Library
    r"|\bwaitFor(?:Selector|URL|Response|Request|LoadState|Function|Event|Navigation)\s*\("
    r"|\.\s*waitFor\s*\("                              # locator.waitFor()
    r"|\bwaitFor\s*\("                                 # RTL waitFor(() => ...)
    r"|\bassert(?:\s*\(|\s*\.\s*\w+\s*\()"             # node:assert, chai
    r"|\binvariant\s*\("
    r"|\bshould\s*\.\s*\w+"                            # chai should-style
    r"|\.\s*should\s*\."
    r"|\bthrow\b"
)

TEST_DECL = re.compile(
    r"\b(?P<fn>test|it)(?P<mod>\s*\.\s*(only|skip|failing|fixme|todo|concurrent|each))?\s*\(",
)
DESCRIBE_ONLY = re.compile(r"\bdescribe\s*\.\s*only\s*\(")

SLEEP_CALL = re.compile(r"\b(?:page\s*\.\s*waitForTimeout|setTimeout)\s*\(")

# `if (await thing.isVisible())` and friends — a presence check.
VISIBILITY_PREDICATE = re.compile(
    r"\b(?:isVisible|isHidden|isEnabled|isChecked|isEditable|isDisabled|count)\s*\(\s*\)"
)

PRESENCE_MATCHERS = {
    "toBeVisible", "toBeHidden", "toBeEnabled", "toBeChecked", "toBeEditable",
    "toBeAttached", "toHaveCount",
}


# ----------------------------------------------------------------------
# Tokenizing
# ----------------------------------------------------------------------

_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>") | {"\n"}


def blank_noncode(src: str) -> str:
    """Replace strings, comments, template literals and regexes with spaces.

    Length and newlines are preserved so every offset in the result still
    points at the same character in the original.
    """
    out = list(src)
    i, n = 0, len(src)
    prev_significant = "\n"

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if ch == "/" and nxt == "/":
            j = src.find("\n", i)
            j = n if j == -1 else j
            blank(i, j)
            i = j
            continue

        if ch == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            blank(i, j)
            i = j
            continue

        if ch in "'\"":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == ch or src[j] == "\n":
                    break
                j += 1
            blank(i, min(j + 1, n))
            i = min(j + 1, n)
            prev_significant = "x"
            continue

        if ch == "`":
            # Template literal. ${...} holds real code, so only the literal
            # segments are blanked.
            j = i + 1
            depth = 0
            while j < n:
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if depth == 0 and c == "$" and j + 1 < n and src[j + 1] == "{":
                    blank(i, j)
                    depth = 1
                    j += 2
                    i = j
                    continue
                if depth > 0:
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            i = j + 1
                    j += 1
                    continue
                if c == "`":
                    break
                j += 1
            blank(i, min(j + 1, n))
            i = min(j + 1, n)
            prev_significant = "x"
            continue

        if ch == "/" and prev_significant in _REGEX_PRECEDERS:
            j = i + 1
            in_class = False
            ok = False
            while j < n:
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    ok = True
                    break
                elif c == "\n":
                    break
                j += 1
            if ok:
                blank(i, j + 1)
                i = j + 1
                prev_significant = "x"
                continue

        if not ch.isspace():
            prev_significant = ch
        i += 1

    return "".join(out)


def _match_block(code: str, open_idx: int) -> int:
    """Index just past the '}' matching the '{' at open_idx."""
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(code)


def _match_paren(code: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(code)


def _callback_body(args: str) -> Optional[int]:
    """Offset of the test callback's opening brace within the argument list.

    Naively taking the first '{' is wrong: `async ({ page }) => {` opens a brace
    for the destructured fixture parameter before the body ever starts, which
    yields an empty body and makes every test look assertion-free. The body is
    the brace after the arrow, or after a function keyword's parameter list.
    """
    arrow = args.find("=>")
    if arrow != -1:
        brace = args.find("{", arrow)
        return brace if brace != -1 else None

    fm = re.search(r"\bfunction\b\s*\w*\s*\(", args)
    if fm:
        close = _match_paren(args, fm.end() - 1)
        brace = args.find("{", close)
        return brace if brace != -1 else None

    return None


def _line_of(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


def _title_at(src: str, paren_idx: int) -> str:
    """Read the test's title string from the ORIGINAL source."""
    segment = src[paren_idx: paren_idx + 400]
    m = re.search(r"""["'`](?P<t>(?:\\.|[^"'`\\])*)["'`]""", segment)
    return (m.group("t").strip() if m else "") or "<unnamed test>"


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------


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

    def run(self) -> None:
        code = self.code

        # `.only` anywhere is critical: the rest of the suite silently does not run.
        for m in list(TEST_DECL.finditer(code)) :
            mod = (m.group("mod") or "")
            if ".only" in mod.replace(" ", ""):
                line = _line_of(self.src, m.start())
                self.add(Rule.DISABLED_TEST, Severity.CRITICAL, line,
                         _title_at(self.src, m.end() - 1),
                         "test.only silently skips every other test in this file; "
                         "CI reports green having run one test")
        for m in DESCRIBE_ONLY.finditer(code):
            line = _line_of(self.src, m.start())
            self.add(Rule.DISABLED_TEST, Severity.CRITICAL, line, "<describe.only>",
                     "describe.only silently skips every other suite in this file")

        for m in TEST_DECL.finditer(code):
            mod = (m.group("mod") or "").replace(" ", "")
            paren_open = m.end() - 1
            paren_end = _match_paren(code, paren_open)
            args = code[paren_open:paren_end]

            body_rel = _callback_body(args)
            if body_rel is None:
                continue
            body_start = paren_open + body_rel
            body_end = _match_block(code, body_start)

            title = _title_at(self.src, paren_open)
            decl_line = _line_of(self.src, m.start())
            disabled = any(k in mod for k in (".skip", ".todo", ".fixme"))

            case = TestCase(name=title, file=self.path, line=decl_line, is_disabled=disabled)
            self.result.tests.append(case)

            if disabled:
                self.add(Rule.DISABLED_TEST, Severity.LOW, decl_line, title,
                         f"marked {mod.strip('.')}")
                continue

            self._scan_body(case, title, body_start, body_end)

    def _scan_body(self, case: TestCase, title: str, start: int, end: int) -> None:
        code = self.code
        body = code[start:end]
        effective = 0
        unconditional = 0
        found_any = False

        for em in re.finditer(r"\bexpect\s*(\.\s*soft\s*)?\(", body):
            expect_start = start + em.start()
            abs_open = start + em.end() - 1
            abs_close = _match_paren(code, abs_open)
            subject = self.src[abs_open + 1: abs_close - 1].strip()

            # What follows the expect(...) call: .not, .resolves, matcher, etc.
            tail = code[abs_close: abs_close + 220]
            mm = re.match(r"\s*((?:\.\s*(?:not|resolves|rejects)\s*)*)\.\s*(?P<matcher>\w+)\s*\(", tail)
            line = _line_of(self.src, abs_open)

            if not mm:
                self.add(Rule.DANGLING_EXPECT, Severity.CRITICAL, line, title,
                         "expect(...) with no matcher - the assertion is never made")
                found_any = True
                continue

            found_any = True
            matcher = mm.group("matcher")
            chain = mm.group(1) or ""
            needs_await = matcher in ASYNC_MATCHERS or "resolves" in chain or "rejects" in chain

            # Is this expect awaited, returned, or handed to an awaited combinator?
            # Measured from the start of `expect`, not its open paren - otherwise
            # the word `expect` itself sits between `await` and the anchor and no
            # amount of awaiting ever matches.
            before = code[max(start, expect_start - 160): expect_start]
            awaited = bool(re.search(r"\b(?:await|return)\s+$", before))
            if not awaited:
                # Inside `await Promise.all([...])` / `allSettled` / `race`, the
                # assertion is a member of an array that is itself awaited.
                awaited = bool(re.search(
                    r"\bawait\s+Promise\s*\.\s*(?:all|allSettled|race|any)\s*\(\s*\[[^\]]*$",
                    before,
                ))

            if needs_await and not awaited:
                self.add(Rule.UNAWAITED_EXPECT, Severity.CRITICAL, line, title,
                         f"expect(...).{matcher}() returns a promise that is never awaited, "
                         f"so a failure cannot fail this test")
                continue

            if self._is_tautology(subject, matcher, code[abs_close: abs_close + 120]):
                self.add(Rule.TAUTOLOGICAL_ASSERTION, Severity.CRITICAL, line, title,
                         "the condition holds regardless of the code under test")
                continue

            guard = self._enclosing_guard(start, abs_open)
            # An else branch that fails makes the assertion mandatory again, so
            # only an unguarded-on-failure conditional is a finding.
            if (guard and not guard[1]
                    and matcher in PRESENCE_MATCHERS
                    and guard[0] == _normalize(subject)):
                self.add(Rule.SELF_GUARDED_ASSERTION, Severity.CRITICAL, line, title,
                         f"only runs when '{subject[:50]}' is already known present, and the "
                         f"missing case falls through without failing")
                continue

            swallowed = self._swallowing_catch(start, end, abs_open)
            if swallowed is not None:
                self.add(Rule.SWALLOWED_ASSERTION, Severity.CRITICAL, line, title,
                         f"a catch on line {swallowed} discards the failure without rethrowing")
                continue

            effective += 1
            # A guard whose else branch fails still forces the assertion to
            # matter on every path, so it counts as unconditional.
            if guard is None or guard[1]:
                unconditional += 1

        throwing = THROWING_ASSERTION.search(body)
        if not found_any and not SYNC_MATCHER_HINT.search(body) and not throwing:
            self.add(Rule.NO_ASSERTIONS, Severity.CRITICAL, case.line, title,
                     "no expect(), no throwing query, and no assert anywhere in the body")
            case.effective_assertions = 0
            return

        if throwing:
            # A throwing query is a real, unconditional assertion.
            effective += 1
            unconditional += 1

        if effective and unconditional == 0:
            self.add(Rule.OPTIONAL_ASSERTION, Severity.HIGH, case.line, title,
                     f"all {effective} assertion(s) sit inside conditionals with no failing "
                     f"alternative, so there is a run of this test that verifies nothing")
            case.effective_assertions = 0
        else:
            case.effective_assertions = effective

        for sm in SLEEP_CALL.finditer(body):
            self.add(Rule.SLEEP_INSTEAD_OF_WAIT, Severity.LOW,
                     _line_of(self.src, start + sm.start()), title,
                     "a fixed wait instead of a condition")

    @staticmethod
    def _is_tautology(subject: str, matcher: str, tail: str) -> bool:
        s = subject.strip()
        arg = ""
        am = re.match(r"\s*\.\s*\w+\s*\(([^)]*)\)", tail)
        if am:
            arg = am.group(1).strip()
        if s in {"true", "1"} and matcher in {"toBe", "toEqual"} and arg in {"true", "1"}:
            return True
        if s and arg and s == arg and matcher in {"toBe", "toEqual", "toStrictEqual"}:
            return True
        return False

    def _enclosing_guard(self, body_start: int, idx: int) -> Optional[Tuple[str, bool]]:
        """Return (guarded subject, else_fails) if idx sits inside an if-block.

        Only a presence-style condition counts; a plain boolean flag is not the
        pattern this rule is about.
        """
        code = self.code
        depth = 0
        i = idx
        while i > body_start:
            c = code[i]
            if c == "}":
                depth += 1
            elif c == "{":
                if depth == 0:
                    head = code[max(body_start, i - 220): i]
                    hm = re.search(r"\bif\s*\(([^{]*)\)\s*$", head)
                    if hm:
                        cond = hm.group(1)
                        if VISIBILITY_PREDICATE.search(cond):
                            close = _match_block(code, i)
                            after = code[close: close + 40]
                            else_fails = False
                            if re.match(r"\s*else\b", after):
                                em = re.search(r"\belse\b", code[close:close + 40])
                                if em:
                                    eb = code.find("{", close + em.start())
                                    if eb != -1:
                                        ebody = code[eb:_match_block(code, eb)]
                                        else_fails = bool(
                                            re.search(r"\b(throw|expect|assert|fail)\b", ebody)
                                        )
                            subj = re.sub(
                                r"^\s*(?:await\s+)?|\s*\.\s*(?:isVisible|isHidden|isEnabled|"
                                r"isChecked|isEditable|isDisabled|count)\s*\(\s*\).*$",
                                "", cond,
                            )
                            return _normalize(subj), else_fails
                        return None
                    i -= 1
                    continue
                depth -= 1
            i -= 1
        return None

    def _swallowing_catch(self, body_start: int, body_end: int, idx: int) -> Optional[int]:
        """Line of an enclosing catch that discards failures, if any."""
        code = self.code
        depth = 0
        i = idx
        while i > body_start:
            c = code[i]
            if c == "}":
                depth += 1
            elif c == "{":
                if depth == 0:
                    head = code[max(body_start, i - 40): i]
                    if re.search(r"\btry\s*$", head):
                        close = _match_block(code, i)
                        cm = re.search(r"\bcatch\s*(\([^)]*\))?\s*\{", code[close: close + 80])
                        if cm:
                            cb = close + cm.end() - 1
                            cbody = code[cb:_match_block(code, cb)]
                            rethrows = bool(re.search(r"\b(throw|fail\s*\()", cbody))
                            if not rethrows:
                                return _line_of(self.src, close + cm.start())
                        return None
                    i -= 1
                    continue
                depth -= 1
            i -= 1
        return None


def _normalize(expr: str) -> str:
    return re.sub(r"\s+", "", expr or "").strip("();")


def scan_js_file(path: Path, result: ScanResult) -> None:
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
