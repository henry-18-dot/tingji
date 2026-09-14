import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.resolve(here, '..');
const out = path.resolve(web, '../docs/note-standard-v5/qa');
const graph = 'flowchart LR\n A[机械臂] -->|建立几何关系| B(运动学)\n B --> C{已知量}\n C -->|关节角| D[正运动学]\n C -->|末端位姿| E[逆运动学]';
const sample = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/knowledge-relations.css"><style>body{margin:0;background:#f5f6f2;font:15px/1.8 'Segoe UI','Microsoft YaHei',sans-serif;color:#333}main{box-sizing:border-box;max-width:920px;margin:auto;padding:32px 24px;background:#fff;min-height:100vh}h1{font-size:22px;font-weight:600}h2{font-size:17px;font-weight:500;margin-top:26px}p{margin:8px 0;color:#5a625b}</style><main><h1>机械臂的运动关系</h1><p>用关系图区分求解方向，用从属树查看知识层级。</p><h2>运动学求解</h2><section id="graph"></section><h2>知识层级</h2><section id="tree"></section><h2>不支持的输入</h2><section id="failure"></section></main><script type="module">import { renderRelation, renderTree } from '/knowledge-relations.js';window.api={renderRelation,renderTree};const graph=renderRelation(${JSON.stringify(graph)});document.querySelector('#graph').append(graph);document.querySelector('#tree').append(renderTree('机械臂\\n  运动学\\n    正运动学\\n    逆运动学\\n  动力学'));document.querySelector('#failure').append(renderRelation('flowchart TD\\n A[输入]\\n click A "https://example.com"'));await graph.renderComplete;window.ready=true;</script></html>`;
const server = http.createServer(async (request, response) => {
  try {
    if (request.url === '/') {
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:" });
      return response.end(sample.replace(/<script type="module">[\s\S]*?<\/script>/, '<script type="module" src="/fixture.js"></script>'));
    }
    if (request.url === '/fixture.js') {
      response.writeHead(200, { 'Content-Type': 'text/javascript; charset=utf-8' });
      return response.end(sample.match(/<script type="module">([\s\S]*?)<\/script>/)[1]);
    }
    if (request.url === '/favicon.ico') { response.writeHead(204); return response.end(); }
    const file = path.resolve(web, '.' + new URL(request.url, 'http://localhost').pathname);
    if (!file.startsWith(web + path.sep)) throw new Error('path');
    response.writeHead(200, { 'Content-Type': { '.js': 'text/javascript', '.css': 'text/css' }[path.extname(file)] || 'application/octet-stream' });
    response.end(await fs.readFile(file));
  } catch { response.writeHead(404); response.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, executablePath: process.env.EDGE_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' });
const context = await browser.newContext({ viewport: { width: 1100, height: 1000 }, serviceWorkers: 'block' });
const page = await context.newPage(), errors = [], external = [];
page.on('pageerror', error => errors.push(error.message));
page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
await context.route('**/*', route => {
  if (route.request().url().startsWith(origin)) route.continue();
  else { external.push(route.request().url()); route.abort(); }
});
try {
  await page.goto(origin);
  await page.waitForFunction(() => window.ready === true);
  assert.equal(await page.locator('#graph .knowledge-relation').getAttribute('data-render-state'), 'ready');
  assert.equal(await page.locator('#tree .knowledge-tree').getAttribute('data-render-state'), 'ready');
  await fs.mkdir(out, { recursive: true });
  await page.screenshot({ path: path.join(out, 'relations-desktop.png'), fullPage: true });
  const branch = page.locator('#tree details').first();
  await branch.locator(':scope > summary').focus();
  await page.keyboard.press('Enter');
  assert.equal(await branch.getAttribute('open'), null, 'native keyboard collapse');
  await page.keyboard.press('Space');
  assert.equal(await branch.getAttribute('open'), '', 'native keyboard expand');
  const valid = [
    graph,
    'graph TD; A[开始] --> B(计算) --> C{是否收敛}; C -->|是| D([完成]); C -->|否| B',
    'flowchart LR\n A((位置)) --- B[[速度]]\n B -.变化率.-> C[(加速度)]',
    'flowchart TB\n A[力] -- 产生 --> B[加速度]\n B == 积分 ==> C[速度]',
    'graph LR\n A[左] <--> B[右]',
  ];
  const validResults = await page.evaluate(async sources => {
    const host = document.createElement('div'); document.body.append(host);
    const results = [];
    for (const source of sources) {
      const element = window.api.renderRelation(source); host.append(element); await element.renderComplete;
      results.push({ state: element.dataset.renderState, text: element.textContent });
    }
    const ids = [...document.querySelectorAll('svg [id],svg[id]')].map(node => node.id);
    const markers = [...document.querySelectorAll('svg [marker-end]')].map(node => node.getAttribute('marker-end').match(/^url\(#(.+)\)$/)?.[1]);
    const unsafe = [...document.querySelectorAll('svg *')].filter(node => ['script', 'foreignObject', 'image', 'a', 'style'].includes(node.localName) || [...node.attributes].some(attr => /^on|href|style$/i.test(attr.name)));
    const markerValid = markers.length > 0 && markers.every(id => id && document.getElementById(id)?.localName === 'marker');
    const unique = new Set(ids).size === ids.length;
    const labelBounds = [...document.querySelectorAll('svg .node')].map(node => {
      const label = node.querySelector('.label text')?.getBoundingClientRect();
      const shape = node.querySelector('rect,circle,ellipse,polygon,path')?.getBoundingClientRect();
      return { text: node.textContent, fits: Boolean(label && shape && label.left >= shape.left - 1 && label.right <= shape.right + 1) };
    });
    const centeredLabels = labelBounds.every(item => item.fits);
    host.remove();
    return { results, unique, markerValid, centeredLabels, labelErrors: labelBounds.filter(item => !item.fits), unsafe: unsafe.length };
  }, valid);
  assert.ok(validResults.results.every(result => result.state === 'ready'), JSON.stringify(validResults.results));
  assert.ok(validResults.unique, 'SVG IDs unique across repeated diagrams');
  assert.ok(validResults.markerValid, 'arrows reference present local markers');
  assert.ok(validResults.centeredLabels, JSON.stringify(validResults.labelErrors));
  assert.equal(validResults.unsafe, 0);
  const attacks = [
    'flowchart TD\n A[ok]\n click A "https://evil.example/a"',
    'flowchart TD\n A[ok]\n click A call dangerous()',
    '%%{init: {"securityLevel":"loose","htmlLabels":true}}%%\nflowchart TD\nA-->B',
    '---\nconfig:\n  securityLevel: loose\n---\nflowchart TD\n A-->B',
    'flowchart TD\n A[<img src=https://evil.example/x onerror=window.pwned=1>] --> B',
    'flowchart TD\n A[<svg onload=window.pwned=1>] --> B',
    'flowchart TD\n A --> B\n style A fill:url(https://evil.example/x)',
    'flowchart TD\n A --> B\n classDef x color:red',
    'flowchart TD\n A --> B\n linkStyle 0 stroke:red',
    'sequenceDiagram\n Alice->>Bob: test',
    'flowchart TD\n A[unterminated --> B',
    `flowchart TD\n ${Array.from({length:13}, (_,i)=>`A${i}`).join('-->')}`,
    'flowchart TD\n A[```<script>window.pwned=1</script>```] --> B',
  ];
  const rejected = await page.evaluate(async sources => {
    const results = [];
    for (const source of sources) {
      const element = window.api.renderRelation(source); document.body.append(element); await element.renderComplete;
      results.push(element.dataset.renderState === 'error' && element.querySelector('code')?.textContent === source && !element.querySelector('svg,script,img'));
      element.remove();
    }
    return results;
  }, attacks);
  assert.ok(rejected.every(Boolean), 'unsafe or invalid input is visible plain-text fallback');
  const treeResults = await page.evaluate(() => {
    const values = ['A\nB', 'A\n  B\n      C', 'A\n  B\n    C\n      D\n        E', '  B', 'A\n\tB', `A\n${Array.from({length:24},(_,i)=>'  '+i).join('\n')}`];
    const invalid = values.every(source => window.api.renderTree(source).dataset.renderState === 'error');
    const literal = window.api.renderTree('目录\n  <img src=x onerror=window.pwned=1>');
    return { invalid, literal: !literal.querySelector('img') && literal.textContent.includes('<img'), attacked: Boolean(window.pwned) };
  });
  assert.ok(treeResults.invalid && treeResults.literal && !treeResults.attacked);
  await page.setViewportSize({ width: 390, height: 844 });
  const mobile = await page.evaluate(() => {
    const scroll = document.querySelector('.knowledge-relation-scroll');
    const before = scroll.scrollLeft; scroll.scrollLeft = 200;
    return { pageFits: document.documentElement.scrollWidth <= innerWidth, graphScrolls: scroll.scrollWidth > scroll.clientWidth && scroll.scrollLeft > before };
  });
  assert.ok(mobile.pageFits && mobile.graphScrolls, JSON.stringify(mobile));
  await page.locator('.knowledge-relation-scroll').first().evaluate(node => { node.scrollLeft = 0; });
  await page.screenshot({ path: path.join(out, 'relations-mobile.png'), fullPage: true });
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  const report = { date: new Date().toISOString(), cspScriptSrc: 'self', validGraphs: valid.length, rejectedGraphs: attacks.length, treeChecks: 8, uniqueIds: validResults.unique, localArrowMarkers: validResults.markerValid, centeredChineseLabels: validResults.centeredLabels, unsafeSvgNodes: validResults.unsafe, keyboardCollapseExpand: true, mobile, externalRequests: external.length, browserErrors: errors };
  await fs.writeFile(path.join(out, 'relations-check.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
} finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
