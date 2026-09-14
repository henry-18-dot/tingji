import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const here = path.dirname(fileURLToPath(import.meta.url)), web = path.resolve(here, '..');
const fixture = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/vendor/katex/katex.min.css"><link rel="stylesheet" href="/knowledge.css"><link rel="stylesheet" href="/reading-interactions.css">
<style>body{margin:0;background:#f4f5f0}main{box-sizing:border-box;max-width:850px;margin:auto;padding:35px 25px 65px;background:white;min-height:100vh}.chapter-body{padding-top:4px}summary h2{display:inline-block}summary{cursor:pointer}</style>
<script src="/vendor/katex/katex.min.js"></script><main><article class="knowledge-document" id="doc"><h1>机器人空间描述</h1>
<p>位置描述坐标原点，姿态描述坐标轴朝向。<button type="button" class="concept-trigger" data-concept="dh">DH 变换</button>把相邻坐标系连接起来。</p>
<p>用齐次变换一起描述位置和姿态。<code>齐次变换</code> <a href="https://example.org">齐次变换</a></p>
<details class="knowledge-chapter" open><summary><h2>四个参数</h2></summary><div class="chapter-body">
<p>先看连杆长度 <span class="knowledge-math" data-source-tex="a_i" data-display="false"></span>，然后看偏距 <span class="knowledge-math" data-source-tex="d_i" data-display="false"></span>。</p>
<div class="knowledge-math knowledge-math--display" data-source-tex="a_i + a_{i} + a_j + a^i + \\alpha_i + \\theta_i + d_i" data-display="true"></div>
<p>扭角 <span class="knowledge-math" data-source-tex="\\alpha_i" data-display="false"></span> 和转角 <span class="knowledge-math" data-source-tex="\\theta_i" data-display="false"></span> 都是角度。</p>
<div class="knowledge-math knowledge-math--display" data-source-tex="\\begin{bmatrix}a_i &amp; d_i \\\\ \\theta_i &amp; \\alpha_i\\end{bmatrix}" data-display="true"></div>
</div></details>
<details class="knowledge-chapter" open><summary><h2>另一个小节</h2></summary><div class="chapter-body"><p><span class="knowledge-math" data-source-tex="a_i" data-display="false"></span> 留在自己的小节。</p></div></details>
<details class="knowledge-chapter" open><summary><h2>继续探索</h2></summary><div class="chapter-body"><ul><li>为什么齐次变换用四维矩阵？</li><li>DH 坐标系应该怎样建立？</li><li>转动次序为什么会影响结果？</li></ul></div></details>
</article><button id="outside">正文之外</button></main>
<script type="module">
import { mountReadingFeatures } from '/reading-interactions.js';
const root = document.querySelector('#doc');
for (const item of root.querySelectorAll('.knowledge-math')) katex.render(item.dataset.sourceTex,item,{displayMode:item.dataset.display==='true',trust:false,output:'htmlAndMathml'});
window.readingData={concepts:[{id:'dh',term:'DH 变换',question:'DH 变换为什么长成这样？',links:[{title:'非法链接',url:'javascript:alert(1)'},{title:'参数与坐标系',url:'https://example.com/dh'},{title:'矩阵推导',url:'https://example.com/matrix'},{title:'更多图解',url:'https://example.com/figure'},{title:'第四个链接',url:'https://example.com/extra'}]},{id:'transform',term:'齐次变换',question:'为什么要多加一个坐标？',links:[]}],terms:[{text:'位置',group:'position'},{text:'姿态',group:'orientation'}],symbols:['a_i','d_i','theta_i','alpha_i']};
window.cleanupReading=mountReadingFeatures(root,window.readingData);window.mountAgain=()=>window.cleanupReading=mountReadingFeatures(root,window.readingData);window.ready=true;
</script>`;
const types = { '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf' };
const server = http.createServer(async (req, res) => {
  try {
    if (req.url === '/') { res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); return res.end(fixture); }
    if (req.url === '/favicon.ico') { res.writeHead(204); return res.end(); }
    const file = path.resolve(web, '.' + new URL(req.url, 'http://localhost').pathname);
    if (!file.startsWith(web + path.sep)) throw new Error('bad path');
    const bytes = await fs.readFile(file); res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' }); res.end(bytes);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' });
const context = await browser.newContext({ viewport: { width: 1200, height: 900 }, serviceWorkers: 'block' });
await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin });
const tab = await context.newPage(), errors = [], external = [], checks = [];
tab.on('pageerror', error => errors.push(error.message));
tab.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
tab.on('request', request => { if (!request.url().startsWith(origin)) external.push(request.url()); });
async function check(name, fn) { await fn(); checks.push(name); }
try {
  await tab.goto(origin); await tab.waitForFunction(() => window.ready); await tab.evaluate(() => document.fonts.ready);
  await check('closed initially, terms colored, complete symbols available', async () => {
    assert.equal(await tab.locator('.reading-popover:visible').count(), 0);
    assert.equal(await tab.locator('.reading-symbol.is-highlighted').count(), 0);
    assert.equal(await tab.locator('.reading-symbol-control').count(), 5);
    const colors = await tab.locator('.reading-term').evaluateAll(items => items.map(item => getComputedStyle(item).color));
    assert.equal(colors.length, 4); assert.equal(new Set(colors).size, 2);
    assert.equal(await tab.locator('.concept-trigger[data-concept="transform"]').count(), 1);
    assert.equal(await tab.locator('code .concept-trigger,a .concept-trigger').count(), 0);
  });
  await check('concept click with one question, three safe links, right-side copy', async () => {
    await tab.getByRole('button', { name: 'DH 变换', exact: true }).click();
    const popup = tab.getByRole('dialog', { name: 'DH 变换', exact: true });
    assert.equal(await popup.locator('.reading-question').count(), 1); assert.equal(await popup.getByRole('link').count(), 3);
    assert.equal(await popup.getByText('可复制的问题').count(), 0); assert.equal(await popup.getByText('相关讲解').count(), 0);
    assert.ok((await popup.getByRole('link').evaluateAll(links => links.map(link => link.href))).every(url => /^https:\/\//.test(url)));
    const question = await popup.locator('.reading-question').boundingBox(), copy = await popup.getByRole('button').boundingBox(); assert.ok(copy.x > question.x + question.width);
  });
  await check('copy success and failure recovery', async () => {
    await tab.getByRole('dialog', { name: 'DH 变换', exact: true }).getByRole('button', { name: '复制问题', exact: true }).click();
    await tab.waitForSelector('.reading-copy.is-copied'); assert.equal(await tab.evaluate(() => navigator.clipboard.readText()), 'DH 变换为什么长成这样？');
    assert.equal(await tab.locator('.reading-copy.is-copied').count(), 1);
    await tab.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('test denied'); } } }));
    await tab.getByRole('dialog', { name: 'DH 变换', exact: true }).getByRole('button').click();
    const fallback = tab.getByRole('textbox', { name: '请手动复制' }); await fallback.waitFor({ state: 'visible' }); assert.equal(await fallback.inputValue(), 'DH 变换为什么长成这样？');
    assert.equal(await fallback.evaluate(field => field.selectionEnd - field.selectionStart), 'DH 变换为什么长成这样？'.length);
  });
  await check('escape, keyboard activation and outside close', async () => {
    await tab.keyboard.press('Escape'); assert.equal(await tab.locator('.reading-popover:visible').count(), 0);
    assert.equal(await tab.evaluate(() => document.activeElement.dataset.concept), 'dh'); await tab.keyboard.press('Enter');
    assert.equal(await tab.locator('.reading-concept-popover:visible').count(), 1); await tab.locator('#outside').click(); assert.equal(await tab.locator('.reading-popover:visible').count(), 0);
  });
  await check('exact a_i highlight, no a_j/alpha_i/superscript or other section', async () => {
    const first = tab.locator('.reading-symbol-control').first(); await first.focus(); await tab.keyboard.press('Enter');
    const highlighted = await tab.locator('.reading-symbol.is-highlighted').evaluateAll(items => items.map(item => item.dataset.readingSymbol));
    assert.deepEqual(highlighted, ['a_i', 'a_i', 'a_i', 'a_i']);
    assert.equal(await tab.locator('.knowledge-chapter').nth(1).locator('.is-highlighted').count(), 0);
    assert.equal(await tab.locator('.knowledge-math--display').first().locator('[data-reading-symbol="a_i"]').count(), 2);
    await first.press('Space'); assert.equal(await tab.locator('.is-highlighted').count(), 0);
  });
  await check('settings are compact and persist; disabled symbols do not highlight', async () => {
    await tab.getByRole('button', { name: '阅读设置', exact: true }).click();
    await tab.getByLabel('术语颜色', { exact: true }).uncheck(); await tab.getByLabel('公式变量联动', { exact: true }).uncheck();
    await tab.keyboard.press('Escape'); await tab.locator('.reading-symbol-control').first().click({ force: true }); assert.equal(await tab.locator('.is-highlighted').count(), 0);
    await tab.waitForFunction(() => [...document.querySelectorAll('.reading-term')].every(item => getComputedStyle(item).color === getComputedStyle(item.parentElement).color));
    await tab.reload(); await tab.waitForFunction(() => window.ready); assert.equal(await tab.locator('.reading-terms-on,.reading-symbols-on').count(), 0);
    await tab.getByRole('button', { name: '阅读设置', exact: true }).click(); assert.equal(await tab.getByLabel('术语颜色', { exact: true }).isChecked(), false);
    await tab.getByLabel('术语颜色', { exact: true }).check(); await tab.getByLabel('公式变量联动', { exact: true }).check(); await tab.keyboard.press('Escape');
  });
  await check('three exploration copy buttons', async () => {
    assert.equal(await tab.locator('.reading-exploration > li > .reading-copy').count(), 3);
    await tab.locator('.reading-exploration > li > .reading-copy').nth(1).click(); await tab.waitForSelector('.reading-exploration .reading-copy.is-copied'); assert.equal(await tab.evaluate(() => navigator.clipboard.readText()), 'DH 坐标系应该怎样建立？');
  });
  await check('safe remount and cleanup', async () => {
    await tab.evaluate(() => window.mountAgain()); assert.equal(await tab.locator('.reading-settings-toggle').count(), 1); assert.equal(await tab.locator('.reading-exploration .reading-copy').count(), 3);
    await tab.evaluate(() => window.cleanupReading()); assert.equal(await tab.locator('.reading-popover,.reading-term,.reading-symbol,.reading-copy').count(), 0);
    assert.equal(await tab.locator('.knowledge-math .katex').count(), 7); await tab.evaluate(() => window.mountAgain());
  });
  await check('delayed KaTeX is enhanced after load', async () => {
    await tab.evaluate(() => {
      window.cleanupReading(); window.savedKatex = window.katex; window.katex = undefined;
      for (const math of document.querySelectorAll('.knowledge-math')) { math.classList.add('is-pending'); math.textContent = math.dataset.sourceTex; }
      window.mountAgain();
    });
    assert.equal(await tab.locator('.reading-symbol-control').count(), 0);
    await tab.evaluate(() => {
      window.katex = window.savedKatex;
      for (const math of document.querySelectorAll('.knowledge-math')) { katex.render(math.dataset.sourceTex, math, { trust: false, displayMode: math.dataset.display === 'true' }); math.classList.remove('is-pending'); }
      window.dispatchEvent(new Event('load'));
    });
    assert.equal(await tab.locator('.reading-symbol-control').count(), 5);
  });
  await check('source HTML commands and text are never enabled by enhancement', async () => {
    await tab.evaluate(() => {
      window.cleanupReading();
      const holder = document.createElement('p'); holder.id = 'unsafe-fixture';
      for (const source of ['\\htmlClass{evil}{a_i}', '\\href{https://example.com/bad}{a_i}', '\\text{a_i}', 'a_{i+1}']) {
        const math = document.createElement('span'); math.className = 'knowledge-math'; math.dataset.sourceTex = source; math.dataset.display = 'false';
        katex.render(source, math, { trust: false, strict: false, throwOnError: false }); holder.append(math);
      }
      document.querySelector('#doc').append(holder); window.mountAgain();
    });
    assert.equal(await tab.locator('#unsafe-fixture .reading-symbol,#unsafe-fixture .evil,#unsafe-fixture a').count(), 0);
    await tab.evaluate(() => { window.cleanupReading(); document.querySelector('#unsafe-fixture').remove(); window.mountAgain(); });
  });
  await tab.getByRole('button', { name: 'DH 变换', exact: true }).click();
  await tab.screenshot({ path: path.join(here, 'reading-interactions-desktop.png'), fullPage: true });
  await tab.keyboard.press('Escape'); await tab.setViewportSize({ width: 390, height: 844 });
  await check('mobile popup stays in viewport, with working touch-sized copy', async () => {
    await tab.getByRole('button', { name: 'DH 变换', exact: true }).click();
    const box = await tab.getByRole('dialog', { name: 'DH 变换', exact: true }).boundingBox(); assert.ok(box.x >= 0 && box.x + box.width <= 390 && box.y >= 0 && box.y + box.height <= 844);
    const copy = await tab.getByRole('dialog', { name: 'DH 变换', exact: true }).getByRole('button').boundingBox(); assert.ok(copy.width >= 42 && copy.height >= 42);
    assert.ok(await tab.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  });
  await tab.screenshot({ path: path.join(here, 'reading-interactions-mobile.png'), fullPage: true });
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  const result = { ok: true, checks, browserErrors: errors, externalRequests: external, screenshots: ['reading-interactions-desktop.png', 'reading-interactions-mobile.png'] };
  await fs.writeFile(path.join(here, 'reading-interactions-result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result, null, 2));
} finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
