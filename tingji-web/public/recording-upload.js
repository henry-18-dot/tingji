import {uploadDrafts,fileMetadata,matchesOriginalFiles} from './upload-drafts.js';

export function createRecordingUpload({api,toast,getState,onComplete}) {
  const $=s=>document.querySelector(s),dialog=$('#upload-dialog'),form=$('#upload-form');
  let files=[],requestId='',batch=null,done=new Set(),busy=false,requestBody=null;
  let draft=null,ownerId='',loading=false,epoch=0,activeXhr=null,storageWarned=false,persistenceAvailable=true;
  const newId=()=>globalThis.crypto?.randomUUID?.()||`upload-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const day=()=>{const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;};
  const resume=document.createElement('div'),resumeText=document.createElement('p'),discard=document.createElement('button');
  resume.id='upload-resume';resume.hidden=true;resumeText.id='upload-resume-text';resumeText.className='small';resumeText.setAttribute('role','status');
  discard.id='upload-draft-discard';discard.type='button';discard.className='text-button';discard.textContent='开始新批次';
  const discardHint=document.createElement('p');discardHint.className='small muted';discardHint.textContent='开始新批次只清除本机续传记录，服务器已有文件仍保留。';
  resume.append(resumeText,discard,discardHint);$('#file-drop').before(resume);
  function error(message){const target=$('#upload-error');target.textContent=message;target.hidden=!message;}
  function storageWarning(){persistenceAvailable=false;if(!storageWarned){storageWarned=true;toast('浏览器未能保存续传记录，请保持页面打开完成上传。');}}
  function controls(){form.querySelectorAll('input,select,textarea,#upload-lesson').forEach(x=>x.disabled=busy||loading||!!requestBody&&x.id!=='audio-file');discard.disabled=busy||loading;$('#upload-submit').disabled=busy||loading;}
  function draw(){
    const list=$('#upload-file-list');list.replaceChildren();const displayed=files.length?files:(draft?.files||[]);
    displayed.forEach((file,index)=>{const row=document.createElement('li'),name=document.createElement('span');name.textContent=`${index+1}. ${file.name} · ${(file.size/1024**2).toFixed(1)} MB${done.has(index)?' · 已上传':''}`;row.append(name);
      for(const [label,delta] of [['上移',-1],['下移',1]]){const b=document.createElement('button');b.type='button';b.className='text-button';b.textContent=label;b.setAttribute('aria-label',`${file.name}${label}`);b.disabled=busy||loading||!!requestBody||!files.length||index+delta<0||index+delta>=files.length;b.onclick=()=>{[files[index],files[index+delta]]=[files[index+delta],files[index]];draw();};row.append(b);}list.append(row);});
    $('#file-label').textContent=loading?'正在检查上次上传…':files.length?`已选 ${files.length} 段录音`:draft?'重新选择这组原录音':'点击选择或拖入录音';
    $('#upload-group-row').hidden=displayed.length<2;$('#upload-order-hint').hidden=displayed.length<2;resume.hidden=!draft;
    if(draft)resumeText.textContent=files.length?`原文件已核对，继续上传剩余 ${files.length-done.size} 段。`:`发现未完成的上传：${draft.files.length} 段，其中 ${done.size} 段已上传。请按下方顺序重新选择全部原文件后继续。`;
    controls();
  }
  function reset(){epoch++;activeXhr?.abort();activeXhr=null;busy=loading=false;getState().uploading=false;files=[];requestId='';batch=null;requestBody=null;draft=null;done.clear();dialog.classList.remove('is-uploading');draw();}
  function restore(value){draft=value;requestBody=value.requestBody;requestId=requestBody.requestId;done=new Set(value.done);$('#recording-date').value=requestBody.recordingDate;$('#upload-group').value=requestBody.mode;$('#upload-title').value=requestBody.title;$('#upload-prompt').value=requestBody.prompt||'';}
  async function open(){
    const user=getState().user;if(!user?.verified){toast('请先完成学校邮箱验证。');return;}if(busy)return;
    form.reset();reset();ownerId=user.id;persistenceAvailable=true;storageWarned=false;$('#recording-date').value='';$('#upload-progress').hidden=true;error('');dialog.showModal();const token=epoch;loading=true;draw();
    try{const value=await uploadDrafts.load(ownerId);if(token===epoch&&getState().user?.id===ownerId&&value)restore(value);}catch{if(token===epoch)storageWarning();}finally{if(token===epoch){loading=false;draw();}}
  }
  function pick(input){
    if(busy||loading)return;const chosen=Array.from(input);if(!chosen.length)return;error('');
    if(requestBody){if(!matchesOriginalFiles(chosen,draft.files)){files=[];$('#audio-file').value='';draw();error('文件与未完成批次不一致。请按列表顺序选择原文件，或点“开始新批次”。');return;}files=chosen;draw();return;}
    files=chosen;ownerId=getState().user?.id||'';requestId=newId();batch=null;done.clear();draw();
  }
  function current(token,userId){return token===epoch&&getState().user?.id===userId;}
  async function persist(token,userId){if(!current(token,userId)||!requestBody)return;draft={userId,batchId:batch?.id||draft?.batchId||'',requestBody,files:draft?.files||fileMetadata(files),done:[...done]};if(!persistenceAvailable)return;try{await uploadDrafts.save(draft);}catch{if(current(token,userId))storageWarning();}}
  function put(upload,file,onProgress){return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();activeXhr=xhr;xhr.open(upload.method||'PUT',upload.url);Object.entries(upload.headers||{}).forEach(([k,v])=>xhr.setRequestHeader(k,v));xhr.timeout=45*60*1000;xhr.upload.onprogress=e=>{if(e.lengthComputable)onProgress(e.loaded/e.total);};xhr.onload=()=>xhr.status>=200&&xhr.status<300?resolve():reject(Error('上传未完成，请保留页面后重试。'));xhr.onerror=()=>reject(Error('连接中断，重试会继续上传剩余文件。'));xhr.ontimeout=()=>reject(Error('上传超时，请重试。'));xhr.onabort=()=>reject(Error('上传已停止。'));xhr.send(file);});}
  async function clear(userId=getState().user?.id||ownerId){if(!userId)return;if(userId===ownerId){reset();ownerId='';}try{await uploadDrafts.clear(userId);}catch{storageWarning();}}
  discard.onclick=async()=>{if(busy||loading)return;const userId=ownerId;loading=true;draw();await clear(userId);ownerId=getState().user?.id||'';form.reset();$('#recording-date').value='';$('#upload-progress').hidden=true;error('');draw();};
  async function upload(){
    if(busy||loading)return;const state=getState();if(!state.user?.verified)throw Error('请先完成学校邮箱验证。');
    if(!files.length)throw Error(draft?'请按列表顺序重新选择这组原录音。':'请先选择录音。');if(files.length>24)throw Error('一次最多选择 24 段录音。');
    if(!/^\d{4}-\d{2}-\d{2}$/.test($('#recording-date').value))throw Error('请选择上课日期。');
    for(const f of files){if(!/\.(mp3|m4a|wav|webm|ogg|flac|mp4|aac|opus|amr|wma)$/i.test(f.name))throw Error(`${f.name} 不是支持的音频文件。课件请到“上传课件”上传。`);if(!f.size)throw Error(`${f.name} 是空文件。`);if(f.size>(state.config.maxUploadBytes||1024**3))throw Error(`${f.name} 超过单个文件大小限制。`);}
    if(requestBody&&(!draft||!matchesOriginalFiles(files,draft.files)))throw Error('原录音信息发生变化，请重新选择。');
    const token=epoch,userId=state.user.id;ownerId=userId;const assertCurrent=()=>{if(!current(token,userId))throw Error('账号已变化，请重新打开上传窗口。');};
    busy=state.uploading=true;dialog.classList.add('is-uploading');$('#upload-progress').hidden=false;draw();const label=$('#upload-progress-text'),progress=$('#upload-progress progress');
    try{
      label.textContent=requestBody?'正在恢复上传任务…':'正在创建上传任务…';
      const body=requestBody||={requestId:requestId||=newId(),files:files.map(f=>({filename:f.name,size:f.size})),courseId:null,recordingDate:$('#recording-date').value,mode:$('#upload-group').value,title:$('#upload-title').value,prompt:$('#upload-prompt').value||undefined};
      // Persist the idempotency key before a possibly ambiguous create response.
      await persist(token,userId);assertCurrent();batch=await api('/recordings/batches',{method:'POST',body});assertCurrent();
      if(!Array.isArray(batch.files)||batch.files.length!==files.length)throw Error('服务器返回的录音数量不一致，请稍后重试。');await persist(token,userId);assertCurrent();
      for(let i=0;i<files.length;i++){
        // done exists only after a definite PUT 2xx; unknown PUTs are resent.
        if(done.has(i)||batch.files[i].confirmed||batch.files[i].present===true){done.add(i);await persist(token,userId);assertCurrent();continue;}
        done.delete(i);await persist(token,userId);assertCurrent();label.textContent=`正在上传第 ${i+1}/${files.length} 段 · ${files[i].name}`;
        await put(batch.files[i].upload,files[i],value=>{if(current(token,userId))progress.value=(i+value)/files.length*100;});activeXhr=null;assertCurrent();done.add(i);await persist(token,userId);assertCurrent();draw();
      }
      progress.value=100;label.textContent='正在确认全部录音…';const result=await api(`/recordings/batches/${batch.id}/uploaded`,{method:'POST',body:{}});assertCurrent();
      try{await uploadDrafts.clear(userId);}catch{storageWarning();}assertCurrent();dialog.close();reset();toast(state.config.localMode?'录音已保存，请保持电脑运行。':'录音已上传，可以关闭网页，稍后查看笔记。');await onComplete(result.notes);
    }catch(e){if(current(token,userId))label.textContent=`${done.size}/${files.length} 段已上传。再次点击可继续。`;throw e;}
    finally{if(current(token,userId)){busy=state.uploading=false;activeXhr=null;dialog.classList.remove('is-uploading');draw();}}
  }
  $('#audio-file').onchange=e=>pick(e.target.files);const drop=$('#file-drop');drop.ondragover=e=>{e.preventDefault();drop.classList.add('dragging');};drop.ondragleave=()=>drop.classList.remove('dragging');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('dragging');if(!busy&&!loading){pick(e.dataTransfer.files);try{if(files.length)$('#audio-file').files=e.dataTransfer.files;}catch{/* FileList assignment is optional. */}}};
  return {open,upload,reset,clear};
}
