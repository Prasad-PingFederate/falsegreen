"""Tests for the Python AST detector and industry false-green rules.

Covers:
1. Mock assertion typos:
   - Bare attribute access without () e.g. mock.assert_called_once
   - Misspelled mock methods e.g. mock.assert_called_with_once()
2. Constant condition traps in assert:
   - assert x == 200 or 201 (truthy constant trap)
   - assert status == "ok" or "pending"
3. Broad pytest.raises:
   - with pytest.raises(Exception): without match=
4. Loop-only assertions:
   - Assertions placed solely inside a for-loop with no assertion outside
"""

from __future__ import annotations

from pathlib import Path
import pytest

from falsegreen.detectors.python_ast import scan_python_file
from falsegreen.models import Rule, ScanResult, Severity


def scan(tmp_path: Path, source: str) -> ScanResult:
    f = tmp_path / "test_sample.py"
    f.write_text(source, encoding="utf-8")
    result = ScanResult(root=tmp_path)
    scan_python_file(f, result)
    return result


def rules(result: ScanResult) -> set:
    return {f.rule for f in result.findings}


# ==============================================================================
# Mock Assertion Typo Tests
# ==============================================================================

def test_bare_mock_assert_called_once(tmp_path: Path):
    src = """
def test_user_creation():
    mock_service.assert_called_once
"""
    res = scan(tmp_path, src)
    assert Rule.MOCK_ASSERTION_TYPO in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.MOCK_ASSERTION_TYPO][0]
    assert finding.severity == Severity.CRITICAL
    assert "assert_called_once" in finding.detail


def test_bare_mock_assert_called_with(tmp_path: Path):
    src = """
def test_send_email():
    mailer.assert_called_with
"""
    res = scan(tmp_path, src)
    assert Rule.MOCK_ASSERTION_TYPO in rules(res)


def test_bare_mock_assert_not_called(tmp_path: Path):
    src = """
def test_no_send():
    mailer.assert_not_called
"""
    res = scan(tmp_path, src)
    assert Rule.MOCK_ASSERTION_TYPO in rules(res)


def test_mock_typo_method_call(tmp_path: Path):
    src = """
def test_update_record():
    mock_db.assert_called_with_once("arg1", "arg2")
"""
    res = scan(tmp_path, src)
    assert Rule.MOCK_ASSERTION_TYPO in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.MOCK_ASSERTION_TYPO][0]
    assert "assert_called_once_with" in finding.detail


def test_mock_typo_assert_not_called_with(tmp_path: Path):
    src = """
def test_update():
    mock_db.assert_not_called_with()
"""
    res = scan(tmp_path, src)
    assert Rule.MOCK_ASSERTION_TYPO in rules(res)


def test_mock_valid_calls_no_typo_finding(tmp_path: Path):
    src = """
def test_valid_mocks():
    mock_db.assert_called_once()
    mock_db.assert_called_with(1, 2)
    mock_db.assert_not_called()
"""
    res = scan(tmp_path, src)
    assert Rule.MOCK_ASSERTION_TYPO not in rules(res)
    assert len(res.findings) == 0


# ==============================================================================
# Constant Condition Trap Tests
# ==============================================================================

def test_constant_or_number_trap(tmp_path: Path):
    src = """
def test_http_status():
    status = 500
    assert status == 200 or 201
"""
    res = scan(tmp_path, src)
    assert Rule.CONSTANT_CONDITION_TRAP in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.CONSTANT_CONDITION_TRAP][0]
    assert finding.severity == Severity.CRITICAL
    assert "201" in finding.detail


def test_constant_or_string_trap(tmp_path: Path):
    src = """
def test_response_mode():
    mode = "error"
    assert mode == "live" or "sandbox"
"""
    res = scan(tmp_path, src)
    assert Rule.CONSTANT_CONDITION_TRAP in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.CONSTANT_CONDITION_TRAP][0]
    assert "sandbox" in finding.detail


def test_valid_boolean_or_no_finding(tmp_path: Path):
    src = """
def test_valid_or():
    status = 200
    assert status == 200 or status == 201
"""
    res = scan(tmp_path, src)
    assert Rule.CONSTANT_CONDITION_TRAP not in rules(res)


# ==============================================================================
# Broad Pytest Raises Tests
# ==============================================================================

def test_broad_raises_exception(tmp_path: Path):
    src = """
import pytest

def test_failure():
    with pytest.raises(Exception):
        raise KeyError("missing")
"""
    res = scan(tmp_path, src)
    assert Rule.BROAD_RAISES in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.BROAD_RAISES][0]
    assert finding.severity == Severity.HIGH
    assert "Exception" in finding.detail


def test_broad_raises_base_exception(tmp_path: Path):
    src = """
import pytest

def test_failure():
    with pytest.raises(BaseException):
        raise KeyboardInterrupt()
"""
    res = scan(tmp_path, src)
    assert Rule.BROAD_RAISES in rules(res)


def test_specific_raises_no_finding(tmp_path: Path):
    src = """
import pytest

def test_failure():
    with pytest.raises(ValueError):
        int("abc")
"""
    res = scan(tmp_path, src)
    assert Rule.BROAD_RAISES not in rules(res)


def test_broad_raises_with_match_allowed(tmp_path: Path):
    src = """
import pytest

def test_failure():
    with pytest.raises(Exception, match="Connection refused"):
        raise ConnectionError("Connection refused")
"""
    res = scan(tmp_path, src)
    assert Rule.BROAD_RAISES not in rules(res)


# ==============================================================================
# Loop-Only Assertion Tests
# ==============================================================================

def test_loop_only_assertion(tmp_path: Path):
    src = """
def test_all_items_active():
    items = []
    for item in items:
        assert item.is_active is True
"""
    res = scan(tmp_path, src)
    assert Rule.LOOP_ONLY_ASSERTION in rules(res)
    finding = [f for f in res.findings if f.rule == Rule.LOOP_ONLY_ASSERTION][0]
    assert finding.severity == Severity.HIGH
    assert "for-loop" in finding.detail


def test_loop_with_prior_assertion_not_flagged(tmp_path: Path):
    src = """
def test_all_items_active_guarded():
    items = get_items()
    assert len(items) > 0
    for item in items:
        assert item.is_active is True
"""
    res = scan(tmp_path, src)
    assert Rule.LOOP_ONLY_ASSERTION not in rules(res)
