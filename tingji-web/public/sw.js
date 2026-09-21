// Public application files only. /api and external URLs always go to the network.
// IndexedDB is shared with offline.js, but this worker never reads private notes.
const DATABASE = 'tingji-opened-notes-v1', VERSION = 1;
// BEGIN GENERATED RELEASE
const RELEASE = '189c81cde2ab01f6';
const SHELL = ["/releases/189c81cde2ab01f6/app.css","/releases/189c81cde2ab01f6/app.js","/releases/189c81cde2ab01f6/assets/note-resources/ifr-2024-markets.png","/releases/189c81cde2ab01f6/assets/note-resources/mit-2004-cruise-p4.webp","/releases/189c81cde2ab01f6/assets/note-resources/mit-212-inverse-p4.webp","/releases/189c81cde2ab01f6/assets/note-resources/mit-212-jacobian-p1.webp","/releases/189c81cde2ab01f6/assets/note-resources/mit-212-jacobian-p3.webp","/releases/189c81cde2ab01f6/assets/note-resources/mit-212-kinematics-p1.webp","/releases/189c81cde2ab01f6/assets/note-resources/mit-6011-state-space-p4.webp","/releases/189c81cde2ab01f6/assets/note-resources/mit-6011-state-space-p5.webp","/releases/189c81cde2ab01f6/assets/note-resources/nasa-perseverance-mass-test.jpg","/releases/189c81cde2ab01f6/audio-storage.js","/releases/189c81cde2ab01f6/calendar.css","/releases/189c81cde2ab01f6/calendar.js","/releases/189c81cde2ab01f6/enter-reader.css","/releases/189c81cde2ab01f6/enter-reader.js","/releases/189c81cde2ab01f6/favicon.svg","/releases/189c81cde2ab01f6/history.js","/releases/189c81cde2ab01f6/knowledge-relations.css","/releases/189c81cde2ab01f6/knowledge-relations.js","/releases/189c81cde2ab01f6/knowledge.css","/releases/189c81cde2ab01f6/knowledge.js","/releases/189c81cde2ab01f6/lesson-picker.js","/releases/189c81cde2ab01f6/library.css","/releases/189c81cde2ab01f6/library.js","/releases/189c81cde2ab01f6/local-transfer.js","/releases/189c81cde2ab01f6/note-image-registry.js","/releases/189c81cde2ab01f6/note-resources.js","/releases/189c81cde2ab01f6/offline.js","/releases/189c81cde2ab01f6/reading-interactions.css","/releases/189c81cde2ab01f6/reading-interactions.js","/releases/189c81cde2ab01f6/recording-merge.js","/releases/189c81cde2ab01f6/recording-upload.js","/releases/189c81cde2ab01f6/settings.css","/releases/189c81cde2ab01f6/settings.js","/releases/189c81cde2ab01f6/slides.css","/releases/189c81cde2ab01f6/slides.js","/releases/189c81cde2ab01f6/upload-drafts.js","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_AMS-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_AMS-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_AMS-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Caligraphic-Bold.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Caligraphic-Bold.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Caligraphic-Bold.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Caligraphic-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Caligraphic-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Caligraphic-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Fraktur-Bold.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Fraktur-Bold.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Fraktur-Bold.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Fraktur-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Fraktur-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Fraktur-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-Bold.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-Bold.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-Bold.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-BoldItalic.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-BoldItalic.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-BoldItalic.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-Italic.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-Italic.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-Italic.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Main-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Math-BoldItalic.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Math-BoldItalic.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Math-BoldItalic.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Math-Italic.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Math-Italic.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Math-Italic.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_SansSerif-Bold.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_SansSerif-Bold.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_SansSerif-Bold.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_SansSerif-Italic.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_SansSerif-Italic.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_SansSerif-Italic.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_SansSerif-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_SansSerif-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_SansSerif-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Script-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Script-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Script-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size1-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size1-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size1-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size2-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size2-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size2-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size3-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size3-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size3-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size4-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size4-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Size4-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Typewriter-Regular.ttf","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Typewriter-Regular.woff","/releases/189c81cde2ab01f6/vendor/katex/fonts/KaTeX_Typewriter-Regular.woff2","/releases/189c81cde2ab01f6/vendor/katex/katex.min.css","/releases/189c81cde2ab01f6/vendor/katex/katex.min.js","/releases/189c81cde2ab01f6/vendor/mermaid/mermaid.tiny-11.12.0.js","/releases/189c81cde2ab01f6/index.html","/assets/note-resources/ifr-2024-markets.png","/assets/note-resources/mit-2004-cruise-p4.webp","/assets/note-resources/mit-212-inverse-p4.webp","/assets/note-resources/mit-212-jacobian-p1.webp","/assets/note-resources/mit-212-jacobian-p3.webp","/assets/note-resources/mit-212-kinematics-p1.webp","/assets/note-resources/mit-6011-state-space-p4.webp","/assets/note-resources/mit-6011-state-space-p5.webp","/assets/note-resources/nasa-perseverance-mass-test.jpg"];
// END GENERATED RELEASE
const released = pathname => /^\/releases\/[a-f0-9]{16}\/[A-Za-z0-9_./-]+\.(?:js|css|html|svg|png|jpe?g|webp|woff2?|ttf)$/.test(pathname);
const allowed = pathname => released(pathname)
  || /^\/assets\/note-resources\/[a-z0-9_-]+\.(?:png|jpe?g|webp)$/.test(pathname);

function database() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, VERSION);
    let late = false;
    const timer = setTimeout(() => { late = true; reject(new Error('storage timeout')); }, 4000);
    request.onupgradeneeded = () => { for (const name of ['meta','notes','assets']) if (!request.result.objectStoreNames.contains(name)) request.result.createObjectStore(name,{keyPath:'key'}); };
    request.onsuccess = () => { clearTimeout(timer); if (late) { request.result.close(); return; } const db = request.result; db.onversionchange = () => db.close(); resolve(db); };
    request.onerror = () => { clearTimeout(timer); reject(request.error); };
    request.onblocked = () => { clearTimeout(timer); late = true; reject(new Error('storage blocked')); };
  });
}

async function put(pathname, response) {
  if (!response.ok || response.type === 'opaque' || response.redirected) throw new Error('asset unavailable');
  const copy = response.clone(), contentType = copy.headers.get('Content-Type') || '';
  if (pathname.endsWith('.js') && !/(?:javascript|ecmascript)/i.test(contentType)) throw new Error('invalid script');
  if (pathname.endsWith('.css') && !/text\/css/i.test(contentType)) throw new Error('invalid stylesheet');
  const body = await copy.arrayBuffer();
  if (body.byteLength > 8 * 1024 * 1024) throw new Error('asset too large');
  const headers = {};
  for (const name of ['Content-Type','Content-Security-Policy','X-Content-Type-Options','Referrer-Policy']) if (copy.headers.has(name)) headers[name] = copy.headers.get(name);
  const db = await database();
  await new Promise((resolve,reject) => { const tx = db.transaction('assets','readwrite'); tx.objectStore('assets').put({key:pathname,body,headers}); tx.oncomplete = () => {db.close();resolve();}; tx.onerror = tx.onabort = () => {db.close();reject(tx.error);}; });
}

async function cached(pathname) {
  const db = await database();
  return new Promise((resolve,reject) => {
    const tx = db.transaction('assets','readonly'), request = tx.objectStore('assets').get(pathname);
    let record;
    request.onsuccess = () => {record=request.result;};
    tx.oncomplete = () => {db.close();resolve(record ? new Response(record.body,{status:200,headers:record.headers}) : null);};
    tx.onerror = tx.onabort = () => {db.close();reject(tx.error);};
  });
}

self.addEventListener('install', event => event.waitUntil((async () => {
  // A release becomes active only after its HTML, scripts, styles and fonts are complete.
  // Source files are built before serving/deploying; an incomplete build keeps the old worker.
  if (!RELEASE || !SHELL.length) throw new Error('release not built');
  await Promise.all(SHELL.map(async pathname => {
    const response = await fetch(pathname, {cache:'no-store',credentials:'omit'});
    await put(pathname, response);
  }));
  await self.skipWaiting();
})()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  const request = event.request, url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  const navigation = request.mode === 'navigate' && ['/', '/index.html'].includes(url.pathname);
  if (!navigation && !allowed(url.pathname)) return;
  event.respondWith((async () => {
    const pathname = navigation ? `/releases/${RELEASE}/index.html` : url.pathname;
    if (!navigation && released(pathname)) {
      const response = await cached(pathname).catch(() => null);
      if (response) return response;
    }
    try {
      const response = await fetch(request, {cache: navigation ? 'no-store' : 'default'});
      if (!response.ok) throw new Error('network unavailable');
      // Never save current server HTML under another release's offline entry.
      if (!navigation) event.waitUntil(put(pathname,response).catch(() => {}));
      return response;
    } catch (error) {
      const response = await cached(pathname).catch(() => null);
      if (response) return response;
      throw error;
    }
  })());
});
