# falsegreen

**Find tests that cannot fail.**

A passing test suite only means something if its tests were capable of going red.
Some of them weren't. `falsegreen` finds those.

Works on **Python** (pytest, Playwright) and **JavaScript/TypeScript**
(Playwright, Jest, Vitest, Testing Library).

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
| `unawaited-expect` | JS/TS: an async `expect` nobody awaits, so it cannot fail the test. |
| `disabled-test` | Skipped tests, and `test.only` — which silently skips all the others. |

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

### The JavaScript one that matters most

Playwright's web-first assertions are asynchronous:

```js
await expect(page.locator('#row')).toBeVisible();   // correct
expect(page.locator('#row')).toBeVisible();         // never fails the test
```

The second returns a promise nobody waits on. The assertion settles after the
test has already passed, so a failure shows up as an unhandled rejection at
best and as nothing at all at worst.

This is not a hypothetical. Scanning [vercel/swr](https://github.com/vercel/swr)
turns up:

```js
expect(globalMutate(key, Promise.resolve('data'))).resolves.toBe('data')
```

`.resolves` returns a promise too. No `await`, no failure, ever.

The rule only fires for matchers that are genuinely async, so ordinary
synchronous Jest assertions like `expect(total).toBe(4)` are left alone.

### And `test.only`, which is worse than it looks

```js
test.only('just this one', async ({ page }) => { ... });
```

Committed by accident, this silently stops every other test in the file from
running. CI goes green having executed one test. Flagged as critical.

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

## What it scans

| Language | Frameworks | Files picked up by default |
|---|---|---|
| Python | pytest, Playwright | `test_*.py`, `*_test.py`, `tests/**/*.py` |
| JS / TS | Playwright, Jest, Vitest, Cypress, Testing Library | `*.spec.*`, `*.test.*`, `*.cy.*`, `tests/`, `e2e/`, `__tests__/` |

Use `--include` to add your own patterns.

The JavaScript support has no parser dependency — falsegreen still installs with
zero dependencies. It tokenizes far enough to know code from strings, comments,
template literals and regex literals, so `expect(` inside a comment or a string
never produces a finding.

It also understands that a throwing query is an assertion. React Testing
Library's `getByText` / `findByRole` throw when nothing matches, so a test using
them asserts something even with no `expect` in sight. (`queryBy*` returns null
rather than throwing, so it does not count — which is correct, and is why those
two families are treated differently.)

## Accuracy

Scores from real projects, as a sanity check that this discriminates rather than
calling everything broken:

| Project | Language | Trust Score |
|---|---|---|
| encode/httpx | Python | 99 |
| tiangolo/fastapi | Python | 98 |
| vercel/swr | TypeScript | 98 |
| psf/requests | Python | 94 |
| pallets/flask | Python | 92 |

Well-maintained libraries land in the nineties. A score in the sixties means
something real.

## License

MIT.
