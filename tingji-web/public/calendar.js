const DAYS = ['一','二','三','四','五','六','日'];
const PERIODS = ['一、二节','三、四节','五、六节','七、八节','九、十节'];
const COLORS = ['#dce7d4','#d7e4ec','#e9dbd0','#e4deed','#ece5c8','#d6e8e4','#e9d9df','#dfe2c9','#d9deef','#eeded1','#e4e5dc','#d1e1db'];
const node = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; };
const button = (text, cls='secondary', action) => { const b=node('button',cls,text); b.type='button'; if(action)b.onclick=action; return b; };
const localDate = text => { if(!/^\d{4}-\d{2}-\d{2}$/.test(text||''))return null; const [y,m,d]=text.split('-').map(Number),v=new Date(y,m-1,d,12); return v.getFullYear()===y&&v.getMonth()===m-1&&v.getDate()===d?v:null; };
const isoDate = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
const addDays = (d,count) => { const n=new Date(d); n.setDate(n.getDate()+count); return n; };
const monday = d => addDays(new Date(d.getFullYear(),d.getMonth(),d.getDate(),12),-((d.getDay()+6)%7));
const daysBetween = (a,b) => Math.round((Date.UTC(a.getFullYear(),a.getMonth(),a.getDate())-Date.UTC(b.getFullYear(),b.getMonth(),b.getDate()))/86400000);
const dateText = d => `${d.getMonth()+1}月${d.getDate()}日`;
const safeColor = (color,index=0) => /^#[0-9a-f]{6}$/i.test(color||'')?color:COLORS[index%COLORS.length];
const errorText = error => error?.message||'没有完成，请稍后再试。';

// Keep the visible/clicked date distinct from the weekday used by a makeup class.
export function calendarDay(source,week,weekday) {
  const start=localDate(source?.semesterStart),date=start?isoDate(addDays(monday(start),(week-1)*7+weekday-1)):null;
  const calendar=date&&source.academicCalendar?.semesterStart===source.semesterStart?source.academicCalendar:null;
  if(!calendar)return {date,weekday,closed:false,label:''};
  if(date<calendar.semesterStart||date>calendar.teachingEnd)return {date,weekday,closed:true,label:date>calendar.teachingEnd&&date<=calendar.end?'复习考试':''};
  const holiday=(calendar.holidays||[]).find(value=>date>=value.start&&date<=value.end);
  if(holiday)return {date,weekday,closed:true,label:holiday.label||'停课'};
  const adjustment=(calendar.adjustments||[]).find(value=>value.date===date);
  return {date,weekday:adjustment?.weekday||weekday,closed:false,label:adjustment?.label||'',weekParity:adjustment?.weekParity||null};
}

export function calendarSlotsAt(source,weekday,period,week,preview=false) {
  const day=preview?{weekday,closed:false}:calendarDay(source,week,weekday);
  if(day.closed)return [];
  return (source.slots||[]).filter(slot=>{
    if(slot.weekday!==day.weekday||slot.period!==period)return false;
    if(preview||!slot.weeks?.length)return true;
    if(!day.weekParity)return slot.weeks.includes(week);
    // The notice gives odd/even weekday, not a replacement week number.
    // Retain the course's actual teaching range and apply only that stated parity.
    const parity=day.weekParity==='odd'?1:0,matching=slot.weeks.filter(value=>value%2===parity);
    return matching.length>0&&week>=Math.min(...slot.weeks)&&week<=Math.max(...slot.weeks);
  });
}

// The outline follows only the outer edges of connected free cells.
// Shared edges disappear, so neighbouring cells read as one selection area.
export function freeCellOutline(free, columns=7, rows=5) {
  const paths=[];
  for(let y=0;y<rows;y++)for(let x=0;x<columns;x++)if(free.has(`${x}:${y}`)) {
    if(!free.has(`${x}:${y-1}`))paths.push(`M${x} ${y}h1`);
    if(!free.has(`${x+1}:${y}`))paths.push(`M${x+1} ${y}v1`);
    if(!free.has(`${x}:${y+1}`))paths.push(`M${x+1} ${y+1}h-1`);
    if(!free.has(`${x-1}:${y}`))paths.push(`M${x} ${y+1}v-1`);
  }
  return paths.join('');
}

export function createCalendar({root,api,toast,onCoursesChanged=()=>{},confirmDelete,getCourses=()=>[],getNotes=()=>[],onOpenNote=()=>{},onOpenDeck=()=>{}}) {
  let data=null,loadedAt=0,request=null,epoch=0,selectedWeek=null,editing=null,importing=false;
  const dialogs=new Set();
  const picker=node('input'); picker.type='file';picker.hidden=true;picker.accept='image/*,.pdf,.xlsx,.xls';document.body.append(picker);
  const courseData=()=>data?.courses||getCourses();
  const courseById=id=>courseData().find(c=>c.id===id);
  const allWeeks=()=>Array.from({length:data?.weeks||20},(_,i)=>i+1);
  function makeDialog(title,extra='') {
    const dialog=node('dialog',`cal-dialog ${extra}`),heading=node('div','dialog-heading'),h=node('h2','',title),close=button('×','icon-button',()=>{if(dialog.dataset.locked!=='true')dialog.close();});
    h.id=`cal-title-${crypto.randomUUID()}`;dialog.setAttribute('aria-labelledby',h.id);close.setAttribute('aria-label','关闭');heading.append(h,close);dialog.append(heading);document.body.append(dialog);dialogs.add(dialog);
    dialog.addEventListener('cancel',event=>{if(dialog.dataset.locked==='true')event.preventDefault();});dialog.addEventListener('close',()=>{dialogs.delete(dialog);dialog.remove();},{once:true});
    return dialog;
  }
  function formError(parent,error) { let e=parent.querySelector('.cal-error');if(!e){e=node('p','cal-error form-error');e.setAttribute('role','alert');parent.append(e);}e.textContent=errorText(error);return e; }
  async function action(b,fn,parent) {if(b.disabled)return;b.disabled=true;try{await fn();}catch(e){if(parent?.isConnected)formError(parent,e);else toast(errorText(e));}finally{b.disabled=false;}}
  async function reload() {loadedAt=0;await render({force:true});await onCoursesChanged();}
  function updateEntry() {const b=document.getElementById('course-add');if(b){b.textContent=courseData().length?'课程设置':'导入课表';b.hidden=!courseData().length;}}
  function normalize(value) {const t=value.timetable||value;return {...t,courses:t.courses||getCourses(),slots:t.slots||[],weeks:Number(t.weeks)||20};}
  async function render({force=false}={}) {
    if(!root)return;
    if(editing&&!force)return;
    if(data&&!force&&Date.now()-loadedAt<30000){draw();return;}
    if(request)return request;
    const token=epoch;
    if(!data)root.replaceChildren(node('p','cal-loading muted','正在读取课表…'));
    request=(async()=>{try{const value=await api('/timetable');if(token!==epoch)return;data=normalize(value);loadedAt=Date.now();updateEntry();draw();}catch(e){if(token!==epoch)return;if(data){toast(errorText(e));return;}const box=node('div','cal-load-error');box.append(node('p','form-error',errorText(e)),button('重新读取','secondary',()=>render({force:true})));root.replaceChildren(box);}finally{request=null;}})();
    return request;
  }
  function weekIndex(date) {const start=localDate(data?.semesterStart);return start?Math.floor(daysBetween(date,monday(start))/7)+1:1;}
  function currentWeek() {return localDate(data?.semesterStart)?weekIndex(monday(new Date())):1;}
  function weekDate(week) {const start=localDate(data?.semesterStart);return start?addDays(monday(start),(week-1)*7):null;}
  function titleForWeek(week) {const date=weekDate(week),current=currentWeek();if(!date)return `第 ${week} 周`;return `${week===current?'本周 · ':''}第 ${week} 周 · ${dateText(date)} — ${dateText(addDays(date,6))}`;}
  function shortNames(courses) {const used=new Set(),out=new Map();for(const c of courses){let text=(c.shortName||c.name||'课程').slice(0,4),base=text,n=2;while(used.has(text))text=`${base.slice(0,3)}${n++}`;used.add(text);out.set(c.id||c.key,text);}return out;}

  function draw() {
    updateEntry();root.classList.toggle('cal-editing',!!editing);root.replaceChildren();
    if(!data?.courses?.length&&!data?.slots?.length&&!editing) {const empty=node('div','cal-empty-space');empty.append(button('导入课表','primary cal-empty-import',openSettingsOrImport));root.append(empty);return;}
    if(editing){const banner=node('div','cal-edit-banner');banner.setAttribute('role','status');banner.append(node('strong','',editing.courseId?`为「${editing.name}」添加时间`:'选择上课时间'));root.append(banner);}
    const pending=(data.assignments||[]).filter(a=>!a.dueDate&&a.status!=='done');if(!editing&&pending.length){const section=node('section','cal-pending-assignments');section.append(node('h3','','作业 · 日期待确认'));for(const item of pending)section.append(button(item.title,'cal-assignment',()=>openAssignment(item)));root.append(section);}
    const weeks=node('div','cal-weeks');for(const week of editing?[1]:allWeeks().reverse())weeks.append(weekPanel(week));
    root.append(weeks);
    if(!editing&&selectedWeek===null){selectedWeek=Math.max(1,Math.min(data.weeks,currentWeek()));requestAnimationFrame(()=>weeks.querySelector(`[data-week="${selectedWeek}"]`)?.scrollIntoView({block:'start'}));}
    if(editing) {
      const bar=node('div','cal-edit-footer'),help=node('span','',editing.pending.length?`已选 ${editing.pending.length} 个时间段`:'选择虚线内的空白区域');
      bar.append(help,button('取消','text-button',stopEditing));
      const save=button('保存并退出','primary',()=>saveEditing(save,bar));save.disabled=!editing.pending.length;bar.append(save);root.append(bar);
    }
  }
  function weekPanel(week,{preview=false,source=data,onCell=null}={}) {
    const section=node('section',`cal-week${preview?' cal-preview-week':''}`),heading=node('h2','cal-week-title',preview||editing?'课表预览':titleForWeek(week));section.dataset.week=week;section.append(heading);
    const start=preview||editing?null:weekDate(week),head=node('div','cal-grid-head');head.append(node('span','cal-period-head','节次'));
    for(let d=0;d<7;d++){const label=node('div','cal-day'),day=start?addDays(monday(start),d):null;label.append(node('span','',`周${DAYS[d]}`));if(day){label.append(node('small','',`${day.getMonth()+1}/${day.getDate()}`));const notice=calendarDay(source,week,d+1).label;if(notice)label.append(node('small','',notice));}if(day&&isoDate(day)===isoDate(new Date()))label.classList.add('cal-today');if(day&&!preview&&!editing){for(const item of data.assignments||[])if(item.dueDate===isoDate(day)){const due=button(item.status==='done'?'作业 ✓':'作业','cal-assignment-badge',()=>openAssignment(item));due.title=item.title;due.setAttribute('aria-label',`${item.status==='done'?'已完成作业':'作业'}：${item.title}`);label.append(due);}}head.append(label);}section.append(head);
    const body=node('div','cal-grid-body'),labels=node('div','cal-periods');for(let p=0;p<5;p++)labels.append(node('span','',PERIODS[p]));body.append(labels);
    const grid=node('div','cal-cells'),names=shortNames(source.courses),free=new Set();grid.setAttribute('role','group');grid.setAttribute('aria-label',preview?'导入课表':titleForWeek(week));
    for(let p=1;p<=5;p++)for(let d=1;d<=7;d++) {
      const active=calendarSlotsAt(source,d,p,week,preview||!!editing); 
      const pending=editing?.pending.find(s=>s.weekday===d&&s.period===p),cell=node('div','cal-cell');
      if(active.length){cell.classList.add('cal-occupied');for(const s of active){const c=source.courses.find(x=>(x.id||x.key)===(s.courseId||s.courseKey));if(!c)continue;const item=button(names.get(c.id||c.key),'cal-course',preview?()=>onCell?.(d,p,s):()=>openLesson(c,week,d));item.style.setProperty('--course-color',safeColor(c.color,source.courses.indexOf(c)));item.title=[c.name,s.location].filter(Boolean).join(' · ');item.setAttribute('aria-label',`${c.name}，周${DAYS[d-1]}${PERIODS[p-1]}${s.location?`，${s.location}`:''}`);if(editing&&!preview)item.disabled=true;cell.append(item);}}
      else if(preview&&onCell){const add=button('+','cal-preview-add',()=>onCell(d,p));add.setAttribute('aria-label',`添加周${DAYS[d-1]}${PERIODS[p-1]}的课程`);cell.append(add);}
      else if(editing&&!preview){free.add(`${d-1}:${p-1}`);cell.classList.add('cal-free');const add=button(pending?'已选':'','cal-select-cell',()=>pickSlot(d,p,week));add.setAttribute('aria-label',`${pending?'调整已选':'选择'}周${DAYS[d-1]}${PERIODS[p-1]}`);if(pending){cell.classList.add('cal-selected');add.textContent=editing.name||'已选';}cell.append(add);}
      grid.append(cell);
    }
    if(editing&&!preview){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'),path=document.createElementNS(svg.namespaceURI,'path');svg.classList.add('cal-free-outline');svg.setAttribute('viewBox','0 0 7 5');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('aria-hidden','true');path.setAttribute('d',freeCellOutline(free));path.setAttribute('vector-effect','non-scaling-stroke');svg.append(path);grid.append(svg);}
    body.append(grid);section.append(body);return section;
  }

  async function openSettingsOrImport() {if(importing)return;if(!courseData().length){picker.value='';picker.click();return;}if(!data)await render();if(data)openSettings();}
  function openSettings() {
    const dialog=makeDialog('课程设置'),importButton=button('导入新课表','secondary full',()=>{dialog.close();picker.value='';picker.click();});dialog.append(importButton);
    const details=node('details','cal-add-course'),summary=node('summary','','添加课程'),fields=node('div','cal-add-fields'),nameLabel=node('label','','课程名称'),name=node('input');name.placeholder='课程名称';name.maxLength=120;nameLabel.append(name);
    const timeLabel=node('label','','上课时间'),time=button('选择时间段','secondary full',()=>{const title=name.value.trim();dialog.close();startEditing(null,title);});timeLabel.append(time);fields.append(nameLabel,timeLabel);details.append(summary,fields);dialog.append(details);
    const startLabel=node('label','cal-import-start','第一周周一'),startDate=node('input');startDate.type='date';startDate.value=data.semesterStart||'';startLabel.append(startDate);dialog.append(startLabel);
    const saveStart=button('保存学期日期','secondary',()=>action(saveStart,async()=>{if(!startDate.value)throw Error('请选择第一周周一的日期。');await api('/timetable',{method:'PATCH',body:{semesterStart:startDate.value,baseRevision:data.revision}});dialog.close();selectedWeek=null;await reload();},dialog));dialog.append(saveStart);
    const list=node('div','cal-settings-list');for(const c of courseData()){const b=button('','cal-settings-course',()=>{dialog.close();openCourse(c);}),dot=node('span','cal-course-dot');dot.style.background=safeColor(c.color,courseData().indexOf(c));b.append(dot,node('span','',c.name),node('span','cal-settings-mark','⚙'));b.setAttribute('aria-label',`设置${c.name}`);list.append(b);}dialog.append(list);dialog.showModal();
  }
  async function openLesson(c,week,day) {
    const date=calendarDay(data,week,day).date,dialog=makeDialog(c.name);
    dialog.append(node('p','small muted',date||`第 ${week} 周 · 周${DAYS[day-1]}`));
    const allNotes=getNotes().filter(n=>n.courseId===c.id),notes=date?allNotes.filter(n=>n.recordingDate===date):[];
    const content=node('div','cal-lesson-content');dialog.append(content);content.append(node('h3','','整理文'));
    if(notes.length)for(const note of notes){const row=button(note.title||note.sourceName||'课堂笔记','cal-lesson-link',()=>{dialog.close();onOpenNote(note.id);});content.append(row);}
    else content.append(node('p','small muted',date?'本次暂无整理文。':'设置学期日期后，按日期显示课次内容。'));
    const slides=node('div','cal-lesson-slides');slides.append(node('h3','','PPT'),node('p','small muted','正在读取…'));content.append(slides);
    dialog.showModal();
    try{const response=await api('/slides');if(!dialog.open)return;slides.replaceChildren(node('h3','','PPT'));
      const ids=new Set(notes.map(n=>n.id)),decks=response.decks||[],matches=decks.filter(d=>[d.noteId,...(d.noteIds||[]),...(d.matchedNotes||[]).map(n=>n.id)].some(id=>ids.has(id)));
      const appendDeck=(parent,d)=>parent.append(button(d.filename,'cal-lesson-link',()=>{dialog.close();onOpenDeck(d.id);}));
      if(matches.length)matches.forEach(d=>appendDeck(slides,d));else slides.append(node('p','small muted','本次暂无对应课件。'));
    }catch(e){if(dialog.open){slides.replaceChildren(node('h3','','PPT'));formError(slides,e);}}
  }
  function openAssignment(item){
    const dialog=makeDialog('作业'),form=node('form'),title=node('input'),detail=node('textarea'),due=node('input'),time=node('input'),done=node('input');title.value=item.title||'';title.maxLength=200;title.required=true;detail.value=item.details||'';detail.rows=4;due.type='date';due.value=item.dueDate||'';time.type='text';time.placeholder='例如 23:59 或 24:00';time.pattern='(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00';time.value=item.dueTime||'';done.type='checkbox';done.checked=item.status==='done';
    for(const [label,input] of [['内容',title],['要求',detail],['截止日期',due],['截止时间',time],['已完成',done]]){const l=node('label','cal-assignment-field',label);l.append(input);form.append(l);}
    const actions=node('div','form-actions'),save=button('保存','primary');save.type='submit';actions.append(save);
    if(item.deckId)actions.append(button(`查看 PPT${item.pageNumber?` · 第 ${item.pageNumber} 页`:''}`,'text-button',()=>{dialog.close();onOpenDeck(item.deckId,item.pageNumber||1);}));else if(item.noteId)actions.append(button('查看原文','text-button',()=>{dialog.close();onOpenNote(item.noteId);}));
    form.append(actions);form.onsubmit=e=>{e.preventDefault();action(save,async()=>{await api(`/assignments/${item.id}`,{method:'PATCH',body:{title:title.value.trim(),details:detail.value.trim(),dueDate:due.value||null,dueTime:time.value||null,status:done.checked?'done':'open'}});dialog.close();await reload();},dialog);};dialog.append(form);dialog.showModal();
  }
  function openCourse(course) {
    const c=courseById(course.id)||course,dialog=makeDialog(c.name),palette=node('fieldset','cal-palette');palette.append(node('legend','','课程颜色'));
    COLORS.forEach((color,i)=>{const b=button('','cal-swatch');b.style.background=color;b.setAttribute('aria-label',`颜色 ${i+1}`);b.setAttribute('aria-pressed',String(safeColor(c.color)===color));b.onclick=()=>action(b,async()=>{const result=await api(`/courses/${c.id}/appearance`,{method:'PATCH',body:{color}});c.color=result.course?.color||color;palette.querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));draw();await onCoursesChanged();},dialog);palette.append(b);});dialog.append(palette);
    const knowledge=node('ul','course-knowledge');knowledge.hidden=true;dialog.append(knowledge);
    api(`/courses/${c.id}/knowledge`).then(({items})=>{if(!dialog.open)return;for(const item of items||[])knowledge.append(node('li','',item));knowledge.hidden=!knowledge.childElementCount;}).catch(()=>{});
    const materials=node('div','cal-materials');dialog.append(materials);
    let materialTimer;
    const readMaterials=async()=>{try{const response=await api(`/courses/${c.id}/materials`);if(!dialog.open)return;materials.replaceChildren();
      const status=response.discovery?.status,text={queued:'正在查找官方课程大纲…',running:'正在查找官方课程大纲…',not_found:'暂未找到公开的官方课程大纲。',error:'官方课程大纲暂时未能获取。'}[status];
      if(text){const notice=node('p','small muted',text);notice.setAttribute('role','status');materials.append(notice);}
      const official=(response.materials||[]).find(item=>{try{const url=new URL(item.sourceUrl);const domain=data.school?.officialDomain||'sustech.edu.cn';return url.protocol==='https:'&&(url.hostname===domain||url.hostname.endsWith('.'+domain));}catch{return false;}});
      if(official){const browse=node('a','secondary cal-material-view','浏览');browse.href=official.sourceUrl;browse.target='_blank';browse.rel='noopener';browse.setAttribute('aria-label','浏览官方课程大纲');materials.append(browse);}
      if(['queued','running'].includes(status))materialTimer=setTimeout(readMaterials,4000);
    }catch(e){if(dialog.open)formError(materials,e);}};
    dialog.addEventListener('close',()=>clearTimeout(materialTimer),{once:true});
    const reinforce=button('串联整理至今所有课次','secondary full',()=>{dialog.close();openReinforce(c);}),reinforceState=node('p','cal-reinforce-state small muted');reinforceState.hidden=true;reinforceState.setAttribute('role','status');dialog.append(reinforce,reinforceState);
    let statusTimer;const readReinforcement=async()=>{try{const response=await api(`/courses/${c.id}/reinforce`);if(!dialog.open)return;const task=response.reinforcement;reinforceState.hidden=!task;if(task){const active=['queued','running','uncertain'].includes(task.status);reinforce.disabled=active;reinforceState.textContent=task.error||task.stage||({completed:'课次关联已更新。',conflict:'笔记有新的修改，请查看后再试。'}[task.status]||'');if(['queued','running'].includes(task.status))statusTimer=setTimeout(readReinforcement,5000);}}catch(e){if(dialog.open){reinforceState.hidden=false;reinforceState.textContent=errorText(e);}}};dialog.addEventListener('close',()=>clearTimeout(statusTimer),{once:true});
    dialog.showModal();readReinforcement();readMaterials();
  }

  function openReinforce(c) {
    const dialog=makeDialog('串联整理至今所有课次'),count=getNotes().filter(n=>n.courseId===c.id&&n.status==='ready').length;
    dialog.append(node('p','cal-dialog-copy',`串联整理「${c.name}」${count?`的 ${count} 份笔记`:'已有的笔记'}。原文和历史版本会保留。`));
    const actions=node('div','form-actions'),start=button('加入队列','primary');start.onclick=()=>action(start,async()=>{const result=await api(`/courses/${c.id}/reinforce`,{method:'POST',body:{}}),task=result.reinforcement;dialog.close();toast(task?.error||task?.stage||result.message||(task?.status==='completed'?'课次关联已更新。':'已加入整理队列。'));await onCoursesChanged();openCourse(c);},dialog);actions.append(start,button('返回','text-button',()=>{dialog.close();openCourse(c);}));dialog.append(actions);dialog.showModal();
  }
  function startEditing(course=null,name='') {editing={courseId:course?.id||null,name:course?.name||name,pending:[]};draw();root.scrollIntoView({block:'start',behavior:'instant'});root.querySelector('.cal-select-cell')?.focus({preventScroll:true});}
  function stopEditing() {if(editing?.saving)return;editing=null;draw();}
  function pickSlot(day,period,week) {
    if(!editing||editing.saving)return;let added=false;
    if(!editing.pending.some(s=>s.weekday===day&&s.period===period)){editing.pending.push({weekday:day,period,weeks:allWeeks()});added=true;}
    draw();
    const dialog=makeDialog(`周${DAYS[day-1]} · ${PERIODS[period-1]}`,'cal-slot-dialog'),label=node('label','','课程名称'),name=node('input');name.value=editing.name;name.maxLength=120;name.placeholder='课程名称';name.required=true;name.disabled=!!editing.courseId;label.append(name);dialog.append(label);
    const updateName=()=>{if(editing)editing.name=name.value.trim();};name.oninput=updateName;
    const actions=node('div','cal-slot-actions'),more=button('继续为此课程选时间段','secondary',()=>{updateName();dialog.close();draw();root.querySelector('.cal-free:not(.cal-selected) button')?.focus({preventScroll:true});});
    const save=button('保存并退出','primary',()=>{updateName();saveEditing(save,dialog,()=>dialog.close());});actions.append(more,save);dialog.append(actions);
    const remove=button('取消这个时间段','text-button',()=>{if(editing)editing.pending=editing.pending.filter(s=>!(s.weekday===day&&s.period===period));dialog.close();draw();});dialog.append(remove);
    dialog.addEventListener('cancel',event=>{if(!event.defaultPrevented&&added&&editing){editing.pending=editing.pending.filter(s=>!(s.weekday===day&&s.period===period));draw();}});
    dialog.showModal();if(!editing.courseId)name.focus();
  }
  async function saveEditing(b,parent,after=()=>{}) {
    if(!editing?.pending.length||editing.saving)return;
    if(!editing.name.trim()){formError(parent,new Error('填一个课程名称。'));parent.querySelector('input')?.focus();return;}
    const draft=editing,dialog=parent.closest('dialog'),controls=[...parent.querySelectorAll('button,input')].filter(control=>control!==b).map(control=>[control,control.disabled]);draft.saving=true;if(dialog)dialog.dataset.locked='true';controls.forEach(([control])=>control.disabled=true);
    try{await action(b,async()=>{const body=draft.courseId?{courseId:draft.courseId,slots:draft.pending,baseRevision:data.revision}:{name:draft.name.trim(),slots:draft.pending,semesterStart:data.semesterStart};const result=await api(draft.courseId?'/timetable/slots/batch':'/timetable/courses',{method:'POST',body});if(editing!==draft)return;editing=null;after();if(result.slots&&result.courses){data=normalize(result);loadedAt=Date.now();draw();await onCoursesChanged();}else await reload();toast('课程时间已保存。');},parent);}finally{draft.saving=false;if(dialog)delete dialog.dataset.locked;controls.forEach(([control,disabled])=>control.disabled=disabled);}
  }
  picker.onchange=async()=>{
    const file=picker.files[0];if(!file||importing)return;
    if(file.size>3*1024*1024){toast('课表文件请控制在 3 MB 内。');return;}
    importing=true;const token=epoch,dialog=makeDialog('正在识别课表'),message=node('p','cal-import-progress','正在读取课程和上课时间…');dialog.append(message);dialog.showModal();
    try {const contentBase64=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('文件没有读取成功，请重新选择。'));reader.readAsDataURL(file);});const result=await api('/timetable/import',{method:'POST',body:{filename:file.name,contentBase64}});if(token!==epoch)return;dialog.close();openImportPreview(result);}
    catch(e){if(token!==epoch)return;message.textContent='';formError(dialog,e);dialog.querySelector('h2').textContent='课表还没有导入';dialog.append(button('重新选择文件','secondary',()=>{dialog.close();picker.value='';picker.click();}));}
    finally{importing=false;picker.value='';}
  };
  function openImportPreview(result) {
    const preview=structuredClone(result.preview),dialog=makeDialog('课表预览','cal-import-dialog');preview.courses||=[];preview.slots||=[];
    const info=node('p','cal-import-summary');dialog.append(info);
    if(result.warnings?.length){const warning=node('div','cal-import-warnings');for(const w of result.warnings)warning.append(node('p','',typeof w==='string'?w:(w.message||w.text||'')));dialog.append(warning);}
    const previewContainer=node('div','cal-preview-container'),form=node('form');
    const startLabel=node('label','cal-import-start','第一周周一'),startDate=node('input');startDate.type='date';startDate.value=preview.semesterStart||data?.semesterStart||'';startDate.onchange=()=>{preview.semesterStart=startDate.value||null;};startLabel.append(startDate);form.append(startLabel);
    const corrections=node('details','cal-import-corrections');corrections.append(node('summary','','修改课程名称'));
    const actions=node('div','form-actions'),save=button('保存课表','primary');save.type='submit';
    const drawPreview=()=>{
      info.textContent=`${preview.courses.length} 门课程 · ${preview.slots.length} 个时间段，点格子可修改。`;
      previewContainer.replaceChildren(weekPanel(1,{preview:true,source:preview,onCell:editCell}));
      corrections.querySelectorAll('label').forEach(el=>el.remove());
      for(const c of preview.courses){const label=node('label','cal-preview-course'),mark=node('span','cal-course-dot');mark.style.background=safeColor(c.color,preview.courses.indexOf(c));const input=node('input');input.value=c.name;input.maxLength=120;input.setAttribute('aria-label',`课程 ${c.name}`);input.oninput=()=>{c.name=input.value.trim();c.shortName='';};input.onchange=drawPreview;label.append(mark,input);corrections.append(label);}
      save.disabled=!preview.courses.length||!preview.slots.length;
    };
    function editCell(day,period,slot=null){
      const sheet=makeDialog('修改上课时间','cal-slot-dialog'),fields=node('form'),courseLabel=node('label','','课程'),choose=node('select');
      for(const c of preview.courses)choose.add(new Option(c.name,c.key));choose.add(new Option('新课程',''));choose.value=slot?.courseKey||'';choose.setAttribute('aria-label','课程');courseLabel.append(choose);
      const nameLabel=node('label','','课程名称'),name=node('input');name.placeholder='填写课程名称';name.maxLength=120;nameLabel.append(name);nameLabel.hidden=!!choose.value;choose.onchange=()=>{nameLabel.hidden=!!choose.value;if(!choose.value)name.focus();};
      const position=node('div','cal-preview-position'),dayLabel=node('label','','星期'),dayInput=node('select'),periodLabel=node('label','','节次'),periodInput=node('select');
      DAYS.forEach((v,i)=>dayInput.add(new Option(`周${v}`,String(i+1))));PERIODS.forEach((v,i)=>periodInput.add(new Option(v,String(i+1))));dayInput.setAttribute('aria-label','星期');periodInput.setAttribute('aria-label','节次');dayInput.value=String(day);periodInput.value=String(period);dayLabel.append(dayInput);periodLabel.append(periodInput);position.append(dayLabel,periodLabel);fields.append(courseLabel,nameLabel,position);
      const buttons=node('div','form-actions'),done=button('保存','primary');done.type='submit';buttons.append(done);
      if(slot)buttons.append(button('移除','text-button danger',()=>{preview.slots=preview.slots.filter(s=>s!==slot);sheet.close();drawPreview();}));fields.append(buttons);
      fields.onsubmit=e=>{e.preventDefault();let key=choose.value;if(!key){const title=name.value.trim();if(!title){formError(fields,new Error('填一个课程名称。'));return;}let course=preview.courses.find(c=>c.name===title);if(!course){course={key:crypto.randomUUID(),name:title,shortName:'',color:COLORS[preview.courses.length%COLORS.length],hotwords:''};preview.courses.push(course);}key=course.key;}
        const next={courseKey:key,weekday:Number(dayInput.value),period:Number(periodInput.value),weeks:slot?.weeks||Array.from({length:20},(_,i)=>i+1),location:slot?.location||''};
        if(slot)preview.slots=preview.slots.filter(s=>s!==slot);const match=preview.slots.find(s=>s.courseKey===key&&s.weekday===next.weekday&&s.period===next.period);if(match)match.weeks=[...new Set([...match.weeks,...next.weeks])].sort((a,b)=>a-b);else preview.slots.push(next);sheet.close();drawPreview();};
      sheet.append(fields);sheet.showModal();if(!choose.value)name.focus();
    }
    drawPreview();dialog.append(previewContainer);form.append(corrections);
    let mode=null;if(data?.slots?.length){const label=node('label','cal-import-mode'),check=node('input');check.type='checkbox';label.append(check,node('span','','替换原有排课'));mode=check;form.append(label);}
    actions.append(save,button('重新选择','text-button',()=>{dialog.close();picker.value='';picker.click();}));form.append(actions);
    form.onsubmit=e=>{e.preventDefault();if(preview.courses.some(c=>!c.name.trim())){formError(form,new Error('课程名称不能为空。'));return;}action(save,async()=>{await api('/timetable/apply',{method:'POST',body:{importId:result.importId,preview,mode:mode?.checked?'replace':'merge',baseRevision:data?.revision}});dialog.close();selectedWeek=null;await reload();toast('课表已导入。');},form);};dialog.append(form);dialog.showModal();
  }
  function reset() {epoch++;data=null;loadedAt=0;request=null;selectedWeek=null;editing=null;importing=false;picker.value='';for(const dialog of dialogs)dialog.close();root?.replaceChildren();root?.classList.remove('cal-editing');}
  return {render,reset,openSettingsOrImport};
}
