"""Tests for the Java detector.

The Java-specific judgement worth protecting is the exception hierarchy.
`AssertionError` extends `Error`, not `Exception`, so `catch (Exception e)`
around a JUnit assertion does **not** swallow the failure, while
`catch (Throwable t)` does. Getting that backwards in either direction is
costly: flagging every try/catch trains people to ignore the tool, and missing
`catch (Throwable)` misses the real thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from falsegreen.detectors.java import blank_noncode, scan_java_file
from falsegreen.models import Rule, ScanResult

CLASS = """package com.example;
import org.junit.Test;
public class SampleTest {
%s
}
"""


def scan(tmp_path: Path, body: str) -> ScanResult:
    f = tmp_path / "SampleTest.java"
    f.write_text(CLASS % body, encoding="utf-8")
    result = ScanResult(root=tmp_path)
    scan_java_file(f, result)
    return result


def rules(result: ScanResult) -> set:
    return {f.rule for f in result.findings}


def named(result: ScanResult, name: str):
    return next(t for t in result.tests if t.name == name)


# ---------------------------------------------------------------------------
# Source normalisation
# ---------------------------------------------------------------------------


def test_braces_inside_strings_do_not_affect_matching():
    src = 'void f() { String s = "}"; int x = 1; }'
    blanked = blank_noncode(src)
    assert len(blanked) == len(src)
    assert blanked.count("}") == 1


def test_comments_are_blanked_but_line_numbers_survive():
    src = "a\n// }\nb\n"
    blanked = blank_noncode(src)
    assert blanked.count("\n") == src.count("\n")
    assert "}" not in blanked


# ---------------------------------------------------------------------------
# The core judgement
# ---------------------------------------------------------------------------


def test_a_test_that_only_drives_the_app_is_flagged(tmp_path: Path):
    result = scan(tmp_path, """
    @Test
    public void driveAndHope() {
        driver.get("/login");
        driver.findElement(By.id("go")).click();
    }
""")
    assert Rule.NO_ASSERTIONS in rules(result)
    assert not named(result, "driveAndHope").is_trustworthy


@pytest.mark.parametrize(
    "assertion",
    [
        'assertEquals("a", b);',
        "assertTrue(flag);",
        'assertThat(title).isEqualTo("Welcome");',
        "Assert.assertNotNull(element);",
        "Assertions.assertAll(checks);",
        "verify(service).save(entity);",
        'fail("should not reach");',
    ],
)
def test_real_assertions_are_recognised(tmp_path: Path, assertion: str):
    """A miss here reports a healthy suite as broken, which gets the tool switched off."""
    result = scan(tmp_path, f"""
    @Test
    public void checksSomething() {{
        {assertion}
    }}
""")
    assert Rule.NO_ASSERTIONS not in rules(result), f"{assertion} not seen as an assertion"


# ---------------------------------------------------------------------------
# The exception hierarchy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caught", ["Throwable t", "Error e", "AssertionError e"])
def test_catching_an_error_swallows_the_assertion(tmp_path: Path, caught: str):
    result = scan(tmp_path, f"""
    @Test
    public void swallows() {{
        try {{
            assertEquals("Welcome", driver.getTitle());
        }} catch ({caught}) {{
            log("ignored");
        }}
    }}
""")
    assert Rule.SWALLOWED_ASSERTION in rules(result)
    assert not named(result, "swallows").is_trustworthy


def test_catching_exception_does_not_swallow_an_assertion(tmp_path: Path):
    """AssertionError is an Error, so `catch (Exception e)` never sees it.

    Flagging this would be wrong, and would be the most common false positive
    the detector could possibly produce, since the pattern is everywhere in
    Selenium code.
    """
    result = scan(tmp_path, """
    @Test
    public void stillFails() {
        try {
            assertEquals("Welcome", driver.getTitle());
        } catch (Exception e) {
            log("not an AssertionError");
        }
    }
""")
    assert Rule.SWALLOWED_ASSERTION not in rules(result)
    assert named(result, "stillFails").is_trustworthy


@pytest.mark.parametrize("handler", ["throw e;", 'fail("boom");', "Assert.fail();"])
def test_a_handler_that_rethrows_is_not_swallowing(tmp_path: Path, handler: str):
    result = scan(tmp_path, f"""
    @Test
    public void rethrows() {{
        try {{
            assertEquals("Welcome", driver.getTitle());
        }} catch (Throwable e) {{
            {handler}
        }}
    }}
""")
    assert Rule.SWALLOWED_ASSERTION not in rules(result)


# ---------------------------------------------------------------------------
# Assertions that do not look like assertions
# ---------------------------------------------------------------------------


def test_expected_exception_attribute_counts_as_an_assertion(tmp_path: Path):
    """@Test(expected = ...) asserts, though the body contains no assert."""
    result = scan(tmp_path, """
    @Test(expected = IllegalStateException.class)
    public void expectsAnException() {
        service.boom();
    }
""")
    assert Rule.NO_ASSERTIONS not in rules(result)


def test_assert_throws_counts_as_an_assertion(tmp_path: Path):
    result = scan(tmp_path, """
    @Test
    public void expectsAnException() {
        assertThrows(IllegalStateException.class, () -> service.boom());
    }
""")
    assert Rule.NO_ASSERTIONS not in rules(result)


def test_an_explicit_wait_counts_as_an_assertion(tmp_path: Path):
    """WebDriverWait throws TimeoutException when the condition never holds."""
    result = scan(tmp_path, """
    @Test
    public void waits() {
        new WebDriverWait(driver, 10).until(ExpectedConditions.titleIs("Welcome"));
    }
""")
    assert Rule.NO_ASSERTIONS not in rules(result)


# ---------------------------------------------------------------------------
# Other rules
# ---------------------------------------------------------------------------


def test_thread_sleep_is_flagged(tmp_path: Path):
    result = scan(tmp_path, """
    @Test
    public void sleeps() throws Exception {
        Thread.sleep(5000);
        assertTrue(ok);
    }
""")
    assert Rule.SLEEP_INSTEAD_OF_WAIT in rules(result)
    assert named(result, "sleeps").is_trustworthy


@pytest.mark.parametrize(
    "assertion,flagged",
    [
        ("assertTrue(true);", True),
        ("assertEquals(actual, actual);", True),
        ("assertFalse(false);", True),
        ("assertTrue(flag);", False),
        ("assertEquals(expected, actual);", False),
    ],
)
def test_tautological_assertions(tmp_path: Path, assertion: str, flagged: bool):
    result = scan(tmp_path, f"""
    @Test
    public void checks() {{
        {assertion}
    }}
""")
    assert (Rule.TAUTOLOGICAL_ASSERTION in rules(result)) is flagged


@pytest.mark.parametrize("annotation", ["@Disabled", "@Ignore"])
def test_disabled_tests_are_recorded(tmp_path: Path, annotation: str):
    result = scan(tmp_path, f"""
    @Test
    {annotation}
    public void skipped() {{
        assertEquals(1, 2);
    }}
""")
    assert Rule.DISABLED_TEST in rules(result)
    assert named(result, "skipped").is_disabled


def test_testng_enabled_false_is_a_disabled_test(tmp_path: Path):
    result = scan(tmp_path, """
    @Test(enabled = false)
    public void skipped() {
        assertEquals(1, 2);
    }
""")
    assert named(result, "skipped").is_disabled


def test_a_helper_named_verify_that_never_asserts_is_reported(tmp_path: Path):
    result = scan(tmp_path, """
    @Test
    public void usesHelper() {
        verifyDashboard();
    }

    private void verifyDashboard() {
        log("pretending");
    }
""")
    assert Rule.ASSERTION_FREE_HELPER in rules(result)


def test_a_helper_that_does_assert_is_left_alone(tmp_path: Path):
    result = scan(tmp_path, """
    private void verifyDashboard() {
        assertTrue(dashboard.isVisible());
    }
""")
    assert Rule.ASSERTION_FREE_HELPER not in rules(result)


def test_assertions_only_inside_an_if_are_optional(tmp_path: Path):
    result = scan(tmp_path, """
    @Test
    public void maybeChecks() {
        if (flag) {
            assertEquals("Welcome", driver.getTitle());
        }
    }
""")
    assert Rule.OPTIONAL_ASSERTION in rules(result)


def test_an_if_with_an_else_is_not_optional(tmp_path: Path):
    result = scan(tmp_path, """
    @Test
    public void checksBothWays() {
        if (flag) {
            assertEquals("A", title);
        } else {
            assertEquals("B", title);
        }
    }
""")
    assert Rule.OPTIONAL_ASSERTION not in rules(result)


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_parameterized_and_repeated_tests_are_collected(tmp_path: Path):
    result = scan(tmp_path, """
    @ParameterizedTest
    public void paramTest() {
        driver.get("/x");
    }

    @RepeatedTest(3)
    public void repeated() {
        driver.get("/y");
    }
""")
    assert {t.name for t in result.tests} == {"paramTest", "repeated"}


def test_an_unreadable_file_is_recorded_not_raised(tmp_path: Path):
    result = ScanResult(root=tmp_path)
    scan_java_file(tmp_path / "Missing.java", result)

    assert result.files_scanned == 0
    assert len(result.errors) == 1
