// File-first workflow fixtures: real browser/IndexedDB, no live server or provider calls.
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url);
const {chromium}=require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=process.cwd(), out=path.join(root,'docs/releases/2026-09-12-file-first/qa');
await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
const origin='http://127.0.0.1:19888', errors=[], checks=[];
const defaultSettings={asrConfigured:true,deepseekConfigured:true,tosConfigured:true,tosPrivateConfirmed:true,dailyBudgetYuan:10,failureOnlyNotifications:true};
async function fixture(viewport, localBrowser=true){
 const ctx=await browser.newContext({viewport,hasTouch:!localBrowser,serviceWorkers:'block',reducedMotion:'reduce'});
 let settings={...defaultSettings},notes=[], writes=[], payloads=[],failUpload=false, uploads=new Map(), automation={dailyBudgetYuan:10,usedYuan:1.25,remainingYuan:8.75,enabled:true,queuedCount:0,runningCount:0,blockedReason:'',message:'自动处理已开启，等待新录音。',inboxPath:'D:\\fixture\\inbox'};
 const schedule={schemaVersion:1,revision:1,courses:[],week1Start:'2026-09-07',periods:{},dayOverrides:{},exceptions:[]};
 const makeNote=(name,size)=>({id:'note-'+notes.length,title:name.replace(/\.[^.]+$/,''),sourceName:name,status:'idle',autoProcessState:'waiting',summary:'',transcript:'',asrComplete:false,createdAt:new Date().toISOString(),updatedAt:new Date().toISOString(),revision:1,language:'auto',template:'lecture',audioAvailable:true,fileSize:size});
 await ctx.route('**/*',async route=>{
  const r=route.request(), u=new URL(r.url());
  if(u.origin!==origin){errors.push('external '+u);return route.abort();}
  const json=(v,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(v)});
  if(u.pathname.startsWith('/api/')){
   if(r.method()!=='GET'){writes.push(u.pathname);payloads.push({path:u.pathname,headers:r.headers(),body:r.headers()['content-type']?.includes('json')?r.postDataJSON():null});}
   if(u.pathname==='/api/bootstrap')return json({token:'fixture',accountId:'file-first-'+localBrowser,localBrowser,notes,settings,capabilities:{}});
   if(u.pathname==='/api/automation'){if(r.method()==='POST')automation={...automation,...r.postDataJSON(),message:r.postDataJSON().enabled?'自动处理已开启，等待新录音。':'自动处理已暂停'};return json(automation);}
   if(u.pathname==='/api/settings'){settings={...settings,...r.postDataJSON()};automation.dailyBudgetYuan=settings.dailyBudgetYuan;automation.remainingYuan=settings.dailyBudgetYuan-automation.usedYuan;return json({settings});}
   if(u.pathname==='/api/notes')return json({notes});
   if(u.pathname==='/api/timetable')return json(schedule);
   if(u.pathname==='/api/timetable/week')return json({schedule,lessons:[]});
   if(u.pathname==='/api/upload'){const name=decodeURIComponent(r.headers()['x-file-name']);const n=makeNote(name,r.postDataBuffer().length);notes.push(n);return json(n,201);}
   if(u.pathname==='/api/sync/uploads'){
    if(failUpload)return json({error:'测试连接中断：文件保留，可重试'},400);
    const d=r.postDataJSON(), id='upload-'+uploads.size; uploads.set(id,d);return json({uploadId:id,chunkSize:65536,parts:[]});
   }
   if(/^\/api\/sync\/uploads\/[^/]+\/\d+$/.test(u.pathname))return json({ok:true});
   if(/^\/api\/sync\/uploads\/[^/]+\/complete$/.test(u.pathname)){const d=uploads.get(u.pathname.split('/')[4]);const n=makeNote(d.name,d.size);notes.push(n);return json({note:n});}
   if(u.pathname==='/api/sync/push'){const d=r.postDataJSON();const n=notes.find(n=>n.id===d.note.id);return json({note:n});}
   const processing=notes.find(n=>u.pathname==='/api/notes/'+n.id+'/transcribe'||u.pathname==='/api/notes/'+n.id+'/summarize');
   if(processing){processing.status=u.pathname.endsWith('/transcribe')?'transcribing':'summarizing';return json(processing);}
   const n=notes.find(n=>u.pathname==='/api/notes/'+n.id);if(n)return json(n);
   errors.push('unexpected API '+u.pathname);return json({error:'fixture missing'},404);
  }
  const f=path.join(root,'web',u.pathname==='/'?'index.html':u.pathname.slice(1));
  try{return route.fulfill({contentType:({'.html':'text/html','.js':'text/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.webmanifest':'application/manifest+json'})[path.extname(f)]||'text/plain',body:await fs.readFile(f)});}catch{return route.fulfill({status:404,body:''});}
 });
 const page=await ctx.newPage();page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin);await page.getByText('自动处理已开启，等待新录音。',{exact:true}).first().waitFor();
 return {ctx,page,writes,payloads,notes,setFail:value=>{failUpload=value;}};
}
const file=(name='课堂.m4a')=>({name,mimeType:'audio/mp4',buffer:Buffer.from('fixture-audio-'+name)});
try{
 const d=await fixture({width:1365,height:960}),p=d.page;
 assert.equal(await p.locator('#record-button,#record-dialog,#cloud-upload-dialog,#hotwords,#new-language,#new-template,#note-template,#open-sync').count(),0);
 assert.equal(await p.locator('.sidebar-bottom #open-course-table,.sidebar-bottom #open-note-merge').count(),0);
 assert.equal(await p.locator('#upload-button').isEnabled(),true);
 await p.screenshot({path:path.join(out,'desktop-import-home.png'),fullPage:true});
 await p.locator('#connection-label').click();await p.locator('#sync-dialog').waitFor({state:'visible'});await p.locator('#sync-dialog .dialog-close').click();
 await p.locator('#open-settings').click();
 assert.equal(await p.locator('#daily-budget-yuan').inputValue(),'10');assert.equal(await p.locator('#failure-only-notifications').isChecked(),true);
 assert.match(await p.locator('#budget-usage').innerText(),/已预留 ¥1.25.*剩余 ¥8.75/);
 await p.locator('#daily-budget-yuan').fill('12.5');await p.locator('#failure-only-notifications').uncheck();await p.locator('#save-settings').click();await p.locator('#settings-dialog').waitFor({state:'hidden'});
 assert.equal(d.payloads.find(x=>x.path==='/api/settings').body.dailyBudgetYuan,12.5);assert.equal(d.payloads.find(x=>x.path==='/api/settings').body.failureOnlyNotifications,false);
 await p.locator('#open-settings').click();assert.equal(await p.locator('#daily-budget-yuan').inputValue(),'12.5');assert.equal(await p.locator('#failure-only-notifications').isChecked(),false);
 await p.screenshot({path:path.join(out,'desktop-budget-settings.png'),fullPage:true});
 await p.locator('#automation-enabled').uncheck();await p.locator('#automation-settings-status').filter({hasText:'自动处理已暂停'}).waitFor();
 await p.locator('#automation-enabled').check();await p.locator('#open-course-table').click();await p.locator('#workflow-dialog').waitFor({state:'visible'});assert.equal(await p.locator('#settings-dialog').isVisible(),false);
 await p.locator('#workflow-dialog').getByRole('button',{name:'关闭',exact:true}).click();
 await p.locator('.history-filter summary').click();assert.equal(await p.locator('#open-note-merge').isVisible(),true);await p.locator('.history-filter summary').click();
 await p.locator('#file-input').setInputFiles([file('课堂-1.m4a'),file('课堂-2.m4a')]);
 await p.getByRole('heading',{name:'录音已接收',exact:true}).waitFor();
 assert.equal(d.writes.filter(x=>x==='/api/upload').length,2);
 assert.ok(d.payloads.filter(x=>x.path==='/api/upload').every(x=>x.headers['x-language']==='auto'&&x.headers['x-template']==='lecture'));
 assert.equal(d.writes.filter(x=>x.includes('transcribe')||x==='/api/groups'||x==='/api/batches').length,0);
 assert.equal(await p.getByRole('button',{name:'完成',exact:true}).isEnabled(),true);
 await p.screenshot({path:path.join(out,'desktop-auto-import.png'),fullPage:true});
 checks.push('顶部连接状态打开上传下载；无语言与类型选择，导入固定 auto/lecture；每日额度与提醒设置保存后恢复，显示保守预留额');
 await p.locator('#workflow-dialog').getByRole('button',{name:'完成',exact:true}).click();
 await p.goto(origin+'/?note='+d.notes[0].id);
 await p.locator('#summary-empty-description').filter({hasText:'已进入自动处理流程'}).waitFor();
 assert.equal(await p.locator('#primary-process').isVisible(),false);
 const base={...d.notes[0],status:'idle',autoProcessState:undefined};
 d.notes.push({...base,id:'legacy-audio',title:'旧录音尚未排队'}, {...base,id:'legacy-text',title:'原文尚未整理',sourceName:'',audioAvailable:false,transcript:'闭环控制利用反馈减小误差。',asrComplete:true});
 for (const [id,action] of [['legacy-audio','transcribe'],['legacy-text','summarize']]) {
  await p.goto(origin+'/?note='+id);
  await p.locator('#primary-process').waitFor({state:'visible'});
  assert.equal(await p.locator('#primary-process').isEnabled(),true);
  assert.doesNotMatch(await p.locator('#summary-empty-description').textContent(),/已进入自动处理流程/);
  await p.locator('#primary-process').click();
  await p.waitForFunction(()=>!document.querySelector('#note-status').hidden);
  assert.ok(d.writes.includes('/api/notes/'+id+'/'+action));
 }
 checks.push('仅持久自动等待状态隐藏处理入口；旧 idle 录音和原文可手动开始相应处理');
 await d.ctx.close();
 const t=await fixture({width:820,height:1180},false),q=t.page;
 await q.locator('#file-input').setInputFiles(file());
 await q.getByRole('heading',{name:'录音已接收',exact:true}).waitFor();
 await q.waitForFunction(()=>document.querySelector('#sync-progress').textContent.includes('电脑已收到'));
 assert.equal(t.writes.filter(x=>x==='/api/sync/push').length,1);
 assert.equal(await q.locator('#toast').isVisible(),false);
 assert.equal(t.payloads.find(x=>x.path==='/api/sync/push').body.note.language,'auto');assert.equal(t.payloads.find(x=>x.path==='/api/sync/push').body.note.template,'lecture');
 await q.locator('#workflow-dialog').getByRole('button',{name:'完成',exact:true}).click();
 await q.locator('#connection-label').click();
 await q.getByText('全部已上传。电脑会自动转录并整理，可关闭此窗口。',{exact:true}).waitFor();
 assert.equal(await q.locator('#push-pending-notes').isEnabled(),true);
 await q.getByText('下载电脑笔记',{exact:true}).click();await q.locator('#computer-sync-list input').uncheck();await q.locator('#download-selected-notes').click();assert.match(await q.locator('#toast').innerText(),/请先勾选/);
 await q.screenshot({path:path.join(out,'ipad-upload-complete.png'),fullPage:true});
 checks.push('iPad 自动分块上传固定 auto/lecture；默认成功不弹提醒，行内有完成状态，下载未选择有反馈');
 await q.locator('#sync-dialog .dialog-close').click();
 t.setFail(true);await q.locator('#file-input').setInputFiles(file('失败可重试.m4a'));
 await q.waitForFunction(()=>document.querySelector('#sync-error').textContent.includes('测试连接中断'));
 await q.locator('#workflow-dialog').getByRole('button',{name:'完成',exact:true}).click();
 await q.locator('#connection-label').click();
 assert.equal(await q.locator('#push-pending-notes').isEnabled(),true);assert.match(await q.locator('#pending-sync-description').innerText(),/等待自动上传/);
 t.setFail(false);await q.locator('#push-pending-notes').click();
 await q.getByText('全部已上传。电脑会自动转录并整理，可关闭此窗口。',{exact:true}).waitFor();
 assert.equal(t.writes.filter(x=>x==='/api/sync/push').length,2);
 assert.equal(await q.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await q.screenshot({path:path.join(out,'ipad-retry-complete.png'),fullPage:true});
 checks.push('上传失败保留本机录音，按钮可重试并成功完成；iPad 无横向溢出');
 await t.ctx.close();
 assert.deepEqual(errors,[]);await fs.writeFile(path.join(out,'file-first-checks.json'),JSON.stringify({checks,errors},null,2));console.log(JSON.stringify({checks,errors},null,2));
}finally{await browser.close();}


