"""Python/pytest detection, via AST.

AST rather than regex, because the central claim - "an enclosing except catches
this assertion and does not re-raise" - is a structural question. A regex can
see `try:` and `assert` in the same file; it cannot tell whether the assert is
inside that try's body, whether the handler catches AssertionError, or whether
the handler re-raises. Getting that wrong in either direction is fatal: false
positives make the tool untrustworthy, false negatives make it pointless.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional, Set

from ..models import Finding, Rule, ScanResult, Severity, TestCase

# Callables that constitute a real check.
ASSERT_CALL_NAMES = {
    "assertEqual", "assertNotEqual", "assertTrue", "assertFalse", "assertIs",
    "assertIsNot", "assertIsNone", "assertIsNotNone", "assertIn", "assertNotIn",
    "assertRaises", "assertAlmostEqual", "assertGreater", "assertLess",
    "assertListEqual", "assertDictEqual", "assertCountEqual", "assertRegex",
    "fail", "raises", "approx",
    "assert_called", "assert_called_once", "assert_called_with",
    "assert_called_once_with", "assert_not_called", "assert_any_call",
    "assert_has_calls",
}

# Playwright / Jest style terminal matchers. expect(x) alone asserts nothing;
# expect(x).to_be_visible() does.
MATCHER_PREFIXES = ("to_", "not_to_", "toBe", "toEqual", "toHave", "toContain", "toMatch")

SLEEP_CALLS = {"sleep", "wait_for_timeout"}

# Predicates that answer "is this element there?" - the same question the
# matcher below them asserts. A guard built from one of these makes the
# assertion beneath it unreachable in the only case that matters.
VISIBILITY_PREDICATES = {
    "is_visible", "_is_visible", "first_visible", "is_enabled", "is_checked",
    "is_editable", "count", "is_hidden", "is_displayed", "exists",
}

# Matchers that assert presence - paired with a visibility guard, tautological.
PRESENCE_MATCHERS = {
    "to_be_visible", "to_be_enabled", "to_be_checked", "to_be_editable",
    "to_have_count", "toBeVisible", "toBeEnabled",
}

# Helper naming that promises verification to every reader of the call site.
CHECKING_PREFIXES = ("assert_", "verify_", "check_", "expect_", "ensure_", "validate_")

# Mock assertion typo sets
MOCK_BARE_ASSERT_ATTRS = {
    "assert_called",
    "assert_called_once",
    "assert_called_with",
    "assert_called_once_with",
    "assert_not_called",
    "assert_any_call",
    "assert_has_calls",
}

MOCK_TYPO_METHOD_NAMES = {
    "assert_called_with_once": "assert_called_once_with",
    "assert_not_called_with": "assert_not_called",
    "assert_was_called": "assert_called",
    "assert_is_called": "assert_called",
    "assert_called_times": "assert_called",
    "called_once_with": "assert_called_once_with",
    "assert_once_called": "assert_called_once",
    "assert_called_once_without_arguments": "assert_called_once_with",
    "assert_not_called_once": "assert_not_called",
}


def _is_test_function(node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if node.name.startswith("test_") or node.name.endswith("_test"):
        return True
    return False


def _decorator_names(node) -> Set[str]:
    names = set()
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        names.add(".".join(reversed(parts)))
    return names


def _is_disabled(node) -> bool:
    for name in _decorator_names(node):
        if "skip" in name.lower() or "xfail" in name.lower():
            return True
    return False


def _call_name(call: ast.Call) -> str:
    """Rightmost attribute or bare name of a call target."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _root_call_name(call: ast.Call) -> str:
    """Leftmost name in a chain: expect(x).to_be_visible() -> 'expect'."""
    node = call.func
    while True:
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Name):
            return node.id
        else:
            return ""


def _handler_catches_assertion(handler: ast.ExceptHandler) -> bool:
    """Would this except clause catch an AssertionError?"""
    if handler.type is None:
        return True  # bare except

    candidates = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    for node in candidates:
        name = ""
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if name in {"AssertionError", "Exception", "BaseException", "Error"}:
            return True
    return False


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    """Does the handler propagate a failure rather than discard it?"""
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            name = _call_name(node)
            # pytest.fail(...) / self.fail(...) / sys.exit(...) all fail the test.
            if name in {"fail", "exit", "error"}:
                return True
    return False


def _handler_swallows(handler: ast.ExceptHandler) -> bool:
    return _handler_catches_assertion(handler) and not _handler_reraises(handler)


def _describe(node: ast.AST) -> str:
    """Readable, stable identity for an expression.

    Used both to compare a guard against the assertion beneath it and to name
    the subject in the report, so it has to survive round-tripping AND read like
    the source the user wrote.
    """
    try:
        return ast.unparse(node)
    except Exception:
        try:
            return ast.dump(node, annotate_fields=False)
        except Exception:
            return ""


def _visibility_guard_target(test: ast.AST) -> str:
    """If `test` asks whether something is present, return that something's identity.

    Handles `is_visible(x)`, `x.is_visible()`, `x.count() > 0` and `not x.is_hidden()`.
    """
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _visibility_guard_target(test.operand)

    if isinstance(test, ast.Compare) and len(test.comparators) == 1:
        return _visibility_guard_target(test.left)

    if isinstance(test, ast.BoolOp):
        for value in test.values:
            found = _visibility_guard_target(value)
            if found:
                return found
        return ""

    if not isinstance(test, ast.Call):
        return ""

    name = _call_name(test)
    if name not in VISIBILITY_PREDICATES:
        return ""

    # x.is_visible()  ->  subject is x
    if isinstance(test.func, ast.Attribute):
        return _describe(test.func.value)

    # is_visible(x, ...)  ->  subject is the first positional argument
    if test.args:
        return _describe(test.args[0])
    return ""


def _expect_target(call: ast.Call) -> str:
    """expect(x).to_be_visible() -> identity of x."""
    node = call.func
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Call) and node.args:
        return _describe(node.args[0])
    return ""


def _branch_can_fail(body: List[ast.stmt]) -> bool:
    """Does this branch fail the test, rather than log and continue?"""
    for node in body:
        for child in ast.walk(node):
            if isinstance(child, (ast.Raise, ast.Assert)):
                return True
            if isinstance(child, ast.Call):
                name = _call_name(child)
                if name in ASSERT_CALL_NAMES or name.startswith(("to_", "toBe")):
                    return True
    return False


class _Analyzer(ast.NodeVisitor):
    """Walks one module, attributing findings to the enclosing test."""

    def __init__(self, path: Path, source: str, result: ScanResult):
        self.path = path
        self.lines = source.splitlines()
        self.result = result
        # Stack of Try nodes we are currently inside the *body* of.
        self._try_body_stack: List[ast.Try] = []
        self._current_test: Optional[TestCase] = None
        self._current_func_name: str = ""
        self._assertion_count = 0
        self._effective_assertions = 0
        self._forced_calls: List[ast.Call] = []
        # Stack of If nodes we are inside the body of, with the guard's target.
        self._if_guard_stack: List[tuple] = []
        self._unconditional_assertions = 0
        self._loop_depth = 0
        self._loop_assertion_count = 0
        self._outside_loop_assertion_count = 0

    # -- helpers --------------------------------------------------------

    def _snippet(self, lineno: int) -> str:
        idx = lineno - 1
        if 0 <= idx < len(self.lines):
            return self.lines[idx].strip()[:160]
        return ""

    def _swallowing_try(self) -> Optional[ast.Try]:
        """Innermost enclosing try (by body) whose handlers swallow assertions."""
        for node in reversed(self._try_body_stack):
            if any(_handler_swallows(h) for h in node.handlers):
                return node
        return None

    def _add(self, rule: Rule, severity: Severity, lineno: int, detail: str = "") -> None:
        self.result.findings.append(
            Finding(
                rule=rule,
                severity=severity,
                file=self.path,
                line=lineno,
                test_name=self._current_test.name if self._current_test else self._current_func_name,
                detail=detail,
                snippet=self._snippet(lineno),
            )
        )

    def _record_assertion(self, lineno: int, what: str, target: str = "", matcher: str = "") -> None:
        self._assertion_count += 1

        swallowing = self._swallowing_try()
        if swallowing is not None:
            self._add(
                Rule.SWALLOWED_ASSERTION,
                Severity.CRITICAL,
                lineno,
                f"{what} sits inside a try whose except (line {swallowing.handlers[0].lineno}) "
                f"catches it without re-raising",
            )
            return

        # Guarded by a predicate asking the same question the matcher asserts.
        if matcher in PRESENCE_MATCHERS:
            for guard_target, else_fails in self._if_guard_stack:
                if guard_target and not else_fails and guard_target == target:
                    self._add(
                        Rule.SELF_GUARDED_ASSERTION,
                        Severity.CRITICAL,
                        lineno,
                        f"only runs when '{target}' is already known present, and the "
                        f"missing case falls through without failing",
                    )
                    return

        if self._loop_depth > 0:
            self._loop_assertion_count += 1
        else:
            self._outside_loop_assertion_count += 1

        conditional = any(not else_fails for _, else_fails in self._if_guard_stack)
        self._effective_assertions += 1
        if not conditional:
            self._unconditional_assertions += 1

    # -- traversal ------------------------------------------------------

    def visit_If(self, node: ast.If) -> None:
        guard_target = _visibility_guard_target(node.test)
        # An else branch that fails the test makes the assertion mandatory again.
        else_fails = bool(node.orelse) and _branch_can_fail(node.orelse)

        self._if_guard_stack.append((guard_target, else_fails))
        for stmt in node.body:
            self.visit(stmt)
        self._if_guard_stack.pop()

        for stmt in node.orelse:
            self.visit(stmt)


    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self.visit(node.target)
        self._loop_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self._loop_depth -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit(node.iter)
        self.visit(node.target)
        self._loop_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self._loop_depth -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_With(self, node: ast.With) -> None:
        self._check_with_items(node.items, node.lineno)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._check_with_items(node.items, node.lineno)
        self.generic_visit(node)

    def _check_with_items(self, items: List[ast.withitem], lineno: int) -> None:
        for item in items:
            expr = item.context_expr
            if isinstance(expr, ast.Call):
                name = _call_name(expr)
                if name in {"raises", "assertRaises"} and expr.args:
                    first_arg = expr.args[0]
                    exc_name = ""
                    if isinstance(first_arg, ast.Name):
                        exc_name = first_arg.id
                    elif isinstance(first_arg, ast.Attribute):
                        exc_name = first_arg.attr
                    if exc_name in {"Exception", "BaseException"}:
                        has_match = any(kw.arg == "match" for kw in expr.keywords)
                        if not has_match:
                            self._add(
                                Rule.BROAD_RAISES,
                                Severity.HIGH,
                                getattr(expr, "lineno", lineno),
                                f"pytest.raises({exc_name}) with no match= catches all crashes, masking unexpected bugs",
                            )

    def visit_Try(self, node: ast.Try) -> None:
        self._try_body_stack.append(node)
        for stmt in node.body:
            self.visit(stmt)
        self._try_body_stack.pop()

        # Handlers/else/finally are outside the swallowing region.
        for handler in node.handlers:
            for stmt in handler.body:
                self.visit(stmt)
        for stmt in node.orelse + node.finalbody:
            self.visit(stmt)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node) -> None:
        is_test = _is_test_function(node)

        outer_test = self._current_test
        outer_name = self._current_func_name
        outer_asserts = self._assertion_count
        outer_effective = self._effective_assertions
        outer_forced = self._forced_calls
        outer_stack = self._try_body_stack

        self._current_func_name = node.name
        self._assertion_count = 0
        self._effective_assertions = 0
        self._unconditional_assertions = 0
        self._forced_calls = []
        self._try_body_stack = []
        outer_guards = self._if_guard_stack
        self._if_guard_stack = []
        outer_loop_depth = self._loop_depth
        outer_loop_asserts = self._loop_assertion_count
        outer_outside_asserts = self._outside_loop_assertion_count
        self._loop_depth = 0
        self._loop_assertion_count = 0
        self._outside_loop_assertion_count = 0

        if is_test:
            case = TestCase(
                name=node.name,
                file=self.path,
                line=node.lineno,
                is_disabled=_is_disabled(node),
            )
            self._current_test = case
            self.result.tests.append(case)

            if case.is_disabled:
                self._add(Rule.DISABLED_TEST, Severity.LOW, node.lineno, "skipped or xfailed")

            if self._body_is_blanket_try(node):
                self._add(
                    Rule.BLANKET_TRY_EXCEPT,
                    Severity.CRITICAL,
                    node.body[0].lineno,
                    "the entire test body is inside a try whose except discards failures",
                )

        for stmt in node.body:
            self.visit(stmt)

        if is_test and self._current_test is not None:
            self._current_test.effective_assertions = self._effective_assertions

            if self._assertion_count == 0 and not self._current_test.is_disabled:
                self._add(
                    Rule.NO_ASSERTIONS,
                    Severity.CRITICAL,
                    node.lineno,
                    "no assert, no expect matcher, no pytest.raises anywhere in the body",
                )
            elif (
                self._effective_assertions > 0
                and self._unconditional_assertions == 0
                and not self._current_test.is_disabled
            ):
                # Every assertion is skippable, so there is a run of this test
                # that verifies nothing and still reports a pass.
                self._current_test.effective_assertions = 0
                self._add(
                    Rule.OPTIONAL_ASSERTION,
                    Severity.HIGH,
                    node.lineno,
                    f"all {self._effective_assertions} assertion(s) sit inside conditionals "
                    f"with no failing alternative",
                )
            elif (
                self._effective_assertions > 0
                and self._loop_assertion_count > 0
                and self._outside_loop_assertion_count == 0
                and not self._current_test.is_disabled
            ):
                self._add(
                    Rule.LOOP_ONLY_ASSERTION,
                    Severity.HIGH,
                    node.lineno,
                    f"all {self._loop_assertion_count} assertion(s) sit inside a for-loop; "
                    f"if the collection is empty, the test passes without verifying anything",
                )

            # A forced interaction is only interesting when nothing verifies it.
            if self._forced_calls and self._effective_assertions == 0:
                call = self._forced_calls[0]
                self._add(
                    Rule.FORCED_INTERACTION,
                    Severity.HIGH,
                    call.lineno,
                    "force=True bypasses the actionability checks and nothing after it "
                    "verifies the interaction landed",
                )

        elif node.name.startswith(CHECKING_PREFIXES) and self._assertion_count == 0:
            if not self._is_trivial_wrapper(node):
                self._add(
                    Rule.ASSERTION_FREE_HELPER,
                    Severity.HIGH,
                    node.lineno,
                    f"'{node.name}' reads as a verification at every call site but never asserts",
                )

        self._current_test = outer_test
        self._current_func_name = outer_name
        self._assertion_count = outer_asserts
        self._effective_assertions = outer_effective
        self._forced_calls = outer_forced
        self._try_body_stack = outer_stack
        self._if_guard_stack = outer_guards
        self._loop_depth = outer_loop_depth
        self._loop_assertion_count = outer_loop_asserts
        self._outside_loop_assertion_count = outer_outside_asserts

    @staticmethod
    def _is_trivial_wrapper(node) -> bool:
        """A one-line delegation or a bool-returning predicate is not a false promise."""
        body = [s for s in node.body if not isinstance(s, ast.Expr) or not isinstance(s.value, ast.Constant)]
        if len(body) == 1 and isinstance(body[0], ast.Return):
            return True
        return any(
            isinstance(s, ast.Return) and s.value is not None for s in ast.walk(node)
        )

    @staticmethod
    def _body_is_blanket_try(node) -> bool:
        real = [
            s for s in node.body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        ]
        if len(real) != 1 or not isinstance(real[0], ast.Try):
            return False
        return any(_handler_swallows(h) for h in real[0].handlers)

    def visit_Assert(self, node: ast.Assert) -> None:
        if self._is_tautology(node.test):
            self._add(
                Rule.TAUTOLOGICAL_ASSERTION,
                Severity.CRITICAL,
                node.lineno,
                "condition holds regardless of the code under test",
            )
            self._assertion_count += 1
            self.generic_visit(node)
            return

        trap = self._constant_or_trap(node.test)
        if trap is not None:
            self._add(
                Rule.CONSTANT_CONDITION_TRAP,
                Severity.CRITICAL,
                node.lineno,
                f"`assert ... or {trap}` is always true because '{trap}' is a truthy constant",
            )
            self._assertion_count += 1
            self.generic_visit(node)
            return

        self._record_assertion(node.lineno, "assert")
        self.generic_visit(node)

    @staticmethod
    def _constant_or_trap(test: ast.AST) -> Optional[str]:
        """Detect `assert x == 200 or 201` or `assert cond or 'truthy_string'`."""
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            for val in test.values[1:]:
                if isinstance(val, ast.Constant) and bool(val.value):
                    return _describe(val)
        return None

    @staticmethod
    def _is_tautology(test: ast.AST) -> bool:
        if isinstance(test, ast.Constant):
            return bool(test.value)  # assert True / assert 1 / assert "x"
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            if isinstance(test.ops[0], (ast.Eq, ast.Is)):
                try:
                    return ast.dump(test.left) == ast.dump(test.comparators[0])
                except Exception:
                    return False
        return False

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        root = _root_call_name(node)

        if name in MOCK_TYPO_METHOD_NAMES:
            suggested = MOCK_TYPO_METHOD_NAMES[name]
            self._add(
                Rule.MOCK_ASSERTION_TYPO,
                Severity.CRITICAL,
                node.lineno,
                f"'{name}()' is not a valid mock assertion (did you mean '{suggested}()'?)",
            )

        if name in ASSERT_CALL_NAMES:
            self._record_assertion(node.lineno, f"{name}()")
        elif root == "expect" and name.startswith(MATCHER_PREFIXES):
            self._record_assertion(
                node.lineno,
                f"expect(...).{name}()",
                target=_expect_target(node),
                matcher=name,
            )
        elif name in SLEEP_CALLS and self._current_test is not None:
            self._add(
                Rule.SLEEP_INSTEAD_OF_WAIT,
                Severity.LOW,
                node.lineno,
                f"{name}() pauses for a fixed time instead of waiting for a condition",
            )

        for kw in node.keywords:
            if kw.arg == "force" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                self._forced_calls.append(node)

        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        """A bare `expect(x)` statement builds an assertion and throws it away."""
        if isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Name) and call.func.id == "expect":
                self._add(
                    Rule.DANGLING_EXPECT,
                    Severity.CRITICAL,
                    node.lineno,
                    "expect(...) with no matcher - the assertion object is discarded",
                )
        elif isinstance(node.value, ast.Attribute):
            attr_name = node.value.attr
            if attr_name in MOCK_BARE_ASSERT_ATTRS:
                self._add(
                    Rule.MOCK_ASSERTION_TYPO,
                    Severity.CRITICAL,
                    node.lineno,
                    f"'{attr_name}' was accessed as an attribute without (); the assertion never executed",
                )
        self.generic_visit(node)


def scan_python_file(path: Path, result: ScanResult) -> None:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        result.errors.append(f"{path}: could not read ({err})")
        return

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as err:
        result.errors.append(f"{path}: syntax error on line {err.lineno}")
        return

    _Analyzer(path, source, result).visit(tree)
    result.files_scanned += 1
