# falsegreen

**Find tests that cannot fail.**

A passing test suite only means something if its tests were capable of going red.
Some of them weren't. `falsegreen` finds those.

```
  Trust Score  65/100   grade D
  180 of 519 tests (35%) cannot fail.

  CRITICAL Assertion is swallowed by except
           tests/test_vault.py:214  in test_delete_record
           expect(...).to_be_hidden() sits inside a try whose except (line 216)
           catches it without re-raising
           | expect(row).to_be_hidden(timeout=15000)
```

That output is from a real production suite — 519 Playwright tests that had been
reporting ~460 passes a day for months.

---

## Install

```bash
pip install falsegreen
falsegreen .
```

No dependencies. It's stdlib `ast` and nothing else, so it adds no supply-chain
surface to your CI and can't break because a transitive pin moved.

---

## What it finds

| Rule | What it means |
|---|---|
| `no-assertions` | The test runs code and ends. Passes unless something throws. |
| `swallowed-assertion` | An enclosing `except` catches the `AssertionError` and doesn't re-raise. |
| `blanket-try-except` | The whole test body is inside a swallowing `try`. Nothing in it can fail. |
| `self-guarded-assertion` | `if is_visible(x): expect(x).to_be_visible()` — a tautology across two lines. |
| `optional-assertion` | Every assertion sits behind a conditional with no failing alternative. |
| `tautological-assertion` | `assert True`, `assert x == x`. |
| `dangling-expect` | `expect(x)` with no matcher. Builds an assertion object and discards it. |
| `assertion-free-helper` | A function named `assert_*` / `verify_*` / `check_*` that never asserts. |
| `forced-interaction` | `force=True` click with nothing verifying it landed. |
| `sleep-instead-of-wait` | Fixed sleep standing in for a wait condition. |

### The one people argue about

```python
if _is_visible(element, timeout=8000):
    expect(element).to_be_visible()
else:
    print("[WARN] not available")
```

This reads like a careful test. It is a tautology. The assertion only runs when
the element is already known to be present, and the case where it's *missing* —
the bug the test exists to catch — prints a warning and passes.

### Why `assertion-free-helper` matters

```python
def assert_row_gone(self, row_text):
    try:
        expect(row).to_be_hidden(timeout=15000)
    except Exception:
        pass
```

Every caller reads as verified. Nothing is verified. This one shipped in a real
codebase for months, and every `delete_row()` that called it silently
self-certified.

---

## Use it in CI

```yaml
- uses: YOURNAME/falsegreen@v1
  with:
    path: tests/
    fail-under: 80      # fail the build below a Trust Score of 80
```

Findings upload as SARIF, so they appear inline in the PR diff.

The realistic way to adopt this on an existing suite: run it once, note the
score, then set `fail-under` to your current number. Nothing breaks today, and
the score can only go up from there.

---

## Trust Score

The share of tests that can actually fail.

It is deliberately **not** a quality score. A suite of shallow-but-honest tests
scores 100, and that's correct — the claim is only "these tests report real
results," which is the precondition for every other claim your suite makes about
itself.

A score you can argue with in code review is worth more than a weighted index
nobody can explain.

---

## CLI

```bash
falsegreen .                            # scan current directory
falsegreen tests/ --format markdown     # markdown report
falsegreen . --format sarif -o out.sarif
falsegreen . --fail-under 80            # exit 1 below 80
falsegreen . --fail-on critical         # exit 1 on any critical
falsegreen . --quiet                    # just the score line
falsegreen . --include 'check_*.py'     # custom test-file globs
```

---

## What it does not do

- **It does not run your tests.** Pure static analysis, so it's fast and safe on
  any repo, but it cannot know whether an assertion is *meaningful* — only
  whether it's *reachable*.
- **It does not judge coverage.** A test asserting one trivial thing passes every
  rule here. Use coverage tooling for that question.
- **It cannot see through indirection it can't resolve.** If your assertions live
  behind a helper it can't trace, it may report `no-assertions` on a test that is
  genuinely fine. Check the finding before acting on it.

False positives are the thing that kills a tool like this, so the rules are
deliberately conservative: a `try/except` that re-raises is not flagged, and a
conditional with a failing `else` is not flagged.

---

## Python only, for now

JavaScript/TypeScript support (Playwright, Jest, Vitest) is the obvious next
step — `await`-less `expect` is the same bug class and is rampant.

## License

MIT.
