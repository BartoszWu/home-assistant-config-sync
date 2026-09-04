// Run with PLAYWRIGHT_MODULE pointing to an installed Playwright module and
// IMPORT_TEST_PYTHON pointing to Python with Import's runtime dependencies.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {readFile} from 'node:fs/promises';

test('browser: four frames, tab cache, cancel cleanup and fail-safe', {
  skip: !process.env.PLAYWRIGHT_MODULE, timeout: 60000,
}, async () => {
  const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE);
  const server = spawn(process.env.IMPORT_TEST_PYTHON || 'python3',
    [fileURLToPath(new URL('./preview_fixture.py', import.meta.url))], {stdio: ['ignore', 'pipe', 'pipe']});
  let browser;
  try {
    const lines = createInterface({input: server.stdout});
    const port = await new Promise((resolve, reject) => {
      lines.once('line', resolve);
      server.once('exit', () => reject(new Error('Fixture exited')));
    });
    browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE, timeout: 15000});
    const page = await browser.newPage();
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const url = `http://127.0.0.1:${port}/`;
    await page.goto(url);
    await page.locator('.preview-load').click();
    try {
      await page.getByText('Read-only preview. No dashboard has been saved.', {exact: true}).waitFor();
    } catch (error) {
      throw new Error(JSON.stringify({message: error.message, errors,
        status: await page.locator('.preview-status').textContent(),
        frameCount: page.frames().length - 1,
      }));
    }
    assert.equal(await page.locator('iframe').count(), 4);
    const frames = page.frames().filter(frame => frame !== page.mainFrame());
    const renders = await Promise.all(frames.map(frame => frame.evaluate(() => ({
      width: window.innerWidth, height: window.innerHeight,
      title: document.body.querySelector('hui-root').shadowRoot.querySelector('h1').textContent,
      writes: window.writes,
    }))));
    assert.deepEqual(renders, [
      {width: 1440, height: 900, title: 'Before', writes: 0},
      {width: 1440, height: 900, title: 'After', writes: 0},
      {width: 390, height: 844, title: 'Before', writes: 0},
      {width: 390, height: 844, title: 'After', writes: 0},
    ]);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    if (process.env.PREVIEW_QA_SCREENSHOT) await page.screenshot({path: process.env.PREVIEW_QA_SCREENSHOT, fullPage: true});
    await page.getByRole('tab', {name: 'YAML diff'}).click();
    assert.equal(await page.locator('.yaml-panel').isVisible(), true);
    await page.getByRole('tab', {name: 'Visual', exact: true}).click();
    assert.equal(await page.locator('iframe').count(), 4);
    await page.locator('input[name="selected"]').check();
    await page.getByRole('button', {name: 'Cancel', exact: true}).click();
    assert.equal(await page.locator('iframe').count(), 0);
    assert.equal(await page.locator('input[name="selected"]').isChecked(), false);

    await page.reload();
    await page.evaluate(() => { document.querySelector('.preview-data').textContent = JSON.stringify({error: 'unavailable'}); });
    await page.locator('.preview-load').click();
    await page.getByText('Visual preview unavailable. YAML diff is still available.', {exact: true}).waitFor();
    assert.equal(await page.locator('iframe').count(), 0);
    assert.equal(await page.locator('#apply-button').isEnabled(), true);
    await page.getByRole('tab', {name: 'YAML diff'}).click();
    assert.equal(await page.locator('.yaml-panel').isVisible(), true);
    assert.deepEqual(errors, []);

    // Native frontend error removes even partially mounted frames.
    await page.reload();
    const fixture = await readFile(new URL('./fixtures/native-frontend.html', import.meta.url), 'utf8');
    await page.route('**/test-dashboard/0', route => route.fulfill({contentType: 'text/html',
      body: fixture.replace('<h1></h1><hui-view></hui-view>', '<h1></h1><hui-error-card></hui-error-card>'),
    }));
    await page.locator('.preview-load').click();
    await page.getByText('Visual preview unavailable. YAML diff is still available.', {exact: true}).waitFor();
    assert.equal(await page.locator('iframe').count(), 0);
    assert.equal(await page.locator('#apply-button').isEnabled(), true);

    // A missing/broken JS bundle must not hide the original diff.
    await page.route('**/static/visual-preview.mjs', route => route.abort());
    await page.reload();
    assert.equal(await page.locator('.yaml-panel').isVisible(), true);
  } finally {
    await browser?.close();
    server.kill();
  }
});
