import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const here = path.dirname(fileURLToPath(import.meta.url)), web = path.resolve(here, '..');
const markdown = [
  '# 机器人动力学与控制',
  '',
  ':::details 课程信息',
  '机器人建模与控制 · 9 月 12 日',
  ':::',
  '',
  '开门见山：**动力学模型**把运动与受力连起来。',
  '',
  '## 四种公式写法',
  '',
  '行内一 $J\\ddot q=\\tau$，行内二 \\(x=A\\cos(\\omega t)\\)。',
  '',
  '\\[E_k=\\frac12J\\dot q^2\\]',
  '',
  '$$\\tau=J\\alpha+b\\omega$$',
  '',
  '$$',
  '\\boldsymbol{M}(q)\\ddot q+\\boldsymbol{C}(q,\\dot q)\\dot q+\\boldsymbol{g}(q)=\\tau',
  '$$',
  '',
  '## 二维机械简图',
  '',
  '```svg 单关节转动惯量简图',
  '<svg viewBox="0 0 760 320" onload="fetch(\'https://bad.example/x\')">',
  '<script>window.pwned=true</script><foreignObject><div>bad</div></foreignObject><image href="https://bad.example/a.png"/>',
  '<line x1="90" y1="200" x2="600" y2="200" stroke="red" stroke-width="5"/>',
  '<circle cx="300" cy="200" r="30" fill="white" stroke="black" stroke-width="4"/>',
  '<path d="M 300 200 L 520 100" stroke="#000" stroke-width="5" fill="none"/>',
  '<polygon points="520,100 497,104 510,122" fill="black"/>',
  '<text x="535" y="95" font-size="26" font-weight="700" onclick="alert(1)">τ</text>',
  '</svg>',
  '```',
  '',
  ':::details 来源定位',
  'S1 06:00–06:32',
  ':::',
].join('\n');

const page = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/vendor/katex/katex.min.css"><link rel="stylesheet" href="/knowledge.css">
<style>body{margin:0;background:#f3f4ef}main{box-sizing:border-box;max-width:900px;margin:auto;padding:42px 30px 90px;background:#fff;min-height:100vh}</style>
<script defer src="/vendor/katex/katex.min.js"></script><main><article id="doc"></article></main>
<script type="module">import {renderMarkdown} from '/knowledge.js';renderMarkdown(${JSON.stringify(markdown).replaceAll('<', '\\u003c')},document.querySelector('#doc'));window.rendererReady=true;</script>`;

const types = { '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf' };
const server = http.createServer(async (req, res) => {
  try {
    if (req.url === '/') { res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); return res.end(page); }
    if (req.url === '/favicon.ico') { res.writeHead(204); return res.end(); }
    const file = path.resolve(web, '.' + new URL(req.url, 'http://localhost').pathname);
    if (!file.startsWith(web + path.sep)) throw new Error('bad path');
    const bytes = await fs.readFile(file);
    res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' }); res.end(bytes);
  } catch { if (!res.headersSent) res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' });
const context = await browser.newContext({ viewport: { width: 1200, height: 900 }, serviceWorkers: 'block' });
const tab = await context.newPage(), errors = [], external = [];
tab.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
tab.on('pageerror', error => errors.push(error.message));
tab.on('request', request => { if (!request.url().startsWith(origin)) external.push(request.url()); });
try {
  await tab.goto(origin);
  await tab.waitForFunction(() => window.rendererReady && document.querySelectorAll('.knowledge-math .katex').length === 5);
  await tab.evaluate(() => document.fonts.ready);
  const result = await tab.evaluate(() => {
    const doc = document.querySelector('#doc'), svg = doc.querySelector('.knowledge-diagram svg'), meta = doc.querySelector('.knowledge-meta');
    const unsafeAttributes = [...svg.querySelectorAll('*')].flatMap(element => [...element.attributes].filter(attribute => /^(?:on|href|xlink:href|style)/i.test(attribute.name)).map(attribute => attribute.name));
    return {
      formulas: doc.querySelectorAll('.knowledge-math .katex').length,
      formulaErrors: doc.querySelectorAll('.knowledge-math.has-error,.knowledge-math.is-pending').length,
      metaCount: meta?.querySelectorAll('.knowledge-details--meta').length,
      metaAfterTitle: meta?.previousElementSibling?.tagName === 'H1',
      diagrams: doc.querySelectorAll('.knowledge-diagram').length,
      forbiddenSvgNodes: svg.querySelectorAll('script,foreignObject,image,a,use,style').length,
      unsafeAttributes,
      svgColors: [...svg.querySelectorAll('[fill],[stroke]')].flatMap(element => ['fill', 'stroke'].map(name => element.getAttribute(name)).filter(Boolean)),
      bodyWeight: getComputedStyle(doc).fontWeight,
      strongWeight: getComputedStyle(doc.querySelector('strong')).fontWeight,
      detailWeight: getComputedStyle(meta.querySelector('.details-body')).fontWeight,
      overflow: document.documentElement.scrollWidth - innerWidth,
    };
  });
  assert.equal(result.formulas, 5); assert.equal(result.formulaErrors, 0);
  assert.equal(result.metaCount, 2); assert.equal(result.metaAfterTitle, true);
  assert.equal(result.diagrams, 1); assert.equal(result.forbiddenSvgNodes, 0); assert.deepEqual(result.unsafeAttributes, []);
  assert.ok(result.svgColors.every(color => ['#000', '#fff', 'none'].includes(color)));
  assert.equal(result.bodyWeight, '400'); assert.equal(result.strongWeight, '700'); assert.ok(Number(result.detailWeight) >= 300 && Number(result.detailWeight) <= 400);
  assert.ok(result.overflow <= 0);
  const before = await tab.evaluate(() => document.body.scrollHeight);
  await tab.getByText('课程信息', { exact: true }).click();
  const after = await tab.evaluate(() => document.body.scrollHeight);
  assert.ok(Math.abs(after - before) <= 2, `metadata changed document height by ${after - before}px`);
  await tab.screenshot({ path: path.join(here, 'knowledge-renderer-desktop.png'), fullPage: true });
  await tab.getByText('课程信息', { exact: true }).click();
  await tab.setViewportSize({ width: 390, height: 844 });
  assert.ok(await tab.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await tab.screenshot({ path: path.join(here, 'knowledge-renderer-mobile.png'), fullPage: true });
  assert.deepEqual(external, []); assert.deepEqual(errors, []);
  console.log(JSON.stringify({ ok: true, ...result, desktop: 'web/qa/knowledge-renderer-desktop.png', mobile: 'web/qa/knowledge-renderer-mobile.png' }, null, 2));
} finally {
  await browser.close(); await new Promise(resolve => server.close(resolve));
}
