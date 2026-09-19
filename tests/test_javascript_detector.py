"""Tests for the JavaScript / TypeScript detector (Playwright, Jest, Vitest, Testing Library)."""

from __future__ import annotations

from pathlib import Path
import pytest

from falsegreen.detectors.javascript import blank_noncode, scan_js_file
from falsegreen.models import Rule, ScanResult, Severity


def scan(tmp_path: Path, source: str, filename: str = "sample.spec.ts") -> ScanResult:
    f = tmp_path / filename
    f.write_text(source, encoding="utf-8")
    result = ScanResult(root=tmp_path)
    scan_js_file(f, result)
    return result


def rules(result: ScanResult) -> set:
    return {f.rule for f in result.findings}


# ==============================================================================
# Playwright Async Expect Tests
# ==============================================================================

def test_unawaited_playwright_expect(tmp_path: Path):
    src = """
test("dashboard loads", async ({ page }) => {
    await page.goto("/dashboard");
    expect(page.locator("#profile")).toBeVisible();
});
"""
    res = scan(tmp_path, src)
    assert Rule.UNAWAITED_EXPECT in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.UNAWAITED_EXPECT][0]
    assert finding.severity == Severity.CRITICAL
    assert "toBeVisible" in finding.detail


def test_awaited_playwright_expect_valid(tmp_path: Path):
    src = """
test("dashboard loads", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.locator("#profile")).toBeVisible();
});
"""
    res = scan(tmp_path, src)
    assert len(res.findings) == 0


def test_unawaited_resolves_matcher(tmp_path: Path):
    src = """
test("async data fetch", () => {
    expect(fetchData()).resolves.toBe("cached");
});
"""
    res = scan(tmp_path, src)
    assert Rule.UNAWAITED_EXPECT in rules(res)


# ==============================================================================
# Jest / Vitest Synchronous Matcher Tests
# ==============================================================================

def test_sync_jest_expect_valid(tmp_path: Path):
    src = """
test("calculator addition", () => {
    const total = calculator.add(1, 2);
    expect(total).toBe(3);
});
"""
    res = scan(tmp_path, src)
    assert len(res.findings) == 0


def test_testing_library_getby_is_assertion(tmp_path: Path):
    src = """
test("renders submit button", () => {
    render(<Button />);
    screen.getByText("Submit");
});
"""
    res = scan(tmp_path, src)
    assert len(res.findings) == 0


# ==============================================================================
# Swallowed & Missing Assertions
# ==============================================================================

def test_no_assertions_in_js_test(tmp_path: Path):
    src = """
test("no check", async ({ page }) => {
    await page.goto("/home");
    console.log("visited");
});
"""
    res = scan(tmp_path, src)
    assert Rule.NO_ASSERTIONS in rules(res)


def test_swallowed_assertion_in_try_catch(tmp_path: Path):
    src = """
test("swallows error", () => {
    try {
        expect(result).toBe(true);
    } catch (e) {
        // ignore
    }
});
"""
    res = scan(tmp_path, src)
    assert Rule.SWALLOWED_ASSERTION in rules(res)
