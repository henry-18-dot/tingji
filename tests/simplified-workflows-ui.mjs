import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Henry_1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const out = path.join(process.cwd(), 'docs/releases/2026-09-12-simplified-workflow/qa');
await fs.mkdir(out, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
const errors = [], results = [];
async function fixture(width, height) {
  const ctx = await browser.newContext({ viewport: { width, height }, hasTouch: width < 1100, reducedMotion: 'reduce' });
  await ctx.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: `<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/styles.css"><link rel="stylesheet" href="/workflows.css"></head><body><div><span id="note-source"></span></div><button id="open-course-table">课表</button><button id="open-note-merge">合并</button><script type="module">
      import {installWorkflows} from '/workflows.js';
      const stamp='2026-09-12T12:00:00Z';
      window.writes=[];window.uploads=[];window.failNext=true;window.synced=0;
      const notes=[{id:'note1',title:'机器人监控与控制',summary:'笔记',createdAt:stamp},{id:'note2',title:'课后补充',summary:'补充',classDate:'2026-09-09',createdAt:stamp},{id:'audio1',title:'课堂原始录音',sourceName:'课堂.m4a',audioStored:true,audioAvailable:true,status:'ready',asrComplete:true}];
      window.state={notes,connected:true,localBrowser:false,settings:{asrConfigured:true,deepseekConfigured:true,tosConfigured:true},syncClient:{deleteLocalAudio:async id=>{writes.push({path:'device-delete',id});notes.find(n=>n.id===id).audioStored=false;}}};
      const schedule={courses:[{id:'robot',name:'机器人',lessons:[]}],revision:1};
      const lesson={lessonId:'robot-weekly',courseId:'robot',courseName:'机器人',classDate:'2026-09-10',date:'2026-09-11',periods:[1,2]};
      const request=async (path,args={})=>{
        if(args.method==='POST')writes.push({path,body:args.body});
        if(path.startsWith('/api/timetable/week'))return {lessons:[lesson]};
        if(path==='/api/timetable')return schedule;
        if(path.endsWith('/lesson')){const n=notes.find(n=>n.id===path.split('/')[3]);Object.assign(n,{courseId:'',lessonId:'',courseName:''},args.body.lesson);return n;}
        if(path==='/api/audio/batch-delete')return {items:args.body.noteIds.map(noteId=>({noteId,state:'completed'}))};
        if(path==='/api/audio/batch-archive')return {items:args.body.noteIds.map(noteId=>({noteId,state:'completed'}))};
        if(path==='/api/batches')return {id:'batch',state:'complete',items:[{noteId:'group',title:'机器人四段',state:'completed'}]};
        throw Error('Unexpected '+path);
      };
      window.api=installWorkflows({state,request,refresh:async()=>{},accept:async n=>n,closeSidebar:()=>{},toast:(text)=>{window.lastToast=text},openSettings:()=>{},openNote:async id=>{window.opened=id},uploadFile:async f=>{uploads.push(f.name);if(f.name==='坏文件.m4a'&&failNext){failNext=false;throw Error('网络连接断开，请重试');}const n={id:'import-'+f.name,sourceName:f.name,syncState:'local',audioStored:true};notes.push(n);return n;},ensureRemote:async n=>n,syncPendingNotes:async()=>{synced++},startEditor:()=>{}});
      window.ready=true;
    </script></body></html>` });
    try { return route.fulfill({ contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: await fs.readFile(path.join(process.cwd(), 'web', url.pathname.slice(1))) }); } catch { return route.fulfill({ status: 404, body: '' }); }
  });
  const page = await ctx.newPage(); page.on('pageerror', e => { errors.push(e.message); console.error(e.message); }); await page.goto('http://127.0.0.1:19886'); await page.waitForFunction(() => window.ready); return {ctx,page};
}
try {
  for (const [name,width,height] of [['desktop',1365,960],['ipad',834,1112],['phone',390,844]]) {
    const {ctx,page:p}=await fixture(width,height);
    await p.evaluate(()=>api.openCalendar());
    await p.locator('#calendar-month').fill('2026-09'); await p.locator('#calendar-month').dispatchEvent('change');
    await p.locator('[data-calendar-date="2026-09-12"]').waitFor();
    assert.equal(await p.locator('.calendar-day').count(),42);
    await p.locator('[data-calendar-note="note1"]').click();
    await p.getByRole('button',{name:'2026-09-12，放入笔记',exact:true}).click();
    await p.waitForFunction(()=>state.notes[0].classDate==='2026-09-12');
    assert.equal(await p.locator('[data-calendar-date="2026-09-12"] [data-calendar-note="note1"]').count(),1);
    if(name==='desktop') {
      await p.locator('[data-calendar-note="note1"]').dragTo(p.locator('[data-calendar-date="2026-09-13"]'));
      await p.waitForFunction(()=>state.notes[0].classDate==='2026-09-13');
    }
    await p.screenshot({path:path.join(out,name+'-calendar.png')});
    assert.ok(await p.locator('#workflow-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth+1));
    await p.evaluate(()=>api.openLibrary());
    await p.getByRole('button',{name:'删除所选录音',exact:true}).click();
    assert.match(await p.locator('#workflow-dialog .field-error').innerText(),/请先选择/);
    assert.equal(await p.getByRole('button',{name:'整理所选录音',exact:true}).count(),0);
    await p.locator('input[data-note-id="audio1"]').check();
    await p.locator('#delete-scope').selectOption('device');
    await p.getByRole('button',{name:'删除所选录音',exact:true}).click();
    await p.getByRole('heading',{name:'清理结果'}).waitFor();
    assert.equal(await p.evaluate(()=>writes.filter(w=>w.path==='device-delete').length),1);
    if(name==='ipad') {
      await p.evaluate(()=>api.openImport([new File(['a'],'第一段.m4a'),new File(['b'],'坏文件.m4a')]));
      assert.equal(await p.evaluate(()=>uploads.length),2);
      await p.getByRole('button',{name:'重试保存失败项'}).click();
      await p.getByRole('heading',{name:'录音已接收'}).waitFor();
      assert.equal(await p.evaluate(()=>uploads.filter(n=>n==='第一段.m4a').length),1);
      assert.equal(await p.evaluate(()=>uploads.filter(n=>n==='坏文件.m4a').length),2);
      assert.equal(await p.locator('#workflow-dialog button:enabled').count(),2);
      await p.screenshot({path:path.join(out,'ipad-import.png')});
      await p.locator('#workflow-dialog').getByRole('button',{name:'关闭',exact:true}).click();
      await p.evaluate(()=>api.renderSources({id:'group',status:'error',sourceNoteIds:['part1','part2'],sources:[{noteId:'part1',title:'第一段',asrComplete:true,state:'completed'},{noteId:'part2',title:'第二段',state:'failed',error:'缺少云端上传授权',audioAvailable:true}]}));
      assert.match(await p.locator('#group-sources').innerText(),/缺少云端上传授权/);
      assert.equal(await p.getByRole('button',{name:'继续未完成部分',exact:true}).count(),1);
      await p.getByRole('button',{name:'继续未完成部分',exact:true}).click();
      await p.waitForFunction(()=>writes.some(w=>w.path==='/api/batches'));
      const recovery = await p.evaluate(()=>writes.find(w=>w.path==='/api/batches').body.options);
      assert.equal(recovery.cloudUploadConsent,true);
      assert.equal(recovery.audioUploadConsent,true);
    }
    results.push(name+': calendar placement, no overflow, management-only library, scoped deletion'); await ctx.close();
  }
  assert.deepEqual(errors,[]); await fs.writeFile(path.join(out,'workflow-browser-results.json'),JSON.stringify({results,errors},null,2)); console.log(JSON.stringify({results,errors},null,2));
}finally{await browser.close();}
