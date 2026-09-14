// Renew only a request explicitly rejected before execution. Network uncertainty
// must never trigger an automatic repeat of a paid action.
export function createSessionTransport({ fetchImpl = (...args) => globalThis.fetch(...args), origin = globalThis.location?.origin } = {}) {
  let session = null, refreshing = null;
  const listeners = new Set();
  const accountError = () => Object.assign(new Error('电脑账号已变化，请回到原账号后继续。'), { code: 'account_changed', permanent: true });
  const checkAccount = accountId => {
    if (accountId && session?.accountId && accountId !== session.accountId) throw accountError();
  };
  function abortable(promise, signal) {
    if (!signal) return promise;
    if (signal.aborted) return Promise.reject(signal.reason || new DOMException('Aborted', 'AbortError'));
    return new Promise((resolve, reject) => {
      const abort = () => reject(signal.reason || new DOMException('Aborted', 'AbortError'));
      signal.addEventListener('abort', abort, { once: true });
      promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
    });
  }
  async function bootstrapSession({ signal, accountId = session?.accountId } = {}) {
    checkAccount(accountId);
    if (!refreshing) {
      const expectedAccount = accountId;
      refreshing = (async () => {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        try {
          const response = await fetchImpl(new URL('/api/bootstrap', origin), {
            credentials: 'same-origin', cache: 'no-store', signal: controller.signal
          });
          const data = await response.json();
          if (!response.ok) throw Object.assign(new Error(data.error || '暂时无法连接电脑。'), { status: response.status, permanent: response.status < 500 });
          if (typeof data.token !== 'string' || !data.token || typeof data.accountId !== 'string' || !data.accountId) throw Object.assign(new Error('电脑连接信息不完整，请稍后重试。'), { permanent: true });
          if (expectedAccount && data.accountId !== expectedAccount) throw accountError();
          session = data;
          for (const listener of listeners) listener(data);
          return data;
        } finally { clearTimeout(timeout); }
      })().finally(() => { refreshing = null; });
    }
    const data = await abortable(refreshing, signal);
    if (accountId && data.accountId !== accountId) throw accountError();
    return data;
  }
  async function sessionFetch(input, { accountId = session?.accountId, ...options } = {}) {
    const url = new URL(input, origin);
    if (url.origin !== origin) throw Object.assign(new Error('请通过当前电脑的听记地址连接。'), { permanent: true });
    checkAccount(accountId);
    const method = (options.method || 'GET').toUpperCase();
    const mutates = !['GET', 'HEAD'].includes(method);
    if (mutates && !session?.token) await bootstrapSession({ accountId, signal: options.signal });
    const headers = new Headers(options.headers);
    const token = session?.token || headers.get('X-App-Token') || '';
    if (token) headers.set('X-App-Token', token);
    const send = () => fetchImpl(url, { ...options, headers, credentials: 'same-origin', cache: 'no-store' });
    const response = await send();
    if (!mutates || response.status !== 403) return response;
    let error;
    try { error = await response.clone().json(); } catch { return response; }
    const expired = error.code === 'session_expired' || (!error.code && error.error === '页面会话已过期，请刷新页面再试。');
    if (!expired || (globalThis.ReadableStream && options.body instanceof ReadableStream)) return response;
    // Another concurrent request may already have obtained the replacement token.
    if (!session?.token || session.token === token) await bootstrapSession({ accountId, signal: options.signal });
    checkAccount(accountId);
    if (options.signal?.aborted) throw options.signal.reason || new DOMException('Aborted', 'AbortError');
    headers.set('X-App-Token', session.token);
    return send();
  }
  return { sessionFetch, bootstrapSession, currentSession: () => session,
    onSession: listener => { listeners.add(listener); return () => listeners.delete(listener); } };
}

const transport = createSessionTransport();
export const { sessionFetch, bootstrapSession, currentSession, onSession } = transport;
