import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../web/session.js', import.meta.url), 'utf8');
const { createSessionTransport } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const origin = 'http://127.0.0.1:19879';
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
let checks = 0;
function fixture() {
  let token = 'before-restart', accountId = 'account-one', outcome = 'success', bootWait = null;
  const calls = [];
  const transport = createSessionTransport({ origin, fetchImpl: async (url, options) => {
    const headers = new Headers(options.headers), path = new URL(url).pathname;
    calls.push({ path, token: headers.get('X-App-Token'), actionId: headers.get('X-Action-Id'), body: options.body });
    if (path === '/api/bootstrap') { if (bootWait) await bootWait; return json({ token, accountId }); }
    if (outcome === 'network') throw new TypeError('network failure after possible server acceptance');
    if (outcome === 'forbidden') return json({ error: 'owner denied' }, 403);
    if (outcome === 'double-expiry') return json({ code: 'session_expired' }, 403);
    if (headers.get('X-App-Token') !== token) return outcome === 'legacy' ? json({ error: '页面会话已过期，请刷新页面再试。' }, 403) : json({ code: 'session_expired' }, 403);
    return json({ accepted: true });
  }});
  return { ...transport, calls, rotate: () => { token = 'after-restart'; }, account: value => { accountId = value; },
    outcome: value => { outcome = value; }, wait: promise => { bootWait = promise; } };
}

const concurrent = fixture();
await concurrent.bootstrapSession();
concurrent.rotate();
const body = new Blob(['original recording bytes']);
const results = await Promise.all(Array.from({ length: 8 }, (_, index) => concurrent.sessionFetch('/api/work', {
  method: 'POST', accountId: 'account-one', headers: { 'X-Action-Id': `same-action-${index}` }, body
})));
assert(results.every(result => result.ok));
assert.equal(concurrent.calls.filter(call => call.path === '/api/bootstrap').length, 2);
for (let index = 0; index < 8; index++) {
  const attempts = concurrent.calls.filter(call => call.actionId === `same-action-${index}`);
  assert.equal(attempts.length, 2); assert(attempts.every(call => call.body === body));
  assert.deepEqual(attempts.map(call => call.token), ['before-restart', 'after-restart']);
}
checks++;

for (const outcome of ['network', 'forbidden']) {
  const single = fixture(); await single.bootstrapSession(); single.outcome(outcome);
  if (outcome === 'network') await assert.rejects(single.sessionFetch('/api/paid', { method: 'POST' }), TypeError);
  else assert.equal((await single.sessionFetch('/api/paid', { method: 'POST' })).status, 403);
  assert.equal(single.calls.filter(call => call.path === '/api/paid').length, 1);
  assert.equal(single.calls.filter(call => call.path === '/api/bootstrap').length, 1);
  checks++;
}

const changed = fixture(); await changed.bootstrapSession(); changed.rotate(); changed.account('account-two');
await assert.rejects(changed.sessionFetch('/api/upload', { method: 'PUT', accountId: 'account-one', body }), error => error.code === 'account_changed' && error.permanent);
assert.equal(changed.calls.filter(call => call.path === '/api/upload').length, 1);
assert.equal(changed.currentSession().accountId, 'account-one');
checks++;

for (const outcome of ['legacy', 'double-expiry']) {
  const retry = fixture(); await retry.bootstrapSession(); retry.rotate(); retry.outcome(outcome);
  const result = await retry.sessionFetch('/api/paid', { method: 'POST' });
  assert.equal(result.status, outcome === 'legacy' ? 200 : 403);
  assert.equal(retry.calls.filter(call => call.path === '/api/paid').length, 2);
  checks++;
}

const cancelled = fixture(); await cancelled.bootstrapSession(); cancelled.rotate();
let resume;
cancelled.wait(new Promise(resolve => { resume = resolve; }));
const controller = new AbortController();
const first = cancelled.sessionFetch('/api/one', { method: 'POST', signal: controller.signal });
const second = cancelled.sessionFetch('/api/two', { method: 'POST' });
// Both explicit rejections reached their shared bootstrap before one caller cancels.
while (cancelled.calls.filter(call => call.path === '/api/bootstrap').length < 2) await new Promise(resolve => setImmediate(resolve));
controller.abort();
await assert.rejects(first, error => error.name === 'AbortError');
resume();
assert((await second).ok);
assert.equal(cancelled.calls.filter(call => call.path === '/api/one').length, 1);
assert.equal(cancelled.calls.filter(call => call.path === '/api/two').length, 2);
checks++;
console.log(JSON.stringify({ ok: true, checks }));
