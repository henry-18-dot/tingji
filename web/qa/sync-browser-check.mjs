// Isolated local fixture: synthetic notes/audio, random loopback port, no cloud calls.
import { createRequire } from 'node:module';
import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const here = path.dirname(fileURLToPath(import.meta.url)), web = path.resolve(here, '..');
const notes = new Map(), operations = new Map(), uploads = new Map(), requests = [];
const FIXTURE_AUDIO_SIZE = 5 * 8388608 + 2700;
const flags = { dropPushResponse: false, dropPartOne: 0, dropPartResponse: 0, dropCompleteResponse: 0, rejectPart: false, delayPush: false };
let sequence = 0;
function send(res, body, status = 200) { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(body)); }
async function bytes(req) { const chunks = []; for await (const chunk of req) chunks.push(chunk); return Buffer.concat(chunks); }
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost');
    if (!url.pathname.startsWith('/api/')) {
      if (url.pathname === '/') { res.writeHead(200, { 'Content-Type': 'text/html' }); return res.end('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Isolated sync fixture</title><body>Isolated sync fixture — no real notes</body></html>'); }
      const target = path.resolve(web, '.' + url.pathname);
      if (!target.startsWith(web + path.sep)) return send(res, {}, 404);
      const content = await fs.readFile(target);
      const ext = path.extname(target);
      res.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json' })[ext] || 'text/plain' });
      return res.end(content);
    }
    const raw = req.method === 'GET' ? null : await bytes(req);
    const body = req.headers['content-type']?.includes('json') ? JSON.parse(raw.toString()) : raw;
    requests.push({ path: url.pathname, method: req.method, operationId: body?.operationId, chunk: req.headers['x-chunk-sha256'] });
    if (url.pathname === '/api/bootstrap') return send(res, { token: 'token-A', accountId: 'fixture-A' });
    if (url.pathname === '/api/sync/push') {
      const key = req.headers['x-app-token'] + ':' + body.operationId;
      if (operations.has(key)) return send(res, operations.get(key));
      if (flags.delayPush) { flags.delayPush = false; await new Promise(resolve => setTimeout(resolve, 650)); }
      const previous = notes.get(body.note.id);
      if (previous?.transcriptPurgedAt && previous.revision !== body.baseRevision) return send(res, { error: '电脑笔记已有更新。本机修改已保留，请先打开电脑上的笔记。' }, 400);
      const conflict = !!previous && previous.revision !== body.baseRevision;
      const id = previous && !conflict ? previous.id : `remote-${++sequence}`;
      const note = { ...(previous || {}), ...body.note, id, revision: conflict ? 1 : (previous?.revision || 0) + 1, accountId: 'fixture-A', updatedAt: new Date().toISOString(), status: 'idle' };
      if (note.transcriptPurgedAt) Object.assign(note, { transcript: '', segments: [], chat: [] });
      notes.set(id, note);
      const result = { note, conflict, ...(conflict ? { remote: previous } : {}) }; operations.set(key, result);
      if (flags.dropPushResponse) { flags.dropPushResponse = false; return send(res, { error: 'Fixture accepted the operation but acknowledgement is temporarily unavailable.' }, 503); }
      return send(res, result);
    }
    if (url.pathname === '/api/sync/uploads') {
      let upload = uploads.get(body.localId);
      if (!upload) { upload = { uploadId: `upload-${uploads.size + 1}`, ...body, data: new Map(), hashes: new Map() }; uploads.set(body.localId, upload); }
      else if (upload.audioId !== body.audioId || upload.size !== body.size || upload.name !== body.name) return send(res, { error: '本地录音已改变，请另存为新记录后上传。' }, 400);
      return send(res, { uploadId: upload.uploadId, audioId: upload.audioId, chunkSize: 8388608, noteId: upload.note?.id, parts: [...upload.hashes].map(([index, sha256]) => ({ index, sha256 })) });
    }
    const part = url.pathname.match(/^\/api\/sync\/uploads\/(upload-\d+)\/(\d+)$/);
    if (part && req.method === 'PUT') {
      const upload = [...uploads.values()].find(u => u.uploadId === part[1]), index = Number(part[2]);
      const hash = crypto.createHash('sha256').update(raw).digest('hex');
      if (hash !== req.headers['x-chunk-sha256']) return send(res, { error: 'wrong hash' }, 400);
      if (flags.rejectPart) return send(res, { error: 'Fixture rejected the chunk.' }, 400);
      if (index === 5 && flags.dropPartOne) { flags.dropPartOne--; return send(res, { error: 'Fixture interrupted this chunk.' }, 503); }
      if (upload.hashes.has(index) && upload.hashes.get(index) !== hash) return send(res, { error: 'changed chunk' }, 400);
      upload.hashes.set(index, hash); upload.data.set(index, raw);
      if (flags.dropPartResponse) { flags.dropPartResponse--; return send(res, { error: 'Fixture lost the chunk acknowledgement.' }, 503); }
      return send(res, { ok: true });
    }
    const complete = url.pathname.match(/^\/api\/sync\/uploads\/(upload-\d+)\/complete$/);
    if (complete) {
      const upload = [...uploads.values()].find(u => u.uploadId === complete[1]);
      if (!upload.note) { const id = `remote-${++sequence}`; upload.note = { id, localId: upload.localId, title: upload.name, sourceName: upload.name, transcript: '', summary: '', revision: 1, audioAvailable: true, fileSize: upload.size, audioUrl: `/api/audio/${id}`, accountId: 'fixture-A', status: 'idle' }; notes.set(id, upload.note); }
      if (flags.dropCompleteResponse) { flags.dropCompleteResponse--; return send(res, { error: 'Fixture lost the completion acknowledgement.' }, 503); }
      return send(res, { note: upload.note });
    }
    const noteMatch = url.pathname.match(/^\/api\/notes\/([^/]+)$/);
    if (noteMatch) return notes.has(noteMatch[1]) ? send(res, notes.get(noteMatch[1])) : send(res, { error: 'missing note' }, 404);
    const audioMatch = url.pathname.match(/^\/api\/audio\/([^/]+)$/);
    if (audioMatch) {
      const upload = [...uploads.values()].find(u => u.note?.id === audioMatch[1]);
      const audio = Buffer.concat([...upload.data].sort((a, b) => a[0] - b[0]).map(p => p[1]));
      res.writeHead(200, { 'Content-Type': 'audio/webm', 'Content-Length': audio.length }); return res.end(audio);
    }
    send(res, { error: 'unknown fixture endpoint' }, 404);
  } catch (error) { send(res, { error: error.message }, 500); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
const page = await context.newPage();
const browserErrors = [], checks = [];
page.on('pageerror', error => browserErrors.push(error.message));
const assert = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
async function setup() { await page.evaluate(async () => { window.mod = await import('/sync.js'); window.a = mod.createSyncClient({ accountId: 'fixture-A', getToken: () => 'token-A' }); window.b = mod.createSyncClient({ accountId: 'fixture-B', getToken: () => 'token-B' }); await Promise.all([a.ready, b.ready]); }); }
try {
  await page.goto(origin, { waitUntil: 'networkidle' }); await setup();
  const isolated = await page.evaluate(async () => { await a.saveLocalNote({ id: 'local-text', title: 'Synthetic note', transcript: 'Local original', localOnly: true }); return (await b.listNotes()).length === 0 && (await b.getNote('local-text')) === null; });
  assert(isolated, 'Account-scoped IndexedDB isolation');
  flags.dropPushResponse = true;
  const first = await page.evaluate(async () => { try { await a.pushNote('local-text'); return false; } catch { return (await a.getNote('local-text')).dirty; } });
  assert(first, 'Lost response retains local dirty note');
  const pushed = await page.evaluate(() => a.pushNote('local-text'));
  const postRequests = requests.filter(r => r.path === '/api/sync/push');
  assert(postRequests.length === 2 && postRequests[0].operationId === postRequests[1].operationId && operations.size === 1, 'Retry reuses persisted operationId');
  assert(!pushed.note.dirty && (await page.evaluate(() => a.listPending())).length === 0, 'Successful push clears only accepted edits');
  assert(pushed.note._fullCached === true, 'Accepted sync retains the complete offline content marker');
  const originalId = pushed.note.id;
  notes.set(originalId, { ...notes.get(originalId), transcript: 'Computer edit', revision: 9 });
  await page.evaluate(id => a.updateLocalNote(id, { transcript: 'Device conflict edit' }), originalId);
  const conflict = await page.evaluate(id => a.pushNote(id), originalId);
  const twoCopies = await page.evaluate(async ({ oldId, newId }) => ({ old: await a.getNote(oldId), fresh: await a.getNote(newId) }), { oldId: originalId, newId: conflict.note.id });
  assert(conflict.conflict && twoCopies.old.transcript === 'Computer edit' && twoCopies.fresh.transcript === 'Device conflict edit' && !twoCopies.old.dirty && !twoCopies.fresh.dirty, 'Conflict preserves both clean copies');
  assert(twoCopies.old._fullCached === true && twoCopies.fresh._fullCached === true, 'Both conflict versions remain available for offline opening');
  const beforeNoop = requests.length; await page.evaluate(id => a.pushNote(id), conflict.note.id);
  assert(requests.length === beforeNoop, 'Conflict copy is not repeatedly resubmitted');
  await page.evaluate(id => a.updateLocalNote(id, { transcript: 'First device edit' }), conflict.note.id);
  flags.delayPush = true;
  await page.evaluate(id => { window.runningPush = a.pushNote(id); }, conflict.note.id);
  await page.waitForTimeout(130);
  await page.evaluate(id => a.updateLocalNote(id, { transcript: 'Edit while upload was running' }), conflict.note.id);
  const concurrent = await page.evaluate(() => runningPush);
  assert(concurrent.pendingEdits && concurrent.note.dirty && concurrent.note.transcript === 'Edit while upload was running', 'Edits during upload remain dirty and preserved');
  await page.evaluate(id => a.pushNote(id), conflict.note.id);
  const recording = await page.evaluate(async size => { const bytes = new Uint8Array(size); for (let i = 0; i < bytes.length; i++) bytes[i] = i % 251; return a.saveLocalRecording(new Blob([bytes], { type: 'audio/webm' }), { name: 'synthetic-recording.webm' }); }, FIXTURE_AUDIO_SIZE);
  await page.evaluate(() => { const original = Blob.prototype.arrayBuffer; window.largestAudioBuffer = 0; Blob.prototype.arrayBuffer = function () { largestAudioBuffer = Math.max(largestAudioBuffer, this.size); return original.call(this); }; });
  flags.dropPartOne = 3;
  assert(await page.evaluate(async id => { try { await a.pushNote(id); return false; } catch { return !!(await a.getAudioBlob(id)); } }, recording.id), 'Interrupted chunk upload keeps recording Blob');
  assert(await page.evaluate(async id => { try { await a.deleteLocalAudio(id); return false; } catch { return !(await a.getNote(id)).canDeleteLocalAudio && !!(await a.getAudioBlob(id)); } }, recording.id), 'Incomplete audio cannot be marked or deleted as synchronized');
  assert(requests.filter(r => /\/upload-1\/5$/.test(r.path)).length === 3, 'A persistent upload failure stops after three attempts');
  assert(await page.evaluate(() => largestAudioBuffer) === 8388608, 'Hashing reads at most one 8 MB audio chunk at a time');
  await page.reload({ waitUntil: 'networkidle' }); await setup();
  assert(await page.evaluate(async id => (await a.getAudioBlob(id)).size, recording.id) === FIXTURE_AUDIO_SIZE, 'Recording Blob survives a real page reload');
  const audioPushed = await page.evaluate(id => a.pushNote(id), recording.id);
  assert([0, 1, 2, 3, 4].every(index => requests.filter(r => r.path.endsWith(`/upload-1/${index}`)).length === 1) && requests.filter(r => /\/upload-1\/5$/.test(r.path)).length === 4, 'Resuming five of six chunks uploads only the missing final chunk');
  assert(!audioPushed.note.dirty && audioPushed.note.sourceName === 'synthetic-recording.webm', 'Recording and metadata complete without AI calls');
  assert(audioPushed.note.canDeleteLocalAudio, 'Complete upload receipt marks the matching local recording deletable');
  const retryRecording = await page.evaluate(() => a.saveLocalRecording(new Blob(['synthetic retry content'], { type: 'audio/webm' }), { name: 'retry.webm' }));
  flags.dropPartResponse = 1;
  const autoRecovered = await page.evaluate(id => a.pushNote(id), retryRecording.id);
  assert(!autoRecovered.note.audioDirty && requests.filter(r => /\/upload-2\/0$/.test(r.path)).length === 2, 'A lost chunk acknowledgement automatically retries the same bytes');
  assert(uploads.get(retryRecording.localId).data.size === 1, 'Chunk retry never duplicates stored audio');
  const completedRecording = await page.evaluate(() => a.saveLocalRecording(new Blob(['completed before response'], { type: 'audio/webm' }), { name: 'complete-retry.webm' }));
  flags.dropCompleteResponse = 3;
  assert(await page.evaluate(async id => { try { await a.pushNote(id); return false; } catch { return (await a.getNote(id)).audioDirty; } }, completedRecording.id), 'Lost completion response keeps the local recording pending');
  await page.reload({ waitUntil: 'networkidle' }); await setup();
  const recoveredCompletion = await page.evaluate(async id => {
    const digest = crypto.subtle.digest;
    crypto.subtle.digest = () => { throw new Error('Completed audio must not be hashed again'); };
    try { return await a.pushNote(id); } finally { crypto.subtle.digest = digest; }
  }, completedRecording.id);
  assert(!recoveredCompletion.note.audioDirty && recoveredCompletion.note.id === uploads.get(completedRecording.localId).note.id, 'A completed server receipt recovers directly after reload without hashing or uploading again');
  assert(requests.filter(r => /\/upload-3\/0$/.test(r.path)).length === 1, 'Completion retries leave completed chunks untouched');
  const rejectedRecording = await page.evaluate(() => a.saveLocalRecording(new Blob(['rejected fixture bytes'], { type: 'audio/webm' }), { name: 'rejected.webm' }));
  flags.rejectPart = true;
  assert(await page.evaluate(async id => { try { await a.pushNote(id); return false; } catch (error) { return error.message === 'Fixture rejected the chunk.' && !!(await a.getAudioBlob(id)); } }, rejectedRecording.id), 'A permanent upload rejection preserves the error and original recording');
  flags.rejectPart = false;
  assert(requests.filter(r => /\/upload-4\/0$/.test(r.path)).length === 1, 'A permanent upload rejection is never retried');
  const beforeOnline = requests.length;
  await context.setOffline(true); await page.waitForTimeout(100); await context.setOffline(false); await page.waitForTimeout(300);
  assert(requests.length === beforeOnline, 'Reconnection never uploads or calls AI');
  const downloaded = await page.evaluate(id => a.downloadNote(id, { includeAudio: true }), audioPushed.note.id);
  assert(downloaded.audioDownloaded && await page.evaluate(async id => (await a.getAudioBlob(id)).size, audioPushed.note.id) === FIXTURE_AUDIO_SIZE, 'Explicit computer-to-device audio download');
  await page.evaluate(id => a.updateLocalNote(id, { transcript: 'Keep this unsynced text' }), audioPushed.note.id);
  const protectedDownload = await page.evaluate(id => a.downloadNote(id, { includeAudio: false }), audioPushed.note.id);
  assert(protectedDownload.conflict && protectedDownload.note.transcript === 'Keep this unsynced text' && protectedDownload.note.dirty, 'Downloading never overwrites unsynced local edits');
  const staleId = 'metadata-revision-guard';
  const stale = { id: staleId, title: 'Original cached body', transcript: 'Revision zero body', summary: '', revision: 0, accountId: 'fixture-A', asrComplete: false };
  await page.evaluate(note => a.cacheNotes([note]), stale);
  notes.set(staleId, { ...stale, title: 'Computer changed this', transcript: 'Revision one computer body', revision: 1, asrComplete: true });
  await page.evaluate(id => a.cacheNotes([{ id, title: 'Computer changed this', revision: 1, hasTranscript: true }]), staleId);
  const stillOld = await page.evaluate(id => a.getNote(id), staleId);
  assert(stillOld.transcript === 'Revision zero body' && stillOld.baseRevision === 0 && stillOld.revision === 0 && stillOld.remoteRevision === 1, 'Metadata refresh never advances cached body revision');
  await page.evaluate(id => a.updateLocalNote(id, { transcript: 'Offline edit based on revision zero' }), staleId);
  const staleConflict = await page.evaluate(id => a.pushNote(id), staleId);
  assert(staleConflict.conflict && staleConflict.remote.transcript === 'Revision one computer body', 'Offline edit after metadata refresh creates safe conflict copy');
  const downgradeOperation = [...operations.values()].find(result => result.note.transcript === 'Offline edit based on revision zero');
  assert(downgradeOperation.note.asrComplete === false, 'Incomplete ASR state is sent as a conservative downgrade');
  const serverAudio = { id: 'clean-server-audio', title: 'Already on computer', transcript: 'Server final text', summary: '', revision: 4, accountId: 'fixture-A' };
  const cleanAudio = await page.evaluate(async note => { const cached = await a.cacheAudio(note, new Blob(['stored on server'], { type: 'audio/wav' })); return { cached, blob: !!(await a.getAudioBlob(note.id)) }; }, serverAudio);
  assert(cleanAudio.cached && !cleanAudio.cached.dirty && !cleanAudio.cached.audioDirty && cleanAudio.blob, 'Already-saved server audio is cached clean');
  await page.evaluate(id => a.updateLocalNote(id, { transcript: 'Device edit to preserve' }), serverAudio.id);
  const refusedAudio = await page.evaluate(note => a.cacheAudio(note, new Blob(['new server blob'])), serverAudio);
  assert(refusedAudio === null && (await page.evaluate(id => a.getNote(id), serverAudio.id)).transcript === 'Device edit to preserve', 'Clean audio cache refuses to overwrite local dirty edits');
  const cachePurged = await page.evaluate(async note => {
    await a.cacheAudio({ ...note, transcriptPurgedAt: new Date().toISOString(), transcript: '', summary: 'Notes ready' }, new Blob(['ignored new audio']));
    return { note: await a.getNote(note.id), history: await a.listHistory(note.id) };
  }, serverAudio);
  assert(cachePurged.note.transcript === '' && !JSON.stringify(cachePurged.history).includes('Device edit to preserve'), 'An audio-cache refresh also clears purged raw text from dirty local records and history');
  const acceptedPurge = { id: 'accepted-purge-fixture', title: 'Before purge', transcript: 'UNIQUE-PENDING-RAW-TO-PURGE', summary: 'Notes', revision: 3, accountId: 'fixture-A' };
  await page.evaluate(async note => { await a.cacheNotes([note]); await a.updateLocalNote(note.id, { title: 'Edited title' }); }, acceptedPurge);
  notes.set(acceptedPurge.id, { ...acceptedPurge, transcript: '', segments: [], chat: [], transcriptPurgedAt: new Date().toISOString() });
  const acceptedPurgeResult = await page.evaluate(async id => {
    await a.pushNote(id);
    const db = await new Promise((resolve, reject) => { const request = indexedDB.open('tingji-device-sync-v1'); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
    const operations = await new Promise((resolve, reject) => { const request = db.transaction('operations').objectStore('operations').getAll(); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
    db.close(); return { note: await a.getNote(id), operations, history: await a.listHistory(id) };
  }, acceptedPurge.id);
  assert(acceptedPurgeResult.note.transcript === '' && !JSON.stringify(acceptedPurgeResult).includes('UNIQUE-PENDING-RAW-TO-PURGE'), 'A purge received during push clears the completed operation payload as well as note and history');
  const purgeId = autoRecovered.note.id;
  const beforePurge = { ...notes.get(purgeId), transcript: 'UNIQUE-RAW-TEXT-TO-PURGE', segments: [{ text: 'UNIQUE-RAW-TEXT-TO-PURGE' }], chat: [{ content: 'UNIQUE-RAW-TEXT-TO-PURGE' }], summary: 'Older summary' };
  notes.set(purgeId, beforePurge);
  await page.evaluate(note => a.cacheNotes([note]), beforePurge);
  await page.evaluate(id => a.updateLocalNote(id, { summary: 'Keep my unsynced note edits' }), purgeId);
  const tombstone = { ...beforePurge, transcript: '', segments: [], chat: [], summary: 'Computer final notes', transcriptPurgedAt: new Date().toISOString(), revision: beforePurge.revision + 1 };
  notes.set(purgeId, tombstone);
  const purged = await page.evaluate(async note => {
    await a.cacheNotes([{ id: note.id, revision: note.revision, transcriptPurgedAt: note.transcriptPurgedAt, audioAvailable: true }]);
    return { current: await a.getNote(note.id), history: await a.listHistory(note.id) };
  }, tombstone);
  assert(purged.current.transcript === '' && purged.current.segments.length === 0 && purged.current.chat.length === 0 && !JSON.stringify(purged.history).includes('UNIQUE-RAW-TEXT-TO-PURGE'), 'A remote purge clears cached raw text, segments, chat, and local history');
  assert(purged.current.summary === 'Keep my unsynced note edits' && purged.current.dirty, 'A remote purge preserves unsynced user note edits');
  assert(await page.evaluate(async id => { try { await a.pushNote(id); return false; } catch { return (await a.getNote(id)).summary === 'Keep my unsynced note edits'; } }, purgeId), 'Conflicting edits to a purged note are retained locally without restoring old raw text');
  const locallyDeleted = await page.evaluate(async id => ({ note: await a.deleteLocalAudio(id), blob: await a.getAudioBlob(id) }), purgeId);
  assert(!locallyDeleted.note.audioStored && locallyDeleted.blob === null && locallyDeleted.note.summary === 'Keep my unsynced note edits' && locallyDeleted.note.dirty, 'Deleting a fully synced device recording removes its Blob and preserves local note edits');
  const noServerAudio = { ...notes.get(recoveredCompletion.note.id), audioDeletedAt: new Date().toISOString(), audioAvailable: false };
  notes.set(noServerAudio.id, noServerAudio);
  assert(await page.evaluate(async note => {
    await a.cacheNotes([note]);
    try { await a.deleteLocalAudio(note.id); return false; } catch { return !(await a.getNote(note.id)).canDeleteLocalAudio && !!(await a.getAudioBlob(note.id)); }
  }, noServerAudio), 'A deleted computer copy never permits deletion of the remaining device recording');
  const replaceable = await page.evaluate(() => a.saveLocalRecording(new Blob(['old-audio'], { type: 'audio/webm' }), { name: 'named-course.webm', title: '用户输入的课程名' }));
  const named = await page.evaluate(id => a.pushNote(id), replaceable.id);
  assert(named.note.title === '用户输入的课程名', 'The recording name survives complete upload and metadata synchronization');
  const replaced = await page.evaluate(note => a.saveLocalNote(note, new Blob(['new-audio'], { type: 'audio/webm' })), named.note);
  assert(!replaced.canDeleteLocalAudio && replaced.audioDirty, 'Replacing audio invalidates the prior completion receipt even at the same size');
  assert(await page.evaluate(async id => { try { await a.pushNote(id); return false; } catch { return !!(await a.getAudioBlob(id)); } }, replaced.id), 'A same-name same-size replacement cannot reuse an earlier completed upload');
  const stalled = await page.evaluate(() => a.saveLocalRecording(new Blob(['stall-fixture'], { type: 'audio/webm' }), { name: 'stall-fixture.webm' }));
  const readTimeout = await page.evaluate(async id => {
    const read = Blob.prototype.arrayBuffer, schedule = window.setTimeout;
    Blob.prototype.arrayBuffer = () => new Promise(() => {});
    window.setTimeout = (callback, delay, ...args) => schedule(callback, delay === 20000 ? 80 : delay, ...args);
    try { await a.pushNote(id); return false; } catch (error) { return error.message.includes('录音读取超时') && !!(await a.getAudioBlob(id)); }
    finally { Blob.prototype.arrayBuffer = read; window.setTimeout = schedule; }
  }, stalled.id);
  assert(readTimeout, 'A stalled browser Blob read times out with the original recording preserved');
  const resumedAfterRead = await page.evaluate(id => a.pushNote(id), stalled.id);
  assert(!resumedAfterRead.note.audioDirty, 'A timed-out local read releases the upload lock and can resume');
  const cancelledRecording = await page.evaluate(() => a.saveLocalRecording(new Blob(['cancel-fixture'], { type: 'audio/webm' }), { name: 'cancel-fixture.webm' }));
  await page.evaluate(id => {
    window.originalDigest = crypto.subtle.digest;
    crypto.subtle.digest = () => { window.waitingForCancel = true; return new Promise(() => {}); };
    window.cancelledUpload = a.pushNote(id).then(() => false, error => error.message.includes('上传已暂停'));
  }, cancelledRecording.id);
  await page.waitForFunction(() => window.waitingForCancel);
  const cancelledCleanly = await page.evaluate(async id => {
    a.cancelUploads();
    const paused = await cancelledUpload;
    crypto.subtle.digest = originalDigest;
    return paused && !!(await a.getAudioBlob(id));
  }, cancelledRecording.id);
  assert(cancelledCleanly, 'Stop upload aborts a stalled digest and keeps the local recording');
  assert(!(await page.evaluate(id => a.pushNote(id), cancelledRecording.id)).note.audioDirty, 'A stopped upload releases its busy state and resumes normally');
  await page.evaluate(async () => { await mod.registerOfflineShell(); await navigator.serviceWorker.ready; });
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(() => !!navigator.serviceWorker.controller);
  await context.setOffline(true); await page.goto(origin + '/?offline-fixture=1', { waitUntil: 'domcontentloaded' });
  assert((await page.textContent('body')).includes('Isolated sync fixture'), 'Service worker opens cached shell offline');
  const cached = await page.evaluate(async () => { const js = await fetch('/sync.js'); const key = (await caches.keys()).find(name => name.startsWith('tingji-shell-')); const cache = await caches.open(key); return { js: js.status, api: !!(await cache.match('/api/sync/push')) }; });
  assert(cached.js === 200 && !cached.api, 'Only shell assets are cached by service worker');
  await page.goto(origin + '/offline.html', { waitUntil: 'domcontentloaded' }); await page.screenshot({ path: path.join(here, 'offline-mobile.png'), fullPage: true });
  assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)), 'Offline help fits mobile viewport');
  assert(browserErrors.length === 0, 'No browser runtime errors');
  const report = { ok: true, checks, count: checks.length, browser: 'Chrome headless', accountIds: ['fixture-A', 'fixture-B'], externalCalls: 0, aiCalls: 0, note: 'Random-port isolated fixture; synthetic content only.' };
  await fs.writeFile(path.join(here, 'sync-browser-results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
