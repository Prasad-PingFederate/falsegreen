"""Robot Framework detector.

Robot suites fail quietly in ways the other detectors never see, because the
language has no `assert` keyword at all. A step asserts something only by
convention - it is named `... Should ...`, or it is a `Wait Until ...` that
fails on timeout - and a test that simply drives a browser and stops is
syntactically indistinguishable from one that verifies the result. That makes
"no assertion" the normal accident here rather than an unusual one.

Robot also ships two keywords whose entire purpose is to discard a failure,
`Run Keyword And Ignore Error` and `Run Keyword And Return Status`. Wrapping an
assertion in either is the exact shape of a swallowed assertion, written on one
line and reading like diligence.

Parsing is textual and tolerant, matching the other detectors: no dependency on
the `robotframework` package, nothing executed, and an unparseable file
degrades to a recorded error rather than taking the run down.

What makes this more than keyword matching: user keywords defined in the file
are resolved transitively, so a test whose only step is `Verify Dashboard`
counts as asserting when that keyword asserts, and is reported as empty when it
does not. Without that, every well-factored suite would look assertion-free.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..models import Finding, Rule, ScanResult, Severity, TestCase

# --------------------------------------------------------------------------
# Lexical helpers
# --------------------------------------------------------------------------

#: `*** Test Cases ***`, in any of the spellings Robot accepts.
SECTION_RE = re.compile(r"^\s*\*+\s*(?P<name>[^*]+?)\s*\*+\s*$", re.IGNORECASE)

#: Cells are separated by two or more spaces, or by a tab.
CELL_SPLIT_RE = re.compile(r"\s{2,}|\t+")

#: Settings that appear inside a test or keyword body, e.g. `[Tags]`.
SETTING_RE = re.compile(r"^\[(?P<name>[^\]]+)\]$")

#: Control structures, which are steps but never keyword calls.
CONTROL_WORDS = {
    "IF", "ELSE", "ELSE IF", "END", "FOR", "WHILE",
    "TRY", "EXCEPT", "FINALLY", "BREAK", "CONTINUE", "RETURN",
}

#: A step asserts if its name looks like one. Robot has no assert statement, so
#: the naming convention is the only signal, and it is a strong one: the whole
#: standard library and every major external library follow it.
ASSERTION_NAME_RE = re.compile(
    r"(?:^|\s)(?:should|assert)(?:\s|$)"          # Should Be Equal, Assert X
    r"|should\s+(?:not\s+)?(?:be|contain|match|exist|start|end)"
    r"|^wait\s+until\s"                            # fails on timeout
    r"|^fail$"                                     # explicit failure
    r"|^(?:element|page|table|list|dictionary|file|directory)\s+should",
    re.IGNORECASE,
)

#: Keywords that run another keyword and throw its failure away.
SWALLOWING_WRAPPERS = {
    "run keyword and ignore error",
    "run keyword and return status",
    "run keyword and warn on failure",
    "run keyword and continue on failure",
}

#: Keywords that run another keyword conditionally.
CONDITIONAL_WRAPPERS = {"run keyword if", "run keyword unless"}

#: User keywords named like a check are expected to contain one.
CHECKING_NAME_RE = re.compile(
    r"^(?:verify|check|assert|validate|ensure|confirm)\b", re.IGNORECASE
)

#: `Sleep  3s` - a fixed wait where a condition belongs.
SLEEP_RE = re.compile(r"^sleep$", re.IGNORECASE)

#: Tags and keywords that disable a test.
SKIP_TAGS = {"robot:skip", "robot:exclude"}


def normalize(name: str) -> str:
    """Robot keyword names ignore case, spaces and underscores.

    `Should Be Equal`, `should_be_equal` and `ShouldBeEqual` are the same
    keyword, so comparisons have to be made on a normalized form or half the
    real-world spellings slip past.
    """
    return re.sub(r"[\s_]+", " ", name).strip().lower()


def strip_comment(line: str) -> str:
    """Remove a trailing comment.

    A `#` only starts a comment at the beginning of a cell, so `${x}  # note`
    is a comment but `Log  a#b` is not.
    """
    out = []
    for i, ch in enumerate(line):
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def split_cells(line: str) -> List[str]:
    """Split one line into Robot cells, handling the pipe-separated format too."""
    stripped = line.strip()

    if stripped.startswith("|"):
        # | Step | arg | arg |
        parts = stripped.split("|")[1:]
        if parts and not parts[-1].strip():
            parts = parts[:-1]
        return [p.strip() for p in parts]

    return [c for c in CELL_SPLIT_RE.split(line.strip()) if c]


# --------------------------------------------------------------------------
# Parsed shapes
# --------------------------------------------------------------------------


class Step:
    """One executable line: a keyword name plus its arguments."""

    __slots__ = ("name", "args", "line")

    def __init__(self, name: str, args: List[str], line: int):
        self.name = name
        self.args = args
        self.line = line

    @property
    def key(self) -> str:
        return normalize(self.name)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Step({self.name!r}, line={self.line})"


class Block:
    """A test case or a user keyword: a name, its steps and its settings."""

    def __init__(self, name: str, line: int):
        self.name = name
        self.line = line
        self.steps: List[Step] = []
        self.settings: Dict[str, List[str]] = {}

    @property
    def tags(self) -> Set[str]:
        return {t.strip().lower() for t in self.settings.get("tags", [])}

    @property
    def has_template(self) -> bool:
        """Data-driven tests delegate every assertion to the template keyword."""
        return bool(self.settings.get("template"))


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def parse(source: str) -> Dict[str, List[Block]]:
    """Split a .robot file into its test cases and user keywords.

    Returns {"tests": [...], "keywords": [...]}. Sections other than Test
    Cases / Tasks / Keywords are ignored - variables and settings cannot
    contain an assertion.
    """
    tests: List[Block] = []
    keywords: List[Block] = []

    section: Optional[str] = None
    current: Optional[Block] = None
    bucket: Optional[List[Block]] = None

    for number, raw in enumerate(source.splitlines(), start=1):
        line = strip_comment(raw)
        if not line.strip():
            continue

        header = SECTION_RE.match(line)
        if header:
            name = normalize(header.group("name"))
            if name in ("test cases", "test case", "tasks", "task"):
                section, bucket = "tests", tests
            elif name in ("keywords", "keyword"):
                section, bucket = "keywords", keywords
            else:
                section, bucket = None, None
            current = None
            continue

        if section is None:
            continue

        indented = raw[:1] in (" ", "\t") or raw.lstrip().startswith("|")
        cells = split_cells(line)
        if not cells:
            continue

        # An unindented cell opens a new test case or keyword.
        if not indented:
            current = Block(cells[0], number)
            assert bucket is not None
            bucket.append(current)
            # `Name    Step    arg` on one line is legal in the pipe format.
            cells = cells[1:]
            if not cells:
                continue

        if current is None:
            continue

        # `...` continues the previous line rather than starting a step.
        if cells[0] == "...":
            if current.steps:
                current.steps[-1].args.extend(cells[1:])
            continue

        setting = SETTING_RE.match(cells[0])
        if setting:
            current.settings[normalize(setting.group("name"))] = cells[1:]
            continue

        first = cells[0].strip()
        if first.upper() in CONTROL_WORDS:
            # Keep control words as steps so IF/TRY structure stays visible,
            # but they are never keyword calls.
            current.steps.append(Step(first.upper(), cells[1:], number))
            continue

        # `${result} =    Keyword    arg` - assignment targets precede the call.
        while cells and re.match(r"^[$@&]\{.+\}\s*=?$", cells[0]):
            cells = cells[1:]
        if not cells:
            continue

        current.steps.append(Step(cells[0], cells[1:], number))

    return {"tests": tests, "keywords": keywords}


# --------------------------------------------------------------------------
# Assertion analysis
# --------------------------------------------------------------------------


def step_is_assertion(step: Step) -> bool:
    """True when this step can, by itself, fail the test."""
    return bool(ASSERTION_NAME_RE.search(normalize(step.name)))


def _args_assert(step: Step) -> bool:
    """True when a wrapper's arguments contain an assertion keyword.

    `Run Keyword If    ${cond}    Page Should Contain    Welcome` hides the real
    assertion in the argument list, so the wrapper has to be looked through.
    """
    return any(ASSERTION_NAME_RE.search(normalize(a)) for a in step.args)


def build_keyword_index(keywords: List[Block]) -> Dict[str, Block]:
    return {normalize(k.name): k for k in keywords}


def keyword_asserts(
    name: str,
    index: Dict[str, Block],
    cache: Dict[str, bool],
    stack: Optional[Set[str]] = None,
) -> bool:
    """Does this user keyword eventually assert something?

    Resolved transitively, because Robot suites are written as thin tests over
    layered keywords: without following the chain, a well-factored suite would
    be reported as entirely assertion-free. Recursion is cycle-guarded - Robot
    permits mutually recursive keywords, and a suite is not going to be
    rejected for it.
    """
    key = normalize(name)
    if key in cache:
        return cache[key]

    block = index.get(key)
    if block is None:
        # Not defined here: it comes from a library. Judge it on its name only.
        return bool(ASSERTION_NAME_RE.search(key))

    stack = stack or set()
    if key in stack:
        return False  # cycle: contributes nothing rather than looping
    stack = stack | {key}

    result = False
    for step in block.steps:
        if step.name.upper() in CONTROL_WORDS:
            continue
        if step_is_assertion(step) or _args_assert(step):
            result = True
            break
        if keyword_asserts(step.name, index, cache, stack):
            result = True
            break

    cache[key] = result
    return result


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------


class _Scanner:
    def __init__(self, path: Path, src: str, result: ScanResult):
        self.path = path
        self.src = src
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
        parsed = parse(self.src)
        tests = parsed["tests"]
        keywords = parsed["keywords"]

        index = build_keyword_index(keywords)
        cache: Dict[str, bool] = {}

        self._check_helpers(keywords, index, cache)

        for block in tests:
            self._check_test(block, index, cache)

    # -- user keywords ----------------------------------------------------

    def _check_helpers(
        self, keywords: List[Block], index: Dict[str, Block], cache: Dict[str, bool]
    ) -> None:
        """A keyword named `Verify X` that never asserts makes every caller lie."""
        for block in keywords:
            if not CHECKING_NAME_RE.match(block.name.strip()):
                continue
            if keyword_asserts(block.name, index, cache):
                continue
            self.add(
                Rule.ASSERTION_FREE_HELPER, Severity.HIGH, block.line, block.name,
                f"'{block.name}' reads as a verification at every call site but "
                f"contains no assertion, directly or through the keywords it calls",
            )

    # -- test cases -------------------------------------------------------

    def _check_test(
        self, block: Block, index: Dict[str, Block], cache: Dict[str, bool]
    ) -> None:
        name = block.name
        disabled = bool(block.tags & SKIP_TAGS) or any(
            normalize(s.name) == "skip" for s in block.steps
        )

        case = TestCase(name=name, file=self.path, line=block.line, is_disabled=disabled)
        self.result.tests.append(case)

        if disabled:
            self.add(Rule.DISABLED_TEST, Severity.LOW, block.line, name,
                     "skipped via robot:skip tag or the Skip keyword")
            return

        # A templated test delegates its assertions to the template keyword.
        if block.has_template:
            template = " ".join(block.settings.get("template", []))
            if keyword_asserts(template, index, cache):
                case.effective_assertions = 1
            else:
                self.add(
                    Rule.NO_ASSERTIONS, Severity.CRITICAL, block.line, name,
                    f"data-driven test whose template '{template}' never asserts, so "
                    f"every row passes regardless of the data",
                )
            return

        effective = 0
        conditional_only = True
        in_try = False

        for step in block.steps:
            upper = step.name.upper()

            if upper == "TRY":
                in_try = True
                continue
            if upper in ("EXCEPT", "FINALLY"):
                in_try = False
                continue
            if upper == "END":
                in_try = False
                continue
            if upper in CONTROL_WORDS:
                continue

            key = step.key

            # Sleep instead of a wait condition.
            if SLEEP_RE.match(key):
                self.add(
                    Rule.SLEEP_INSTEAD_OF_WAIT, Severity.MEDIUM, step.line, name,
                    f"Sleep {' '.join(step.args)} - a Wait Until ... keyword fails "
                    f"fast and does not race the application",
                )
                continue

            # A wrapper that discards the failure of the keyword it runs.
            if key in SWALLOWING_WRAPPERS:
                if _args_assert(step):
                    self.add(
                        Rule.SWALLOWED_ASSERTION, Severity.CRITICAL, step.line, name,
                        f"'{step.name}' runs an assertion and discards its failure, so "
                        f"the check reports success either way",
                    )
                continue

            # A conditional wrapper: asserts only when the condition holds.
            if key in CONDITIONAL_WRAPPERS:
                if _args_assert(step):
                    effective += 1
                    self.add(
                        Rule.OPTIONAL_ASSERTION, Severity.HIGH, step.line, name,
                        f"'{step.name}' runs the assertion only when its condition is "
                        f"true; when it is false the test passes having checked nothing",
                    )
                continue

            asserts = step_is_assertion(step) or keyword_asserts(step.name, index, cache)
            if not asserts:
                continue

            if in_try:
                # An assertion inside TRY whose EXCEPT does not re-raise.
                self.add(
                    Rule.SWALLOWED_ASSERTION, Severity.CRITICAL, step.line, name,
                    "assertion sits inside a TRY whose EXCEPT catches the failure",
                )
                continue

            effective += 1
            conditional_only = False

            # Should Be Equal    ${x}    ${x}
            if self._is_tautology(step):
                self.add(
                    Rule.TAUTOLOGICAL_ASSERTION, Severity.HIGH, step.line, name,
                    f"'{step.name}' compares a value with itself, so it holds whatever "
                    f"the application did",
                )

        case.effective_assertions = effective

        if effective == 0:
            self.add(
                Rule.NO_ASSERTIONS, Severity.CRITICAL, block.line, name,
                "the test drives the application and ends; it passes unless a keyword "
                "raises, so it cannot tell correct behaviour from wrong behaviour",
            )
        elif conditional_only:
            # Every assertion was reached through a conditional wrapper.
            pass

    @staticmethod
    def _is_tautology(step: Step) -> bool:
        args = [a for a in step.args if not a.startswith("msg=")]
        if len(args) >= 2 and args[0] == args[1]:
            return True
        if normalize(step.name) == "should be true" and args[:1] in (["True"], ["${True}"]):
            return True
        return False


def scan_robot_file(path: Path, result: ScanResult) -> None:
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
