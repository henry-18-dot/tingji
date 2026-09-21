// Only opened note bodies and minimal display identity are stored here.
// API responses, cookies, email addresses and authentication tokens are excluded.
const DATABASE = 'tingji-opened-notes-v1', VERSION = 1;
const MAX_NOTES = 100, MAX_BYTES = 32 * 1024 * 1024, MAX_NOTE_BYTES = 2 * 1024 * 1024;
const STORES = ['meta', 'notes', 'assets'];
const text = value => typeof value === 'string' ? value : '';
const validId = value => typeof value === 'string' && /^[a-zA-Z0-9_-]{1,100}$/.test(value);

function openDatabase() {
  return new Promise((resolve, reject) => {
    let finished = false;
    const timer = setTimeout(() => { finished = true; reject(new Error('离线存储暂时不可用。')); }, 4000);
    const request = indexedDB.open(DATABASE, VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      for (const name of STORES) if (!db.objectStoreNames.contains(name)) db.createObjectStore(name, {keyPath: 'key'});
    };
    request.onsuccess = () => { clearTimeout(timer); if (finished) { request.result.close(); return; } const db = request.result; db.onversionchange = () => db.close(); resolve(db); };
    request.onerror = () => { clearTimeout(timer); reject(request.error); };
    request.onblocked = () => { clearTimeout(timer); finished = true; reject(new Error('离线存储正在更新。')); };
  });
}

async function transaction(names, mode, action) {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    let result = null, error;
    const tx = db.transaction(names, mode), timer = setTimeout(() => { try { tx.abort(); } catch {} }, 5000);
    tx.oncomplete = () => { clearTimeout(timer); db.close(); resolve(result); };
    tx.onerror = tx.onabort = () => { clearTimeout(timer); db.close(); reject(error || tx.error || new Error('离线存储没有完成。')); };
    try { action(tx, value => { result = value; }); } catch (e) { error = e; tx.abort(); }
  });
}

function safeNote(note) {
  if (!note || !validId(note.id)) return null;
  const summary = text(note.summary), transcript = text(note.transcript);
  if (!summary && !transcript) return null;
  const value = {id: note.id, title: text(note.title).slice(0,500), sourceName: text(note.sourceName).slice(0,500),
    courseId: validId(note.courseId) ? note.courseId : null, createdAt: text(note.createdAt), updatedAt: text(note.updatedAt),
    duration: Number.isFinite(note.duration) ? note.duration : null, summary, transcript,
    status: 'ready', stage: '', error: '', hasAudio: false, versions: [], promptSnapshot: '', model: ''};
  return new TextEncoder().encode(JSON.stringify(value)).length <= MAX_NOTE_BYTES ? value : null;
}

export function createOffline({onCleared = () => {}} = {}) {
  let generation = 0;
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('tingji-opened-notes-v1') : null;
  if (channel) channel.onmessage = event => { if (event.data?.type === 'clear' || event.data?.type === 'account-changed') { generation++; onCleared(event.data); } };
  const attempt = async (fn, fallback = null) => { try { return await fn(); } catch { return fallback; } };
  return {
    async register() {
      if (!('serviceWorker' in navigator) || !('indexedDB' in globalThis)) return false;
      return attempt(async () => {
        const release = new URL(import.meta.url).pathname.match(/^\/releases\/([a-f0-9]{16})\//)?.[1];
        const registration = await navigator.serviceWorker.register('/sw.js' + (release ? `?release=${release}` : ''),
          {scope: '/', updateViaCache: 'none'});
        // Request a worker check on each online visit; private IndexedDB stores are untouched.
        if (navigator.onLine) registration.update().catch(() => {});
        return true;
      }, false);
    },
    async setUser(user) {
      if (!validId(user?.id)) return false;
      const identity = {id: user.id, name: text(user.name).slice(0,80) || '同学'};
      const token = generation;
      return attempt(() => transaction(['meta', 'notes'], 'readwrite', (tx, done) => {
        const meta = tx.objectStore('meta'), request = meta.get('user');
        request.onsuccess = () => {
          if (token !== generation) return;
          const previous = request.result?.value;
          if (previous && previous.id !== identity.id) { tx.objectStore('notes').clear(); channel?.postMessage({type:'account-changed'}); }
          meta.put({key:'user', value:identity}); done(true);
        };
      }), false);
    },
    async saveNote(userId, note, courses = []) {
      const value = safeNote(note), token = generation;
      if (!validId(userId) || !value) return false;
      const courseName = text(courses.find(course => course.id === value.courseId)?.name).slice(0,160);
      const size = new TextEncoder().encode(JSON.stringify(value)).length;
      return attempt(() => transaction(['meta', 'notes'], 'readwrite', (tx, done) => {
        const request = tx.objectStore('meta').get('user');
        request.onsuccess = () => {
          if (token !== generation || request.result?.value?.id !== userId) return;
          const store = tx.objectStore('notes'), key = `${userId}:${value.id}`;
          store.put({key, userId, note:value, courseName, savedAt:Date.now(), bytes:size});
          const all = store.getAll();
          all.onsuccess = () => {
            const rows = all.result.filter(row => row.userId === userId).sort((a,b) => b.savedAt - a.savedAt);
            let bytes = 0;
            rows.forEach((row,index) => { bytes += row.bytes || 0; if (index >= MAX_NOTES || bytes > MAX_BYTES) store.delete(row.key); });
            done(true);
          };
        };
      }), false);
    },
    async snapshot() {
      return attempt(() => transaction(['meta', 'notes'], 'readonly', (tx, done) => {
        const request = tx.objectStore('meta').get('user');
        request.onsuccess = () => {
          const user = request.result?.value; if (!user?.id) return;
          const all = tx.objectStore('notes').getAll();
          all.onsuccess = () => {
            const rows = all.result.filter(row => row.userId === user.id).sort((a,b) => b.savedAt - a.savedAt);
            if (!rows.length) return;
            const courses = [...new Map(rows.filter(row => row.note.courseId).map(row => [row.note.courseId, {id:row.note.courseId, name:row.courseName || '未分类'}])).values()];
            done({user, notes:rows.map(row => ({...row.note, offlineCachedAt:row.savedAt})), courses});
          };
        };
      }));
    },
    async getNote(userId, noteId) {
      if (!validId(userId) || !validId(noteId)) return null;
      return attempt(() => transaction(['meta', 'notes'], 'readonly', (tx, done) => {
        const request = tx.objectStore('meta').get('user');
        request.onsuccess = () => {
          if (request.result?.value?.id !== userId) return;
          const item = tx.objectStore('notes').get(`${userId}:${noteId}`);
          item.onsuccess = () => { const row = item.result; if (row?.userId === userId) done({...row.note, offlineCachedAt:row.savedAt}); };
        };
      }));
    },
    async prune(userId, visibleIds) {
      if (!validId(userId) || !Array.isArray(visibleIds)) return false;
      const visible = new Set(visibleIds);
      return attempt(() => transaction(['meta', 'notes'], 'readwrite', (tx, done) => {
        const request = tx.objectStore('meta').get('user');
        request.onsuccess = () => {
          if (request.result?.value?.id !== userId) return;
          const store = tx.objectStore('notes'), cursor = store.openCursor();
          cursor.onsuccess = () => { const item = cursor.result; if (!item) { done(true); return; } if (item.value.userId === userId && !visible.has(item.value.note.id)) item.delete(); item.continue(); };
        };
      }), false);
    },
    async clear() {
      generation++;
      channel?.postMessage({type:'clear'});
      return attempt(() => transaction(['meta', 'notes'], 'readwrite', (tx, done) => {
        tx.objectStore('meta').clear(); tx.objectStore('notes').clear(); done(true);
      }), false);
    },
    dispose() { channel?.close(); },
  };
}
