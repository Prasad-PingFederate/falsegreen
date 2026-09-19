import { test, expect } from '@playwright/test';

// --- SHOULD BE FLAGGED ---

// 1. UNAWAITED_EXPECT - the headline bug
test('unawaited assertion', async ({ page }) => {
  await page.goto('/settings');
  expect(page.locator('#saved')).toBeVisible();
});

// 2. NO_ASSERTIONS
test('no assertions at all', async ({ page }) => {
  await page.goto('/settings');
  await page.locator('#save').click();
});

// 3. SWALLOWED_ASSERTION
test('swallowed', async ({ page }) => {
  try {
    await expect(page.locator('#row')).toBeHidden();
  } catch (e) {
    console.log('ignored');
  }
});

// 4. TAUTOLOGICAL
test('tautology', async ({ page }) => {
  expect(true).toBe(true);
});

// 5. SELF_GUARDED
test('self guarded', async ({ page }) => {
  const el = page.locator('#two-fa');
  if (await el.isVisible()) {
    await expect(el).toBeVisible();
  } else {
    console.log('[WARN] not available');
  }
});

// 6. DANGLING_EXPECT
test('dangling', async ({ page }) => {
  expect(page.locator('#thing'));
});

// 7. test.only - silently skips the whole rest of the file
test.only('the only one that runs', async ({ page }) => {
  await expect(page.locator('#a')).toBeVisible();
});

// --- SHOULD NOT BE FLAGGED ---

// Correct: awaited
test('good awaited', async ({ page }) => {
  await page.goto('/settings');
  await expect(page.locator('#saved-banner')).toBeVisible();
});

// Correct: synchronous Jest-style matcher needs no await
test('good sync matcher', async () => {
  const total = 2 + 2;
  expect(total).toBe(4);
});

// Correct: try/catch that rethrows
test('good rethrow', async ({ page }) => {
  try {
    await expect(page.locator('#a')).toBeVisible();
  } catch (e) {
    await page.screenshot({ path: 'fail.png' });
    throw e;
  }
});

// Correct: guard whose else branch actually fails
test('good guard with failing else', async ({ page }) => {
  const el = page.locator('#x');
  if (await el.isVisible()) {
    await expect(el).toBeVisible();
  } else {
    throw new Error('element missing');
  }
});

// Correct: returned rather than awaited
test('good returned', async ({ page }) => {
  return expect(page.locator('#b')).toBeVisible();
});

// Correct: awaited inside Promise.all
test('good promise all', async ({ page }) => {
  await Promise.all([
    expect(page.locator('#c')).toBeVisible(),
    expect(page.locator('#d')).toBeVisible(),
  ]);
});

// Tricky: the word "expect" appears in a string and a comment only.
// expect(page.locator('#commented')).toBeVisible();
test('good string mentions', async ({ page }) => {
  const msg = "expect(page.locator('#fake')).toBeVisible()";
  const tpl = `also expect(x).toBeVisible() in a template`;
  await expect(page.locator('#real')).toHaveText(msg + tpl);
});
