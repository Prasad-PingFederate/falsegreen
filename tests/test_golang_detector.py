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


def test_go_skip_guarded_by_testing_short_is_not_disabled():
    """if testing.Short() { t.Skip(...) } is the stdlib's own documented pattern.

    Regression test. The test runs fully, real assertion included, on every
    ordinary `go test`; only `go test -short` skips it. Reporting it as an
    unconditional DISABLED_TEST discarded the real t.Fatalf and reported a
    healthy test as broken.
    """
    code = """
    package pkg

    import "testing"

    func TestSlowPath(t *testing.T) {
        if testing.Short() {
            t.Skip("skipping in short mode")
        }
        got := Compute()
        if got != 42 {
            t.Fatalf("got %d", got)
        }
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert not res.tests[0].is_disabled
    assert res.tests[0].effective_assertions > 0
    assert res.tests[0].is_trustworthy
    assert not any(f.rule == Rule.DISABLED_TEST for f in res.findings)


def test_go_skip_guarded_by_env_var_is_not_disabled():
    """if os.Getenv(...) == "" { t.Skip(...) } is the standard integration-test gate."""
    code = """
    package pkg

    import (
        "os"
        "testing"
    )

    func TestIntegration(t *testing.T) {
        if os.Getenv("INTEGRATION") == "" {
            t.Skip("set INTEGRATION=1 to run")
        }
        require.NoError(t, run())
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert not res.tests[0].is_disabled


def test_go_skip_inside_a_subtest_closure_does_not_disable_the_parent():
    """A t.Run subtest's own Skip must not mark the parent test disabled."""
    code = """
    package pkg

    import "testing"

    func TestParent(t *testing.T) {
        t.Run("a", func(t *testing.T) {
            t.Skip()
        })
        require.NoError(t, run())
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert not res.tests[0].is_disabled


def test_go_guarded_skip_still_surfaces_other_findings():
    """A guarded skip must fall through to the checks below it, not swallow them.

    The old `continue` after a matched skip suppressed tautology and sleep
    detection along with the (wrong) disabled verdict - a guarded-skip test
    with a real problem reported only 'disabled-test' and nothing else.
    """
    code = """
    package pkg

    import (
        "testing"
        "time"
    )

    func TestBoth(t *testing.T) {
        if testing.Short() {
            t.Skip()
        }
        time.Sleep(2 * time.Second)
        assert.True(t, true)
    }
    """
    res = scan_snippet(code)
    rules = {f.rule for f in res.findings}
    assert Rule.DISABLED_TEST not in rules
    assert Rule.SLEEP_INSTEAD_OF_WAIT in rules
    assert Rule.TAUTOLOGICAL_ASSERTION in rules
