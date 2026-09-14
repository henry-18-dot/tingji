// Synthetic fixture only: no production service, keys, or paid requests.
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = process.cwd(), output = path.join(root, 'docs/screenshots/course-ui');
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
const origin = 'http://127.0.0.1:19883', errors = [], checks = [];
const settings = { asrMode: 'standard', asrConfigured: true, deepseekConfigured: true, tosConfigured: true, tosPrivateConfirmed: true };
const stamp = new Date().toISOString(), archived = new Date(Date.now() - 8 * 86400000).toISOString();
const fixture = { id: 'retained-note', title: '控制理论 第三讲', sourceName: '控制理论 第三讲.webm', summary: '## 稳定性\n\n闭环极点决定系统的稳定性。\n\n- 先建立模型\n- 再检查响应', transcript: '', transcriptPurgedAt: stamp, asrComplete: true, status: 'ready', audioUrl: '/api/audio/retained-note', audioAvailable: true, fileSize: 20, revision: 1, language: 'zh', template: 'lecture', createdAt: archived, updatedAt: stamp, audioArchivedAt: archived, audioDeleteDueAt: new Date(Date.now() - 86400000).toISOString(), cleanupDue: true };
async function makeContext(viewport, localBrowser) {
 const context = await browser.newContext({ permissions: ['microphone'], viewport, serviceWorkers: 'block', reducedMotion: 'reduce', hasTouch: !localBrowser });
 let notes = [{ ...structuredClone(fixture), title: '机械制造基础-绪论-周三 12 节', courseName: '机械制造基础', courseColor: '#597d87', duration: 2126 }, { ...structuredClone(fixture), id: 'working-note', title: '待整理录音', summary: '', transcriptPurgedAt: null, asrComplete: false, duration: 900, status: 'transcribing', audioArchivedAt: null, audioDeleteDueAt: null, cleanupDue: false, asrTask: { requestId: 'fixture-task', state: 'processing' } }], accepted = [], bootstrapCount = 0;
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
   if (url.pathname === '/api/bootstrap') { bootstrapCount++; return json({ token: 'fixture', accountId: `minimal-fixture-${localBrowser}`, localBrowser, settings, notes, capabilities: {} }); }
   if (url.pathname === '/api/notes') return json({ notes });
   if (url.pathname === '/api/upload') {
    const sourceName = decodeURIComponent(request.headers()['x-file-name']); accepted.push({ sourceName, recordedAt: request.headers()['x-recorded-at'], duration: request.headers()['x-duration'] });
    const note = { ...fixture, id: 'new-recording', title: sourceName.replace(/\.[^.]+$/, ''), sourceName, summary: '', transcriptPurgedAt: null, status: 'idle', audioArchivedAt: null, audioDeleteDueAt: null, cleanupDue: false, audioUrl: '', fileSize: request.postDataBuffer().length, recordedAt: request.headers()['x-recorded-at'], duration: Number(request.headers()['x-duration']) };
    notes.push(note); return json(note, 201);
   }
   if (url.pathname === '/api/sync/push') { const payload=request.postDataJSON(); const stored=notes.find(n=>n.id===payload.note.id); assert.ok(payload.metadataPatch); assert.ok(stored); Object.assign(stored,payload.metadataPatch,{revision:stored.revision+1}); return json({note:stored,conflict:false}); }
   if (url.pathname === '/api/settings') return json(settings);
   const note = notes.find(n => url.pathname === `/api/notes/${n.id}` || url.pathname.startsWith(`/api/notes/${n.id}/`));
   if (note && /\/(archive|delete|restore)$/.test(url.pathname)) { const action = url.pathname.split('/').pop(); Object.assign(note, action === 'archive' ? { archivedAt: request.postDataJSON().archived ? stamp : null } : { trashedAt: action === 'delete' ? stamp : null }, { revision: note.revision + 1 }); return json(note); }
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
 return { context, page, accepted, getNotes: () => notes, bootstrapCount: () => bootstrapCount };
}
try {
 const desk = await makeContext({width:1280,height:900}, true), p=desk.page;
 await p.locator('.history-note').filter({hasText:'机械制造基础'}).click(); await p.waitForFunction(() => document.querySelector('#note-source').textContent.includes('35:26'));
 assert.match(await p.locator('#note-source').innerText(), /35:26/);
 assert.equal(await p.locator('.course-name').evaluate(el => getComputedStyle(el).color), 'rgb(89, 125, 135)');
 assert.equal(await p.locator('[data-note-id="working-note"]').evaluate(el => el.classList.contains('unorganized')), true);
 await p.locator('#edit-summary').click(); await p.locator('#summary-editor').fill('尚未保存的课堂笔记');
 const before = desk.bootstrapCount(); await p.locator('#refresh-app').click();
 await p.waitForFunction(() => !document.querySelector('#refresh-app').disabled);
 assert.equal(desk.bootstrapCount(),before+1); assert.equal(await p.locator('#summary-editor').inputValue(),'尚未保存的课堂笔记');
 await p.locator('#cancel-summary-edit').click();
 const reloaded = p.waitForEvent('load'); await p.locator('#refresh-app').click(); await reloaded;
 await p.locator('#note-page').waitFor({state:'visible'}); checks.push('Refresh reloads safely and preserves unsaved editor');
 await p.locator('[data-note-id="working-note"] .history-note').click();
 assert.equal(await p.locator('#note-title').getAttribute('readonly'), null);
 await p.locator('#note-title').fill('机器人驱动系统-电机'); await p.locator('#note-source').click();
 await p.waitForFunction(() => document.querySelector('#save-status').textContent.includes('已保存'));
 assert.equal(desk.getNotes()[1].nameSource,'manual'); assert.equal(desk.getNotes()[1].status,'transcribing');
 assert.equal(await p.locator('#audio-download').isVisible(), false);
 await p.locator('#note-audio-more summary').click(); assert.equal(await p.locator('#audio-download').isVisible(), true);
 await p.locator('#note-audio-more summary').click(); checks.push('Busy note can rename, recording backup tucked in More');
 const row = p.locator('[data-note-id="retained-note"]'); await row.hover(); await row.locator('.history-more').click();
 await p.getByRole('menuitem',{name:'归档',exact:true}).click(); await p.waitForFunction(() => !document.querySelector('[data-note-id="retained-note"]'));
 await p.locator('.history-filter summary').click(); await p.locator('[data-history-filter="archived"]').click();
 await p.locator('[data-note-id="retained-note"] .history-more').click(); await p.getByRole('menuitem',{name:'删除',exact:true}).click();
 await p.locator('.history-filter summary').click(); await p.locator('[data-history-filter="trash"]').click();
 await p.locator('[data-note-id="retained-note"] .history-more').click(); await p.getByRole('menuitem',{name:'恢复',exact:true}).click();
 assert.match(desk.getNotes()[0].summary,/闭环极点/); checks.push('Archive/trash/restore preserve note content');
 await p.locator('.history-filter summary').click(); await p.locator('[data-history-filter="active"]').click();
 await p.locator('#new-note').click(); await p.locator('#record-button').click();
 await p.locator('#record-name').fill('课前名称'); await p.locator('#start-recording').click(); await p.locator('#record-active').waitFor({state:'visible'});
 await p.evaluate(() => new Promise(resolve => window.recorders[0].addEventListener('dataavailable',resolve,{once:true})));
 await p.locator('#active-record-name').fill('录音中改名'); await p.locator('#record-timer').click();
 const cache = await p.evaluate(() => new Promise((resolve,reject) => { const req=indexedDB.open('tingji-recording-recovery');req.onsuccess=()=>{const r=req.result.transaction('metadata').objectStore('metadata').get('active:minimal-fixture-true');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);}; }));
 assert.match(cache.name,/录音中改名/); assert.ok(cache.recordedAt);
 await p.screenshot({path:path.join(output,'desktop-recording.png'),fullPage:true});
 await p.locator('#stop-recording').click(); await p.locator('#note-page').waitFor({state:'visible'});
 assert.equal(await p.locator('#note-title').inputValue(),'录音中改名');
 assert.ok(Number(desk.accepted[0].duration)>0); assert.equal(desk.accepted[0].recordedAt,cache.recordedAt);
 checks.push('During-recording rename persists, capture timestamp and duration retained');
 await p.locator('[data-note-id="working-note"] .history-note').click();
 await p.screenshot({path:path.join(output,'desktop-note.png'),fullPage:true});
 await desk.context.close();
 const tablet=await makeContext({width:820,height:1180},false),t=tablet.page;
 assert.equal(await t.locator('#sidebar-toggle').getAttribute('aria-expanded'),'false');
 assert.equal(await t.locator('#sidebar').evaluate(el=>el.getBoundingClientRect().right<=0),true);
 await t.screenshot({path:path.join(output,'ipad-home.png'),fullPage:true});
 await t.locator('#sidebar-toggle').click(); await t.locator('[data-note-id="retained-note"] .history-note').click(); await t.waitForFunction(() => document.querySelector('#note-source').textContent.includes('35:26'));
 assert.match(await t.locator('#note-source').innerText(),/35:26/);
 await t.locator('#sidebar-toggle').click(); assert.equal(await t.locator('[data-note-id="retained-note"] .history-more').evaluate(el=>getComputedStyle(el).opacity),'1');
 await t.screenshot({path:path.join(output,'ipad-sidebar.png'),fullPage:true});
 assert.equal(await t.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 checks.push('Tablet has compact capture page, drawer navigation and visible menu targets');
 await t.locator('#sidebar-backdrop').click();
 await t.locator('#note-title').fill('机械制造基础-新绪论-周三 12 节'); await t.locator('#note-source').click();
 await t.waitForFunction(()=>document.querySelector('#save-status').textContent==='已保存');
 assert.equal(tablet.getNotes()[0].title,'机械制造基础-新绪论-周三 12 节'); assert.equal(tablet.getNotes()[0].nameSource,'manual');
 assert.equal(tablet.accepted.length,0); checks.push('Online tablet title edit syncs metadata without reuploading audio');
 await tablet.context.close();
 assert.deepEqual(errors,[]); console.log(JSON.stringify({checks,errors},null,2));
 await fs.writeFile(path.join(output,'checks.json'),JSON.stringify({checks,errors},null,2));
} finally { await browser.close(); }
