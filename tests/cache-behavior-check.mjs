// Real IndexedDB/browser behavior against an isolated loopback fixture.
// No production files, cloud objects, credentials, or paid providers are used.
import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const web = path.resolve('web');
const notes = new Map(), uploads = new Map(), requests = [];
let sequence = 0, duplicateNext = false;
const stamp = () => new Date().toISOString();
const send = (res, body, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(body)); };
async function rawBody(req) { const parts = []; for await (const part of req) parts.push(part); return Buffer.concat(parts); }
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost');
    if (!url.pathname.startsWith('/api/')) {
      if (url.pathname === '/') { res.writeHead(200, { 'Content-Type': 'text/html' }); return res.end('<!doctype html><title>cache fixture</title>'); }
      const target = path.resolve(web, '.' + url.pathname);
      if (!target.startsWith(web + path.sep)) return send(res, {}, 404);
      const content = await fs.readFile(target);
      res.writeHead(200, { 'Content-Type': path.extname(target) === '.js' ? 'text/javascript' : 'text/plain' }); return res.end(content);
    }
    const bytes = req.method === 'GET' ? null : await rawBody(req);
    const body = req.headers['content-type']?.includes('json') ? JSON.parse(bytes.toString()) : bytes;
    requests.push({ method: req.method, path: url.pathname });
    if (url.pathname === '/api/bootstrap') return send(res, { token: 'fixture-token', accountId: 'cache-fixture' });
    if (url.pathname === '/api/sync/uploads' && req.method === 'POST') {
      let upload = uploads.get(body.localId);
      if (!upload) {
        upload = { id: `upload-${++sequence}`, ...body, parts: new Map(), duplicate: duplicateNext };
        duplicateNext = false; uploads.set(body.localId, upload);
      }
      return send(res, { uploadId: upload.id, audioId: upload.audioId, chunkSize: 65536,
        noteId: upload.duplicate ? 'duplicate-note' : undefined, parts: [] });
    }
    const part = url.pathname.match(/^\/api\/sync\/uploads\/(upload-\d+)\/(\d+)$/);
    if (part && req.method === 'PUT') {
      const upload = [...uploads.values()].find(item => item.id === part[1]);
      upload.parts.set(Number(part[2]), bytes); return send(res, { ok: true });
    }
    const complete = url.pathname.match(/^\/api\/sync\/uploads\/(upload-\d+)\/complete$/);
    if (complete && req.method === 'POST') {
      const upload = [...uploads.values()].find(item => item.id === complete[1]);
      const id = upload.duplicate ? 'duplicate-note' : `remote-${++sequence}`;
      const note = { id, accountId: 'cache-fixture', title: upload.duplicate ? '电脑标题' : upload.name,
        sourceName: upload.name, transcript: '', summary: upload.duplicate ? '电脑成稿' : '', segments: [], chat: [],
        revision: 1, status: 'ready', asrComplete: true, audioAvailable: true, fileSize: upload.size,
        audioUrl: `/api/audio/${id}`, updatedAt: stamp(), ...(upload.duplicate ? { duplicateUpload: true } : {}) };
      upload.note = note; notes.set(id, note); return send(res, { note });
    }
    if (url.pathname === '/api/sync/push' && req.method === 'POST') {
      const previous = notes.get(body.note.id);
      const note = { ...(previous || {}), ...body.note, id: body.note.id, accountId: 'cache-fixture',
        revision: (previous?.revision || 0) + 1, updatedAt: stamp() };
      notes.set(note.id, note); return send(res, { note, conflict: false });
    }
    const noteMatch = url.pathname.match(/^\/api\/notes\/([^/]+)$/);
    if (noteMatch && req.method === 'GET') return notes.has(noteMatch[1]) ? send(res, notes.get(noteMatch[1])) : send(res, { error: 'missing' }, 404);
    return send(res, { error: 'unexpected fixture request' }, 404);
  } catch (error) { return send(res, { error: error.message }, 500); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
const context = await browser.newContext();
const page = await context.newPage();
const checks = [];
const check = (condition, label) => { assert.ok(condition, label); checks.push(label); };
try {
  await page.goto(origin);
  await page.evaluate(async () => { const mod = await import('/sync.js'); window.a = mod.createSyncClient({ accountId: 'cache-fixture', getToken: () => 'fixture-token' }); await a.ready; });

  const uploaded = await page.evaluate(() => a.saveLocalRecording(new Blob(['uploaded-audio'], { type: 'audio/webm' }), { title: '自动清理', name: 'auto.webm' }));
  const pushed = await page.evaluate(id => a.pushNote(id), uploaded.id);
  const afterPush = await page.evaluate(async id => ({ blob: await a.getAudioBlob(id), note: await a.getNote(id) }), uploaded.id);
  check(pushed.note.id && afterPush.blob === null, 'Accepted audio receipt removes device Blob after inFlight release');
  check(afterPush.note && afterPush.note.title === '自动清理', 'Automatic cache cleanup preserves the note body');
  check(requests.some(item => item.method === 'GET' && item.path === `/api/notes/${pushed.note.id}`), 'Automatic cleanup rechecks the computer source in real time');

  const retainedNote = { id: 'retained-note', accountId: 'cache-fixture', title: '保留缓存', transcript: '', summary: '成稿', revision: 1,
    status: 'ready', sourceName: 'retained.webm', audioAvailable: true, fileSize: 14, audioUrl: '/api/audio/retained-note' };
  notes.set(retainedNote.id, { ...retainedNote });
  const retained = await page.evaluate(note => a.cacheAudio(note, new Blob(['retained-audio'], { type: 'audio/webm' })), retainedNote);
  notes.set(retainedNote.id, { ...retainedNote, audioAvailable: false, audioDeletedAt: stamp() });
  const rejected = await page.evaluate(async id => { const before = await a.getNote(id); try { await a.cleanAudioCache(id); return { ok: false, before, blob: !!(await a.getAudioBlob(id)) }; } catch (error) { return { ok: true, before, message: error.message, blob: !!(await a.getAudioBlob(id)) }; } }, retainedNote.id);
  check(rejected.ok && rejected.blob, `Computer deletion marker retains the only device cache (${JSON.stringify(rejected)})`);

  const tombstoned = { ...retainedNote, id: 'remote-tombstone', audioDeletedAt: stamp(), audioAvailable: false };
  notes.set(tombstoned.id, tombstoned);
  await page.evaluate(note => a.cacheAudio({ ...note, audioAvailable: true, audioDeletedAt: null }, new Blob(['local-copy'], { type: 'audio/webm' })), { ...tombstoned, audioDeletedAt: null, audioAvailable: true });
  await page.evaluate(note => a.cacheNotes([note]), tombstoned);
  const localAfterTombstone = await page.evaluate(async id => ({ blobSize: (await a.getAudioBlob(id))?.size ?? null, note: await a.getNote(id) }), tombstoned.id);
  check(localAfterTombstone.blobSize === 10, `Other-device audio tombstone does not delete this device Blob (${JSON.stringify(localAfterTombstone)})`);

  duplicateNext = true;
  const duplicate = await page.evaluate(() => a.saveLocalRecording(new Blob(['duplicate-audio'], { type: 'audio/webm' }), { title: '本机标题', name: 'duplicate.webm' }));
  const duplicateResult = await page.evaluate(id => a.pushNote(id), duplicate.id);
  check(duplicateResult.note.title === '电脑标题' && duplicateResult.note.summary === '电脑成稿', 'Duplicate upload receipt protects the computer note body');
  console.log(JSON.stringify({ ok: true, checks, requests: requests.length }));
} finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
