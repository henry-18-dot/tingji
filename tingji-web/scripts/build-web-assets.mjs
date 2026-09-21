#!/usr/bin/env node
// Snapshot one coherent public application release. No private or runtime data is read.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';

const args = process.argv.slice(2), rootArg = args.indexOf('--public-root');
const root = path.resolve(rootArg >= 0 ? args[rootArg + 1] : path.join(path.dirname(fileURLToPath(import.meta.url)), '../public'));
const checking = args.includes('--check');
const indexFile = path.join(root, 'index.html'), workerFile = path.join(root, 'sw.js');
const releaseBlock = /\/\/ BEGIN GENERATED RELEASE[\s\S]*?\/\/ END GENERATED RELEASE/;
const emptyBlock = "// BEGIN GENERATED RELEASE\nconst RELEASE = '';\nconst SHELL = [];\n// END GENERATED RELEASE";
const sourceHtml = fs.readFileSync(indexFile, 'utf8')
  .replace(/<meta name="tingji-release" content="[a-f0-9]+">\s*/g, '')
  .replace(/(["'])\/?releases\/[a-f0-9]{16}\//g, '$1./');
const worker = fs.readFileSync(workerFile, 'utf8').replace(releaseBlock, emptyBlock);
if (!releaseBlock.test(worker)) throw new Error('Missing worker release marker');
const files = new Map();
const supported = /\.(?:js|css|svg|png|jpe?g|webp|woff2?|ttf)$/i;
function collect(folder, prefix = '') {
  for (const item of fs.readdirSync(folder, {withFileTypes:true}).sort((a,b) => a.name.localeCompare(b.name))) {
    if (item.isSymbolicLink()) throw new Error('Public symlink is not allowed');
    const relative = prefix + item.name;
    if (item.isDirectory()) {
      if (!prefix && !['assets', 'vendor'].includes(item.name)) continue;
      collect(path.join(folder, item.name), relative + '/');
    } else if (item.name !== 'sw.js' && supported.test(item.name)) files.set(relative, fs.readFileSync(path.join(folder, item.name)));
  }
}
collect(root);
const hash = crypto.createHash('sha256');
hash.update(sourceHtml).update('\0').update(worker).update('\0');
for (const [name, bytes] of files) hash.update(name).update('\0').update(bytes).update('\0');
const release = hash.digest('hex').slice(0,16), prefix = `/releases/${release}/`;
const split = sourceHtml.indexOf('</head>');
if (split < 0) throw new Error('Entry page has no head');
const head = sourceHtml.slice(0,split).replace(/\b(src|href)=(['"])(?:\.\/|\/)?([^'"?#]+)\2/g, (all, attr, quote, name) =>
  files.has(name) ? `${attr}=${quote}${prefix}${name}${quote}` : all);
const html = head + `<meta name="tingji-release" content="${release}">\n` + sourceHtml.slice(split);
const shell = [...files.keys(), 'index.html'].map(name => prefix + name);
// Note images are referenced by saved Markdown at stable URLs; include them for offline reading too.
shell.push(...[...files.keys()].filter(name => name.startsWith('assets/note-resources/')).map(name => '/' + name));
const builtWorker = worker.replace(releaseBlock, `// BEGIN GENERATED RELEASE\nconst RELEASE = '${release}';\nconst SHELL = ${JSON.stringify(shell)};\n// END GENERATED RELEASE`);
const target = path.join(root, 'releases', release);
function writeOrCheck(file, data) {
  const expected = Buffer.isBuffer(data) ? data : Buffer.from(data);
  if (checking) {
    if (!fs.existsSync(file) || !fs.readFileSync(file).equals(expected)) throw new Error(`Stale web release: ${path.relative(root,file)}`);
  } else {
    fs.mkdirSync(path.dirname(file), {recursive:true});
    fs.writeFileSync(file, expected);
  }
}
for (const [name, bytes] of files) writeOrCheck(path.join(target,name), bytes);
writeOrCheck(path.join(target,'index.html'),html);
writeOrCheck(indexFile, html);
writeOrCheck(workerFile, builtWorker);
console.log(JSON.stringify({release, assets: files.size, checked:checking}));
