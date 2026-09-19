"""Tests for real-world enterprise test automation scenarios and auto-fixes."""

from __future__ import annotations

from pathlib import Path
import pytest

from falsegreen.detectors.python_ast import scan_python_file
from falsegreen.models import Rule, ScanResult, Severity


def scan(tmp_path: Path, source: str) -> ScanResult:
    f = tmp_path / "test_scenario.py"
    f.write_text(source, encoding="utf-8")
    result = ScanResult(root=tmp_path)
    scan_python_file(f, result)
    return result


def rules(result: ScanResult) -> set:
    return {f.rule for f in result.findings}


# ==============================================================================
# Ignored Predicate Helper Calls (POM & UI tests)
# ==============================================================================

def test_ignored_page_is_visible(tmp_path: Path):
    src = """
def test_login_dialog(page):
    page.goto("/login")
    page.is_visible("#username")
"""
    res = scan(tmp_path, src)
    assert Rule.IGNORED_PREDICATE_CALL in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.IGNORED_PREDICATE_CALL][0]
    assert finding.severity == Severity.CRITICAL
    assert "assert" in finding.suggested_fix
    assert "is_visible" in finding.detail


def test_ignored_custom_predicate_has_row(tmp_path: Path):
    src = """
def test_vault_table(vault_page):
    vault_page.has_row("ID_123")
"""
    res = scan(tmp_path, src)
    assert Rule.IGNORED_PREDICATE_CALL in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.IGNORED_PREDICATE_CALL][0]
    assert "assert vault_page.has_row(\"ID_123\")" in finding.suggested_fix


def test_asserted_predicate_not_flagged(tmp_path: Path):
    src = """
def test_login_dialog_valid(page):
    page.goto("/login")
    assert page.is_visible("#username")
"""
    res = scan(tmp_path, src)
    assert Rule.IGNORED_PREDICATE_CALL not in rules(res)


def test_predicate_in_if_not_flagged_as_ignored(tmp_path: Path):
    src = """
def test_optional_banner(page):
    if page.is_visible("#banner"):
        page.click("#close-banner")
    assert page.is_visible("#main-content")
"""
    res = scan(tmp_path, src)
    assert Rule.IGNORED_PREDICATE_CALL not in rules(res)


# ==============================================================================
# Empty String Substring Tautology Tests
# ==============================================================================

def test_empty_string_in_assertion(tmp_path: Path):
    src = """
def test_header_text(page):
    text = page.title()
    assert "" in text
"""
    res = scan(tmp_path, src)
    assert Rule.EMPTY_STRING_ASSERTION in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.EMPTY_STRING_ASSERTION][0]
    assert finding.severity == Severity.CRITICAL


def test_valid_string_in_assertion_not_flagged(tmp_path: Path):
    src = """
def test_header_text_valid(page):
    text = page.title()
    assert "Dashboard" in text
"""
    res = scan(tmp_path, src)
    assert Rule.EMPTY_STRING_ASSERTION not in rules(res)


# ==============================================================================
# Unfinalized Soft Assertions Tests
# ==============================================================================

def test_unfinalized_soft_assert(tmp_path: Path):
    src = """
def test_multi_field_form(check):
    check.equal(user.name, "Prasad")
    check.equal(user.role, "Admin")
"""
    res = scan(tmp_path, src)
    assert Rule.UNFINALIZED_SOFT_ASSERT in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.UNFINALIZED_SOFT_ASSERT][0]
    assert "assert_all" in finding.suggested_fix


def test_finalized_soft_assert_not_flagged(tmp_path: Path):
    src = """
def test_multi_field_form_valid(check):
    check.equal(user.name, "Prasad")
    check.equal(user.role, "Admin")
    check.assert_all()
"""
    res = scan(tmp_path, src)
    assert Rule.UNFINALIZED_SOFT_ASSERT not in rules(res)


# ==============================================================================
# Suggested Fix Generator Validation
# ==============================================================================

def test_mock_typo_suggested_fix(tmp_path: Path):
    src = """
def test_mock():
    mock_api.assert_called_with_once("arg")
"""
    res = scan(tmp_path, src)
    finding = [f for f in res.findings if f.rule == Rule.MOCK_ASSERTION_TYPO][0]
    assert "mock_api.assert_called_once_with(\"arg\")" in finding.suggested_fix
