// Isolated browser regression: synthetic notes; every API request is mocked.
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = path.join(root, 'docs/screenshots/asr-mode');
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
const checks = [], errors = [];
const assert = (condition, description) => { if (!condition) throw Error(description); checks.push(description); };
const origin = 'http://127.0.0.1:19879';
const baseNote = { id: 'mode-note', title: '模式回归合成录音', transcript: '', summary: '', status: 'idle', audioUrl: '',
  asrComplete: false, sourceName: 'synthetic.wav', revision: 1, language: 'zh', template: 'general', segments: [], chat: [], createdAt: new Date().toISOString() };
async function fixture(mode, extra = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const calls = [];
  let settings = { asrConfigured: true, deepseekConfigured: true, tosConfigured: true, tosPrivateConfirmed: true, tosBucket: 'synthetic-private-bucket', tosRegion: 'cn-beijing', ...(mode ? { asrMode: mode } : {}) };
  let note = { ...baseNote, ...extra };
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) { errors.push('Unexpected external request: ' + url.origin); return route.abort(); }
    const json = value => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) });
    if (url.pathname.startsWith('/api/')) {
      const body = request.postDataJSON();
      calls.push({ path: url.pathname, method: request.method(), body });
      if (url.pathname === '/api/bootstrap') return json({ token: 'synthetic-token', accountId: 'mode-fixture', localBrowser: true, settings, notes: [note], capabilities: {} });
      if (url.pathname === '/api/settings') { settings = { ...settings, ...body }; return json(settings); }
      if (url.pathname === '/api/notes') return json({ notes: [note] });
      if (url.pathname === '/api/notes/mode-note') return json(note);
      if (url.pathname === '/api/notes/mode-note/transcribe') {
        note = { ...note, status: 'transcribing', error: '', asrTask: { requestId: 'synthetic-standard-request', state: 'queued' } };
        return json(note);
      }
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":"Unknown mock route"}' });
    }
    const relative = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    const target = path.resolve(root, 'web', relative);
    if (!target.startsWith(path.join(root, 'web') + path.sep)) return route.abort();
    try {
      const contentType = ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.webmanifest': 'application/manifest+json' })[path.extname(target)] || 'application/octet-stream';
      return route.fulfill({ contentType, body: await fs.readFile(target) });
    } catch { return route.fulfill({ status: 404, body: '' }); }
  });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin + '/?note=mode-note');
  await page.locator('#note-page').waitFor({ state: 'visible' });
  return { page, calls, context };
}
try {
  const missing = await fixture();
  await missing.page.locator('#open-settings').click();
  assert(await missing.page.locator('#asr-mode').inputValue() === 'standard', 'Missing stored mode selects standard in settings');
  assert((await missing.page.locator('#asr-mode-caption').innerText()).includes('标准版'), 'Default mode caption identifies standard');
  await missing.page.keyboard.press('Escape');
  await missing.page.locator('#primary-process').click();
  await missing.page.locator('#cloud-upload-dialog').waitFor({ state: 'visible' });
  assert((await missing.page.locator('#cloud-upload-facts').innerText()).includes('标准版异步处理'), 'Missing mode opens standard consent');
  await missing.page.locator('#confirm-cloud-upload').click();
  await missing.page.locator('#note-status-text').filter({ hasText: '排队中' }).waitFor();
  const submission = missing.calls.find(call => call.path.endsWith('/transcribe'));
  assert(submission.body.asrMode === 'standard' && submission.body.cloudUploadConsent === true, 'Submission explicitly sends standard mode and its upload consent');
  assert(!('audioUploadConsent' in submission.body), 'Standard submission does not send fast-mode consent');
  await missing.context.close();

  const recovered = await fixture('standard', { status: 'error', error: '模拟：极速版服务拒绝', fastTask: { state: 'failed', chunks: [] } });
  await recovered.page.locator('#primary-process').click();
  await recovered.page.locator('#cloud-upload-dialog').waitFor({ state: 'visible' });
  assert((await recovered.page.locator('#confirm-cloud-upload').innerText()).includes('上传并提交转写'), 'Rejected fast history can start a standard submission');
  await recovered.page.locator('#confirm-cloud-upload').click();
  await recovered.page.locator('#note-status-text').filter({ hasText: '排队中' }).waitFor();
  assert((await recovered.page.locator('#summary-empty-description').innerText()).includes('标准版在后台排队'), 'Accepted standard task takes precedence over prior fast history in status');
  await recovered.page.evaluate(() => document.fonts.ready);
  await recovered.page.screenshot({ path: path.join(output, 'standard-after-fast-rejection.png'), fullPage: true });
  assert(recovered.calls.filter(call => call.path.endsWith('/transcribe')).length === 1, 'Recovery sends exactly one mocked submission');
  await recovered.context.close();

  const locked = await fixture('fast', { status: 'transcribing', fastTask: { state: 'failed' }, asrTask: { requestId: 'existing-standard-id', state: 'queued' } });
  assert(await locked.page.locator('#primary-process').isDisabled(), 'Existing standard task stays busy despite global fast selection');
  assert((await locked.page.locator('#summary-empty-description').innerText()).includes('标准版在后台排队'), 'Existing task displays its real standard mode');
  assert(!locked.calls.some(call => call.path.endsWith('/transcribe')), 'Viewing existing standard task never submits it again');
  await locked.context.close();

  const explicit = await fixture('fast');
  await explicit.page.locator('#open-settings').click();
  assert(await explicit.page.locator('#asr-mode').inputValue() === 'fast', 'Explicit fast preference remains visible');
  await explicit.page.keyboard.press('Escape');
  await explicit.page.locator('#primary-process').click();
  await explicit.page.locator('#cloud-upload-dialog').waitFor({ state: 'visible' });
  assert((await explicit.page.locator('#confirm-cloud-upload').innerText()).includes('极速转写'), 'Explicit fast choice retains truthful confirmation');
  await explicit.context.close();
  assert(errors.length === 0, 'No browser errors or external requests');
  await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ ok: true, environment: 'Installed Chrome; intercepted synthetic API; no cloud calls', checks, errors }, null, 2));
  console.log(JSON.stringify({ ok: true, checks: checks.length, screenshot: path.join(output, 'standard-after-fast-rejection.png') }));
} finally { await browser.close(); }
