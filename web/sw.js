// Only the application shell is cached here. Account data and audio live in IndexedDB.
const SHELL_CACHE = 'tingji-shell-v14';
const SHELL = ['/', '/styles.css', '/app.js', '/knowledge.js', '/knowledge.css', '/workflows.js', '/workflows.css', '/sync.js', '/session.js', '/offline.html', '/manifest.webmanifest', '/icons/tingji.svg', '/icons/tingji-192.png', '/icons/tingji-512.png'];
SHELL.push('/reading-interactions.js', '/reading-interactions.css', '/knowledge-relations.js', '/knowledge-relations.css', '/vendor/mermaid/mermaid.tiny-11.12.0.js');
SHELL.push('/prompt-lab.html', '/prompt-lab.js', '/prompt-lab.css', '/vendor/katex/katex.min.css', '/vendor/katex/katex.min.js', '/vendor/katex/fonts/KaTeX_AMS-Regular.ttf', '/vendor/katex/fonts/KaTeX_AMS-Regular.woff', '/vendor/katex/fonts/KaTeX_AMS-Regular.woff2', '/vendor/katex/fonts/KaTeX_Caligraphic-Bold.ttf', '/vendor/katex/fonts/KaTeX_Caligraphic-Bold.woff', '/vendor/katex/fonts/KaTeX_Caligraphic-Bold.woff2', '/vendor/katex/fonts/KaTeX_Caligraphic-Regular.ttf', '/vendor/katex/fonts/KaTeX_Caligraphic-Regular.woff', '/vendor/katex/fonts/KaTeX_Caligraphic-Regular.woff2', '/vendor/katex/fonts/KaTeX_Fraktur-Bold.ttf', '/vendor/katex/fonts/KaTeX_Fraktur-Bold.woff', '/vendor/katex/fonts/KaTeX_Fraktur-Bold.woff2', '/vendor/katex/fonts/KaTeX_Fraktur-Regular.ttf', '/vendor/katex/fonts/KaTeX_Fraktur-Regular.woff', '/vendor/katex/fonts/KaTeX_Fraktur-Regular.woff2', '/vendor/katex/fonts/KaTeX_Main-Bold.ttf', '/vendor/katex/fonts/KaTeX_Main-Bold.woff', '/vendor/katex/fonts/KaTeX_Main-Bold.woff2', '/vendor/katex/fonts/KaTeX_Main-BoldItalic.ttf', '/vendor/katex/fonts/KaTeX_Main-BoldItalic.woff', '/vendor/katex/fonts/KaTeX_Main-BoldItalic.woff2', '/vendor/katex/fonts/KaTeX_Main-Italic.ttf', '/vendor/katex/fonts/KaTeX_Main-Italic.woff', '/vendor/katex/fonts/KaTeX_Main-Italic.woff2', '/vendor/katex/fonts/KaTeX_Main-Regular.ttf', '/vendor/katex/fonts/KaTeX_Main-Regular.woff', '/vendor/katex/fonts/KaTeX_Main-Regular.woff2', '/vendor/katex/fonts/KaTeX_Math-BoldItalic.ttf', '/vendor/katex/fonts/KaTeX_Math-BoldItalic.woff', '/vendor/katex/fonts/KaTeX_Math-BoldItalic.woff2', '/vendor/katex/fonts/KaTeX_Math-Italic.ttf', '/vendor/katex/fonts/KaTeX_Math-Italic.woff', '/vendor/katex/fonts/KaTeX_Math-Italic.woff2', '/vendor/katex/fonts/KaTeX_SansSerif-Bold.ttf', '/vendor/katex/fonts/KaTeX_SansSerif-Bold.woff', '/vendor/katex/fonts/KaTeX_SansSerif-Bold.woff2', '/vendor/katex/fonts/KaTeX_SansSerif-Italic.ttf', '/vendor/katex/fonts/KaTeX_SansSerif-Italic.woff', '/vendor/katex/fonts/KaTeX_SansSerif-Italic.woff2', '/vendor/katex/fonts/KaTeX_SansSerif-Regular.ttf', '/vendor/katex/fonts/KaTeX_SansSerif-Regular.woff', '/vendor/katex/fonts/KaTeX_SansSerif-Regular.woff2', '/vendor/katex/fonts/KaTeX_Script-Regular.ttf', '/vendor/katex/fonts/KaTeX_Script-Regular.woff', '/vendor/katex/fonts/KaTeX_Script-Regular.woff2', '/vendor/katex/fonts/KaTeX_Size1-Regular.ttf', '/vendor/katex/fonts/KaTeX_Size1-Regular.woff', '/vendor/katex/fonts/KaTeX_Size1-Regular.woff2', '/vendor/katex/fonts/KaTeX_Size2-Regular.ttf', '/vendor/katex/fonts/KaTeX_Size2-Regular.woff', '/vendor/katex/fonts/KaTeX_Size2-Regular.woff2', '/vendor/katex/fonts/KaTeX_Size3-Regular.ttf', '/vendor/katex/fonts/KaTeX_Size3-Regular.woff', '/vendor/katex/fonts/KaTeX_Size3-Regular.woff2', '/vendor/katex/fonts/KaTeX_Size4-Regular.ttf', '/vendor/katex/fonts/KaTeX_Size4-Regular.woff', '/vendor/katex/fonts/KaTeX_Size4-Regular.woff2', '/vendor/katex/fonts/KaTeX_Typewriter-Regular.ttf', '/vendor/katex/fonts/KaTeX_Typewriter-Regular.woff', '/vendor/katex/fonts/KaTeX_Typewriter-Regular.woff2');
const STATIC = new Set(SHELL.filter(path => path !== '/'));
self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // Offline help is the minimum useful install; other assets can be retried on use.
    await cache.add('/offline.html');
    await Promise.allSettled(SHELL.filter(path => path !== '/offline.html').map(async path => {
      const response = await fetch(path);
      if (response.ok && !response.redirected) await cache.put(path, response);
    }));
    // Updating the worker does not reload the page or interrupt a file upload.
    await self.skipWaiting();
  })());
});
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter(name => name.startsWith('tingji-shell-') && name !== SHELL_CACHE).map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});
self.addEventListener('message', event => { if (event.data?.type === 'SKIP_WAITING') self.skipWaiting(); });
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  if (request.mode === 'navigate' && url.pathname === '/') {
    event.respondWith((async () => {
      const cache = await caches.open(SHELL_CACHE);
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch(request, { signal: controller.signal });
        if (response.ok && !response.redirected && response.headers.get('content-type')?.includes('text/html')) await cache.put('/', response.clone());
        return response;
      } catch { return (await cache.match('/')) || (await cache.match('/offline.html')); }
      finally { clearTimeout(timeout); }
    })());
  } else if (STATIC.has(url.pathname)) {
    event.respondWith((async () => {
      const cache = await caches.open(SHELL_CACHE);
      const cached = await cache.match(url.pathname);
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch(request, { signal: controller.signal });
        if (response.ok && !response.redirected) await cache.put(url.pathname, response.clone());
        return response;
      } catch { return cached || new Response('Offline asset unavailable', { status: 503, headers: { 'Content-Type': 'text/plain;charset=utf-8' } }); }
      finally { clearTimeout(timeout); }
    })());
  }
});
