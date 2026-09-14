// Integrated UI fixture; random local port, synthetic notes/audio, no cloud calls.
import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const here = path.dirname(fileURLToPath(import.meta.url)), web = path.resolve(here, '..');
const notes = new Map(), uploads = new Map(), operations = new Map(), calls = [];
let seq = 0, localBrowser = false, stallBootstrap = false, stallNavigation = false;
let settings = { asrMode: 'fast', asrConfigured: true, deepseekConfigured: true, deepseekModel: 'deepseek-v4-flash', tosConfigured: false, tosPrivateConfirmed: false, tosRegion: 'cn-beijing' };
const initial = { id: 'remote-text', title: '电脑上的完整笔记', transcript: '这是一份电脑全文。\n\n最后一句：离线全文校验标记。', summary: '## 完整内容\n\n- 支持中文与 English。\n- 原文与摘要均可离线查看。', segments: [], chat: [], revision: 1, accountId: 'fixture-ipad', status: 'ready', language: 'zh', template: 'general', sourceName: '粘贴的文稿', audioUrl: '', createdAt: new Date().toISOString() };
notes.set(initial.id, initial);
const wav = Buffer.alloc(44 + 3200); wav.write('RIFF'); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8); wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22); wav.writeUInt32LE(16000, 24); wav.writeUInt32LE(32000, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write('data', 36); wav.writeUInt32LE(3200, 40);
const send = (res, data, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(data)); };
const summary = note => Object.fromEntries(Object.entries({ ...note, hasTranscript: !!note.transcript, hasSummary: !!note.summary }).filter(([key]) => !['transcript', 'summary', 'segments', 'chat'].includes(key)));
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost');
    if (!url.pathname.startsWith('/api/')) {
      if (stallNavigation && url.pathname === '/') return;
      const target = path.resolve(web, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
      if (!target.startsWith(web + path.sep)) return send(res, {}, 404);
      const bytes = await fs.readFile(target);
      res.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.png': 'image/png', '.webmanifest': 'application/manifest+json' })[path.extname(target)] || 'text/plain' }); return res.end(bytes);
    }
    const chunks = []; for await (const chunk of req) chunks.push(chunk); const raw = Buffer.concat(chunks);
    const body = req.headers['content-type']?.includes('json') ? JSON.parse(raw.toString() || '{}') : raw;
    calls.push({ method: req.method, path: url.pathname, actionId: req.headers['x-action-id'], body: body instanceof Buffer ? { size: body.length } : body });
    if (url.pathname === '/api/bootstrap') { if (stallBootstrap) return; return send(res, { token: 'fixture-token', accountId: 'fixture-ipad', localBrowser, settings, remote: { configured: true, url: 'https://private.example.test', account: '个人空间' }, capabilities: { ffmpeg: true }, notes: [...notes.values()].map(summary) }); }
    if (url.pathname === '/api/notes' && req.method === 'GET') return send(res, { notes: [...notes.values()].map(summary) });
    if (url.pathname === '/api/settings') { settings = { ...settings, ...body }; return send(res, settings); }
    if (url.pathname === '/api/sync/uploads') { let item = uploads.get(body.localId); if (!item) { item = { ...body, uploadId: 'upload-' + ++seq, data: new Map(), hashes: new Map() }; uploads.set(body.localId, item); } return send(res, { uploadId: item.uploadId, chunkSize: 8388608, parts: [...item.hashes].map(([index, sha256]) => ({ index, sha256 })) }); }
    const part = url.pathname.match(/^\/api\/sync\/uploads\/(upload-\d+)\/(\d+)$/);
    if (part) { const item = [...uploads.values()].find(item => item.uploadId === part[1]); const hash = crypto.createHash('sha256').update(raw).digest('hex'); if (hash !== req.headers['x-chunk-sha256']) return send(res, { error: 'hash mismatch' }, 400); item.data.set(+part[2], raw); item.hashes.set(+part[2], hash); return send(res, { ok: true }); }
    const complete = url.pathname.match(/^\/api\/sync\/uploads\/(upload-\d+)\/complete$/);
    if (complete) { const item = [...uploads.values()].find(item => item.uploadId === complete[1]); if (!item.note) { const id = 'recording-' + ++seq; item.note = { id, title: item.name, localId: item.localId, sourceName: item.name, transcript: '', summary: '', audioUrl: '/api/audio/' + id, accountId: 'fixture-ipad', revision: 1, status: 'idle', createdAt: new Date().toISOString(), language: 'zh', template: 'general', chat: [], segments: [] }; notes.set(id, item.note); } return send(res, { note: item.note }); }
    if (url.pathname === '/api/sync/push') { if (operations.has(body.operationId)) return send(res, operations.get(body.operationId)); const previous = notes.get(body.note.id); const conflict = !!previous && previous.revision !== body.baseRevision; const id = previous && !conflict ? previous.id : 'note-' + ++seq; const note = { ...(previous || {}), ...body.note, id, revision: conflict ? 1 : (previous?.revision || 0) + 1, accountId: 'fixture-ipad', status: 'idle' }; notes.set(id, note); const result = { note, conflict, ...(conflict ? { remote: previous } : {}) }; operations.set(body.operationId, result); return send(res, result); }
    const get = url.pathname.match(/^\/api\/notes\/([^/]+)$/);
    if (get) { if (req.method === 'PATCH') { if (body.baseRevision !== notes.get(get[1]).revision) return send(res, { error: '版本已更新，请保留本机修改后同步。' }, 400); notes.set(get[1], { ...notes.get(get[1]), ...body, revision: notes.get(get[1]).revision + 1 }); } return send(res, notes.get(get[1])); }
    const transcribe = url.pathname.match(/^\/api\/notes\/([^/]+)\/transcribe$/);
    if (transcribe) { const note = { ...notes.get(transcribe[1]), status: 'ready', transcript: '合成转写原文。', segments: [{ start: 0, end: 1, text: '合成转写原文。' }], summary: '## 合成验证摘要\n\n- 这是隔离测试结果。', asrComplete: true, revision: notes.get(transcribe[1]).revision + 1 }; notes.set(note.id, note); return send(res, note); }
    const audio = url.pathname.match(/^\/api\/audio\/([^/]+)$/);
    if (audio) { res.writeHead(200, { 'Content-Type': 'audio/wav', 'Content-Length': wav.length }); return res.end(wav); }
    if (url.pathname === '/api/upload') { const id = 'desktop-' + ++seq; const note = { ...initial, id, title: '桌面导入', transcript: '', summary: '', sourceName: decodeURIComponent(req.headers['x-file-name']), audioUrl: '/api/audio/' + id, status: 'idle' }; notes.set(id, note); return send(res, note); }
    return send(res, { error: 'Unknown fixture route' }, 404);
  } catch (error) { send(res, { error: error.message }, 500); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, permissions: ['microphone'] });
const page = await context.newPage(), errors = [], checks = [];
page.on('pageerror', error => errors.push(error.message));
const check = (condition, message) => { if (!condition) throw new Error(message); checks.push(message); };
const capture = async name => { await page.waitForTimeout(450); await page.screenshot({ path: path.join(here, name + '.png'), fullPage: true }); };
try {
  await page.goto(origin, { waitUntil: 'networkidle' });
  await page.locator('#storage-location-label').filter({ hasText: '此设备' }).waitFor();
  await page.locator('#file-input').setInputFiles({ name: 'iPad录音.wav', mimeType: 'audio/wav', buffer: wav });
  await page.locator('#note-title').filter({ visible: true }).waitFor();
  await page.locator('#device-note-state').filter({ hasText: '等待你手动同步' }).waitFor();
  check((await page.locator('#audio-player').getAttribute('src')).startsWith('blob:'), 'Device import plays its local Blob');
  check(!calls.some(call => ['/api/upload', '/api/sync/uploads', '/api/sync/push'].includes(call.path)), 'iPad import makes no automatic upload');
  await page.locator('#primary-process').click(); await page.locator('#sync-dialog').waitFor({ state: 'visible' });
  check(!calls.some(call => call.path.endsWith('/transcribe')), 'AI action opens manual sync before any provider request');
  await capture('phone-sync-pending');
  await page.locator('#push-pending-notes').click();
  await page.locator('#sync-progress').filter({ hasText: '已上传到电脑' }).waitFor();
  check(calls.some(call => call.path === '/api/sync/push') && !calls.some(call => call.path.endsWith('/transcribe')), 'Manual push saves to computer without AI');
  await page.keyboard.press('Escape');
  await page.locator('#primary-process').click(); await page.locator('#cloud-upload-dialog').waitFor({ state: 'visible' });
  check((await page.locator('#cloud-upload-facts').textContent()).includes('4.5') && !(await page.locator('#settings-dialog').isVisible()), 'Fast mode consent works without TOS');
  await page.locator('#confirm-cloud-upload').click(); await page.locator('#summary-content').filter({ hasText: '合成验证摘要' }).waitFor();
  const sent = calls.find(call => call.path.endsWith('/transcribe'));
  check(sent.body.asrMode === 'fast' && sent.body.audioUploadConsent === true && sent.body.autoSummarize === true, 'Fast payload matches explicit consent contract');
  check(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(sent.actionId || ''), 'Paid transcription request carries a stable UUID action identifier');
  await page.locator('#tab-transcript').click(); await page.locator('#edit-transcript').click(); await page.locator('#transcript-editor').fill('iPad离线校对，必须保留。'); await page.locator('#save-transcript-edit').click();
  await page.locator('#save-status').filter({ hasText: '待同步' }).waitFor();
  check(!calls.some(call => call.method === 'PATCH'), 'Device edits remain local dirty');
  await page.reload({ waitUntil: 'networkidle' }); await page.locator('#tab-transcript').click(); await page.locator('#transcript-content').filter({ hasText: 'iPad离线校对' }).waitFor();
  check(true, 'Local dirty edit survives real reload');
  await page.locator('#note-open-sync').click();
  for (const input of await page.locator('#computer-sync-list input').all()) await input.uncheck();
  await page.locator('#computer-sync-list input[value="remote-text"]').check();
  await page.locator('#download-selected-notes').click(); await page.locator('#sync-progress').filter({ hasText: '已下载全文' }).waitFor(); await page.keyboard.press('Escape');
  await page.locator('#sidebar-toggle').click(); await page.locator('#history-list button').filter({ hasText: '电脑上的完整笔记' }).click();
  await page.locator('#tab-transcript').click(); await page.locator('#transcript-content').filter({ hasText: '离线全文校验标记' }).waitFor();
  await page.evaluate(async () => { await navigator.serviceWorker.ready; }); await page.waitForFunction(() => !!navigator.serviceWorker.controller);
  await context.setOffline(true); await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('#tab-transcript').click(); await page.locator('#transcript-content').filter({ hasText: '离线全文校验标记' }).waitFor();
  check(true, 'PWA starts offline with complete cached transcript');
  const beforeReconnect = calls.length; await context.setOffline(false); await page.waitForTimeout(350);
  check(calls.length === beforeReconnect, 'Reconnect only prompts; no automatic upload or AI');
  await page.locator('#retry-bootstrap').click(); await page.locator('#connection-error').waitFor({ state: 'hidden' });
  for (const [width, height, name] of [[768, 1024, 'ipad-portrait-note'], [1024, 768, 'ipad-landscape-note']]) {
    await page.setViewportSize({ width, height }); await capture(name);
    check(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)), `${width}x${height} has no horizontal overflow`);
    const small = await page.locator('button:visible, a.icon-button:visible').evaluateAll(nodes => nodes.filter(node => { const r = node.getBoundingClientRect(); return r.width < 43 || r.height < 43; }).map(node => node.id));
    if (small.length) throw new Error('Small visible controls: ' + JSON.stringify(small));
    check(true, `${width}x${height} visible buttons support touch targets`);
  }
  await page.locator('#open-sync').click(); await capture('ipad-landscape-sync');
  await page.locator('#download-selected-notes').scrollIntoViewIfNeeded();
  check(await page.locator('#download-selected-notes').isVisible(), 'iPad landscape sync dialog scrolls to bottom actions');
  const smallSyncButtons = await page.locator('#sync-dialog button').evaluateAll(nodes => nodes.filter(node => !node.hidden && node.getClientRects().length && (node.getBoundingClientRect().width < 43 || node.getBoundingClientRect().height < 43)).map(node => node.id));
  check(!smallSyncButtons.length, 'Sync dialog buttons have 44-pixel touch targets');
  await capture('ipad-landscape-sync-bottom'); await page.keyboard.press('Escape');
  await page.locator('#open-settings').click(); check(await page.locator('#asr-mode').inputValue() === 'fast', 'Settings show fast mode');
  await page.locator('#asr-mode').selectOption('standard'); check(await page.locator('#tos-settings-section').isVisible(), 'Standard mode reveals private TOS settings');
  await page.keyboard.press('Escape');
  await page.locator('#new-note').click();
  const beforeRecording = calls.length;
  await page.locator('#record-button').click(); await page.locator('#start-recording').click(); await page.locator('#record-active').waitFor({ state: 'visible' }); await page.waitForTimeout(1350); await page.locator('#stop-recording').click();
  await page.locator('#device-note-state').filter({ hasText: '等待你手动同步' }).waitFor();
  check(calls.length === beforeRecording, 'Device microphone recording saves without network upload');
  await page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => { const request = indexedDB.open('tingji-recording-recovery', 2); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
    await new Promise((resolve, reject) => { const tx = db.transaction(['metadata', 'accountChunks'], 'readwrite'); tx.objectStore('metadata').put({ name: 'other-account.wav', mime: 'audio/wav', accountId: 'fixture-other' }, 'active:fixture-other'); tx.objectStore('accountChunks').put({ accountId: 'fixture-other', index: 1, blob: new Blob(['other']) }); tx.oncomplete = resolve; tx.onerror = () => reject(tx.error); }); db.close();
  });
  await page.reload({ waitUntil: 'networkidle' });
  check(!(await page.locator('#recording-recovery').isVisible()), 'Recording recovery is isolated from another account');
  localBrowser = true; await page.reload({ waitUntil: 'networkidle' }); await page.locator('#new-note').click();
  await page.locator('#file-input').setInputFiles({ name: 'desktop-test.wav', mimeType: 'audio/wav', buffer: wav });
  await page.locator('#note-title').filter({ visible: true }).waitFor();
  check(calls.filter(call => call.path === '/api/upload').length === 1, 'Computer retains original direct local upload path');
  await page.locator('#history-list button').filter({ hasText: '电脑上的完整笔记' }).click();
  await page.locator('#tab-transcript').click(); await page.locator('#edit-transcript').click(); await page.locator('#transcript-editor').fill('电脑编辑期间的原稿，必须保留。');
  const previous = notes.get('remote-text'); notes.set('remote-text', { ...previous, transcript: 'iPad已经同步的更新稿。', revision: previous.revision + 1 });
  await page.evaluate(async note => { const { createSyncClient } = await import('/sync.js'); const client = createSyncClient({ accountId: 'fixture-ipad' }); await client.ready; await client.cacheNotes([note]); client.dispose(); }, notes.get('remote-text'));
  await page.locator('#save-transcript-edit').click(); await page.locator('#save-status').filter({ hasText: '待同步' }).waitFor();
  const stalePatch = calls.filter(call => call.method === 'PATCH').at(-1);
  check(stalePatch.body.baseRevision === previous.revision, 'PATCH uses the revision captured when editing began');
  const pendingRevision = await page.evaluate(async () => { const { createSyncClient } = await import('/sync.js'); const client = createSyncClient({ accountId: 'fixture-ipad' }); const note = await client.getNote('remote-text'); client.dispose(); return note.baseRevision; });
  check(pendingRevision === previous.revision, 'Background full cache refresh cannot advance the base of an older editor');
  check(notes.get('remote-text').transcript === 'iPad已经同步的更新稿。', 'Stale desktop PATCH is rejected without overwriting device changes');
  await page.locator('#note-open-sync').click(); await page.locator('#push-pending-notes').click(); await page.locator('#sync-progress').filter({ hasText: '版本差异已保留两份' }).waitFor();
  check(notes.get('remote-text').transcript === 'iPad已经同步的更新稿。' && [...notes.values()].some(note => note.transcript === '电脑编辑期间的原稿，必须保留。'), 'Desktop fallback dirty sync preserves both concurrent versions');
  await page.keyboard.press('Escape');
  stallBootstrap = true;
  const bootstrapStart = Date.now(); await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('#connection-label').filter({ hasText: '离线' }).waitFor({ timeout: 7000 });
  try { await page.locator('#note-page').waitFor({ state: 'visible', timeout: 2000 }); }
  catch (error) { console.log('OFFLINE_DIAGNOSTIC', await page.evaluate(async () => { const { createSyncClient } = await import('/sync.js'); const client = createSyncClient({ accountId: 'fixture-ipad' }); const rows = await client.listNotes(); client.dispose(); return { url: location.href, toast: document.querySelector('#toast').textContent, rows: rows.map(note => ({ id: note.id, localId: note.localId, full: note._fullCached, dirty: note.dirty })) }; })); throw error; }
  const bootstrapElapsed = Date.now() - bootstrapStart;
  check(bootstrapElapsed >= 4700 && bootstrapElapsed < 6700, 'Blackholed bootstrap falls back to account cache in about five seconds');
  stallBootstrap = false; stallNavigation = true;
  const navigationStart = Date.now(); await page.reload({ waitUntil: 'domcontentloaded', timeout: 8000 });
  await page.locator('#connection-label').filter({ hasText: '已连接电脑' }).waitFor({ timeout: 2000 });
  const navigationElapsed = Date.now() - navigationStart;
  check(navigationElapsed >= 4700 && navigationElapsed < 7500, 'Blackholed document navigation falls back to the installed shell');
  stallNavigation = false;
  check(errors.length === 0, 'No integrated browser runtime errors');
  const report = { ok: true, checks, count: checks.length, externalCalls: 0, providerCalls: 0, note: 'Random-port synthetic fixture; iPad viewport checks in Chromium, not physical Safari validation.' };
  await fs.writeFile(path.join(here, 'pwa-ui-results.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report));
} finally { await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
