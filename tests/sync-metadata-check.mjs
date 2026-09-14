// Synthetic browser + HTTP fixtures only. No production data, audio upload, or paid calls.
import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const web = path.resolve('web'), notes = new Map(), requests = [], receipts = new Map(), checks = [], errors = [];
const stamp = new Date().toISOString();
const base = (id, extra = {}) => ({ id, accountId: 'metadata-fixture', title: '自动课名', nameSource: 'auto', transcript: '', summary: '完整课堂笔记', revision: 4, status: 'ready', sourceName: 'recording.webm', audioUrl: `/api/audio/${id}`, audioAvailable: true, duration: 120, recordedAt: stamp, createdAt: stamp, updatedAt: stamp, ...extra });
const brief = note => Object.fromEntries(Object.entries({ ...note, hasSummary: !!note.summary, hasTranscript: !!note.transcript }).filter(([key]) => !['summary','transcript','segments','chat'].includes(key)));
let delayNextPush = false, releasePush, pushArrived;
const server = http.createServer(async (req, res) => {
 const json = (value, status=200) => { res.writeHead(status,{'Content-Type':'application/json'}); res.end(JSON.stringify(value)); };
 try {
  const url = new URL(req.url, 'http://localhost');
  if (!url.pathname.startsWith('/api/')) {
   if(url.pathname==='/'){res.writeHead(200,{'Content-Type':'text/html'});return res.end('<!doctype html><meta charset="utf-8"><title>Sync metadata fixture</title>');}
   const target=path.resolve(web,'.'+url.pathname); if(!target.startsWith(web+path.sep))return json({},404);
   const content=await fs.readFile(target);res.writeHead(200,{'Content-Type':'text/javascript'});return res.end(content);
  }
  const chunks=[];for await(const chunk of req)chunks.push(chunk);
  const body=chunks.length?JSON.parse(Buffer.concat(chunks).toString()):null;
  requests.push({path:url.pathname,method:req.method,body});
  if(url.pathname==='/api/bootstrap')return json({token:'fixture-token',accountId:'metadata-fixture'});
  if(url.pathname==='/api/sync/push'){
   if(receipts.has(body.operationId))return json(receipts.get(body.operationId));
   if(delayNextPush){delayNextPush=false;pushArrived?.();await new Promise(resolve=>releasePush=resolve);}
   const current=notes.get(body.note.id);
   if(!current)return json({error:'Unexpected new-note push'},400);
   if(body.metadataPatch){
    for(const key of Object.keys(body.metadataPatch)){
     if(key==='nameSource')continue;
     if(body.baseRevision!==current.revision && (body.metadataBase?.[key]??null)!==(current[key]??null) && !(key==='title'&&current.nameSource==='auto'))return json({error:'Another device edited this field'},400);
    }
   }
   const note={...current,...(body.metadataPatch||body.note),revision:current.revision+1,updatedAt:new Date().toISOString()};
   notes.set(note.id,note);const result={note,conflict:false};receipts.set(body.operationId,result);return json(result);
  }
  const match=url.pathname.match(/^\/api\/notes\/([^/]+)$/);
  if(match)return notes.has(match[1])?json(notes.get(match[1])):json({error:'missing'},404);
  return json({error:'Unexpected audio or other request'},400);
 }catch(error){json({error:error.message},500);}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
const context=await browser.newContext({serviceWorkers:'block'}),page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
async function seed(note, full=true){notes.set(note.id,structuredClone(note));await page.evaluate(n=>a.cacheNotes([n]),full?note:brief(note));}
async function run(label, fn){if(process.argv.includes('--only-concurrent') && !label.startsWith('Second rename') && !label.startsWith('Metadata tests'))return;try{await fn();checks.push({check:label,passed:true});}catch(error){checks.push({check:label,passed:false,error:error.message});}}
try{
 await page.goto(origin);await page.evaluate(async()=>{const m=await import('/sync.js');window.a=m.createSyncClient({accountId:'metadata-fixture',getToken:()=> 'fixture-token'});await a.ready;});
 await run('Title metadata updates during cloud work preserve content and avoid audio upload',async()=>{
  await seed(base('title',{status:'transcribing',summary:'已有笔记',asrTask:{state:'processing',requestId:'test'}}));
  await page.evaluate(()=>a.updateLocalNote('title',{title:'人工课名',nameSource:'manual'}));
  notes.set('title',{...notes.get('title'),revision:5,stage:'云端转写'});
  const result=await page.evaluate(()=>a.pushNote('title'));
  const sent=requests.filter(r=>r.path==='/api/sync/push').at(-1).body;
  assert.deepEqual(sent.metadataPatch,{title:'人工课名',nameSource:'manual'});assert.deepEqual(sent.metadataBase,{title:'自动课名',nameSource:'auto'});
  assert.equal(result.note.summary,'已有笔记');assert.equal(result.note.status,'transcribing');assert.equal(result.note.dirty,false);
 });
 await run('Archive, trash and restore survive offline save and synchronize metadata',async()=>{
  await seed(base('lifecycle'));
  for(const [method,arg,field,truth]of[['setArchived',true,'archivedAt',true],['setArchived',false,'archivedAt',false],['deleteNote',undefined,'trashedAt',true],['restoreNote',undefined,'trashedAt',false]]){
   const before=requests.length;await context.setOffline(true);
   const saved=await page.evaluate(({method,arg})=>a[method]('lifecycle',arg),{method,arg});
   assert.equal(!!saved[field],truth);assert.equal(saved.dirty,true);assert.equal(requests.length,before);
   await context.setOffline(false);const pushed=await page.evaluate(()=>a.pushNote('lifecycle'));assert.equal(!!pushed.note[field],truth);assert.equal(pushed.note.summary,'完整课堂笔记');assert.equal(pushed.note.dirty,false);
  }
 });
 await run('Local recording in trash never uploads and returns to pending after restore',async()=>{
  const recording=await page.evaluate(()=>a.saveLocalRecording(new Blob(['synthetic audio'],{type:'audio/webm'}),{title:'本机待传',name:'本机待传.webm',recordedAt:new Date().toISOString(),duration:27}));
  await page.evaluate(id=>a.deleteNote(id),recording.id);const before=requests.length;
  const pending=await page.evaluate(()=>a.listPending());assert.equal(pending.some(n=>n.id===recording.id),false);
  const pushed=await page.evaluate(id=>a.pushNote(id),recording.id);assert.equal(pushed.unchanged,true);assert.equal(requests.length,before);
  const restored=await page.evaluate(async id=>{await a.restoreNote(id);return{pending:await a.listPending(),size:(await a.getAudioBlob(id)).size};},recording.id);
  assert.equal(restored.pending.some(n=>n.id===recording.id),true);assert.equal(restored.size,15);
 });
 await run('Finished-note cache downloads only clean completed text and skips unchanged revision',async()=>{
  const ready=base('finished'),active=base('active',{summary:'',status:'transcribing'}),dirty=base('dirty');
  for(const note of[ready,active,dirty])await seed(note,false);
  await page.evaluate(()=>a.updateLocalNote('dirty',{summary:'本机正文修改'}));
  const list=[ready,active,dirty].map(brief),before=requests.length;
  await page.evaluate(list=>a.cacheFinishedNotes(list),list);
  assert.deepEqual(requests.slice(before).map(r=>r.path),['/api/notes/finished']);
  const local=await page.evaluate(async()=>({ready:await a.getNote('finished'),dirty:await a.getNote('dirty')}));
  assert.equal(local.ready.offlineReady,true);assert.equal(local.ready.summary,'完整课堂笔记');assert.equal(local.dirty.summary,'本机正文修改');assert.equal(local.dirty.dirty,true);
  const unchanged=requests.length;await page.evaluate(list=>a.cacheFinishedNotes(list),list);assert.equal(requests.length,unchanged);
 });
 await run('Directory refresh updates course metadata while preserving a pending manual name',async()=>{
  await seed(base('metadata',{courseName:'旧课名',courseColor:'#112233'}));
  await page.evaluate(()=>a.updateLocalNote('metadata',{title:'手动标题',nameSource:'manual'}));
  const changed=base('metadata',{revision:8,title:'自动新标题',courseName:'机器人驱动系统',courseColor:'#8866aa',duration:2126,lessonSlot:'周三 78 节',trashedAt:stamp});
  const local=await page.evaluate(async note=>{await a.cacheNotes([note]);return a.getNote(note.id);},brief(changed));
  assert.equal(local.title,'手动标题');assert.equal(local.nameSource,'manual');assert.equal(local.courseName,changed.courseName);assert.equal(local.courseColor,changed.courseColor);assert.equal(local.duration,2126);assert.equal(local.trashedAt,stamp);assert.equal(local.dirty,true);
 });
 await run('Second rename during in-flight sync remains pending and accepts later status-only revision',async()=>{
  await seed(base('concurrent',{nameSource:'manual',title:'原名',status:'transcribing'}));
  await page.evaluate(()=>a.updateLocalNote('concurrent',{title:'第一名',nameSource:'manual'}));
  const arrived=new Promise(resolve=>pushArrived=resolve);delayNextPush=true;
  await page.evaluate(()=>{window.runningPush=a.pushNote('concurrent');});await arrived;
  await page.evaluate(()=>a.updateLocalNote('concurrent',{title:'第二名',nameSource:'manual'}));releasePush();
  const first=await page.evaluate(()=>runningPush);assert.equal(first.pendingEdits,true);assert.equal(first.note.title,'第二名');assert.equal(first.note.dirty,true);
  notes.set('concurrent',{...notes.get('concurrent'),revision:notes.get('concurrent').revision+1,stage:'仍在转写'});
  const second=await page.evaluate(()=>a.pushNote('concurrent'));assert.equal(second.note.title,'第二名');assert.equal(second.note.dirty,false);
 });
 await run('Metadata tests made no audio, ASR or summarization request',async()=>{
  assert.equal(requests.some(r=>/audio|upload|transcribe|summarize/.test(r.path)),false);assert.deepEqual(errors,[]);
 });
 if(process.argv.includes('--only-concurrent')){const prior=JSON.parse(await fs.readFile('docs/screenshots/sync-metadata/checks.json','utf8'));for(const check of prior.checks)if(!checks.some(c=>c.check===check.check))checks.push(check);}
 const output={checks,errors};await fs.mkdir('docs/screenshots/sync-metadata',{recursive:true});await fs.writeFile('docs/screenshots/sync-metadata/checks.json',JSON.stringify(output,null,2));console.log(JSON.stringify(output,null,2));
 if(checks.some(c=>!c.passed))process.exitCode=1;
}finally{releasePush?.();await browser.close();await new Promise(resolve=>server.close(resolve));}
