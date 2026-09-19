"""Tests for Kotlin detector (JUnit 5, Kotest, MockK)."""

from pathlib import Path
import tempfile
import pytest

from falsegreen.detectors.kotlin import scan_kotlin_file
from falsegreen.models import Rule, ScanResult, Severity


def scan_snippet(snippet: str) -> ScanResult:
    with tempfile.NamedTemporaryFile(suffix="_test.kt", mode="w", delete=False, encoding="utf-8") as f:
        f.write(snippet)
        path = Path(f.name)
    try:
        res = ScanResult(root=path.parent)
        scan_kotlin_file(path, res)
        return res
    finally:
        if path.exists():
            path.unlink()


def test_clean_junit_kotlin_test():
    code = """
    import org.junit.jupiter.api.Test
    import org.junit.jupiter.api.Assertions.assertEquals

    class CalculatorTest {
        @Test
        fun `test addition of two numbers`() {
            val calc = Calculator()
            val result = calc.add(2, 3)
            assertEquals(5, result)
        }
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert res.tests[0].is_trustworthy
    assert len(res.findings) == 0


def test_clean_kotest_block_and_matchers():
    code = """
    import io.kotest.core.spec.style.FunSpec
    import io.kotest.matchers.shouldBe

    class UserSpec : FunSpec({
        test("user email is formatted correctly") {
            val user = User("admin@example.com")
            user.email shouldBe "admin@example.com"
        }
    })
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert res.tests[0].is_trustworthy
    assert len(res.findings) == 0


def test_kotlin_no_assertions():
    code = """
    import org.junit.jupiter.api.Test

    class EventTest {
        @Test
        fun testEmitEvent() {
            val emitter = EventEmitter()
            emitter.emit("click")
        }
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert not res.tests[0].is_trustworthy
    assert any(f.rule == Rule.NO_ASSERTIONS for f in res.findings)


def test_kotlin_swallowed_assertion():
    code = """
    import org.junit.jupiter.api.Test
    import org.junit.jupiter.api.Assertions.assertEquals

    class ApiTest {
        @Test
        fun testFetchData() {
            try {
                val data = fetchData()
                assertEquals(10, data.size)
            } catch (e: Throwable) {
                // swallowed
            }
        }
    }
    """
    res = scan_snippet(code)
    assert any(f.rule == Rule.SWALLOWED_ASSERTION for f in res.findings)


def test_kotest_tautology_and_sleep():
    code = """
    import io.kotest.core.spec.style.FunSpec
    import io.kotest.matchers.shouldBe

    class FlakySpec : FunSpec({
        test("flaky timeout test") {
            Thread.sleep(1000)
            true shouldBe true
        }
    })
    """
    res = scan_snippet(code)
    rules = {f.rule for f in res.findings}
    assert Rule.SLEEP_INSTEAD_OF_WAIT in rules
    assert Rule.TAUTOLOGICAL_ASSERTION in rules


def test_kotlin_disabled_test():
    code = """
    import org.junit.jupiter.api.Test
    import org.junit.jupiter.api.Disabled

    class DisabledSuite {
        @Disabled("Waiting on fix")
        @Test
        fun testBackendIntegration() {
            assert(false)
        }
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert res.tests[0].is_disabled
    assert any(f.rule == Rule.DISABLED_TEST for f in res.findings)
