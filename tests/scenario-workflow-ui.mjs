// Isolated browser fixtures: production UI, no provider calls or user data.
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url);
const {chromium}=require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=process.cwd(), out=path.join(root,'docs/releases/2026-09-10-course-workflow/qa');
await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
const origin='http://127.0.0.1:19886', errors=[], checks=[];
const stamp='2026-09-10T01:00:00Z';
const note=(id,extra={})=>({id,title:id,createdAt:stamp,updatedAt:stamp,revision:1,status:'ready',summary:'## '+id+'\n\n课堂笔记内容。',transcript:'',transcriptPurgedAt:stamp,asrComplete:true,language:'zh',template:'lecture',audioAvailable:false,...extra});
async function fixture(viewport){
 const ctx=await browser.newContext({viewport,hasTouch:viewport.width<1100,serviceWorkers:'block',reducedMotion:'reduce'});
 const notes=[note('本周控制理论',{lessonId:'control-weekly',courseId:'control',classDate:'2026-09-10'}),note('上周控制理论',{lessonId:'control-weekly',courseId:'control',classDate:'2026-09-03'}),note('补充笔记'),note('归档录音',{sourceName:'archived.m4a',audioArchivedAt:stamp,audioAvailable:true,audioUrl:'/api/audio/归档录音',fileSize:8})];
 const writes=[]; let schedule={schemaVersion:1,revision:1,scheduleId:'fixture-term',term:'2026 秋季',timezone:'Asia/Shanghai',week1Start:'2026-09-07',periods:{},dayOverrides:{},exceptions:[],courses:[{id:'control',name:'控制理论',lessons:[{id:'control-weekly',weekday:4,periods:[3,4],weeks:[1,16]},{id:'fixed-date',dates:['2026-09-20'],periods:[5,6]}]}]};
 const lesson={lessonId:'control-weekly',courseId:'control',classDate:'2026-09-10',date:'2026-09-11',courseName:'控制理论',periods:[3,4],notes:[notes[0]]};
 await ctx.route('**/*',async route=>{
  const r=route.request(),u=new URL(r.url()); if(u.origin!==origin){errors.push('External request '+u);return route.abort();}
  const json=(v,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(v)});
  if(u.pathname.startsWith('/api/')){
   if(r.method()!=='GET') writes.push({path:u.pathname,body:r.headers()['content-type']?.includes('json')?r.postDataJSON():null});
   if(u.pathname==='/api/bootstrap')return json({token:'fixture',accountId:'workflow-fixture',localBrowser:true,notes,settings:{asrConfigured:true,deepseekConfigured:true,tosConfigured:true,tosPrivateConfirmed:true},capabilities:{}});
   if(u.pathname==='/api/notes')return json({notes});
   if(u.pathname==='/api/timetable/week')return json({schedule:{warnings:[]},lessons:[lesson]});
   if(u.pathname==='/api/timetable/import')return json({status:'needs_review',schedule:{...schedule,courses:[{id:'new',name:'机器人控制',lessons:[{id:'new-rule',weekday:2,periods:[1,2],weeks:[1,16]}]}]},source:{rawText:'机器人控制 周二 1-2 节',warnings:[]}});
   if(u.pathname==='/api/timetable'){if(r.method()==='POST')schedule={...r.postDataJSON().schedule,revision:2};return json(schedule);}
   if(u.pathname==='/api/upload'){const name=decodeURIComponent(r.headers()['x-file-name']);const n=note('audio-'+notes.length,{title:name,sourceName:name,summary:'',transcriptPurgedAt:null,status:'idle',asrComplete:false,audioAvailable:true,fileSize:r.postDataBuffer().length});notes.push(n);return json(n,201);}
   if(u.pathname==='/api/groups'){const d=r.postDataJSON();const n=note('new-group',{title:d.title,summary:'',status:'idle',transcriptPurgedAt:null,sourceNoteIds:d.sourceIds,asrComplete:false});d.sourceIds.forEach(id=>notes.find(n=>n.id===id).groupParentId=n.id);notes.push(n);return json(n,201);}
   if(u.pathname==='/api/batches'){const d=r.postDataJSON();return json({id:'fixture-batch',state:'complete',items:d.noteIds.map(id=>({noteId:id,title:notes.find(n=>n.id===id).title,state:'completed'}))});}
   if(u.pathname==='/api/notes/merge'){const d=r.postDataJSON();const n=note('merged',{title:d.title,summary:d.noteIds.map(id=>notes.find(n=>n.id===id).summary).join('\n\n---\n\n'),groupKind:'manual_merge'});notes.push(n);return json(n);}
   if(u.pathname==='/api/audio/batch-delete'){const d=r.postDataJSON();return json({scope:d.scope,items:d.noteIds.map(id=>({noteId:id,state:'completed'}))});}
   if(u.pathname==='/api/batches')return json({batches:[]});
   const id=decodeURIComponent(u.pathname.split('/')[3]||'');const n=notes.find(n=>n.id===id);
   if(n)return json(n);
   errors.push('Unexpected API '+u.pathname);return json({error:'fixture missing'},404);
  }
  const f=path.join(root,'web',u.pathname==='/'?'index.html':u.pathname.slice(1));
  try{return route.fulfill({contentType:({'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.webmanifest':'application/manifest+json'})[path.extname(f)]||'text/plain',body:await fs.readFile(f)});}catch{return route.fulfill({status:404,body:''});}
 });
 const page=await ctx.newPage();page.setDefaultTimeout(12000);page.on('pageerror',e=>{errors.push(e.message);console.error('PAGE ERROR',e.message);});
 await page.goto(origin);await page.waitForFunction(()=>!document.querySelector('#save-settings').disabled,{},{timeout:12000});
 return {ctx,page,notes,writes};
}
try{
 const f=await fixture({width:1365,height:960}),p=f.page;
 await p.locator('#file-input').setInputFiles([{name:'课堂-2.m4a',mimeType:'audio/mp4',buffer:Buffer.from('part2')},{name:'课堂-1.m4a',mimeType:'audio/mp4',buffer:Buffer.from('part1')}]);
 await p.locator('#workflow-dialog').waitFor({state:'visible'});
 assert.match(await p.locator('.workflow-file').first().innerText(),/课堂-1/);
 await p.locator('.workflow-file').first().getByRole('button',{name:'下移'}).click();
 assert.match(await p.locator('.workflow-file').first().innerText(),/课堂-2/);
 await p.getByRole('button',{name:'先保存录音',exact:true}).click();
 await p.getByText('已保存 2 个文件，之后可在录音库中选择整理。',{exact:true}).waitFor();
 assert.equal(f.writes.filter(w=>w.path==='/api/groups'||w.path==='/api/batches').length,0);
 await p.screenshot({path:path.join(out,'desktop-import.png')});
 await p.locator('#import-start').click();await p.getByRole('heading',{name:'批量整理进度'}).waitFor();
 const group=f.writes.find(w=>w.path==='/api/groups').body;
 assert.equal(f.writes.filter(w=>w.path==='/api/upload').length,2);assert.equal(group.sourceIds.length,2);assert.equal(f.notes.find(n=>n.id===group.sourceIds[0]).sourceName,'课堂-2.m4a');
 assert.equal(f.writes.find(w=>w.path==='/api/batches').body.options.autoSummarize,true);
 checks.push('Multiple imports ordered, one group, note-only batch submission');
 await p.locator('#workflow-dialog').getByRole('button',{name:'关闭',exact:true}).click();
 await p.locator('#open-course-table').click();await p.locator('#calendar-week').fill('2026-09-07');await p.locator('#calendar-week').dispatchEvent('change');
 await p.locator('.course-cell').waitFor();assert.equal(await p.locator('.course-cell').count(),1);
 assert.match(await p.locator('.course-cell').locator('..').innerText(),/周五/);
 await p.screenshot({path:path.join(out,'desktop-calendar.png')});
 await p.locator('.course-cell').click();assert.match(await p.locator('.workflow-content').innerText(),/本周控制理论/);assert.doesNotMatch(await p.locator('.workflow-content').innerText(),/上周控制理论/);
 checks.push('Moved occurrence displayed once on actual day; notes isolated by original class date');
 await p.getByRole('button',{name:'返回课表',exact:true}).click();await p.getByRole('button',{name:'导入 / 编辑课表'}).click();
 await p.locator('#schedule-file').setInputFiles({name:'课程表.pdf',mimeType:'application/pdf',buffer:Buffer.from('%PDF fixture')});
 await p.locator('#schedule-recognize').click();await p.getByLabel('课程名称',{exact:true}).fill('机器人控制 修正');
 await p.locator('#schedule-save').click();await p.locator('#calendar-week').waitFor();
 assert.equal(f.writes.findLast(w=>w.path==='/api/timetable').body.schedule.courses[0].name,'机器人控制 修正');
 checks.push('PDF import preview stays editable before explicit save');
 await p.locator('#workflow-dialog').getByRole('button',{name:'关闭',exact:true}).click();
 await p.locator('#open-note-merge').click();
 await p.locator('input[data-note-id="本周控制理论"]').check();await p.locator('input[data-note-id="补充笔记"]').check();
 await p.getByRole('button',{name:'选择顺序并拼接'}).click();await p.locator('.workflow-file').first().getByRole('button',{name:'下移'}).click();
 await p.getByRole('button',{name:'生成可编辑新稿'}).click();await p.locator('#summary-editor').waitFor({state:'visible'});
 assert.match(await p.locator('#summary-editor').inputValue(),/^## 补充笔记/);assert.equal(f.notes.filter(n=>['本周控制理论','补充笔记'].includes(n.id)).length,2);
 checks.push('Manual merge reorder opens editable new note and preserves originals');
 await p.locator('#cancel-summary-edit').click();
 await p.locator('#open-archive').click();await p.locator('#library-filter').selectOption('archived');
 await p.locator('input[data-note-id="归档录音"]').check();await p.locator('#delete-scope').selectOption('cache');
 await p.getByRole('button',{name:'处理所选录音'}).click();await p.getByRole('heading',{name:'清理结果'}).waitFor();
 assert.equal(f.writes.findLast(w=>w.path==='/api/audio/batch-delete').body.scope,'cache');checks.push('Cache batch acts without confirmation and scope remains cache');
 await f.ctx.close();
 for(const [name,width,height] of [['ipad',834,1112],['phone',390,844]]){
  const f=await fixture({width,height}),p=f.page;
  if((await p.locator('#open-course-table').boundingBox()).x<0)await p.locator('#sidebar-toggle').click();
  await p.locator('#open-course-table').click();await p.locator('#calendar-week').fill('2026-09-07');await p.locator('#calendar-week').dispatchEvent('change');await p.locator('.course-cell').waitFor();
  await p.screenshot({path:path.join(out,name+'-calendar.png')});
  assert.ok(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await p.getByRole('button',{name:'导入 / 编辑课表'}).click();await p.screenshot({path:path.join(out,name+'-editor.png')});
  assert.equal(await p.getByLabel('指定日期（YYYY-MM-DD，逗号分隔）',{exact:true}).inputValue(),'2026-09-20');
  assert.ok(await p.locator('#workflow-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth+1));await f.ctx.close();
 }
 checks.push('Desktop, iPad and phone layouts without horizontal overflow');
 assert.deepEqual(errors,[]);await fs.writeFile(path.join(out,'browser-results.json'),JSON.stringify({checks,errors},null,2));console.log(JSON.stringify({checks,errors},null,2));
}finally{await browser.close();}
