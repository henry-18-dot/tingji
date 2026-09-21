const CATEGORIES = [
  ['math', '数学哲学'], ['life', '人生哲学'], ['novel', '小说'],
  ['drama', '戏剧'], ['film', '电影'], ['game', '游戏'], ['absurd', '胡言乱语'],
];
// 原创欢迎语，不引用或冒充作品台词。
const WELCOMES = {
  math: ['无穷大坐在最后一排，假装点过名。', '今天的误差，也许只是另一组坐标。', '先留一个未知数，给下午。', '空集也有一个位置。', '有些相遇，暂时不满足交换律。'],
  life: ['那把空椅子，也在认真听雨。', '今天不必为每朵云命名。', '时间在走廊里，替你慢了一拍。', '把没想明白的事，先放在窗边。', '你来过，今天就多了一个方向。'],
  novel: ['邮差送来一封明天寄出的信。', '他翻过一页，窗外便换了季节。', '那座城的钟，只在有人告别时走。', '书里的灯还亮着，你先坐。', '这一页的风，刚好吹到这里。'],
  drama: ['幕还没开，月亮先忘了台词。', '请让那张椅子，也说两句。', '旁白今天请假，你来决定下一幕。', '台下有一只认真鼓掌的影子。', '停顿也算一句完整的台词。'],
  film: ['镜头外，有人把黄昏又倒放了一遍。', '这场雨，没有安排替身。', '字幕走完了，窗外的树还在演。', '今天的长镜头，从你坐下开始。', '把这一秒留给背景里的风。'],
  game: ['你获得了：一格没有用途的背包。', '存档点在这里，风会替你站岗。', '这位路人，还没有决定自己的支线。', '任务更新：找到昨天掉落的星期三。', '隐藏成就：和椅子和平相处。'],
  absurd: ['向日葵今天赊了 50 阳光。', '一只括号正在申请单独居住。', '电梯把第六层藏进了口袋。', '周三是一种低速旋转的蔬菜。', '橡皮擦说，今天轮到它记仇。', '有一颗土豆，坚持使用弧度制。'],
};
function node(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }

export function greeting(name, categories = [], now = new Date()) {
  const pool = categories.flatMap(c => WELCOMES[c] || []);
  if (pool.length) {
    const seed = `${name}:${now.getFullYear()}-${now.getMonth()}-${now.getDate()}:${Math.floor(now.getHours() / 6)}`;
    let hash = 0; for (const c of seed) hash = ((hash << 5) - hash + c.charCodeAt(0)) | 0;
    return pool[(hash >>> 0) % pool.length];
  }
  const h = now.getHours(), word = h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 14 ? '中午好' : h < 18 ? '下午好' : '晚上好';
  return `${word}，${name || '同学'}。`;
}

export function createSettings({root, api, toast, onChange, onSchoolChanged=()=>{}}) {
  let saved = null, epoch = 0, loaded = false, attachments = [];
  const feedback = node('section', 'settings-section'); feedback.append(node('h2', '', '向开发者反馈'));
  const feedbackDetails = node('details'); feedbackDetails.append(node('summary', '', '写点反馈'));
  const feedbackForm = node('form'), feedbackLabel = node('label', '', '哪里可以更好用？'), feedbackText = node('textarea');
  feedbackText.id = 'feedback-content'; feedbackText.rows = 4; feedbackText.maxLength = 4000; feedbackText.required = true;
  feedbackText.placeholder = '遇到的问题，或你想要的功能'; feedbackLabel.append(feedbackText);
  const feedbackButton = node('button', 'secondary', '提交反馈'); feedbackButton.type = 'submit';
  const filesLabel=node('label','','图片或文档'),files=node('input'),fileList=node('div','feedback-files');files.type='file';files.multiple=true;files.accept='.png,.jpg,.jpeg,.webp,.gif,.pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.txt,.md,.csv';filesLabel.append(files,node('small','muted','最多 4 个，合计 3 MB。'));
  const clearFiles=()=>{attachments.forEach(a=>{if(a.preview)URL.revokeObjectURL(a.preview);});attachments=[];files.value='';fileList.replaceChildren();};
  const drawFiles=()=>{fileList.replaceChildren();for(const item of attachments){const row=node('div','feedback-file');if(item.preview){const img=node('img');img.src=item.preview;img.alt=item.file.name;row.append(img);}row.append(node('span','',item.file.name));const remove=node('button','text-button','移除');remove.type='button';remove.setAttribute('aria-label',`移除 ${item.file.name}`);remove.onclick=()=>{if(item.preview)URL.revokeObjectURL(item.preview);attachments=attachments.filter(x=>x!==item);drawFiles();};row.append(remove);fileList.append(row);}};
  files.onchange=()=>{const added=[...files.files].filter(f=>!attachments.some(a=>a.file.name===f.name&&a.file.size===f.size));if(attachments.length+added.length>4||attachments.reduce((n,a)=>n+a.file.size,0)+added.reduce((n,f)=>n+f.size,0)>3*1024*1024){report(Error('最多附 4 个文件，合计 3 MB。'));files.value='';return;}attachments.push(...added.map(file=>({file,preview:/^image\/(png|jpeg|gif|webp)$/.test(file.type)?URL.createObjectURL(file):null})));files.value='';drawFiles();};
  const encode=file=>new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve({filename:file.name,contentBase64:String(r.result).split(',')[1]});r.onerror=()=>reject(Error('附件读取失败。'));r.readAsDataURL(file);});
  feedbackForm.append(feedbackLabel,filesLabel,fileList,feedbackButton); feedbackDetails.append(feedbackForm); feedback.append(feedbackDetails);
  const welcome = node('section', 'settings-section welcome-settings'); welcome.append(node('h2', '', '个性化欢迎词'), node('p', 'muted small', '可多选。留空则按时间问好。'));
  const welcomeForm = node('form'), categories = node('div', 'welcome-categories');
  for (const [key, title] of CATEGORIES) { const l = node('label'), input = node('input'); input.type = 'checkbox'; input.name = 'greetingCategory'; input.value = key; l.append(input, node('span', '', title)); categories.append(l); }
  const welcomeButton = node('button', 'secondary', '保存欢迎词'); welcomeButton.type = 'submit';
  const preview = node('p', 'welcome-preview'); preview.setAttribute('aria-live', 'polite');
  welcomeForm.append(categories, preview, welcomeButton); welcome.append(welcomeForm);
  const error = node('p', 'form-error'); error.hidden = true; error.setAttribute('role', 'alert');
  const service=node('section','settings-section');service.append(node('h2','','服务与本地部署'),node('p','','本站服务器今后可能关闭。请及时下载需要保留的笔记和原文，也可以在自己的电脑上运行听记。'));
  const repository=node('a','','GitHub 项目'),localGuide=node('a','','让本地 AI 帮你部署');repository.href='https://github.com/henry-18-dot/tingji';repository.target='_blank';repository.rel='noopener';localGuide.href='./guide/local-ai.html';localGuide.target='_blank';localGuide.rel='noopener';const links=node('div','form-actions');links.append(repository,localGuide);service.append(links);
  const school=node('section','settings-section');school.append(node('h2','','学校'));const schoolName=node('p','muted','正在读取…'),changeSchool=node('button','secondary','更换学校');changeSchool.type='button';school.append(schoolName,changeSchool);changeSchool.onclick=()=>openSchool();
  root.replaceChildren(error,school,feedback,welcome,service);
  async function openSchool(){
    const dialog=node('dialog','school-dialog'),heading=node('div','dialog-heading'),close=node('button','icon-button','×');close.type='button';close.setAttribute('aria-label','关闭');close.onclick=()=>dialog.close();heading.append(node('h2','','学校与校历'),close);const form=node('form'),status=node('p','small muted');status.setAttribute('role','status');dialog.append(heading,form,status);document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove(),{once:true});dialog.showModal();
    try{const response=await api('/school');if(!dialog.open)return;const value=response.school||response,fields={};for(const [key,label,type,example] of [['schoolName','学校','text','学校全名'],['officialDomain','官网域名','text','例如 example.edu.cn'],['calendarUrl','官方校历链接','url','https://…'],['syllabusUrl','官方大纲目录链接（选填）','url','https://…'],['semesterStart','第一周周一','date',''],['semesterEnd','学期结束','date','']]){const l=node('label','',label),input=node('input');input.type=type;input.value=value[key]||'';input.placeholder=example;input.required=['schoolName','officialDomain'].includes(key);fields[key]=input;l.append(input);form.append(l);}
      fields.officialDomain.addEventListener('input',()=>{if(fields.officialDomain.value.trim().toLowerCase()!==String(value.officialDomain||'').toLowerCase()){for(const key of ['calendarUrl','syllabusUrl','semesterStart','semesterEnd'])if(fields[key].value===(value[key]||''))fields[key].value='';}});
      const actions=node('div','form-actions'),save=node('button','primary','保存'),read=node('button','secondary','读取官网校历');save.type='submit';read.type='button';actions.append(save,read);form.append(actions);
      const body=()=>Object.fromEntries(Object.entries(fields).map(([key,input])=>[key,input.value.trim()||null]));
      const perform=async refresh=>{save.disabled=read.disabled=true;try{await api('/school',{method:'PATCH',body:body()});if(refresh){const r=await api('/school/refresh',{method:'POST',body:{}});status.textContent=r.message||'已读取校历。';for(const key of ['semesterStart','semesterEnd'])if(r.school?.[key])fields[key].value=r.school[key];}else{dialog.close();toast('学校设置已保存。');}schoolName.textContent=fields.schoolName.value;onSchoolChanged();}catch(e){status.className='form-error';status.textContent=e.message;}finally{save.disabled=read.disabled=false;}};
      form.onsubmit=e=>{e.preventDefault();if(form.reportValidity())perform(false);};read.onclick=()=>{if(form.reportValidity())perform(true);};
    }catch(e){status.textContent=e.message;status.className='form-error';}
  }
  const allButtons = () => [...root.querySelectorAll('button')];
  allButtons().forEach(b => b.disabled = true);
  function report(e) { error.textContent = e.message; error.hidden = false; }
  function setState(data) {
    saved = data; loaded = true;
    categories.querySelectorAll('input').forEach(i => i.checked = (data.greetingCategories || []).includes(i.value));
    preview.textContent = greeting('同学', data.greetingCategories || []);
    allButtons().forEach(b => b.disabled = false); onChange(data);
  }
  const selected = () => [...categories.querySelectorAll('input:checked')].map(i => i.value);
  categories.onchange = () => preview.textContent = greeting('同学', selected());
  async function update(button, body) {
    if (!loaded || button.disabled) return; button.disabled = true; error.hidden = true; const token = epoch;
    try { const r = await api('/settings', {method: 'PATCH', body}); if (token !== epoch) return; saved = r.settings; onChange(saved); toast('已保存。'); }
    catch(e) { if (token === epoch) report(e); }
    finally { if (token === epoch) button.disabled = false; }
  }
  welcomeForm.onsubmit = e => { e.preventDefault(); update(welcomeButton, {greetingCategories: selected()}); };
  feedbackForm.onsubmit = async e => {
    e.preventDefault(); if (feedbackButton.disabled) return; const content = feedbackText.value.trim(); if (!content) return;
    feedbackButton.disabled = true; error.hidden = true; const token = epoch;
    try { await api('/feedback', {method: 'POST', body: {content,attachments:await Promise.all(attachments.map(a=>encode(a.file)))}}); if (token !== epoch) return; feedbackText.value = ''; clearFiles(); feedbackDetails.open = false; toast('反馈已收到，谢谢。'); }
    catch(e) { if (token === epoch) report(e); }
    finally { if (token === epoch) feedbackButton.disabled = false; }
  };
  return {
    async load() { const token = epoch; try { const r = await api('/settings'); api('/school').then(value=>{if(token===epoch)schoolName.textContent=(value.school||value).schoolName||'未设置学校';}).catch(()=>{if(token===epoch)schoolName.textContent='学校尚未设置';}); if (token === epoch) { error.hidden = true; setState(r.settings); } } catch(e) { if (token === epoch) { report(e); toast('设置暂时未加载，请稍后重新打开设置。'); } } },
    reset() { epoch++; saved = null; loaded = false; welcomeForm.reset(); feedbackForm.reset(); clearFiles(); preview.textContent = ''; allButtons().forEach(b => b.disabled = true); error.hidden = true; },
  };
}
