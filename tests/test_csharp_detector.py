"""Tests for C# (.NET) detector (NUnit, xUnit, MSTest, SpecFlow, FluentAssertions)."""

from pathlib import Path
import tempfile
import pytest

from falsegreen.detectors.csharp import scan_csharp_file
from falsegreen.models import Rule, ScanResult, Severity


def scan_snippet(snippet: str) -> ScanResult:
    with tempfile.NamedTemporaryFile(suffix=".cs", mode="w", delete=False, encoding="utf-8") as f:
        f.write(snippet)
        path = Path(f.name)
    try:
        res = ScanResult(root=path.parent)
        scan_csharp_file(path, res)
        return res
    finally:
        if path.exists():
            path.unlink()


def test_clean_nunit_test():
    code = """
    using NUnit.Framework;

    [TestFixture]
    public class UserTests {
        [Test]
        public void TestLoginSuccess() {
            var service = new AuthService();
            var result = service.Login("user", "pass");
            Assert.That(result.IsSuccess, Is.True);
        }
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert res.tests[0].is_trustworthy
    assert len(res.findings) == 0


def test_nunit_no_assertions():
    code = """
    using NUnit.Framework;

    public class OrderTests {
        [Test]
        public void TestCreateOrder() {
            var cart = new ShoppingCart();
            cart.Checkout();
        }
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert not res.tests[0].is_trustworthy
    assert any(f.rule == Rule.NO_ASSERTIONS for f in res.findings)


def test_xunit_fluent_assertions_clean():
    code = """
    using Xunit;
    using FluentAssertions;

    public class PaymentTests {
        [Fact]
        public void ProcessPayment_ReturnsSuccess() {
            var processor = new PaymentProcessor();
            var response = processor.Charge(100);
            response.Status.Should().Be("SUCCESS");
        }
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert res.tests[0].is_trustworthy
    assert len(res.findings) == 0


def test_fluent_assertions_dangling_should():
    code = """
    using Xunit;
    using FluentAssertions;

    public class CartTests {
        [Fact]
        public void CalculateTotal() {
            var total = 100;
            total.Should();
        }
    }
    """
    res = scan_snippet(code)
    assert any(f.rule == Rule.DANGLING_EXPECT for f in res.findings)


def test_csharp_swallowed_assertion():
    code = """
    using NUnit.Framework;
    using System;

    public class ApiTests {
        [Test]
        public void TestEndpoint() {
            try {
                var res = CallApi();
                Assert.That(res.StatusCode, Is.EqualTo(200));
            } catch (Exception ex) {
                // swallowed
            }
        }
    }
    """
    res = scan_snippet(code)
    assert any(f.rule == Rule.SWALLOWED_ASSERTION for f in res.findings)


def test_csharp_tautology_and_sleep():
    code = """
    using System.Threading;
    using Xunit;

    public class AsyncTests {
        [Fact]
        public void TestPolling() {
            Thread.Sleep(2000);
            Assert.True(true);
        }
    }
    """
    res = scan_snippet(code)
    rules = {f.rule for f in res.findings}
    assert Rule.SLEEP_INSTEAD_OF_WAIT in rules
    assert Rule.TAUTOLOGICAL_ASSERTION in rules


def test_csharp_disabled_test():
    code = """
    using Xunit;

    public class FlakyTests {
        [Fact(Skip = "Temporarily broken in CI")]
        public void TestLegacyExport() {
            Assert.Equal(1, 2);
        }
    }
    """
    res = scan_snippet(code)
    assert len(res.tests) == 1
    assert res.tests[0].is_disabled
    assert any(f.rule == Rule.DISABLED_TEST for f in res.findings)
