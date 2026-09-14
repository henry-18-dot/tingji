// Synthetic fixture only: no production service, keys, or paid requests.
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = process.cwd(), output = path.join(root, 'docs/screenshots/minimal-ui');
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
const origin = 'http://127.0.0.1:19883', errors = [], checks = [];
const settings = { asrMode: 'standard', asrConfigured: true, deepseekConfigured: true, tosConfigured: true, tosPrivateConfirmed: true };
const stamp = new Date().toISOString(), archived = new Date(Date.now() - 8 * 86400000).toISOString();
const fixture = { id: 'retained-note', title: '控制理论 第三讲', sourceName: '控制理论 第三讲.webm', summary: '## 稳定性\n\n闭环极点决定系统的稳定性。\n\n- 先建立模型\n- 再检查响应', transcript: '', transcriptPurgedAt: stamp, asrComplete: true, status: 'ready', audioUrl: '/api/audio/retained-note', audioAvailable: true, fileSize: 20, revision: 1, language: 'zh', template: 'lecture', createdAt: archived, updatedAt: stamp, audioArchivedAt: archived, audioDeleteDueAt: new Date(Date.now() - 86400000).toISOString(), cleanupDue: true };
async function makeContext(viewport, localBrowser) {
 const context = await browser.newContext({ permissions: ['microphone'], viewport, serviceWorkers: 'block', reducedMotion: 'reduce' });
 let notes = [structuredClone(fixture)], accepted = [];
 await context.addInitScript(() => {
  const Base = window.MediaRecorder; window.recorders = []; window.microphoneCalls = 0;
  const get = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = (...args) => { window.microphoneCalls++; return get(...args); };
  window.MediaRecorder = class extends Base { constructor(...args) { super(...args); window.recorders.push(this); } };
 });
 await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== origin) { errors.push('external request'); return route.abort(); }
  const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
  if (url.pathname.startsWith('/api/')) {
   if (url.pathname === '/api/bootstrap') return json({ token: 'fixture', accountId: `minimal-fixture-${localBrowser}`, localBrowser, settings, notes, capabilities: {} });
   if (url.pathname === '/api/notes') return json({ notes });
   if (url.pathname === '/api/upload') {
    const sourceName = decodeURIComponent(request.headers()['x-file-name']); accepted.push(sourceName);
    const note = { ...fixture, id: 'new-recording', title: sourceName.replace(/\.[^.]+$/, ''), sourceName, summary: '', transcriptPurgedAt: null, status: 'idle', audioArchivedAt: null, audioDeleteDueAt: null, cleanupDue: false, audioUrl: '', fileSize: request.postDataBuffer().length };
    notes.push(note); return json(note, 201);
   }
   if (url.pathname === '/api/settings') return json(settings);
   const note = notes.find(n => url.pathname === `/api/notes/${n.id}` || url.pathname === `/api/notes/${n.id}/audio-delete`);
   if (note && url.pathname.endsWith('/audio-delete')) { Object.assign(note, { audioDeletedAt: stamp, audioUrl: '', audioAvailable: false, cleanupDue: false }); return json(note); }
   if (note && request.method() === 'PATCH') { Object.assign(note, request.postDataJSON(), { revision: note.revision + 1 }); return json(note); }
   if (note) return json(note);
   if (url.pathname.startsWith('/api/audio/')) return route.fulfill({ contentType: 'audio/webm', body: Buffer.alloc(20) });
   errors.push(`unexpected API: ${url.pathname}`); return json({ error: 'fixture route absent' },404);
  }
  const filename = path.join(root, 'web', url.pathname === '/' ? 'index.html' : url.pathname.slice(1));
  const type = { '.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml', '.png':'image/png', '.webmanifest':'application/manifest+json' }[path.extname(filename)] || 'text/plain';
  try { await route.fulfill({ contentType: type, body: await fs.readFile(filename) }); } catch { await route.fulfill({ status:404, body:'' }); }
 });
 const page = await context.newPage(); page.on('pageerror',e => errors.push(e.message));
 await page.goto(origin); await page.waitForFunction(() => !document.querySelector('#save-settings').disabled);
 return { context, page, accepted, getNotes: () => notes };
}
try {
 const desk = await makeContext({width:1280,height:900},true), p=desk.page;
 await p.locator('#record-button').click();
 assert.equal(await p.evaluate(() => document.activeElement.id),'record-name');
 await p.locator('#start-recording').click(); assert.equal(await p.evaluate(() => window.microphoneCalls),0);
 await p.locator('#record-close').click(); assert.equal(await p.evaluate(() => window.microphoneCalls),0);
 checks.push('Empty/cancelled name never starts microphone');
 await p.locator('#record-button').click(); await p.locator('#record-name').fill('机器人控制 第一课');
 await p.screenshot({path:path.join(output,'desktop-naming.png'),fullPage:true});
 await p.locator('#start-recording').click(); await p.locator('#record-active').waitFor({state:'visible'});
 await p.evaluate(() => new Promise(resolve => window.recorders[0].addEventListener('dataavailable',resolve,{once:true})));
 await p.locator('#stop-recording').click(); await p.locator('#note-page').waitFor({state:'visible'});
 assert.equal(await p.locator('#note-title').inputValue(),'机器人控制 第一课'); assert.ok(desk.accepted[0].startsWith('机器人控制 第一课.')); checks.push('Named recording persists with matching filename/title');
 await p.locator('#new-note').click(); await p.locator('.history-note').filter({hasText:'控制理论 第三讲'}).click();
 await p.locator('#summary-content').waitFor({state:'visible'}); assert.match(await p.locator('#summary-content').innerText(),/闭环极点/);
 assert.equal(await p.locator('#audio-card').isVisible(),false); assert.equal(await p.locator('#tab-transcript, #tab-chat, #regenerate-summary, #demo-button, #paste-button, #asr-mode').count(),0);
 await p.locator('#edit-summary').click(); await p.locator('#summary-editor').fill('## 稳定性\n\n闭环极点决定稳定性。'); await p.locator('#save-summary-edit').click(); await p.locator('#summary-editor-wrap').waitFor({state:'hidden'});
 await p.screenshot({path:path.join(output,'desktop-note.png'),fullPage:true}); checks.push('Existing note retained/editable, removed UI absent');
 await p.locator('#open-archive').click(); await p.locator('.archive-row').waitFor(); assert.match(await p.locator('#archive-reminder').innerText(),/已归档 7 天/); assert.equal(await p.locator('#archive-list audio').count(),1);
 await p.screenshot({path:path.join(output,'desktop-archive.png'),fullPage:true});
 await p.getByRole('button',{name:'删电脑录音',exact:true}).click(); await p.locator('#confirm-delete-audio').click(); await p.locator('#delete-audio-dialog').waitFor({state:'hidden'}); assert.match(await p.locator('#archive-list').innerText(),/暂无归档录音/); assert.match(desk.getNotes()[0].summary,/闭环极点/); checks.push('7-day reminder, archive replay/delete retain note');
 await desk.context.close();
 const tablet = await makeContext({width:820,height:1180},false), t=tablet.page;
 await t.locator('#record-button').click(); await t.locator('#record-name').fill('机构学 第四讲'); await t.locator('#start-recording').click(); await t.locator('#record-active').waitFor({state:'visible'});
 await t.evaluate(() => new Promise(resolve => window.recorders[0].addEventListener('dataavailable',resolve,{once:true})));
 await t.locator('#stop-recording').click(); await t.locator('#note-page').waitFor({state:'visible'}); assert.equal(await t.locator('#note-title').inputValue(),'机构学 第四讲');
 assert.equal(await t.locator('#audio-safe-badge').isVisible(),false); checks.push('Unuploaded tablet recording cannot be deleted');
 await t.evaluate(async () => { const {createSyncClient} = await import('/sync.js'); const c = createSyncClient({accountId:'minimal-fixture-false',getToken:()=> 'fixture'}); await c.ready; await c.cacheAudio(await c.getNote('retained-note'),new Blob([new Uint8Array(20)])); c.dispose(); });
 await t.locator('.history-note').filter({hasText:'控制理论 第三讲'}).click(); await t.locator('#audio-safe-badge').waitFor({state:'visible'});
 await t.screenshot({path:path.join(output,'ipad-note.png'),fullPage:true});
 await t.locator('#delete-device-audio').click(); await t.locator('#confirm-delete-audio').click(); await t.locator('#delete-audio-dialog').waitFor({state:'hidden'}); assert.equal(await t.locator('#audio-safe-badge').isVisible(),false); checks.push('Complete receipt unlocks local delete, local delete retains note');
 await t.locator('#open-sync').click(); await t.locator('#pending-sync-list .sync-note-row').waitFor(); assert.equal(await t.getByRole('button',{name:'上传 机构学 第四讲',exact:true}).isEnabled(),true); checks.push('Pending files have individual upload actions');
 assert.equal(await t.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
 await t.screenshot({path:path.join(output,'ipad-upload.png'),fullPage:true});
 await t.locator('#sync-dialog .dialog-close').click(); await t.setViewportSize({width:600,height:1000});
 assert.equal(await t.locator('#cleanup-reminder').isVisible(),true); await t.locator('#cleanup-reminder').click(); await t.locator('.archive-row').waitFor();
 await t.screenshot({path:path.join(output,'ipad-reminder.png'),fullPage:true}); checks.push('7-day reminder reachable with sidebar collapsed');
 await tablet.context.close();
 assert.deepEqual(errors,[]); console.log(JSON.stringify({checks,errors},null,2));
 await fs.writeFile(path.join(output,'checks.json'),JSON.stringify({checks,errors},null,2));
} finally { await browser.close(); }
