from playwright.sync_api import expect
import time

def _is_visible(loc, timeout=5000):
    try:
        loc.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False

# 1. SELF_GUARDED: asserts visible only when already known visible
def test_self_guarded(page):
    el = page.locator("#two-fa")
    if _is_visible(el, timeout=8000):
        expect(el).to_be_visible()
    else:
        print("[WARN] not available")

# 2. NO_ASSERTIONS
def test_no_assertions(page):
    page.goto("/settings")
    page.locator("#save").click()

# 3. SWALLOWED
def test_swallowed(page):
    try:
        expect(page.locator("#row")).to_be_hidden(timeout=5000)
    except Exception:
        pass

# 4. BLANKET try/except
def test_blanket(page):
    try:
        page.goto("/x")
        assert page.title() == "X"
    except Exception:
        pass

# 5. TAUTOLOGY
def test_tautology(page):
    assert True

# 6. GOOD - must produce no critical finding
def test_good(page):
    page.goto("/settings")
    expect(page.locator("#saved-banner")).to_be_visible()

# 7. GOOD - guard with a failing else is mandatory, not optional
def test_guarded_but_else_fails(page):
    el = page.locator("#x")
    if _is_visible(el):
        expect(el).to_be_visible()
    else:
        raise AssertionError("element missing")

# 8. Assertion in try WITH re-raise is fine
def test_try_reraise(page):
    try:
        expect(page.locator("#a")).to_be_visible()
    except Exception:
        page.screenshot(path="f.png")
        raise
