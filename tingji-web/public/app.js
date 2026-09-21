import {noteTitle} from './lesson-picker.js';
import {createOffline} from './offline.js';
import {createLibrary} from './library.js';
import {createRecordingMerge} from './recording-merge.js';
import {createSlides} from './slides.js';
import {createRecordingUpload} from './recording-upload.js';
import {createLocalTransfer} from './local-transfer.js';
import {renderMarkdown} from './knowledge.js';
import {parseNoteImageResources, getNoteResource} from './note-image-registry.js';
import {renderHistory} from './history.js';
import {createSettings, greeting} from './settings.js';
import {createCalendar} from './calendar.js';

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const state = {user:null,csrf:'',config:{},notes:[],courses:[],settings:null,view:'notes',note:null,auth:'login',editingCourse:null,uploading:false,uploadNote:null,file:null,epoch:0};
const statusText = {uploaded:'录音已导入',transcribed:'原文已导入',uploading:'待上传',queued:'等待处理',preparing:'准备录音',transcribing:'正在转写',summarizing:'正在整理',ready:'已整理',error:'需要处理',uncertain:'结果待确认'};
const el = (tag,cls,text) => {const n=document.createElement(tag); if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const icon = name => {const s=document.createElementNS('http://www.w3.org/2000/svg','svg'),u=document.createElementNS(s.namespaceURI,'use');u.setAttribute('href',`#icon-${name}`);s.append(u);s.setAttribute('aria-hidden','true');return s;};
const money = v => `¥${Number(v||0).toFixed(2)}`;
const date = v => {const d=new Date(v);return Number.isNaN(d.getTime())?'':d.toLocaleDateString('zh-CN',{month:'long',day:'numeric'});};
const courseName = id => state.courses.find(c=>c.id===id)?.name || '未分类';
const busyStatus = s => ['queued','preparing','transcribing','summarizing'].includes(s);
let toastTimer,searchTimer,searchIds=null,refreshing=false;
function userMessage(value){
  const message=String(value?.message??value??'');
  if(!message)return '';
  if(/null is not an object|cannot (?:read|set) propert|is not defined|unexpected token|syntaxerror|typeerror|referenceerror/i.test(message))return '页面加载失败，请刷新。';
  if(!/[\u3400-\u9fff]/.test(message)||message.length>240)return '操作未完成，请稍后重试。';
  return message;
}
function toast(text){$('#notice').textContent=userMessage(text);$('#notice').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#notice').hidden=true,6000);}
function errorAt(id,err){const node=$(id);node.textContent=userMessage(err);node.hidden=!node.textContent;}
function apiErrorMessage(status,value){
  if(status===422)return '请检查填写内容。';
  if(status===503&&typeof value?.detail==='string'&&value.detail.trim())return value.detail;
  if(status>=500)return '服务暂时无法连接，请稍后再试。';
  for(const key of ['detail','error','message']){const text=value?.[key];if(typeof text==='string'&&text.trim())return text;}
  if(['detail','error','message'].some(key=>value?.[key]!==undefined))return '服务暂时无法连接，请稍后再试。';
  return '操作没有完成，请稍后再试。';
}
function cleanPrivateState({clearOffline=true}={}){const oldUserId=state.user?.id;if(oldUserId)recordingUpload.clear(oldUserId);if(clearOffline)offline.clear();setOfflineMode(false);state.epoch++;searchIds=null;clearTimeout(searchTimer);state.user=null;state.notes=[];state.courses=[];state.note=null;$('#note-audio').pause();$('#note-audio').removeAttribute('src');state.uploadNote=null;state.file=null;recordingUpload.reset();$('#notes-list').replaceChildren();$('#summary-content').replaceChildren();$('#transcript-content').textContent='';$('#generation-prompt').textContent='';renderHistory(null,$('#note-history'));state.settings=null;settingsUI.reset();calendar.reset();slidesUI.reset();libraryUI.reset();$('#greeting').textContent='';$('#search').value='';$('#course-filter').value='';$('#admin-content').replaceChildren();$('#feedback-admin-content').replaceChildren();$$('dialog').forEach(d=>d.close());$$('input[type=password]').forEach(x=>x.value='');$$('textarea').forEach(x=>x.value='');}
async function api(path,{method='GET',body,...options}={}){
  if(state.offline){
    if(method==='GET'&&path==='/notes')return {notes:state.notes};
    if(method==='GET'&&path==='/courses')return {courses:state.courses};
    if(method==='GET'&&/^\/notes\/[^/]+$/.test(path)){const note=await offline.getNote(state.user.id,path.slice(7));if(note)return {note};throw Error('这份笔记尚未缓存在当前设备。');}
    if(method==='GET'&&path.startsWith('/notes?q=')){const q=decodeURIComponent(path.slice(9)).toLowerCase();return {notes:state.notes.filter(n=>`${n.title} ${n.summary} ${n.transcript}`.toLowerCase().includes(q))};}
    throw Error('离线时只能阅读已打开的笔记。');
  }
  const headers={'Accept':'application/json',...options.headers};
  if(body!==undefined)headers['Content-Type']='application/json';
  if(!['GET','HEAD'].includes(method))headers['X-CSRF-Token']=state.csrf;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),45000);let res;try{res=await fetch(`/api${path}`,{method,credentials:'same-origin',cache:'no-store',signal:controller.signal,...options,headers,body:body===undefined?undefined:JSON.stringify(body)});}catch(e){const error=new Error(e.name==='AbortError'?'服务器响应超时，请重试。':'连接暂时中断，请稍后再试。');error.networkFailure=true;throw error;}finally{clearTimeout(timeout);}
  let value;try{value=await res.json();}catch{throw new Error('服务暂时无法连接，请稍后再试。');}
  if(!res.ok){if(res.status===401&&state.user){cleanPrivateState();showAuth('login');}throw new Error(apiErrorMessage(res.status,value));}
  if(value.csrfToken)state.csrf=value.csrfToken;return value;
}
async function submit(form,fn,errorSelector){const b=form.querySelector('button:not([type=button])');if(b?.disabled)return;if(errorSelector)errorAt(errorSelector,'');if(b)b.disabled=true;try{await fn();}catch(e){if(errorSelector)errorAt(errorSelector,e);else toast(e.message);}finally{if(b)b.disabled=false;}}
function showAuth(mode='login'){
  state.auth=mode;$('#loading-view').hidden=true;$('#app-view').hidden=true;$('#auth-view').hidden=false;
  const titles={login:'欢迎回来',register:'开始自己的课堂笔记',forgot:'找回密码',reset:'设置新密码'};
  $('#auth-heading').textContent=titles[mode];$('#auth-tabs').hidden=!['login','register'].includes(mode);
  $$('#auth-tabs button').forEach(b=>b.classList.toggle('active',b.dataset.auth===mode));
  $('#auth-name-row').hidden=mode!=='register';$('#auth-email-row').hidden=mode==='reset';$('#auth-password-row').hidden=mode==='forgot';
  const email=$('#auth-form [name=email]'),pw=$('#auth-form [name=password]');email.required=mode!=='reset';pw.required=mode!=='forgot';pw.value='';pw.autocomplete=mode==='login'?'current-password':'new-password';
  $('#auth-submit').textContent={login:'登录',register:'创建账号并验证邮箱',forgot:'发送重置邮件',reset:'保存新密码'}[mode];
  $('#forgot-password').textContent=mode==='forgot'?'返回登录':'忘记密码';$('#forgot-password').hidden=mode==='register'||mode==='reset';
  $('#auth-hint').textContent=mode==='register'?'支持 .edu、.edu.cn 等学校邮箱，需收信完成验证。':mode==='forgot'?'我们会向已注册邮箱发送密码重置邮件。':mode==='reset'?'请设置至少 4 位的新密码。':'支持 .edu、.edu.cn 等学校邮箱。';errorAt('#auth-error','');
}
function userUI(){const u=state.user;if(!u)return;$('#account-name').textContent=u.name||'同学';$('#avatar').textContent=(u.name||'同学').slice(0,1);$('#account-email').textContent=state.offline?'离线已读笔记':state.config.localMode?'保存在这台电脑':u.email;$('#password-open').hidden=!!state.config.localMode;$('#logout').hidden=!!state.config.localMode;$('#account-display-name').value=u.name||'';$('#admin-panel').hidden=!u.isAdmin;$('#feedback-admin-panel').hidden=!u.isAdmin;renderGreeting();}
async function enterApp(){await offline.setUser(state.user);setOfflineMode(false);const target=new URLSearchParams(location.search).get('note');state.epoch++;$('#auth-view').hidden=true;$('#loading-view').hidden=true;$('#app-view').hidden=false;userUI();showView('notes');await Promise.all([refresh(),settingsUI.load()]);if(target)await openNote(target);else if(new URLSearchParams(location.search).get('deck')){showView('slides');await slidesUI.open(new URLSearchParams(location.search).get('deck'),Number(new URLSearchParams(location.search).get('page'))||1);}else if(state.config.localMode&&new URLSearchParams(location.search).has('transfer'))openUpload();}
function fillCourses(select,empty='暂不分类'){const value=select.value;select.replaceChildren(new Option(empty,''));for(const c of state.courses)select.add(new Option(c.name,c.id));select.value=state.courses.some(c=>c.id===value)?value:'';}
async function refresh(){if(!state.user||refreshing||state.offline)return;refreshing=true;const epoch=state.epoch;try{const [a,b]=await Promise.all([api('/notes'),api('/courses')]);if(epoch!==state.epoch)return;state.notes=a.notes;state.courses=b.courses;offline.prune(state.user.id,state.notes.map(n=>n.id));errorAt('#connection-error','');renderLists();if(state.view==='detail'&&state.note){const id=state.note.id;const n=await api(`/notes/${id}`);if(epoch===state.epoch&&state.note?.id===id&&state.view==='detail')renderNote(n.note);}}catch(e){if(state.user)errorAt('#connection-error',e);}finally{refreshing=false;}}
function renderLists(){fillCourses($('#course-filter'),'全部课程');$('#note-count').textContent=state.notes.length;$('#merge-recordings').hidden=state.notes.filter(n=>['ready','transcribed'].includes(n.status)).length<2;renderNotes();$('#course-add').textContent=state.courses.length?'课程设置':'导入课表';if(state.view==='courses')calendar.render();}
function renderGreeting(){$('#greeting').textContent=greeting(state.user?.name,state.settings?.greetingCategories||[]);}
function renderNotes(){
  const q=$('#search').value.trim().toLowerCase(),cid=$('#course-filter').value;
  const list=state.notes.filter(n=>(!cid||n.courseId===cid)&&(!q||searchIds?.has(n.id)||`${n.title} ${n.sourceName} ${courseName(n.courseId)}`.toLowerCase().includes(q)));
  const root=$('#notes-list');root.replaceChildren();$('#notes-empty').hidden=!!list.length||!(q||cid);root.hidden=!list.length;
  $('#note-filters').hidden=!state.notes.length;$('#notes-welcome').classList.toggle('has-notes',!!state.notes.length);renderGreeting();
  for(const n of list){const row=el('button','note-row');row.type='button';const mark=el('span','note-icon');mark.append(icon('notes'));const text=el('span','note-text');text.append(el('strong','',noteTitle(n,state.courses)));const parts=[];if(n.duration)parts.push(`${Math.ceil(n.duration/60)} 分钟`);text.append(el('small','',parts.join(' · ')));row.append(mark,text,el('span',`status ${n.status}`,statusText[n.status]||n.stage||'处理中'),icon('arrow'));row.addEventListener('click',()=>openNote(n.id));root.append(row);}
}
function showView(view){state.view=view;for(const id of ['notes','courses','slides','settings','detail'])$(`#${id}-view`).hidden=id!==view;$$('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===(view==='detail'?'notes':view)));if(view==='slides')slidesUI.load().catch(e=>toast(e.message));else slidesUI.stop();if(view==='settings')settingsUI.load();if(view==='courses')calendar.render();if(view!=='detail'){$('#note-audio').pause();$('#note-audio').removeAttribute('src');state.note=null;if(new URLSearchParams(location.search).has('note'))history.replaceState(null,'',location.pathname);}if(view==='notes')renderGreeting();window.scrollTo({top:0,behavior:'instant'});}
async function openNote(id){const epoch=state.epoch;try{const {note}=await api(`/notes/${id}`);if(epoch!==state.epoch)return;showView('detail');history.replaceState(null,'',`?note=${encodeURIComponent(note.id)}`);renderNote(note);showReaderTab(note.summary||!note.transcript?'summary':'transcript');if(note.transcript&&!note.summary)$('#download-format').value='txt';}catch(e){toast(e.message);}}
function showReaderTab(kind){if(state.note){if(kind==='summary'&&!state.note.summary&&state.note.transcript)kind='transcript';if(kind==='transcript'&&!state.note.transcript&&state.note.summary)kind='summary';}for(const name of ['summary','transcript']){$(`#${name}-content`).hidden=name!==kind;$(`#show-${name}`).classList.toggle('active',name===kind);}}
function renderNote(n){if(!state.offline&&state.user)offline.saveNote(state.user.id,n,state.courses);const old=state.note;state.note=n;if(old?.id!==n.id){$('#note-audio').pause();$('#note-audio').removeAttribute('src');$('#note-audio-download').removeAttribute('href');$('#note-audio-panel').open=false;$('#note-audio-error').hidden=true;}$('#note-audio-panel').hidden=!n.hasAudio;renderHistory(n,$('#note-history'),{api,toast,onNoteChanged: note=>{if(state.note?.id===note.id){renderNote(note);refresh();}}});$('#detail-title').textContent=noteTitle(n,state.courses);$('#detail-imported').hidden=!n.transcript&&!n.hasAudio;$('#detail-import-date').textContent=`导入日期：${new Date(n.importedAt||n.createdAt).toLocaleString('zh-CN')}`;if(old?.id!==n.id){$('#detail-body').hidden=false;$('#detail-title').setAttribute('aria-expanded','true');$('#detail-import-date').hidden=true;$('#detail-imported').setAttribute('aria-expanded','false');}if(old?.id!==n.id||old?.summary!==n.summary){$('#summary-content').replaceChildren();if(n.summary){renderMarkdown(n.summary,$('#summary-content'));const whole=$('#summary-content > .knowledge-whole'),heading=whole?.querySelector(':scope > summary h1');if(heading&&noteTitle(n,state.courses).includes(heading.textContent.trim()))whole.replaceWith(...whole.querySelector(':scope > .knowledge-whole-body').childNodes);}}$('#transcript-content').textContent=n.transcript||'转写完成后，原文会显示在这里。';$('#generation-prompt').textContent=n.effectivePrompt||n.promptSnapshot||'使用默认课堂整理方式';$('#generation-model').textContent=n.model?`模型：${n.model}`:'';$('#generation-details').hidden=!n.summary;$('#detail-regenerate').textContent=n.summary?'重新整理':'整理笔记';$('#detail-regenerate').disabled=state.offline||!n.transcript||busyStatus(n.status);$('#detail-edit-open').disabled=state.offline||busyStatus(n.status);$('#detail-download').disabled=!n.transcript&&!n.summary;
  const busy=busyStatus(n.status),hasContent=!!(n.summary||n.transcript);
  $('#note-reader').hidden=!hasContent;
  $('#show-summary').hidden=!n.summary;
  $('#show-transcript').hidden=!n.transcript;
  $('#detail-edit-open').hidden=busy;
  $('#detail-regenerate').hidden=busy||!n.transcript;
  for(const option of $('#download-format').options)option.hidden=option.value==='txt'?!n.transcript:!n.summary;
  if(!n.summary)$('#download-format').value='txt';
  else if(!n.transcript||!old?.summary)$('#download-format').value='md';
  if(old?.id!==n.id||(!old?.summary&&n.summary)||(!old?.transcript&&n.transcript&&!n.summary))showReaderTab(n.summary?'summary':'transcript');
  const stage=$('#detail-stage');stage.replaceChildren();
  const audioNeedsTranscript=n.hasAudio&&!n.transcript&&n.status!=='uploading'&&!busy;
  stage.hidden=state.offline||(!busy&&!audioNeedsTranscript&&!['uploading','error','uncertain'].includes(n.status));
  if(stage.hidden)return;
  let label='',hint='',action='';
  if(busy){
    label={queued:n.transcript?'等待整理…':'等待转录…',preparing:'正在处理录音…',transcribing:'正在转录…',summarizing:'正在整理笔记…'}[n.status];
    hint=state.config.localMode?'完成后自动显示，请保持电脑开机。':'完成后自动显示，可离开此页。';
  }else if(n.status==='uploading'){
    label='录音未上传完成';action='继续上传';
  }else if(n.status==='uncertain'){
    label='处理结果待确认';action='查询进度';
  }else if(n.status==='error'){
    label='处理未完成';action='继续处理';
  }else if(audioNeedsTranscript){
    label='录音已保存';action='转录并整理';
  }
  stage.append(el('div','note-progress-title',label));
  if(hint)stage.append(el('p','small muted',hint));
  if(action){
    $('#detail-regenerate').hidden=true;
    const button=el('button','primary',action);button.type='button';button.disabled=!!state.offline;
    button.onclick=async()=>{
      if(n.status==='uploading'){openUpload();return;}
      button.disabled=true;
      try{const result=await api(`/notes/${n.id}/resume`,{method:'POST',body:{}});renderNote(result.note);await refresh();}
      catch(error){toast(error);button.disabled=false;}
    };
    stage.append(button);
  }
}
function openUpload(){recordingUpload.open();}
async function uploadRecording(){await recordingUpload.upload();}
async function confirmDelete(title,text){$('#confirm-title').textContent=title;$('#confirm-text').textContent=text;const d=$('#confirm-dialog');d.returnValue='';d.showModal();return new Promise(resolve=>d.addEventListener('close',()=>resolve(d.returnValue==='confirm'),{once:true}));}
function saveBlob(blob,name){const url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);}
function safeFilename(text){return text.replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').slice(0,160)||'课堂笔记';}
async function download(){const n=state.note,format=$('#download-format').value;if(!n)return;if(format==='html'){if(!n.summary)throw new Error('整理完成后可以下载阅读版。');const doc=document.implementation.createHTMLDocument(n.title);doc.documentElement.lang='zh-CN';const meta=doc.createElement('meta');meta.setAttribute('name','viewport');meta.setAttribute('content','width=device-width,initial-scale=1');const security=doc.createElement('meta');security.httpEquiv='Content-Security-Policy';security.content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:";const style=doc.createElement('style');style.textContent='body{max-width:860px;margin:50px auto;padding:0 24px;color:#30362d;background:#fff;font:16px/1.9 system-ui,sans-serif}h1,h2,h3{line-height:1.5}pre{white-space:pre-wrap;background:#f4f5ef;padding:18px;overflow-wrap:anywhere}table{border-collapse:collapse;display:block;overflow:auto}td,th{border:1px solid #ddd;padding:8px 12px}svg,img{max-width:100%;height:auto}figure{margin:1em 0}figcaption{font-size:13px}a{color:#536c49}math{font-size:1.08em}.katex-mathml{position:static!important;width:auto!important;height:auto!important;clip:auto!important;overflow:visible!important}.knowledge-math--display{margin:1em 0;text-align:center}summary{cursor:pointer}@media print{body{margin:0}}';doc.head.append(meta,security,style);const title=doc.createElement('h1');title.textContent=n.title;doc.body.append(title);const content=$('#summary-content').cloneNode(true);const imageResources=parseNoteImageResources(n.summary);content.removeAttribute('id');content.querySelectorAll('.concept-trigger').forEach(x=>x.replaceWith(doc.createTextNode(x.textContent)));content.querySelectorAll('.katex-html,script,style,iframe,button').forEach(x=>x.remove());content.querySelectorAll('a').forEach(a=>{a.removeAttribute('target');if(/^\/?\?note=/.test(a.getAttribute('href')||''))a.href=new URL(a.getAttribute('href'),location.origin).href;});for(const img of content.querySelectorAll('img')){try{const url=new URL(img.getAttribute('src'),location.href);if(!getNoteResource(img.getAttribute('src'),imageResources)){img.remove();continue;}const response=await fetch(url,{credentials:url.origin===location.origin?'same-origin':'omit',referrerPolicy:'no-referrer'});if(!response.ok)throw Error('image');const blob=await response.blob();img.src=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(blob);});const link=img.closest('a');if(link)link.href=img.src;}catch{img.replaceWith(doc.createTextNode(img.alt||'图片'));}}doc.body.append(content);saveBlob(new Blob(['<!doctype html>\n',doc.documentElement.outerHTML],{type:'text/html;charset=utf-8'}),`${safeFilename(n.title)}.html`);return;}
  if(state.offline){saveBlob(new Blob([format==='txt'?n.transcript:n.summary],{type:'text/plain;charset=utf-8'}),`${safeFilename(n.title)}.${format}`);return;}const res=await fetch(`/api/notes/${n.id}/download?format=${format}`,{credentials:'same-origin',cache:'no-store'});if(!res.ok)throw new Error('下载失败，请重新登录后再试。');saveBlob(await res.blob(),`${safeFilename(n.title)}.${format}`);
}

$$('[data-auth]').forEach(b=>b.onclick=()=>showAuth(b.dataset.auth));
$('#forgot-password').onclick=()=>showAuth(state.auth==='forgot'?'login':'forgot');
$('#auth-form').onsubmit=e=>{e.preventDefault();submit(e.currentTarget,async()=>{const form=new FormData(e.target),mode=state.auth;const body=mode==='reset'?{token:new URLSearchParams(location.search).get('reset'),password:form.get('password')}:mode==='forgot'?{email:String(form.get('email')||'').trim()}:mode==='register'?{email:String(form.get('email')||'').trim(),password:form.get('password'),name:form.get('name')||'同学'}:{email:String(form.get('email')||'').trim(),password:form.get('password')};const r=await api(`/auth/${mode==='forgot'?'forgot':mode}`,{method:'POST',body});if(mode==='login'){state.user=r.user;$('#auth-form').reset();await enterApp();}else{showAuth('login');if(mode==='reset')history.replaceState(null,'',location.pathname);toast(r.message||'请查看邮箱并按提示操作。');}},'#auth-error');};
$$('[data-view]').forEach(b=>b.onclick=()=>showView(b.dataset.view));
$('#search').oninput=()=>{clearTimeout(searchTimer);searchIds=null;renderNotes();const query=$('#search').value.trim(),epoch=state.epoch;if(query)searchTimer=setTimeout(async()=>{try{const result=await api(`/notes?q=${encodeURIComponent(query.slice(0,200))}`);if(epoch===state.epoch&&$('#search').value.trim()===query){searchIds=new Set(result.notes.map(n=>n.id));renderNotes();}}catch(e){if(epoch===state.epoch)toast(e.message);}},220);};$('#course-filter').onchange=renderNotes;
$('#empty-upload').onclick=openUpload;
$('#upload-form').onsubmit=e=>{e.preventDefault();submit(e.currentTarget,uploadRecording,'#upload-error');};
$('#upload-dialog').addEventListener('cancel',e=>{if(state.uploading)e.preventDefault();});
// Close only a pointer gesture entirely outside a dialog, never a drag from its content.
let backdropDialog=null;
const outsideDialog=(dialog,event)=>{const r=dialog.getBoundingClientRect();return event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom;};
document.addEventListener('pointerdown',event=>{backdropDialog=event.target?.tagName==='DIALOG'&&outsideDialog(event.target,event)?event.target:null;});
document.addEventListener('click',event=>{const dialog=backdropDialog;backdropDialog=null;if(dialog&&event.target===dialog&&outsideDialog(dialog,event)&&dialog.dataset.locked!=='true'&&!(dialog.id==='upload-dialog'&&state.uploading))dialog.close();});
const sidebarToggle=$('#sidebar-toggle');
let sidebarCollapsed=false;try{sidebarCollapsed=localStorage.getItem('tingji-sidebar-collapsed')==='true';}catch{}
function setSidebarCollapsed(value){sidebarCollapsed=value;$('#app-view').classList.toggle('sidebar-collapsed',value);sidebarToggle.setAttribute('aria-expanded',String(!value));sidebarToggle.setAttribute('aria-label',value?'展开侧栏':'收起侧栏');sidebarToggle.title=value?'展开侧栏':'收起侧栏';try{localStorage.setItem('tingji-sidebar-collapsed',String(value));}catch{}}
sidebarToggle.onclick=()=>setSidebarCollapsed(!sidebarCollapsed);setSidebarCollapsed(sidebarCollapsed);
$$('[data-close]').forEach(b=>b.onclick=()=>{if(b.closest('dialog').id==='upload-dialog'&&state.uploading)return;b.closest('dialog').close();});
$('#course-add').onclick=()=>calendar.openSettingsOrImport();
$('#account-open').onclick=async()=>{userUI();$('#account-dialog').showModal();try{const r=await api('/session');state.user=r.user;userUI();if(state.user?.isAdmin){const data=await api('/admin/overview');const root=$('#admin-content');root.replaceChildren(el('p','',`${data.totals.users} 位用户 · ${data.totals.notes} 份笔记 · ${money(data.totals.usageYuan)}`));const table=el('table'),tr=el('tr');for(const t of ['用户','用量','笔记'])tr.append(el('th','',t));table.append(tr);for(const u of data.users){const row=el('tr');row.append(el('td','',u.name||u.email),el('td','',money(u.usageYuan)),el('td','',String(u.noteCount)));table.append(row);}root.append(table);await loadFeedback();}}catch(e){toast(e.message);}};
$('#account-form').onsubmit=e=>{e.preventDefault();submit(e.currentTarget,async()=>{const r=await api('/account',{method:'PATCH',body:{name:$('#account-display-name').value}});state.user=r.user;userUI();toast('已保存。');});};
$('#password-open').onclick=()=>{$('#password-form').reset();errorAt('#password-error','');$('#password-dialog').showModal();};
$('#password-form').onsubmit=e=>{e.preventDefault();submit(e.currentTarget,async()=>{await api('/account/password',{method:'POST',body:{currentPassword:$('#current-password').value,password:$('#new-password').value}});$('#password-form').reset();$('#password-dialog').close();toast('密码已更新。');},'#password-error');};
$('#logout').onclick=async()=>{try{await api('/auth/logout',{method:'POST',body:{}});cleanPrivateState();const s=await api('/session');state.csrf=s.csrfToken;showAuth('login');}catch(e){toast(e.message);}};
$('#note-audio-panel').addEventListener('toggle',async()=>{if(!$('#note-audio-panel').open||$('#note-audio').getAttribute('src'))return;const id=state.note?.id;if(!id)return;try{const data=await api(`/notes/${id}/audio`);if(state.note?.id!==id)return;$('#note-audio').src=data.url;$('#note-audio-download').href=data.url;}catch(e){errorAt('#note-audio-error',e);}});
$('#detail-back').onclick=()=>{showView('notes');refresh();};
for(const kind of ['summary','transcript'])$(`#show-${kind}`).onclick=()=>showReaderTab(kind);
$('#detail-title').onclick=()=>{const body=$('#detail-body');body.hidden=!body.hidden;$('#detail-title').setAttribute('aria-expanded',String(!body.hidden));};
$('#detail-imported').onclick=()=>{const panel=$('#detail-import-date');panel.hidden=!panel.hidden;$('#detail-imported').setAttribute('aria-expanded',String(!panel.hidden));};
$('#detail-download').onclick=()=>download().catch(e=>toast(e.message));
$('#detail-edit-open').onclick=()=>{const n=state.note;if(!n)return;$('#edit-title').value=n.title;$('#edit-summary').value=n.summary||'';$('#edit-date').value=n.recordingDate||'';errorAt('#edit-error','');$('#edit-dialog').showModal();};
$('#edit-form').onsubmit=e=>{e.preventDefault();submit(e.currentTarget,async()=>{const r=await api(`/notes/${state.note.id}`,{method:'PATCH',body:{title:$('#edit-title').value,summary:$('#edit-summary').value,recordingDate:$('#edit-date').value}});renderNote(r.note);$('#edit-dialog').close();toast('笔记已保存。');},'#edit-error');};
$('#note-delete').onclick=async()=>{const n=state.note;if(!n||!await confirmDelete('删除笔记',`从笔记列表中移除“${n.title}”？`))return;try{await api(`/notes/${n.id}`,{method:'DELETE'});$('#edit-dialog').close();showView('notes');await refresh();toast('笔记已删除。');}catch(e){errorAt('#edit-error',e);}};
$('#detail-regenerate').onclick=()=>{$('#regenerate-prompt').value='';$('#regenerate-prompt').placeholder='留空使用默认整理方式';errorAt('#regenerate-error','');$('#regenerate-dialog').showModal();};
$('#regenerate-form').onsubmit=e=>{e.preventDefault();submit(e.currentTarget,async()=>{const r=await api(`/notes/${state.note.id}/summarize`,{method:'POST',body:{prompt:$('#regenerate-prompt').value||undefined}});renderNote(r.note);$('#regenerate-dialog').close();toast('已开始重新整理。');},'#regenerate-error');};
window.addEventListener('beforeunload',e=>{if(state.uploading){e.preventDefault();e.returnValue='';}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){renderGreeting();refresh();}});
setInterval(()=>{if(!document.hidden&&state.user&&state.notes.some(n=>busyStatus(n.status)))refresh();},15000);
async function boot(){setOfflineMode(false);try{const session=await api('/session');state.user=session.user;state.csrf=session.csrfToken;state.config=session.config||{};localTransfer.configure(!!state.config.localMode);if(state.config.localMode)navigator.serviceWorker?.getRegistration().then(r=>r?.update()).catch(()=>{});$('#upload-mode-hint').textContent=state.config.localMode?'录音先保存到电脑，再转写和整理。处理时请保持电脑运行。':'录音上传完成后由云端继续处理。请上传你有权使用的课堂录音。';const query=new URLSearchParams(location.search);if(query.has('verify')){try{const result=await api('/auth/verify',{method:'POST',body:{token:query.get('verify')}});toast(result.message||'邮箱验证成功，请登录。');}catch(e){toast(e.message);}history.replaceState(null,'',location.pathname);showAuth('login');return;}if(query.has('reset')){showAuth('reset');return;}if(state.user)await enterApp();else {await offline.clear();showAuth('login');}}catch(e){if(e.networkFailure&&await enterOffline())return;showAuth('login');errorAt('#auth-error',e);$('#auth-submit').disabled=true;const retry=el('button','secondary full','重新连接');retry.type='button';retry.onclick=()=>{retry.remove();$('#auth-submit').disabled=false;boot();};$('#auth-form').append(retry);}}
const settingsUI=createSettings({onSchoolChanged:()=>calendar.reset(),root:$('#settings-content'),api,toast,onChange:value=>{state.settings=value;renderGreeting();}});
const calendar=createCalendar({root:$('#courses-content'),api,toast,confirmDelete,getCourses:()=>state.courses,getNotes:()=>state.notes,onOpenNote:openNote,onOpenDeck:(id,page)=>{showView('slides');slidesUI.open(id,page).catch(e=>toast(e.message));},onCoursesChanged:async()=>{await refresh();}});
async function loadFeedback(){const {feedback}=await api('/admin/feedback');const root=$('#feedback-admin-content');root.replaceChildren();for(const item of feedback||[]){const row=el('div','feedback-record');row.append(el('p','',item.content),el('small','',date(item.createdAt)));for(const file of item.attachments||[]){const link=el('a','feedback-attachment',file.filename);link.href=file.url;link.target='_blank';link.rel='noopener';if(file.isImage){const img=el('img');img.src=file.url;img.alt=file.filename;img.loading='lazy';link.prepend(img);}row.append(link);}root.append(row);}if(!root.childElementCount)root.append(el('p','muted small','还没有反馈。'));}
$('#summary-content').addEventListener('click',e=>{const link=e.target.closest('a');if(!link)return;const url=new URL(link.href,location.href);const id=url.searchParams.get('note');if(url.origin===location.origin&&id){e.preventDefault();openNote(id);}});
window.addEventListener('popstate',()=>{const id=new URLSearchParams(location.search).get('note');if(id&&state.user)openNote(id);else if(state.user)showView('notes');});
const localTransfer=createLocalTransfer({api,toast,getCourses:()=>state.courses,onImported:()=>refresh()});
const libraryUI=createLibrary({api,toast,getCourses:()=>state.courses,onComplete:refresh,onOpenNote:openNote});
$('#library-open').onclick=()=>libraryUI.open();
const slidesUI=createSlides({root:$('#slides-content'),api,toast,renderMarkdown,getCourses:()=>state.courses,getNotes:()=>state.notes,onOpenNote:openNote});
$('#detail-slides').onclick=()=>{const note=state.note;if(!note)return;showView('slides');slidesUI.openForNote(note).catch(e=>toast(e.message));};
$('#merge-recordings').onclick=createRecordingMerge({api,getNotes:()=>state.notes,getCourses:()=>state.courses,toast,onComplete:async note=>{await refresh();await openNote(note.id);}});
const recordingUpload=createRecordingUpload({api,toast,getState:()=>state,onComplete:async notes=>{await refresh();if(notes.length===1)await openNote(notes[0].id);else showView('notes');}});
const offline=createOffline({onCleared:()=>{if(state.user){cleanPrivateState({clearOffline:false});showAuth('login');}}});
function setOfflineMode(value){state.offline=value;$('#offline-banner').hidden=!value;for(const selector of ['#empty-upload','#merge-recordings','#library-open','#local-transfer-open','#account-open','#detail-regenerate','#detail-edit-open','#detail-slides','[data-view=courses]','[data-view=slides]','[data-view=settings]'])$(selector).disabled=value;}
async function enterOffline(){const saved=await offline.snapshot();if(!saved?.notes?.length)return false;state.epoch++;state.user=saved.user;state.notes=saved.notes;state.courses=saved.courses;state.csrf='';state.config={};state.settings=null;state.note=null;localTransfer.configure(false);const target=new URLSearchParams(location.search).get('note');setOfflineMode(true);$('#auth-view').hidden=true;$('#loading-view').hidden=true;$('#app-view').hidden=false;userUI();showView('notes');renderLists();if(target&&saved.notes.some(n=>n.id===target)){history.replaceState(null,'',`?note=${encodeURIComponent(target)}`);await openNote(target);}return true;}
$('#offline-reconnect').onclick=()=>boot();
window.addEventListener('online',()=>{if(state.offline)boot();});
window.addEventListener('offline',()=>{if(state.user&&!state.uploading)enterOffline();});
offline.register();
boot();
