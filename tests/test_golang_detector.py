"""Tests for Go detector (testing.T, Testify, Gomega)."""

from pathlib import Path
import tempfile
import pytest

from falsegreen.detectors.golang import scan_golang_file
from falsegreen.models import Rule, ScanResult, Severity


def scan_snippet(snippet: str) -> ScanResult:
    with tempfile.NamedTemporaryFile(suffix="_test.go", mode="w", delete=False, encoding="utf-8") as f:
        f.write(snippet)
        path = Path(f.name)
    try:
        res = ScanResult(root=path.parent)
        scan_golang_file(path, res)
        return res
    finally:
        if path.exists():
            path.unlink()


def test_clean_go_test():
    code = """
    package auth_test

    import (
        "testing"
        "github.com/stretchr/testify/require"
    )

    func TestAuthenticateUser(t *testing.T) {
        user, err := Authenticate("admin", "secret")
        require.NoError(t, err)
        require.Equal(t, "admin", user.Name)
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert res.tests[0].is_trustworthy
    assert len(res.findings) == 0


def test_go_no_assertions():
    code = """
    package worker_test

    import "testing"

    func TestProcessQueue(t *testing.T) {
        q := NewQueue()
        q.Push("job1")
        q.Process()
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert not res.tests[0].is_trustworthy
    assert any(f.rule == Rule.NO_ASSERTIONS for f in res.findings)


def test_go_tautology_and_sleep():
    code = """
    package cache_test

    import (
        "testing"
        "time"
        "github.com/stretchr/testify/assert"
    )

    func TestCacheExpiration(t *testing.T) {
        time.Sleep(500 * time.Millisecond)
        assert.True(t, true)
    }
    """
    res = scan_snippet(code)
    rules = {f.rule for f in res.findings}
    assert Rule.SLEEP_INSTEAD_OF_WAIT in rules
    assert Rule.TAUTOLOGICAL_ASSERTION in rules


def test_go_unconditional_skip():
    code = """
    package net_test

    import "testing"

    func TestExternalNetwork(t *testing.T) {
        t.Skip("Skipping network test in sandbox")
        conn, _ := Dial("example.com")
        if conn == nil {
            t.Fatal("no conn")
        }
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert res.tests[0].is_disabled
    assert any(f.rule == Rule.DISABLED_TEST for f in res.findings)
