const weekdays=['一','二','三','四','五','六','日'];
const periods=['1–2','3–4','5–6','7–8','9–10'];
const iso=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
const add=(d,n)=>new Date(d.getFullYear(),d.getMonth(),d.getDate()+n,12);
const monday=d=>add(d,-((d.getDay()+6)%7));
const make=(tag,text='',cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};

export async function pickLesson(api,{courseId='',date=''}={}){
  const response=await api('/timetable'),data=response.timetable||response;
  let week=monday(date?new Date(`${date}T12:00:00`):new Date()),chosen=null;
  const dialog=make('dialog','','lesson-dialog'),header=make('div','','dialog-heading'),title=make('h2','选择课次'),close=make('button','×','icon-button');close.type='button';close.setAttribute('aria-label','关闭');header.append(title,close);
  const nav=make('div','','lesson-week'),prev=make('button','上一周','text-button'),label=make('span'),next=make('button','下一周','text-button');nav.append(prev,label,next);
  const grid=make('div','','lesson-grid'),selection=make('p','','small'),confirm=make('button','确认课次','primary full');confirm.disabled=true;
  dialog.append(header,nav,grid,selection,confirm);document.body.append(dialog);
  function draw(){
    label.textContent=`${iso(week)} — ${iso(add(week,6))}`;grid.replaceChildren(make('span','节次'));
    for(let d=0;d<7;d++)grid.append(make('span',`周${weekdays[d]}\n${add(week,d).getMonth()+1}/${add(week,d).getDate()}`,'lesson-day'));
    const semester=data.semesterStart?monday(new Date(`${data.semesterStart}T12:00:00`)):null;
    const weekNumber=semester?Math.floor((Date.UTC(week.getFullYear(),week.getMonth(),week.getDate())-Date.UTC(semester.getFullYear(),semester.getMonth(),semester.getDate()))/604800000)+1:null;
    for(let p=1;p<=5;p++){
      grid.append(make('span',periods[p-1],'lesson-period'));
      for(let d=1;d<=7;d++){
        const cell=make('div','','lesson-cell');
        for(const slot of data.slots||[]){
          if(slot.weekday!==d||slot.period!==p||(weekNumber!==null&&slot.weeks?.length&&!slot.weeks.includes(weekNumber)))continue;
          const course=(data.courses||[]).find(c=>c.id===slot.courseId);if(!course)continue;
          const day=iso(add(week,d-1)),b=make('button',course.shortName||course.name.slice(0,4),'lesson-choice');
          b.type='button';b.setAttribute('aria-label',`${course.name} ${day} ${periods[p-1]}节`);b.setAttribute('aria-pressed',String(chosen?.slot===slot.id&&chosen?.date===day));
          b.onclick=()=>{chosen={courseId:course.id,date:day,slot:slot.id};selection.textContent=`${course.name} · ${day} · ${periods[p-1]}节`;confirm.disabled=false;draw();};cell.append(b);
        }grid.append(cell);
      }
    }
    if(!(data.slots||[]).length)selection.textContent='先在“我的课程”导入课表。';
  }
  prev.onclick=()=>{week=add(week,-7);draw();};next.onclick=()=>{week=add(week,7);draw();};close.onclick=()=>dialog.close();draw();dialog.showModal();
  return new Promise(resolve=>{let result=null;confirm.onclick=()=>{result=chosen;dialog.close();};dialog.addEventListener('close',()=>{dialog.remove();resolve(result);},{once:true});});
}

export function noteTitle(note,courses){
  const course=courses.find(c=>c.id===note.courseId)?.name||'';
  let topic=(note.title||note.sourceName||'课堂笔记').trim();
  if(course&&topic.startsWith(course))topic=topic.slice(course.length).replace(/^[\s·—–\-：:]+/,'');
  topic=topic.replace(/[\s·—–\-]*\d{4}-\d{2}-\d{2}$/,'').trim();
  return [course,topic,note.recordingDate].filter(Boolean).join('—');
}
