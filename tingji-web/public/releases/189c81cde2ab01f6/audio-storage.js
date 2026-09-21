const node=(tag,cls='',text='')=>{const el=document.createElement(tag);el.className=cls;el.textContent=text;return el;};
const size=n=>n>=1024**3?`${(n/1024**3).toFixed(2)} GB`:`${(n/1024**2).toFixed(1)} MB`;
export function createAudioStorage({api,onComplete=()=>{}}){
  let dialog=null,busy=false,plan=null,ids=[],epoch=0;
  function reset(){epoch++;dialog?.close();dialog?.remove();dialog=null;busy=false;plan=null;ids=[];}
  function error(text){if(dialog){const el=dialog.querySelector('.storage-error');el.textContent=text||'';el.hidden=!text;}}
  function lock(value){busy=value;if(dialog)dialog.querySelectorAll('button,select').forEach(e=>e.disabled=value);}
  function render(){
    if(!dialog)return;
    const out=dialog.querySelector('.storage-preview'),confirm=dialog.querySelector('.storage-confirm');out.replaceChildren();confirm.hidden=!plan?.targets.length;
    if(!plan)return;
    const pending=plan.targets.filter(t=>t.state!=='removed'),done=plan.targets.length-pending.length;
    out.append(node('p','storage-total',`${plan.scopeLabel} · ${plan.noteCount} 份笔记 · ${plan.targets.length} 个文件 · ${size(plan.targets.reduce((v,t)=>v+Math.max(0,t.size),0))}`));
    if(done)out.append(node('p','storage-result',`${done} 个文件已删除，${pending.length} 个尚未完成。`));
    const list=node('div','storage-targets');
    for(const t of plan.targets){const item=node('div','storage-target'),name=node('strong','',t.filename),detail=node('details');detail.append(node('summary','',`${t.title} · ${size(t.size)}${t.state==='removed'?' · 已删除':''}`),node('code','',t.location));if(t.versionId)detail.append(node('p','small muted',`版本：${t.versionId}`));item.append(name,detail);if(t.message&&t.state!=='removed')item.append(node('p','form-error',t.message));list.append(item);}out.append(list);
    if(plan.skipped?.length){const skipped=node('details','storage-skipped');skipped.append(node('summary','',`${plan.skipped.length} 项已保留`));for(const t of plan.skipped)skipped.append(node('p','muted',`${t.filename||t.title}：${t.message}`));out.append(skipped);}
    if(pending.length){out.append(node('p','storage-confirm-copy',`确认后将删除上面列出的${plan.scopeLabel}，删除后无法从听记恢复。`));confirm.textContent=done||plan.targets.some(t=>t.state==='failed')?'重试未完成项':`确认删除这 ${pending.length} 个文件`;}else confirm.hidden=true;
  }
  async function preview(){
    const scope=dialog.querySelector('select').value;if(!scope){error('先选择一个清理位置。');return;}lock(true);error('');const token=epoch;
    try{const data=await api('/audio-storage/preview',{method:'POST',body:{scope,noteIds:ids}});if(token!==epoch)return;plan=data;render();}
    catch(e){error(e.message||'清理范围没有读取成功。');}finally{lock(false);}
  }
  async function execute(){
    if(busy||!plan)return;lock(true);error('');
    try{for(const target of plan.targets.filter(t=>t.state!=='removed')){dialog.querySelector('.storage-progress').textContent=`正在清理 ${target.filename}…`;plan=await api(`/audio-storage/${plan.planId}/execute`,{method:'POST',body:{targetId:target.id,confirm:true}});render();}await onComplete();}
    catch(e){error(e.message||'部分结果未确认，请保留窗口后重试。');}
    finally{if(dialog)dialog.querySelector('.storage-progress').textContent='';lock(false);}
  }
  async function open(noteIds){
    reset();ids=[...noteIds];dialog=node('dialog','library-dialog storage-dialog');dialog.setAttribute('aria-labelledby','storage-title');
    const head=node('div','dialog-heading'),title=node('h2','','清理录音');title.id='storage-title';const close=node('button','icon-button','×');close.type='button';close.setAttribute('aria-label','关闭清理录音');close.onclick=()=>{if(!busy)reset();};head.append(title,close);
    const copy=node('p','storage-copy','只清理所选位置的音频。转写原文、整理稿和历史版本会保留。'),label=node('label','','清理位置'),select=node('select');select.setAttribute('aria-label','清理位置');select.add(new Option('选择位置',''));select.onchange=()=>{plan=null;error('');render();};label.append(select);
    const inspect=node('button','secondary','查看具体文件');inspect.type='button';inspect.onclick=preview;
    const out=node('div','storage-preview'),progress=node('p','storage-progress muted'),confirm=node('button','primary danger storage-confirm','确认删除');confirm.type='button';confirm.hidden=true;confirm.onclick=execute;
    const message=node('p','storage-error form-error');message.hidden=true;message.setAttribute('role','alert');progress.setAttribute('role','status');dialog.append(head,copy,label,inspect,out,progress,confirm,message);document.body.append(dialog);dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();else reset();});dialog.showModal();const token=epoch;
    try{const data=await api('/audio-storage/scopes');if(token!==epoch)return;for(const s of data.scopes)select.add(new Option(s.label,s.id));}catch(e){error(e.message);}
  }
  return {open,reset};
}
