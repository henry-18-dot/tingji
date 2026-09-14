// Explicit, account-scoped device synchronisation. This module never starts AI work.
import { sessionFetch } from './session.js';

const DATABASE = 'tingji-device-sync-v1';
const VERSION = 1;
const DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024;
const UPLOAD_TIMEOUT_MS = 180000;
const TRANSIENT_STATUS = new Set([408, 429, 500, 502, 503, 504]);
const STORES = ['notes', 'audio', 'aliases', 'operations', 'uploads', 'history', 'meta'];
const CLIENTS = new Set();
const LOCKS = new Map();
let databasePromise;

const uid = () => crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(36)}-${crypto.getRandomValues(new Uint32Array(4)).join('-')}`;
const now = () => new Date().toISOString();
const copy = value => structuredClone(value);
function bounded(promise, message, { timeoutMs = 20000, signal, onTimeout } = {}) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => { if (settled) return; settled = true; clearTimeout(timer); signal?.removeEventListener('abort', cancel); callback(value); };
    const cancel = () => finish(reject, new Error('上传已暂停，点“上传到电脑”继续。'));
    const timer = setTimeout(() => { onTimeout?.(); finish(reject, new Error(message)); }, timeoutMs);
    signal?.addEventListener('abort', cancel, { once: true });
    if (signal?.aborted) cancel();
    Promise.resolve(promise).then(value => finish(resolve, value), error => finish(reject, error));
  });
}
const storageWait = promise => bounded(promise, '本机存储暂时没有响应。请保持听记在前台，刷新后继续。');
function accountName(value) {
  if (typeof value !== 'string' || !value.trim()) throw new Error('请先连接电脑，确认当前账号后再保存离线笔记。');
  return value.trim();
}
function openDatabase() {
  if (!globalThis.indexedDB) return Promise.reject(new Error('当前浏览器无法保存离线数据，请检查隐私模式或存储权限。'));
  if (!databasePromise) databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      const notes = db.createObjectStore('notes', { keyPath: ['accountId', 'localId'] });
      notes.createIndex('accountId', 'accountId');
      const audio = db.createObjectStore('audio', { keyPath: ['accountId', 'localId'] });
      audio.createIndex('accountId', 'accountId');
      db.createObjectStore('aliases', { keyPath: ['accountId', 'id'] });
      db.createObjectStore('operations', { keyPath: ['accountId', 'operationId'] });
      db.createObjectStore('uploads', { keyPath: ['accountId', 'localId'] });
      const history = db.createObjectStore('history', { keyPath: ['accountId', 'versionId'] });
      history.createIndex('accountId', 'accountId');
      db.createObjectStore('meta', { keyPath: 'key' });
    };
    request.onsuccess = () => {
      const db = request.result;
      db.onversionchange = () => { db.close(); databasePromise = undefined; };
      resolve(db);
    };
    request.onerror = () => { databasePromise = undefined; reject(request.error); };
    request.onblocked = () => reject(new Error('离线存储正在升级，请关闭其他听记页面后重试。'));
  });
  return storageWait(databasePromise);
}
async function read(store, key) {
  const db = await openDatabase();
  return storageWait(new Promise((resolve, reject) => {
    const transaction = db.transaction(store, 'readonly');
    const request = transaction.objectStore(store).get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    transaction.onabort = () => reject(transaction.error || new Error('本机录音读取中断，请重试。'));
  }));
}
async function accountRows(store, accountId) {
  const db = await openDatabase();
  return storageWait(new Promise((resolve, reject) => {
    const transaction = db.transaction(store, 'readonly');
    const request = transaction.objectStore(store).index('accountId').getAll(accountId);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    transaction.onabort = () => reject(transaction.error || new Error('本机笔记读取中断，请重试。'));
  }));
}
async function storeRows(store, accountId) {
  const db = await openDatabase();
  return storageWait(new Promise((resolve, reject) => {
    const request = db.transaction(store, 'readonly').objectStore(store).getAll();
    request.onsuccess = () => resolve(request.result.filter(row => row.accountId === accountId));
    request.onerror = () => reject(request.error);
  }));
}
async function write(entries) {
  if (!entries.length) return;
  const db = await openDatabase();
  const transaction = db.transaction([...new Set(entries.map(([store]) => store))], 'readwrite');
  return bounded(new Promise((resolve, reject) => {
    entries.forEach(([store, value, action]) => action === 'delete' ? transaction.objectStore(store).delete(value) : transaction.objectStore(store).put(value));
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error || new Error('离线保存未完成，请检查设备剩余空间。'));
  }), '本机保存超时，请保持听记在前台后重试。', { onTimeout: () => { try { transaction.abort(); } catch { /* Already settled. */ } } });
}
function serial(accountId, callback) {
  const name = `tingji-sync:${accountId}`;
  if (navigator.locks?.request) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    return navigator.locks.request(name, { signal: controller.signal }, async () => { clearTimeout(timer); return callback(); })
      .catch(error => { if (error.name === 'AbortError') throw new Error('另一个听记页面正在保存。请关闭其他听记页面后重试。'); throw error; })
      .finally(() => clearTimeout(timer));
  }
  const previous = LOCKS.get(name) || Promise.resolve();
  const next = previous.catch(() => {}).then(callback);
  LOCKS.set(name, next);
  next.finally(() => { if (LOCKS.get(name) === next) LOCKS.delete(name); }).catch(() => {});
  return next;
}
function publicNote(record) {
  if (!record) return null;
  return { ...copy(record.note), localId: record.localId, accountId: record.accountId, localOnly: !!record.localOnly,
    baseRevision: record.baseRevision || 0, dirty: !!record.dirty, audioStored: !!record.audioKey, audioDirty: !!record.audioDirty,
    remoteRevision: record.remoteRevision ?? null, syncError: record.syncError || '', syncConflict: !!record.syncConflict,
    canDeleteLocalAudio: !!(record.audioKey && !record.audioDirty && record.audioReceipt?.audioKey === record.audioKey
      && record.audioReceipt?.noteId === record.note.id && record.note.audioAvailable !== false && !record.note.audioDeletedAt),
    localAudioDeletedAt: record.localAudioDeletedAt || null,
    audioCacheCleanedAt: record.audioCacheCleanedAt || null,
    offlineReady: !!(record.note._fullCached && String(record.note.summary || '').trim()) };
}
export const canDeleteLocalAudio = note => !!(note?.audioStored && note.canDeleteLocalAudio && !note.audioDirty);
function withoutTranscript(note, tombstone = note?.transcriptPurgedAt) {
  return tombstone ? { ...note, transcript: '', segments: [], chat: [], transcriptPurgedAt: tombstone, hasTranscript: false, summaryStale: false } : note;
}
function archive(record, reason) {
  return ['history', { accountId: record.accountId, versionId: uid(), localId: record.localId, savedAt: now(), reason, record: copy(record) }];
}
function recordEntries(record, extraAliases = []) {
  return [['notes', record], ...[...new Set([record.localId, record.note.id, ...extraAliases].filter(Boolean))]
    .map(id => ['aliases', { accountId: record.accountId, id, localId: record.localId }])];
}
async function resolveRecord(accountId, id) {
  const alias = await read('aliases', [accountId, id]);
  return read('notes', [accountId, alias?.localId || id]);
}
function assertAccount(accountId, note) {
  if (note?.accountId && note.accountId !== accountId) throw new Error('这份笔记属于另一个账号，无法写入当前离线空间。');
}
function noteBody(note) {
  // File paths, signed URLs, account ownership and cloud task state are server-owned.
  const allowed = ['id', 'localId', 'title', 'transcript', 'summary', 'summaryStale', 'language', 'template', 'createdAt', 'updatedAt', 'sourceName', 'chat', 'recordedAt', 'duration', 'nameSource', 'archivedAt', 'trashedAt'];
  const safe = withoutTranscript(note);
  const body = Object.fromEntries(allowed.filter(key => safe[key] !== undefined).map(key => [key, copy(safe[key])]));
  if (note.asrComplete === false) body.asrComplete = false;
  return body;
}
function makeRecord(accountId, note, existing, dirty = false) {
  const localId = existing?.localId || note.localId || note.id || `local-${uid()}`;
  const id = note.id || localId;
  return { accountId, localId, note: withoutTranscript({ title: '未命名笔记', transcript: '', summary: '', language: 'auto', template: 'general',
      sourceName: '', segments: [], chat: [], status: 'idle', createdAt: now(), updatedAt: now(), ...(existing?.note || {}), ...copy(note), id,
      _fullCached: ('transcript' in note && 'summary' in note) || !!note.localOnly || !!existing?.note?._fullCached }, note.transcriptPurgedAt || existing?.note?.transcriptPurgedAt),
    baseRevision: existing?.baseRevision ?? Number(note.baseRevision ?? note.revision ?? 0),
    dirty, localOnly: existing?.localOnly ?? (note.localOnly ?? !note.revision),
    editVersion: (existing?.editVersion || 0) + (dirty ? 1 : 0),
    editOperationId: dirty ? uid() : (existing?.editOperationId || uid()),
    audioKey: existing?.audioKey || null, audioDirty: existing?.audioDirty || false,
    audioReceipt: existing?.audioReceipt || null, localAudioDeletedAt: existing?.localAudioDeletedAt || null,
    pendingOperation: existing?.pendingOperation || null, metadataPatch: dirty ? existing?.metadataPatch || null : null,
    metadataBase: dirty ? existing?.metadataBase || null : null, syncError: '', syncConflict: existing?.syncConflict || false };
}

const META_KEYS = new Set(['title', 'nameSource', 'archivedAt', 'trashedAt']);
function mergeRemoteMetadata(record, note) {
  if (!record || Number(note.revision || 0) < Number(record.remoteRevision || record.note.revision || 0)) return;
  const fields = ['courseName', 'courseColor', 'topic', 'lessonSlot', 'recordedAt', 'duration', 'namingConfidence', 'status', 'stage', 'asrTask',
    'audioArchivedAt', 'audioDeleteDueAt', 'audioDeletedAt', 'cloudAudioDeletedAt', 'audioAvailable', 'cleanupDue', 'archivedAt', 'trashedAt'];
  if (!record.dirty || record.metadataPatch) fields.push('title', 'nameSource');
  for (const key of fields) if (key in note && !(key in (record.metadataPatch || {}))) record.note[key] = copy(note[key]);
}

export async function getLastAccountId() { return (await read('meta', 'last-account'))?.accountId || null; }
export async function listCachedAccounts() {
  const db = await openDatabase();
  const rows = await new Promise((resolve, reject) => {
    const request = db.transaction('meta', 'readonly').objectStore('meta').getAll();
    request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
  return rows.filter(row => row.key.startsWith('account:')).map(row => ({ accountId: row.accountId, lastUsedAt: row.lastUsedAt }));
}

export function createSyncClient({ accountId: value, getToken = () => '', baseUrl = '', onChange } = {}) {
  const accountId = accountName(value);
  const base = new URL(baseUrl || '/', location.href);
  if (base.origin !== location.origin) throw new Error('离线同步仅连接当前听记网站，不能向其他网站发送笔记。');
  const inFlight = new Set();
  const uploadControllers = new Map();
  let disposed = false;
  const remembered = write([['meta', { key: 'last-account', accountId }], ['meta', { key: `account:${accountId}`, accountId, lastUsedAt: now() }]]);
  remembered.catch(() => {});
  const emit = async (reason, detail = {}) => {
    if (disposed) return;
    const rows = await accountRows('notes', accountId);
    const payload = { accountId, reason, online: navigator.onLine, pending: rows.filter(row => (row.dirty || row.audioDirty) && !(row.localOnly && row.note.trashedAt)).length, ...detail };
    globalThis.dispatchEvent(new CustomEvent('tingji:sync-status', { detail: payload }));
    onChange?.(payload);
  };
  async function api(path, { method = 'GET', body, headers = {}, retries = 0, timeoutMs = 30000, signal, onWait, onRetry } = {}) {
    if (disposed) throw new Error('当前账号同步已关闭，请重新打开笔记。');
    let data = body;
    if (data && !(data instanceof Blob) && !(data instanceof ArrayBuffer)) { headers['Content-Type'] = 'application/json'; data = JSON.stringify(data); }
    for (let attempt = 0; ; attempt++) {
      if (disposed) throw new Error('同步已关闭，请重新打开笔记。');
      if (signal?.aborted) throw new Error('上传已暂停，点“上传到电脑”继续。');
      const token = await getToken();
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      const waitTimer = onWait ? setTimeout(() => onWait(), 15000) : null;
      const cancel = () => controller.abort();
      signal?.addEventListener('abort', cancel, { once: true });
      let failure;
      try {
        const response = await sessionFetch(new URL(path, base), { method, accountId, headers: { 'X-App-Token': token || '', ...headers }, body: data, signal: controller.signal, credentials: 'same-origin', cache: 'no-store' });
        const type = response.headers.get('content-type') || '';
        const result = type.includes('json') ? await response.json() : { error: await response.text() };
        if (response.ok || (response.status === 409 && result.conflict && result.note)) return result;
        failure = new Error(typeof result.error === 'string' ? result.error : result.error?.message || result.message || `同步未完成（${response.status}）`);
        if (!TRANSIENT_STATUS.has(response.status)) throw Object.assign(failure, { permanent: true });
      } catch (error) {
        if (signal?.aborted) throw new Error('上传已暂停，点“上传到电脑”继续。');
        if (error.permanent || !['AbortError', 'TypeError', 'SyntaxError'].includes(error.name)) throw error;
        failure = new Error('连接中断，录音已保留。点“上传到电脑”继续。');
      } finally { clearTimeout(timer); clearTimeout(waitTimer); signal?.removeEventListener('abort', cancel); }
      if (attempt >= retries) throw failure;
      onRetry?.(attempt + 1);
      await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)));
    }
  }
  // These endpoints persist receipts. Retrying them cannot submit AI work or duplicate a note.
  const uploadApi = (path, options) => api(path, { ...options, retries: 2, timeoutMs: UPLOAD_TIMEOUT_MS });
  async function purgeCachedTranscripts(note, existing) {
    if (!note.transcriptPurgedAt) return [];
    const ids = new Set([note.id, existing?.note.id, existing?.localId].filter(Boolean));
    const scrub = value => {
      if (!value || typeof value !== 'object' || value instanceof Blob) return value;
      if (Array.isArray(value)) return value.map(scrub);
      const result = Object.fromEntries(Object.entries(value).map(([key, item]) => [key, scrub(item)]));
      return ids.has(value.id) ? withoutTranscript(result, note.transcriptPurgedAt) : result;
    };
    const entries = [];
    for (const store of ['history', 'operations', 'uploads']) {
      for (const row of await storeRows(store, accountId)) {
        const updated = scrub(row);
        // A changed pending payload must receive a fresh operation ID.
        if (store === 'operations' && row.localId === existing?.localId && row.status === 'pending') updated.status = 'superseded';
        if (JSON.stringify(updated) !== JSON.stringify(row)) entries.push([store, updated]);
      }
    }
    if (existing) {
      existing.note = withoutTranscript(existing.note, note.transcriptPurgedAt);
      existing.pendingOperation = null;
      existing.editOperationId = uid();
    }
    return entries;
  }
  async function cacheOne(note) {
    assertAccount(accountId, note);
    return serial(accountId, async () => {
      const existing = await resolveRecord(accountId, note.id || note.localId);
      const purged = await purgeCachedTranscripts(note, existing);
      mergeRemoteMetadata(existing, note);
      if (existing) {
        for (const key of ['transcriptPurgedAt', 'audioArchivedAt', 'audioDeleteDueAt', 'audioDeletedAt', 'cloudAudioDeletedAt', 'audioAvailable', 'cleanupDue']) {
          if (key in note) existing.note[key] = note[key];
        }
      }
      if (existing?.dirty || existing?.audioDirty) {
        existing.remoteRevision = Number(note.revision ?? existing.remoteRevision ?? existing.baseRevision);
        await write([...purged, ...recordEntries(existing)]);
        return publicNote(existing);
      }
      const fullContent = 'transcript' in note && 'summary' in note;
      const remoteRevision = Number(note.revision ?? existing?.baseRevision ?? 0);
      if (existing?.note?._fullCached && !fullContent && remoteRevision !== existing.baseRevision) {
        // A directory refresh cannot advance the revision of an older cached body.
        // Its original base revision is essential to detect conflicts after offline edits.
        existing.remoteRevision = remoteRevision;
        await write([...purged, ...recordEntries(existing)]);
        return publicNote(existing);
      }
      if (existing?.note?._fullCached && remoteRevision < existing.baseRevision) {
        await write([...purged, ...recordEntries(existing)]); return publicNote(existing);
      }
      const record = makeRecord(accountId, note, existing, false);
      record.localOnly = false;
      record.baseRevision = remoteRevision;
      record.remoteRevision = remoteRevision;
      record.pendingOperation = null;
      const entries = [...purged, ...recordEntries(record)];
      if (existing && Number(note.revision ?? 0) !== Number(existing.note.revision ?? 0)) entries.unshift(archive(existing, 'remote-cache-update'));
      await write(entries);
      return publicNote(record);
    });
  }
  async function saveLocalNote(note, blob) {
    assertAccount(accountId, note);
    if (blob !== undefined && !(blob instanceof Blob)) throw new Error('录音内容必须是有效的音频文件。');
    const saved = await serial(accountId, async () => {
      const existing = note.id || note.localId ? await resolveRecord(accountId, note.id || note.localId) : null;
      const record = makeRecord(accountId, note, existing, true);
      record.note.updatedAt = now();
      if (!existing && note.localOnly === undefined) record.localOnly = true;
      const entries = existing ? [archive(existing, 'local-edit')] : [];
      if (blob) {
        if (!blob.size) throw new Error('录音文件为空，尚未保存。');
        const audioKey = `audio-${uid()}`;
        record.audioKey = audioKey; record.audioDirty = true;
        record.audioReceipt = null; record.localAudioDeletedAt = null;
        entries.push(['audio', { accountId, localId: audioKey, blob, name: note.sourceName || blob.name || '录音.webm', type: blob.type || 'audio/webm', size: blob.size, savedAt: now() }]);
      }
      entries.push(...recordEntries(record));
      await write(entries);
      return publicNote(record);
    });
    await emit('saved-locally');
    return saved;
  }
  async function updateLocalNote(id, patch, { baseRevision } = {}) {
    const saved = await serial(accountId, async () => {
      const existing = await resolveRecord(accountId, id);
      if (!existing) throw new Error('这份笔记尚未保存在此设备。');
      assertAccount(accountId, patch);
      const safe = { ...noteBody(patch) }; delete safe.id; delete safe.localId;
      for (const key of ['archivedAt', 'trashedAt']) if (key in patch) safe[key] = patch[key];
      if (existing.note.transcriptPurgedAt && ['transcript', 'chat'].some(key => key in patch && JSON.stringify(patch[key]) !== JSON.stringify(existing.note[key]))) {
        throw new Error('原文已清理，请编辑笔记。');
      }
      const record = makeRecord(accountId, { ...existing.note, ...safe, updatedAt: now() }, existing, true);
      const metadataOnly = Object.keys(safe).length && Object.keys(safe).every(key => META_KEYS.has(key));
      if (metadataOnly && (!existing.dirty || existing.metadataPatch) && !existing.localOnly && !existing.audioDirty) {
        record.metadataPatch = { ...(existing.metadataPatch || {}), ...safe };
        record.metadataBase = { ...(existing.metadataBase || {}) };
        for (const key of Object.keys(safe)) if (!(key in record.metadataBase)) record.metadataBase[key] = existing.note[key] ?? null;
      } else {
        record.metadataPatch = null; record.metadataBase = null;
      }
      // A full cache refresh in another tab must not make an older editor look current.
      if (Number.isInteger(baseRevision) && baseRevision >= 0 && baseRevision < record.baseRevision) {
        record.baseRevision = baseRevision;
        record.note.revision = baseRevision;
      }
      if ('transcript' in safe) { record.note.transcriptEdited = true; record.note.summaryStale = !!record.note.summary; }
      if ('summary' in safe) record.note.summaryStale = false;
      await write([archive(existing, 'local-edit'), ...recordEntries(record)]);
      return publicNote(record);
    });
    await emit('saved-locally'); return saved;
  }
  async function audioFor(record) { return record?.audioKey ? read('audio', [accountId, record.audioKey]) : null; }
  async function cacheAudio(note, blob) {
    assertAccount(accountId, note);
    if (!note?.id || !(blob instanceof Blob) || !blob.size) return null;
    const saved = await serial(accountId, async () => {
      const existing = await resolveRecord(accountId, note.id);
      const purged = await purgeCachedTranscripts(note, existing);
      if (existing?.dirty || existing?.audioDirty) {
        if (purged.length || note.transcriptPurgedAt) await write([...purged, ...recordEntries(existing)]);
        return null;
      }
      const record = makeRecord(accountId, note, existing, false);
      const audioKey = `audio-${uid()}`;
      record.audioKey = audioKey; record.audioDirty = false; record.dirty = false; record.localOnly = false;
      if (note.audioAvailable && Number(note.fileSize) === blob.size) record.audioReceipt = { audioKey, noteId: note.id, size: blob.size, confirmedAt: now() };
      record.baseRevision = Number(note.revision ?? existing?.baseRevision ?? 0); record.pendingOperation = null;
      const entries = [...purged, ...(existing ? [archive(existing, 'cache-server-audio')] : [])];
      entries.push(['audio', { accountId, localId: audioKey, blob, name: note.sourceName || '录音.wav', type: blob.type || 'audio/wav', size: blob.size, savedAt: now() }], ...recordEntries(record));
      await write(entries); return publicNote(record);
    });
    if (saved) await emit('audio-cached-clean');
    return saved;
  }
  async function saveSyncError(id, error) {
    await serial(accountId, async () => {
      const record = await resolveRecord(accountId, id);
      if (record) { record.syncError = error.message; await write(recordEntries(record)); }
    });
    await emit('sync-error', { message: error.message });
  }
  async function transferAudio(snapshot, onProgress, signal) {
    const audio = await audioFor(snapshot);
    if (!audio?.blob) throw new Error('此设备缺少待同步录音。原文仍保留，请重新选择录音文件。');
    if (audio.blob.size > 1024 * 1024 * 1024) throw new Error('录音超过 1 GB，请先在电脑端导入较小的文件。');
    let upload = await read('uploads', [accountId, snapshot.localId]);
    if (upload?.completedNote && upload.audioKey === snapshot.audioKey) return upload.completedNote;
    let loaded = 0;
    const progress = (phase, extra = {}) => onProgress?.({ phase, loaded, total: audio.blob.size, ...extra });
    const requestOptions = { signal, onWait: () => progress('upload-waiting'), onRetry: attempt => progress('upload-retrying', { attempt }) };
    progress('upload-connecting');
    const opened = await uploadApi('/api/sync/uploads', { ...requestOptions, method: 'POST', body: { localId: snapshot.localId, audioId: snapshot.audioKey, name: audio.name, size: audio.blob.size, type: audio.type,
      title: snapshot.note.title, recordedAt: snapshot.note.recordedAt, duration: snapshot.note.duration, nameSource: snapshot.note.nameSource || 'auto', template: snapshot.note.template, language: snapshot.note.language } });
    const chunkSize = Number(opened.chunkSize || DEFAULT_CHUNK_SIZE);
    if (!opened.uploadId || !Number.isSafeInteger(chunkSize) || chunkSize < 65536 || chunkSize > 32 * 1024 * 1024) throw new Error('电脑返回了无效的分块上传配置。');
    upload = { accountId, localId: snapshot.localId, audioKey: snapshot.audioKey, uploadId: opened.uploadId, chunkSize, name: audio.name, size: audio.blob.size, type: audio.type, parts: opened.parts || [], updatedAt: now() };
    await write([['uploads', upload]]);
    const known = new Map(upload.parts.map(part => [Number(part.index), String(part.sha256).toLowerCase()]));
    const count = Math.ceil(audio.blob.size / chunkSize);
    const trustedCompletion = opened.noteId && opened.audioId === snapshot.audioKey;
    for (let index = 0; !trustedCompletion && index < count; index++) {
      if (signal?.aborted) throw new Error('上传已暂停，点“上传到电脑”继续。');
      progress('uploading-audio', { part: index + 1, parts: count });
      const piece = audio.blob.slice(index * chunkSize, Math.min((index + 1) * chunkSize, audio.blob.size));
      const bytes = await bounded(piece.arrayBuffer(), '录音读取超时。请保持听记在前台，刷新后继续上传。', { signal });
      const digest = await bounded(crypto.subtle.digest('SHA-256', bytes), '录音准备超时。请保持听记在前台，刷新后继续上传。', { signal });
      const hash = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
      if (known.has(index) && known.get(index) !== hash) throw new Error('电脑已收到的分块与此录音不一致，已停止上传以保护原文件。');
      if (opened.noteId && !known.has(index)) throw new Error('电脑上传回执不完整，请保留本机录音。');
      if (!known.has(index)) {
        await uploadApi(`/api/sync/uploads/${encodeURIComponent(upload.uploadId)}/${index}`, { ...requestOptions, method: 'PUT', body: piece, headers: { 'Content-Type': 'application/octet-stream', 'X-Chunk-SHA256': hash } });
        known.set(index, hash);
        upload.parts = [...known].map(([index, sha256]) => ({ index, sha256 })); upload.updatedAt = now();
        await write([['uploads', upload]]);
      }
      loaded = Math.min((index + 1) * chunkSize, audio.blob.size);
      progress('uploading-audio', { part: index + 1, parts: count });
    }
    progress('upload-completing');
    const result = await uploadApi(`/api/sync/uploads/${encodeURIComponent(upload.uploadId)}/complete`, { ...requestOptions, method: 'POST', body: {} });
    const note = result.note || result;
    if (!note.id || note.audioAvailable === false || note.audioDeletedAt || Number(note.fileSize) !== audio.blob.size) throw new Error('电脑未保留这份完整录音，请保留本机文件。');
    upload.completedNote = note; upload.updatedAt = now();
    await write([['uploads', upload]]);
    return note;
  }
  async function verifyRemoteAudio(snapshot) {
    const response = await api(`/api/notes/${encodeURIComponent(snapshot.note.id)}`);
    const remote = response.note || response;
    if (!remote.audioAvailable || remote.audioDeletedAt || remote.id !== snapshot.audioReceipt?.noteId
        || Number(remote.fileSize) !== Number(snapshot.audioReceipt?.size)) {
      throw new Error('电脑未保留这份录音，已取消删除。');
    }
    return remote;
  }
  async function cleanAudioCache(id, expectedAudioKey = null) {
    const snapshot = await resolveRecord(accountId, id);
    if (!snapshot || !canDeleteLocalAudio(publicNote(snapshot)) || (expectedAudioKey && snapshot.audioKey !== expectedAudioKey)) return null;
    await verifyRemoteAudio(snapshot);
    const cleaned = await serial(accountId, async () => {
      const current = await resolveRecord(accountId, id);
      if (!current || !current.audioKey || current.audioDirty || !current.audioReceipt
          || current.note.audioAvailable === false || current.note.audioDeletedAt
          || (expectedAudioKey && current.audioKey !== expectedAudioKey)
          || current.audioReceipt.audioKey !== current.audioKey
          || current.audioReceipt.noteId !== current.note.id) return null;
      const audioKey = current.audioKey;
      const sharedKeys = new Set((await accountRows('notes', accountId))
        .filter(row => row.localId !== current.localId).map(row => row.audioKey).filter(Boolean));
      current.audioKey = null; current.audioReceipt = null; current.audioDirty = false;
      current.audioCacheCleanedAt = now(); current.pendingOperation = null;
      const entries = [['uploads', [accountId, current.localId], 'delete']];
      if (!sharedKeys.has(audioKey)) entries.push(['audio', [accountId, audioKey], 'delete']);
      entries.push(...recordEntries(current)); await write(entries);
      return publicNote(current);
    });
    if (cleaned) await emit('audio-cache-cleaned');
    return cleaned;
  }
  async function pushNote(id, { onProgress } = {}) {
    let snapshot = await resolveRecord(accountId, id);
    if (!snapshot) throw new Error('这份笔记尚未保存在此设备。');
    if (snapshot.localOnly && snapshot.note.trashedAt) return { note: publicNote(snapshot), conflict: false, unchanged: true };
    if (inFlight.has(snapshot.localId)) throw new Error('这份笔记正在同步，请等待当前操作结束。');
    if (!snapshot.dirty && !snapshot.audioDirty) return { note: publicNote(snapshot), conflict: false, unchanged: true };
    const lockId = snapshot.localId;
    inFlight.add(lockId);
    const controller = new AbortController();
    uploadControllers.set(lockId, controller);
    let autoCleanAudioKey = null;
    try {
      await emit('sync-started');
      let operation = snapshot.pendingOperation ? await read('operations', [accountId, snapshot.pendingOperation]) : null;
      if (!operation || operation.status !== 'pending') {
        let serverAudioNote;
        if (snapshot.audioDirty) serverAudioNote = await transferAudio(snapshot, onProgress, controller.signal);
        if (serverAudioNote?.duplicateUpload) snapshot.note = copy(serverAudioNote);
        if (serverAudioNote?.transcriptPurgedAt) {
          snapshot.note = withoutTranscript({ ...snapshot.note, ...serverAudioNote, title: snapshot.note.title,
            summary: snapshot.note.summary || serverAudioNote.summary });
        }
        operation = { accountId, operationId: snapshot.editOperationId || uid(), localId: snapshot.localId, editVersion: snapshot.editVersion, audioKey: snapshot.audioKey,
          baseRevision: serverAudioNote ? Number(serverAudioNote.revision || 0) : snapshot.baseRevision || 0,
          note: { ...noteBody(snapshot.note), localId: snapshot.localId, ...(serverAudioNote ? { id: serverAudioNote.id } : {}) },
          metadataPatch: snapshot.metadataPatch || null, metadataBase: snapshot.metadataBase || null,
          status: 'pending', createdAt: now(), serverAudioNote: serverAudioNote || null };
        await serial(accountId, async () => {
          const current = await resolveRecord(accountId, id);
          if (!current) throw new Error('待同步笔记暂时无法读取。');
          current.pendingOperation = operation.operationId;
          await write([['operations', operation], ...recordEntries(current)]);
        });
      }
      onProgress?.({ phase: 'uploading-note' });
      const acceptUploadedAudio = !!operation.serverAudioNote && !operation.metadataPatch &&
        (operation.note.transcript || '') === (operation.serverAudioNote.transcript || '') &&
        (operation.note.summary || '') === (operation.serverAudioNote.summary || '');
      const result = await api('/api/sync/push', { signal: controller.signal, method: 'POST', body: { operationId: operation.operationId, note: operation.note, baseRevision: operation.baseRevision, acceptUploadedAudio,
        ...(operation.metadataPatch ? { metadataPatch: operation.metadataPatch, metadataBase: operation.metadataBase } : {}) } });
      if (!result.note?.id) throw new Error('未收到电脑保存结果，原笔记仍保留，重试会使用同一操作编号。');
      const output = await serial(accountId, async () => {
        const current = await resolveRecord(accountId, id);
        if (!current) throw new Error('待同步笔记暂时无法读取。');
        const editedDuringSync = current.editVersion !== operation.editVersion || current.audioKey !== operation.audioKey;
        const accepted = result.note;
        assertAccount(accountId, accepted);
        const entries = await purgeCachedTranscripts(accepted, current);
        if (accepted.transcriptPurgedAt) {
          operation.note = withoutTranscript(operation.note, accepted.transcriptPurgedAt);
          if (operation.serverAudioNote) operation.serverAudioNote = withoutTranscript(operation.serverAudioNote, accepted.transcriptPurgedAt);
        }
        entries.push(archive(current, result.conflict ? 'server-conflict-copy' : 'server-sync-accepted'));
        let updated;
        if (result.conflict && result.remote?.id) {
          assertAccount(accountId, result.remote);
          const original = makeRecord(accountId, result.remote, current, false);
          original.note = { ...copy(result.remote), _fullCached: 'transcript' in result.remote && 'summary' in result.remote }; original.baseRevision = Number(result.remote.revision || 0); original.localOnly = false;
          original.pendingOperation = null; original.audioDirty = false; original.syncConflict = false;
          const conflictLocalId = accepted.id;
          updated = { ...current, localId: conflictLocalId, note: withoutTranscript(editedDuringSync ? { ...current.note, ...accepted, ...noteBody(current.note), id: accepted.id, revision: accepted.revision } : copy(accepted)),
            baseRevision: Number(accepted.revision || 0), localOnly: false, dirty: editedDuringSync, audioDirty: editedDuringSync && current.audioKey !== operation.audioKey,
            pendingOperation: null, syncError: '', syncConflict: true };
          entries.push(...recordEntries(original, [result.remote.id]), ...recordEntries(updated));
        } else {
          updated = { ...current, note: withoutTranscript(editedDuringSync ? { ...accepted, ...noteBody(current.note), id: accepted.id, revision: accepted.revision } : copy(accepted)),
            baseRevision: Number(accepted.revision || 0), localOnly: false, dirty: editedDuringSync, audioDirty: editedDuringSync && current.audioKey !== operation.audioKey,
            pendingOperation: null, syncError: '', syncConflict: false };
          entries.push(...recordEntries(updated));
        }
        if (operation.serverAudioNote && operation.audioKey === current.audioKey && !updated.audioDirty && accepted.audioAvailable !== false && !accepted.audioDeletedAt) {
          updated.audioReceipt = { audioKey: current.audioKey, noteId: accepted.id, size: Number(operation.serverAudioNote.fileSize), confirmedAt: now() };
        }
        updated.note._fullCached = ('transcript' in accepted && 'summary' in accepted) || (editedDuringSync && !!current.note._fullCached);
        if (!editedDuringSync) { updated.metadataPatch = null; updated.metadataBase = null; }
        else if (updated.metadataPatch) {
          // A newer local rename is now based on the just-accepted server name.
          updated.metadataBase = Object.fromEntries(Object.keys(updated.metadataPatch).map(key => [key, accepted[key] ?? null]));
        }
        operation.status = 'completed'; operation.completedAt = now(); operation.result = copy(result);
        entries.push(['operations', operation]);
        await write(entries);
        return { note: publicNote(updated), conflict: !!result.conflict, remote: result.remote || null, pendingEdits: editedDuringSync };
      });
      if (operation.serverAudioNote && !output.conflict && !output.pendingEdits && output.note.canDeleteLocalAudio) {
        autoCleanAudioKey = operation.audioKey;
      }
      await emit(result.conflict ? 'conflict-preserved' : 'synced-to-computer');
      onProgress?.({ phase: 'complete' });
      return output;
    } catch (error) { await saveSyncError(id, error).catch(() => {}); throw error; }
    finally {
      inFlight.delete(lockId); uploadControllers.delete(lockId);
      // The receipt is committed first; only then, after the operation lock is
      // released, may the recoverable device audio cache be removed.
      if (autoCleanAudioKey) await cleanAudioCache(id, autoCleanAudioKey).catch(() => {});
    }
  }
  async function downloadNote(id, { includeAudio = true, onProgress } = {}) {
    const existing = await resolveRecord(accountId, id);
    const remoteId = existing?.note.id || id;
    if (existing?.localOnly) throw new Error('这份笔记还未上传到电脑，请先手动上传。');
    onProgress?.({ phase: 'downloading-note' });
    const result = await api(`/api/notes/${encodeURIComponent(remoteId)}`);
    const note = result.note || result;
    assertAccount(accountId, note);
    let downloaded;
    if (includeAudio && note.audioUrl) {
      const url = new URL(note.audioUrl, base);
      if (url.origin !== base.origin || !url.pathname.startsWith('/api/audio/')) throw new Error('电脑返回了不受支持的音频地址，已停止下载。');
      onProgress?.({ phase: 'downloading-audio' });
      const response = await sessionFetch(url, { accountId, credentials: 'same-origin', cache: 'no-store', headers: { 'X-App-Token': await getToken() || '' } });
      if (!response.ok) throw new Error('音频下载未完成，设备上已有内容保持不变。');
      const advertisedSize = Number(response.headers.get('content-length') || 0);
      if (advertisedSize > 1024 * 1024 * 1024) throw new Error('音频超过此设备的单文件下载上限 1 GB。');
      const blob = await response.blob();
      if (blob.size > 1024 * 1024 * 1024) throw new Error('音频超过此设备的单文件下载上限 1 GB。');
      downloaded = { accountId, localId: `audio-${uid()}`, blob, name: note.sourceName || '录音', type: blob.type, size: blob.size, savedAt: now() };
      onProgress?.({ phase: 'downloading-audio', loaded: blob.size, total: blob.size });
    }
    const output = await serial(accountId, async () => {
      const current = await resolveRecord(accountId, id);
      const entries = await purgeCachedTranscripts(note, current);
      if (current?.dirty || current?.audioDirty) {
        entries.push(['history', { accountId, versionId: uid(), localId: current.localId, savedAt: now(), reason: 'download-kept-local-edits', remoteNote: copy(note), audioKey: downloaded?.localId || null }]);
        if (downloaded) entries.push(['audio', downloaded]);
        entries.push(...recordEntries(current)); await write(entries);
        return { note: publicNote(current), conflict: true, remote: note, audioDownloaded: !!downloaded };
      }
      const updated = makeRecord(accountId, note, current, false);
      updated.localOnly = false; updated.baseRevision = Number(note.revision || 0); updated.pendingOperation = null; updated.audioDirty = false;
      if (downloaded) {
        updated.audioKey = downloaded.localId;
        if (note.audioAvailable && Number(note.fileSize) === downloaded.size) updated.audioReceipt = { audioKey: downloaded.localId, noteId: note.id, size: downloaded.size, confirmedAt: now() };
        entries.push(['audio', downloaded]);
      }
      if (current) entries.push(archive(current, 'download-from-computer'));
      entries.push(...recordEntries(updated)); await write(entries);
      return { note: publicNote(updated), conflict: false, audioDownloaded: !!downloaded };
    });
    await emit(output.conflict ? 'download-preserved-local-edits' : 'downloaded-to-device');
    onProgress?.({ phase: 'complete' }); return output;
  }
  async function deleteLocalAudio(id) {
    const snapshot = await resolveRecord(accountId, id);
    if (!snapshot || !canDeleteLocalAudio(publicNote(snapshot)) || inFlight.has(snapshot.localId)) throw new Error('请先把录音完整上传到电脑。');
    await verifyRemoteAudio(snapshot);
    const result = await serial(accountId, async () => {
      const current = await resolveRecord(accountId, id);
      if (!current || current.audioKey !== snapshot.audioKey || !canDeleteLocalAudio(publicNote(current)) || inFlight.has(current.localId)) throw new Error('录音状态已变化，请稍后再试。');
      const audioKey = current.audioKey;
      current.audioKey = null; current.audioReceipt = null; current.audioDirty = false; current.localAudioDeletedAt = now();
      current.pendingOperation = null; current.editOperationId = uid();
      const sharedKeys = new Set((await accountRows('notes', accountId)).filter(row => row.localId !== current.localId).map(row => row.audioKey).filter(Boolean));
      const entries = [['uploads', [accountId, current.localId], 'delete']];
      if (!sharedKeys.has(audioKey)) entries.push(['audio', [accountId, audioKey], 'delete']);
      // History can retain obsolete Blob references, but never needs to retain the recording.
      for (const row of await accountRows('history', accountId)) {
        if (row.localId !== current.localId) continue;
        const historicalKey = row.audioKey || row.record?.audioKey;
        if (historicalKey && !sharedKeys.has(historicalKey)) entries.push(['audio', [accountId, historicalKey], 'delete']);
        if (row.record) { row.record.audioKey = null; row.record.audioReceipt = null; row.record.audioDirty = false; }
        row.audioKey = null; entries.push(['history', row]);
      }
      for (const row of await storeRows('operations', accountId)) {
        if (row.localId === current.localId) entries.push(['operations', [accountId, row.operationId], 'delete']);
      }
      entries.push(...recordEntries(current)); await write(entries);
      return publicNote(current);
    });
    await emit('local-audio-deleted'); return result;
  }
  const client = {
    accountId,
    ready: remembered,
    async listNotes() { return (await accountRows('notes', accountId)).map(publicNote).sort((a, b) => String(b.updatedAt || '').localeCompare(String(a.updatedAt || ''))); },
    async getNote(id) { return publicNote(await resolveRecord(accountId, id)); },
    async cacheNotes(notes) { const saved = []; for (const note of notes || []) saved.push(await cacheOne(note)); await emit('cache-updated'); return saved; },
    async cacheFinishedNotes(notes) {
      for (const note of notes || []) {
        if ((!note.hasSummary && !note.summary) || note.trashedAt) continue;
        const existing = await resolveRecord(accountId, note.id);
        if (existing?.dirty || existing?.audioDirty) continue;
        if (existing?.note?._fullCached && existing.note.summary && Number(existing.baseRevision) === Number(note.revision)) continue;
        await downloadNote(note.id, { includeAudio: false });
      }
    },
    saveLocalNote,
    async saveLocalRecording(blob, { title = '录音笔记', language = 'auto', template = 'general', name = blob?.name || `录音 ${now().replace(/:/g, '-')}.webm`, recordedAt, duration, nameSource = 'auto' } = {}) {
      return saveLocalNote({ id: `local-${uid()}`, title, language, template, sourceName: name, recordedAt, duration, nameSource, localOnly: true, transcript: '', summary: '', audioUrl: '' }, blob);
    },
    updateLocalNote,
    setArchived(id, archived = true) { return updateLocalNote(id, { archivedAt: archived ? now() : null }); },
    deleteNote(id) { return updateLocalNote(id, { trashedAt: now() }); },
    restoreNote(id) { return updateLocalNote(id, { trashedAt: null }); },
    cacheAudio,
    cleanAudioCache,
    deleteLocalAudio,
    async getAudioBlob(id) { return (await audioFor(await resolveRecord(accountId, id)))?.blob || null; },
    async listPending() { return (await accountRows('notes', accountId)).filter(note => (note.dirty || note.audioDirty) && !(note.localOnly && note.note.trashedAt)).map(publicNote); },
    pushNote,
    cancelUploads() { for (const controller of uploadControllers.values()) controller.abort(); },
    downloadNote,
    async downloadNotes(ids, options) { const results = []; for (const id of ids) results.push(await downloadNote(id, options)); return results; },
    async listHistory(id) { const record = await resolveRecord(accountId, id); return (await accountRows('history', accountId)).filter(row => !record || row.localId === record.localId); },
    async requestPersistentStorage() { return navigator.storage?.persist ? navigator.storage.persist() : false; },
    dispose() { disposed = true; CLIENTS.delete(client); },
    _notifyConnectivity() { return emit(navigator.onLine ? 'online-pending-manual-sync' : 'offline'); }
  };
  CLIENTS.add(client);
  return client;
}

for (const event of ['online', 'offline']) globalThis.addEventListener(event, () => {
  for (const client of CLIENTS) client._notifyConnectivity().catch(() => {});
});

export async function registerOfflineShell() {
  if (!('serviceWorker' in navigator) || !globalThis.isSecureContext) return { supported: false, registration: null };
  const registration = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
  return { supported: true, registration };
}
