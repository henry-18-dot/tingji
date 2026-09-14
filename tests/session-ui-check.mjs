// Synthetic browser fixture; no production service, credentials or cloud requests.
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = path.join(root, 'docs/screenshots/session-recovery');
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
const origin = 'http://127.0.0.1:19879';
const context = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1280, height: 900 }, serviceWorkers: 'block', reducedMotion: 'reduce' });
const errors = [], calls = [], checks = [];
let token = 'session-before-restart', settings = { asrMode: 'fast', asrConfigured: true, deepseekConfigured: true, tosConfigured: true, tosPrivateConfirmed: true, tosBucket: 'fixture-bucket', tosRegion: 'cn-beijing' };
let note = null, acceptedUploads = 0;
await context.addInitScript(() => {
  window.documentIdentity = crypto.randomUUID();
  const Recorder = window.MediaRecorder;
  window.recorders = [];
  window.MediaRecorder = class extends Recorder { constructor(...args) { super(...args); window.recorders.push(this); } };
});
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== origin) { errors.push('Unexpected external request'); return route.abort(); }
  const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  if (url.pathname.startsWith('/api/')) {
    const call = { path: url.pathname, method: request.method(), token: request.headers()['x-app-token'], actionId: request.headers()['x-action-id'], bytes: request.postDataBuffer() };
    calls.push(call);
    if (url.pathname === '/api/bootstrap') return json({ token, accountId: 'session-fixture', localBrowser: true, settings, notes: note ? [note] : [], capabilities: {} });
    if (request.method() !== 'GET' && call.token !== token) return json({ error: '连接已更新，请重试。', code: 'session_expired' }, 403);
    if (url.pathname === '/api/session-probe') return json({ ok: true });
    if (url.pathname === '/api/upload') {
      acceptedUploads++;
      note = { id: 'recording-fixture', title: '会话恢复合成录音', sourceName: 'synthetic.webm', transcript: '', summary: '', status: 'idle', audioUrl: '', revision: 0, language: 'zh', template: 'general', segments: [], chat: [], createdAt: new Date().toISOString(), fileSize: call.bytes.length };
      return json(note, 201);
    }
    if (url.pathname === '/api/settings') {
      call.body = request.postDataJSON();
      const update = { ...call.body }; if (!update.asrModeChanged) delete update.asrMode;
      settings = { ...settings, ...update }; return json(settings);
    }
    if (url.pathname === '/api/notes') return json({ notes: note ? [note] : [] });
    if (url.pathname === '/api/notes/recording-fixture') return json(note);
    return json({ error: 'Unexpected fixture route' }, 404);
  }
  const target = path.resolve(root, 'web', url.pathname === '/' ? 'index.html' : url.pathname.slice(1));
  if (!target.startsWith(path.join(root, 'web') + path.sep)) return route.abort();
  try {
    const contentType = ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.webmanifest': 'application/manifest+json' })[path.extname(target)] || 'application/octet-stream';
    return route.fulfill({ contentType, body: await fs.readFile(target) });
  } catch { return route.fulfill({ status: 404, body: '' }); }
});
const page = await context.newPage();
page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto(origin);
  await page.waitForFunction(() => !document.querySelector('#save-settings').disabled);
  const identity = await page.evaluate(() => window.documentIdentity);
  await page.locator('#record-button').click();
  await page.locator('#start-recording').click();
  await page.locator('#record-active').waitFor({ state: 'visible' });
  await page.evaluate(() => new Promise(resolve => window.recorders[0].addEventListener('dataavailable', resolve, { once: true })));
  token = 'session-after-restart';
  await page.evaluate(async () => {
    const { sessionFetch } = await import('/session.js');
    const response = await sessionFetch('/api/session-probe', { method: 'POST', accountId: 'session-fixture', headers: { 'X-Action-Id': 'recording-probe' }, body: '{}' });
    if (!response.ok) throw Error('Probe failed');
  });
  assert.equal(await page.evaluate(() => window.recorders[0].state), 'recording');
  assert.equal(await page.evaluate(() => window.documentIdentity), identity);
  assert(await page.locator('#record-active').isVisible());
  checks.push('Token renewal preserves the current recorder and document');
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(output, 'recording-after-session-renewal.png'), fullPage: true });
  token = 'session-before-save';
  await page.locator('#stop-recording').click();
  await page.locator('#note-page').waitFor({ state: 'visible' });
  assert.equal(acceptedUploads, 1);
  const uploads = calls.filter(call => call.path === '/api/upload');
  assert.equal(uploads.length, 2);
  assert(uploads[0].bytes.length > 0);
  assert.deepEqual(uploads[0].bytes, uploads[1].bytes);
  assert.equal(await page.evaluate(() => window.documentIdentity), identity);
  checks.push('Stopping the recorder renews the expired upload token and saves identical audio exactly once');
  settings.asrMode = 'standard';
  await page.locator('#open-settings').click();
  assert.equal(await page.locator('#asr-mode').inputValue(), 'fast');
  await page.locator('#hotwords').fill('机器人');
  await page.locator('#save-settings').click();
  await page.locator('#settings-dialog').waitFor({ state: 'hidden' });
  let saves = calls.filter(call => call.path === '/api/settings');
  assert(!('asrMode' in saves[0].body));
  assert.equal(settings.asrMode, 'standard');
  checks.push('Saving an untouched stale mode preserves the server standard preference');
  await page.locator('#open-settings').click();
  assert.equal(await page.locator('#asr-mode').inputValue(), 'standard');
  await page.locator('#asr-mode').selectOption('fast');
  await page.locator('#save-settings').click();
  await page.locator('#settings-dialog').waitFor({ state: 'hidden' });
  saves = calls.filter(call => call.path === '/api/settings');
  assert.equal(saves[1].body.asrMode, 'fast');
  assert.equal(saves[1].body.asrModeChanged, true);
  checks.push('An explicit mode selection is saved');
  assert.equal(calls.filter(call => call.path === '/api/bootstrap').length, 3);
  assert(!calls.some(call => /transcribe|summarize|live\/start/.test(call.path)));
  assert.deepEqual(errors, []);
  checks.push('No page errors or paid action calls');
  await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ ok: true, environment: 'Installed Chrome; synthetic microphone and intercepted API', checks, errors }, null, 2));
  console.log(JSON.stringify({ ok: true, checks: checks.length, screenshot: path.join(output, 'recording-after-session-renewal.png') }));
} finally { await browser.close(); }
