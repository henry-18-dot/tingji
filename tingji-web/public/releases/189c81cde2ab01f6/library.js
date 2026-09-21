import {createAudioStorage} from './audio-storage.js';
const node=(tag,cls='',text='')=>{const e=document.createElement(tag);e.className=cls;e.textContent=text;return e;};
const button=(text,fn,cls='secondary')=>{const e=node('button',cls,text);e.type='button';e.onclick=fn;return e;};
const status={uploading:'待上传',uploaded:'录音已保存',transcribed:'有原文',queued:'等待处理',preparing:'准备录音',transcribing:'转写中',summarizing:'整理中',ready:'已整理',error:'需要处理',uncertain:'结果待核对'};

export function createLibrary({api,toast,onComplete=()=>{},onOpenNote=()=>{},getCourses=()=>[]}){
  let dialog=null,view='active',rows=[],selected=new Set(),busy=false,epoch=0,loading=0,pending=null;
  const audioStorage=createAudioStorage({api,onComplete:async()=>{await onComplete();if(dialog)await load();}});
  function reset(){epoch++;audioStorage.reset();dialog?.close();dialog?.remove();dialog=null;rows=[];selected.clear();pending=null;busy=false;}
  function showError(message){const e=dialog?.querySelector('.library-error');if(e){e.textContent=message;e.hidden=!message;}}
  async function load(){const token=epoch,version=++loading;showError('');try{const data=await api(`/library?view=${view}`);if(token!==epoch||version!==loading||!dialog)return;rows=data.notes||[];selected.clear();drawRows();}catch(e){showError(e.message||'列表没有读取成功。');}}
  function actions(){
    const bar=dialog.querySelector('.library-actions');bar.replaceChildren();
    const chosen=rows.filter(n=>selected.has(n.id));
    const add=(label,action,cls='secondary')=>{const b=button(label,()=>run(action),cls);b.disabled=busy||!chosen.length;bar.append(b);};
    if(view==='trash')add('恢复笔记','restore','primary');
    else {add(view==='archived'?'取消归档':'归档',view==='archived'?'unarchive':'archive');add('移入回收站','trash');if(chosen.some(n=>n.hasTranscript))add('重新整理','summarize','primary');if(chosen.some(n=>n.hasAudio&&!n.hasTranscript||['error','uncertain','uploaded'].includes(n.status)))add(chosen.every(n=>n.hasAudio&&!n.hasTranscript&&n.status!=='uncertain')?'转录并整理':'继续原任务','resume');}
    const cleanup=button('清理录音',()=>audioStorage.open([...selected]),'text-button');cleanup.disabled=busy||!chosen.length;bar.append(cleanup);
    dialog.querySelector('.library-selection').textContent=selected.size?`已选 ${selected.size} 份`:`${rows.length} 份`;
    const all=dialog.querySelector('.library-select-all');all.checked=rows.length>0&&selected.size===rows.length;all.indeterminate=selected.size>0&&selected.size<rows.length;
  }
  function drawRows(){
    const list=dialog.querySelector('.library-list');list.replaceChildren();
    if(!rows.length)list.append(node('p','library-empty muted',view==='trash'?'回收站是空的。':view==='archived'?'还没有归档笔记。':'还没有录音笔记。'));
    for(const n of rows){
      const row=node('div','library-row'),check=node('input');check.type='checkbox';check.checked=selected.has(n.id);check.setAttribute('aria-label',`选择 ${n.title}`);check.onchange=()=>{check.checked?selected.add(n.id):selected.delete(n.id);pending=null;actions();};
      const content=node('div','library-row-content'),title=button(n.title,()=>{if(view==='trash')return;reset();onOpenNote(n.id);},'library-title');title.disabled=view==='trash';
      const course=getCourses().find(c=>c.id===n.courseId)?.name,meta=[course,status[n.status]||n.stage].filter(Boolean).join(' · ');content.append(title,node('small','muted',meta));row.append(check,content);list.append(row);
    }
    actions();
  }
  async function run(action){
    if(busy||!selected.size)return;
    const ids=rows.filter(n=>selected.has(n.id)).map(n=>n.id),signature=JSON.stringify({action,ids});
    if(!pending||pending.signature!==signature)pending={signature,body:{requestId:crypto.randomUUID(),action,noteIds:ids}};
    busy=true;showError('');dialog.dataset.locked='true';dialog.querySelectorAll('button,input,select').forEach(e=>e.disabled=true);
    try{
      const result=await api('/library/actions',{method:'POST',body:pending.body});pending=null;
      const failed=(result.items||[]).filter(i=>!i.ok);await onComplete();await load();
      const box=dialog.querySelector('.library-results');box.replaceChildren();
      if(failed.length){box.append(node('p','',`${result.completed} 份${['summarize','resume'].includes(action)?'已安排':'已更新'}，${failed.length} 份需要处理。`));for(const item of failed)box.append(node('p','muted',`${item.title}：${item.message}`));}
      else toast(action==='summarize'||action==='resume'?`已安排 ${result.completed} 份，请稍后查看。`:`已更新 ${result.completed} 份笔记。`);
    }catch(e){showError(e.message||'操作没有完成，保留当前选择后可重试。');}
    finally{busy=false;if(dialog){delete dialog.dataset.locked;dialog.querySelectorAll('button,input,select').forEach(e=>e.disabled=false);dialog.querySelectorAll('.library-title').forEach(e=>e.disabled=view==='trash');actions();}}
  }
  async function open(){
    if(dialog?.open)return;
    reset();view='active';dialog=node('dialog','library-dialog');dialog.setAttribute('aria-labelledby','library-title');
    const heading=node('div','dialog-heading'),title=node('h2','','（重新）整理笔记');title.id='library-title';const close=button('×',()=>{if(!busy)reset();},'icon-button');close.setAttribute('aria-label','关闭');heading.append(title,close);
    const toolbar=node('div','library-toolbar'),filter=node('select');filter.setAttribute('aria-label','笔记位置');for(const [value,label] of [['active','我的笔记'],['archived','已归档'],['trash','回收站']])filter.add(new Option(label,value));filter.onchange=()=>{view=filter.value;pending=null;dialog.querySelector('.library-results').replaceChildren();load();};
    const label=node('label','library-select-label'),all=node('input','library-select-all');all.type='checkbox';all.onchange=()=>{selected=all.checked?new Set(rows.map(n=>n.id)):new Set();pending=null;drawRows();};label.append(all,document.createTextNode('全选'));toolbar.append(filter,label,node('span','library-selection muted'));
    const list=node('div','library-list'),bar=node('div','library-actions'),results=node('div','library-results'),error=node('p','library-error form-error');error.hidden=true;error.setAttribute('role','alert');results.setAttribute('role','status');
    dialog.append(heading,toolbar,list,bar,results,error);document.body.append(dialog);dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();else reset();});dialog.showModal();await load();
  }
  return {open,reset};
}
