function node(tag, cls, text) { const value = document.createElement(tag); if (cls) value.className = cls; if (text !== undefined) value.textContent = text; return value; }
function button(text, cls = 'secondary') { const value = node('button', cls, text); value.type = 'button'; return value; }
function base64(blob) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('文件暂时读不到，请重新选择。')); reader.readAsDataURL(blob); }); }
const STATUS = {uploading: '待上传', queued: '准备页图', rendering: '正在生成页图', ready: '可以阅读', error: '处理未完成'};
const MATCHING = {pending: '正在识别课程和录音…', matching: '正在识别课程和录音…', uncertain: '暂未找到对应录音', unmatched: '暂无对应录音', error: '自动匹配未完成'};
const isMatching = value => ['pending', 'matching'].includes(value.matchingStatus);

function recordingWeek(date, semesterStart, weeks) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date || '') || !/^\d{4}-\d{2}-\d{2}$/.test(semesterStart || '')) return null;
  const value = Date.parse(`${date}T12:00:00Z`), start = Date.parse(`${semesterStart}T12:00:00Z`);
  if (!Number.isFinite(value) || !Number.isFinite(start) || new Date(value).toISOString().slice(0,10) !== date || new Date(start).toISOString().slice(0,10) !== semesterStart) return null;
  const monday = start - ((new Date(start).getUTCDay() + 6) % 7) * 86400000, week = Math.floor((value - monday) / 604800000) + 1;
  return week >= 1 && week <= weeks ? week : null;
}

function lessonLabel(note, courseName = '') {
  let title = String(note.title || note.sourceName || '课堂录音');
  if (note.recordingDate && title.endsWith(note.recordingDate)) title = title.slice(0, -note.recordingDate.length).replace(/[\s—–-]+$/, '');
  if (courseName && title.startsWith(courseName)) title = title.slice(courseName.length).replace(/^[\s—–-]+/, '');
  return title || note.title || note.sourceName || '课堂录音';
}

export function slideLibraryRows(decks, notes, courses, timetable = {}, coverage = {}) {
  const weeks = Math.max(1, Math.min(52, Number(timetable.weeks) || 20)), known = new Map(), rows = new Map();
  for (const note of [...(coverage.recordingWithoutSlides || []), ...notes]) known.set(note.id, {...known.get(note.id), ...note});
  for (const value of decks) for (const note of value.matchedNotes || []) known.set(note.id, {...known.get(note.id), ...note});
  const ensureRow = id => {
    const key = id || 'unassigned';
    if (!rows.has(key)) rows.set(key, {id: key, name: courses.find(c => c.id === id)?.name || (id ? '课程' : '未归课'), cells: new Map()});
    return rows.get(key);
  };
  const cellFor = (courseId, date) => {
    const row = ensureRow(courseId), week = recordingWeek(date, timetable.semesterStart, weeks), key = week || 'other';
    if (!row.cells.has(key)) row.cells.set(key, {notes: new Map(), decks: new Map()});
    return row.cells.get(key);
  };
  courses.forEach(course => ensureRow(course.id));
  for (const note of known.values()) cellFor(note.courseId, note.recordingDate).notes.set(note.id, note);
  for (const value of decks) {
    const ids = new Set([value.noteId, ...(value.noteIds || []), ...(value.matchedNotes || []).map(note => note.id)].filter(Boolean));
    if (!ids.size) { cellFor(value.courseId, null).decks.set(value.id, {deck: value, notes: []}); continue; }
    for (const id of ids) {
      const note = known.get(id) || {id, title: '课堂录音'}, cell = cellFor(note.courseId, note.recordingDate);
      cell.notes.set(id, note);
      if (!cell.decks.has(value.id)) cell.decks.set(value.id, {deck: value, notes: []});
      cell.decks.get(value.id).notes.push(note);
    }
  }
  const result = [...rows.values()];
  return {weeks, rows: result, hasOther: result.some(row => row.cells.has('other'))};
}

export function createSlides({root, api, toast, renderMarkdown, getCourses = () => [], getNotes = () => [], onOpenNote = () => {}}) {
  let epoch = 0, request = 0, timer = null, deck = null, current = 1, page = null, currentResult = null, imageMode = 'zh', uploading = false, batch = [], noteFilter = null;
  const heading = node('div', 'slides-heading');
  const uploadToggle = button('上传 PPT', 'primary'); heading.append(uploadToggle);
  const error = node('p', 'form-error'); error.hidden = true; error.setAttribute('role', 'alert');
  const upload = node('form', 'slide-upload'); upload.hidden = true;
  const file = node('input'); file.type = 'file'; file.accept = '.ppt,.pptx,.pdf'; file.multiple = true; file.hidden = true;
  const progress = node('p', 'muted small'); progress.setAttribute('aria-live', 'polite');
  const selectedFiles = node('div', 'slide-upload-files'); selectedFiles.setAttribute('aria-live', 'polite');
  upload.append(file, node('p','muted small','待上传最多 24 份 · PPT、PPTX、PDF · 每份 40 MB、120 页。'), selectedFiles, progress);
  file.onchange = () => {
    if (!file.files.length) return;
    const incoming = Array.from(file.files), added = [], retried = new Set();
    for (const value of incoming) {
      const duplicate = [...batch, ...added].find(item => item.file.name === value.name && item.file.size === value.size && item.file.lastModified === value.lastModified);
      if (duplicate) {
        if (duplicate.state === 'error') retried.add(duplicate);
        continue;
      }
      added.push({file: value, state: 'pending', message: '排队中'});
    }
    if (batch.filter(item => ['pending', 'uploading'].includes(item.state)).length + added.length + retried.size > 24) { report(new Error('待上传最多 24 份课件，请稍后继续添加。')); file.value = ''; return; }
    for (const item of retried) { item.state = 'pending'; item.message = '排队中'; }
    batch.push(...added);
    file.value = ''; error.hidden = true; upload.hidden = false; renderBatch();
    void runUpload();
  };
  const library = node('div', 'slide-library');
  const reader = node('section', 'slide-reader'); reader.hidden = true;
  const top = node('div', 'slide-reader-top'), back = button('全部 PPT', 'text-button'), title = node('h2'), translateAll = button('翻译整份', 'secondary'), translationProgress = node('span', 'muted small'), download = node('a', 'text-button', '原稿');
  translationProgress.setAttribute('aria-live', 'polite'); top.append(back, title, translateAll, translationProgress, download);
  const stage = node('div', 'slide-stage'); stage.setAttribute('aria-live', 'polite');
  const layout = node('div', 'slide-layout');
  const content = node('div', 'slide-page-content'), controls = node('div', 'slide-page-controls'), prev = button('上一页'), pageSelect = node('select', 'slide-page-select'), next = button('下一页');
  pageSelect.setAttribute('aria-label', '选择课件页码'); controls.append(prev, pageSelect, next);
  const pageMain = node('div', 'slide-page-main'), visual = node('div', 'slide-page-visual'), pictureModes = node('div', 'slide-picture-modes'), showOriginal = button('原文', 'reader-tab'), showChinese = button('中文', 'reader-tab active');
  pictureModes.setAttribute('role', 'group'); pictureModes.setAttribute('aria-label', '页图语言'); pictureModes.append(showOriginal, showChinese);
  const pictureLink = node('a', 'slide-picture-link'), picture = node('img', 'slide-picture'); pictureLink.target = '_blank'; pictureLink.rel = 'noopener'; pictureLink.title = '打开完整页图'; pictureLink.append(picture);
  const result = node('div', 'markdown-body slide-explanation'); result.setAttribute('aria-live', 'polite');
  visual.append(pictureModes, pictureLink); pageMain.append(visual, result); content.append(controls, pageMain); layout.append(content); reader.append(top, stage, layout);
  root.replaceChildren(heading, error, upload, library, reader);

  function report(e) { error.textContent = e.message || String(e); error.hidden = false; }
  function stop() { clearTimeout(timer); timer = null; }
  function plan(action, delay = 2500) { stop(); if (!root.closest('[hidden]')) timer = setTimeout(() => { if (!root.closest('[hidden]')) action().catch(report); }, delay); }
  function preventUnload(event) { event.preventDefault(); event.returnValue = ''; }
  function renderBatch() {
    selectedFiles.replaceChildren();
    let position = 0;
    for (const item of batch) {
      const row = node('div', `slide-upload-file ${item.state}`), text = node('span', 'slide-upload-file-text');
      const message = item.state === 'pending' ? `排队中 · 第 ${++position} 份` : item.message;
      text.append(node('strong', '', item.file.name), node('small', 'muted', message)); row.append(text);
      if (item.state === 'error') { const retry = button('重新排队', 'text-button'); retry.disabled = batch.filter(value => ['pending', 'uploading'].includes(value.state)).length >= 24; retry.onclick = () => { item.state = 'pending'; item.message = '排队中'; renderBatch(); void runUpload(); }; row.append(retry); }
      selectedFiles.append(row);
    }
    const pending = batch.filter(item => item.state === 'pending').length, complete = batch.filter(item => item.state === 'complete').length, failed = batch.filter(item => item.state === 'error').length;
    progress.textContent = [uploading ? '正在上传' : '', pending ? `${pending} 份排队中` : '', complete ? `${complete} 份已保存` : '', failed ? `${failed} 份未完成` : ''].filter(Boolean).join(' · ');
    uploadToggle.textContent = uploading || pending ? '继续添加 PPT' : '上传 PPT';
    if (uploading || pending) window.addEventListener('beforeunload', preventUnload);
    else window.removeEventListener('beforeunload', preventUnload);
  }
  function translationText(value) {
    if (!value) return '';
    const completed = Number(value.completed) || 0, pending = Number(value.pending) || 0, failed = Number(value.failed) || 0, total = Number(value.total) || deck?.pageCount || 0;
    if (pending) return `翻译中 ${completed}/${total}${failed ? ` · ${failed} 页待核对` : ''}`;
    if (failed) return `${completed}/${total} 页完成 · ${failed} 页待核对`;
    if (total && completed >= total) return `已翻译 ${completed}/${total}`;
    return completed ? `已翻译 ${completed}/${total}` : '';
  }
  function setDeck(value) {
    deck = value; title.textContent = value.filename; download.href = `/api/slides/${value.id}/original`; download.hidden = value.status === 'uploading';
    translationProgress.textContent = translationText(value.translation);
    const translation = value.translation, handled = (Number(translation?.completed) || 0) + (Number(translation?.failed) || 0), total = Number(translation?.total) || 0;
    translateAll.disabled = value.status !== 'ready' || Boolean(translation?.pending) || Boolean(total && handled >= total);
  }
  function deckEntry(value, notes = []) {
    const entry = node('div', 'slide-cell-deck'), read = button(value.filename, 'slide-file-link');
    const pages = notes.flatMap(note => note.pageNumbers || []).filter(page => Number.isInteger(page) && page > 0);
    read.onclick = () => open(value.id, pages.length ? Math.min(...pages) : 1).catch(report); entry.append(read);
    const meta = node('div', 'slide-file-meta'), status = value.status === 'ready' && MATCHING[value.matchingStatus] ? MATCHING[value.matchingStatus] : STATUS[value.status] || value.status;
    meta.append(node('span', 'muted small', `${value.pageCount ? value.pageCount + ' 页 · ' : ''}${status}`));
    if (value.status !== 'uploading') { const original = node('a', 'small', '原稿'); original.href = `/api/slides/${encodeURIComponent(value.id)}/original`; original.target = '_blank'; original.rel = 'noopener'; original.setAttribute('aria-label', `打开原稿：${value.filename}`); meta.append(original); }
    entry.append(meta);
    if (notes.length) {
      const links = node('details', 'slide-associations'); links.append(node('summary', '', '调整关联'));
      for (const note of notes) {
        const row = node('div', 'slide-association'), remove = button('移除关联', 'text-button');
        row.append(node('span', '', note.title || note.sourceName || note.recordingDate || '课堂录音'), remove);
        remove.setAttribute('aria-label', `移除 ${value.filename} 与 ${note.title || '课堂录音'} 的关联`);
        remove.onclick = async () => {
          if (remove.disabled) return; const currentEpoch = epoch; remove.disabled = true;
          try { await api(`/slides/${encodeURIComponent(value.id)}/notes/${encodeURIComponent(note.id)}`, {method: 'DELETE'}); if (currentEpoch !== epoch) return; toast('已移除关联，原文件保留。'); await load(noteFilter); }
          catch(e) { if (currentEpoch === epoch) { report(e); remove.disabled = false; } }
        };
        links.append(row);
      }
      entry.append(links);
    }
    return entry;
  }
  function renderMatrix(decks, coverage, timetable, position) {
    const courses = [...(timetable.courses || [])];
    for (const course of getCourses()) if (!courses.some(value => value.id === course.id)) courses.push(course);
    const matrix = slideLibraryRows(decks, getNotes(), courses, timetable, coverage);
    if (!matrix.rows.length) { library.append(node('p', 'muted slide-empty', '还没有 PPT。')); return; }
    const scroll = node('div', 'slide-matrix-scroll'), table = node('table', 'slide-matrix'), thead = node('thead'), head = node('tr');
    scroll.tabIndex = 0; scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', '按课程和教学周查看 PPT，可横向和纵向滚动');
    const corner = node('th', '', '课程'); corner.scope = 'col'; head.append(corner);
    const columns = [...Array.from({length: matrix.weeks}, (_, index) => index + 1), ...(matrix.hasOther ? ['other'] : [])];
    for (const week of columns) { const th = node('th', '', week === 'other' ? '其他' : `第 ${week} 周`); th.scope = 'col'; head.append(th); }
    thead.append(head); const tbody = node('tbody');
    for (const row of matrix.rows) {
      const tr = node('tr'), course = node('th', '', row.name); course.scope = 'row'; tr.append(course);
      for (const week of columns) {
        const td = node('td'), cell = row.cells.get(week);
        if (cell) {
          const linked = new Set([...cell.decks.values()].flatMap(value => value.notes.map(note => note.id)));
          for (const note of [...cell.notes.values()].sort((a,b) => String(a.recordingDate || '').localeCompare(String(b.recordingDate || '')))) {
            const lesson = node('div', 'slide-cell-lesson'), openNote = button(lessonLabel(note, row.name), 'slide-note-link');
            openNote.onclick = () => onOpenNote(note.id); lesson.append(openNote);
            const meta = [note.recordingDate, !linked.has(note.id) ? (coverage.matchingPending ? '匹配中' : '暂无 PPT') : ''].filter(Boolean).join(' · ');
            if (meta) lesson.append(node('small', 'muted', meta)); td.append(lesson);
          }
          for (const value of cell.decks.values()) td.append(deckEntry(value.deck, value.notes));
        }
        if (!cell || !td.childElementCount) td.append(node('span', 'slide-cell-empty muted', '—'));
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(thead, tbody); scroll.append(table); library.append(scroll);
    if (position) { scroll.scrollLeft = position.left; scroll.scrollTop = position.top; }
  }
  async function load(filter = null) {
    const token = ++request, currentEpoch = epoch, previous = library.querySelector('.slide-matrix-scroll'), position = !filter && !noteFilter && previous ? {left: previous.scrollLeft, top: previous.scrollTop} : null;
    stop(); noteFilter = filter; error.hidden = true;
    const [data, coverage, timetableResponse] = await Promise.all([api(filter ? `/slides/notes/${filter.id}` : '/slides'), api('/slides/coverage'), api('/timetable')]); if (token !== request || currentEpoch !== epoch) return;
    deck = null; page = null; reader.hidden = true; library.hidden = false; library.replaceChildren();
    const decks = data.decks || [], timetable = timetableResponse.timetable || timetableResponse;
    if (filter) {
      const top = node('div', 'slide-group-heading'), all = button('全部 PPT', 'text-button'); all.onclick = () => load().catch(report); top.append(node('h2', '', filter.title || '配套 PPT'), all); library.append(top);
      const note = getNotes().find(value => value.id === filter.id) || filter;
      decks.forEach(value => library.append(deckEntry(value, [{...note, ...(value.matchedNotes || []).find(item => item.id === filter.id)}])));
      if (!decks.length) library.append(node('p', 'muted', (coverage.recordingWithoutSlides || []).some(value => value.id === filter.id) ? '这节录音暂无对应 PPT。' : '正在识别 PPT 与录音的对应关系…'));
    } else renderMatrix(decks, coverage, timetable, position);
    if (decks.some(value => ['queued', 'rendering'].includes(value.status) || isMatching(value)) || filter && !decks.length) plan(() => load(filter), 4000);
  }
  async function open(id, selected = 1) {
    stop(); const token = ++request, currentEpoch = epoch; error.hidden = true; upload.hidden = true;
    const data = await api(`/slides/${id}`); if (token !== request || currentEpoch !== epoch) return;
    imageMode = 'zh'; currentResult = null; setDeck(data.deck); library.hidden = true; reader.hidden = false; stage.replaceChildren();
    layout.hidden = deck.status !== 'ready'; stage.hidden = deck.status === 'ready';
    if (deck.status !== 'ready') {
      stage.append(node('p', '', deck.error || (deck.status === 'uploading' ? '上传还没完成。重新选择这份原文件即可继续。' : `${STATUS[deck.status] || '正在处理'}，可以先离开，稍后再回来。`)));
      if (deck.status === 'error') { const retry = button('重试'); retry.onclick = async () => { retry.disabled = true; try { await api(`/slides/${id}/retry`, {method: 'POST', body: {}}); await open(id); } catch(e) { report(e); retry.disabled = false; } }; stage.append(retry); }
      if (deck.status === 'uploading') { const resume = button('继续上传'); resume.onclick = () => {openUpload(); file.click();}; stage.append(resume); }
      if (['queued', 'rendering'].includes(deck.status)) plan(() => open(id));
      return;
    }
    current = Math.max(1, Math.min(selected, deck.pageCount)); pageSelect.replaceChildren();
    for (const value of data.pages) { const text = String(value.title || '').replace(/\s+/g, ' ').trim(), short = text.length > 28 ? text.slice(0, 28) + '…' : text; pageSelect.add(new Option(`${value.number} / ${deck.pageCount} · ${short || '未命名页'}`, String(value.number))); }
    await selectPage(current);
  }
  function isCurrentTranslation(value) { return value?.mode === 'translate' && value.language === 'zh' && value.version === 'slide-zh-1'; }
  function updatePicture() {
    if (!deck) return;
    const originalUrl = `/api/slides/${deck.id}/pages/${current}/image`, translatedUrl = isCurrentTranslation(currentResult) ? currentResult.imageUrl : '';
    const useChinese = imageMode === 'zh' && translatedUrl;
    picture.src = useChinese ? translatedUrl : originalUrl; pictureLink.href = picture.src;
    picture.alt = `${useChinese ? '中文' : '原文'}课件第 ${current} 页`;
    showOriginal.classList.toggle('active', imageMode === 'original'); showChinese.classList.toggle('active', imageMode === 'zh');
    showOriginal.setAttribute('aria-pressed', String(imageMode === 'original')); showChinese.setAttribute('aria-pressed', String(imageMode === 'zh'));
  }
  function showResult(value) {
    currentResult = value || null; result.replaceChildren(); updatePicture();
    if (['completed', 'partial'].includes(value?.status)) {
      if (!value.imageUrl) result.append(node('p', 'muted small', '中文页图暂时不可用，左侧显示原文。'));
      if (value.status === 'partial') result.append(node('p', 'slide-result-note', value.error || '部分文字保留原文，可切换原文核对。'));
      const body = node('div'); result.append(body); if (renderMarkdown) renderMarkdown(value.content || '', body); else body.textContent = value.content || '';
      if (value.status === 'partial' && value.canRetry) { const retry = button('重新排版'); retry.onclick = async () => { retry.disabled = true; try { await api(`/slides/${deck.id}/results/${value.id}/retry`, {method: 'POST', body: {}}); await selectPage(current); } catch(e) { report(e); retry.disabled = false; } }; result.append(retry); }
    }
    else if (['error', 'uncertain'].includes(value?.status)) {
      result.append(node('p', '', value.error || '这页翻译未完成。'));
      if (value.canRetry) { const retry = button('重试这一页'); retry.onclick = async () => { retry.disabled = true; try { await api(`/slides/${deck.id}/results/${value.id}/retry`, {method: 'POST', body: {}}); await selectPage(current); } catch(e) { report(e); retry.disabled = false; } }; result.append(retry); }
    } else result.append(node('p', 'muted', '正在翻译这一页…'));
  }
  async function selectPage(number) {
    if (!deck || deck.status !== 'ready') return;
    stop(); current = number; page = null; currentResult = null; const id = deck.id, token = ++request, currentEpoch = epoch;
    pageSelect.value = String(number); prev.disabled = number <= 1; next.disabled = number >= deck.pageCount;
    updatePicture(); result.replaceChildren(node('p', 'muted', '正在打开…'));
    const response = await api(`/slides/${id}/pages/${number}`); if (token !== request || currentEpoch !== epoch) return;
    page = response.page;
    let value = page.results.find(isCurrentTranslation);
    if (!value) {
      const generated = await api(`/slides/${id}/pages/${number}/generate`, {method: 'POST', body: {mode: 'translate', language: 'zh'}}); if (token !== request || currentEpoch !== epoch) return;
      value = isCurrentTranslation(generated.result) ? generated.result : {mode: 'translate', language: 'zh', version: 'slide-zh-1', status: 'queued'};
    }
    showResult(value);
    if (['queued', 'running'].includes(value?.status) || deck.translation?.pending) plan(() => poll(id, number, token, currentEpoch));
  }
  async function poll(id, number, token, currentEpoch) {
    const responses = await Promise.all([api(`/slides/${id}/pages/${number}`), api(`/slides/${id}`)]); if (token !== request || currentEpoch !== epoch) return;
    page = responses[0].page; setDeck(responses[1].deck); const value = page.results.find(isCurrentTranslation); showResult(value);
    if ((value && ['queued', 'running'].includes(value.status)) || deck.translation?.pending) plan(() => poll(id, number, token, currentEpoch));
  }
  function changeImageMode(value) { imageMode = value; updatePicture(); }
  showOriginal.onclick = () => changeImageMode('original'); showChinese.onclick = () => changeImageMode('zh');
  translateAll.onclick = async () => {
    if (!deck || translateAll.disabled) return;
    translateAll.disabled = true; error.hidden = true;
    try {
      const id = deck.id, response = await api(`/slides/${id}/generate`, {method: 'POST', body: {mode: 'translate', language: 'zh'}});
      setDeck({...deck, translation: {completed: response.completed, pending: response.queued, failed: 0, total: response.total}}); await selectPage(current);
    } catch (e) { report(e); translateAll.disabled = false; }
  };
  prev.onclick = () => selectPage(current - 1).catch(report); pageSelect.onchange = () => selectPage(Number(pageSelect.value)).catch(report); next.onclick = () => selectPage(current + 1).catch(report);
  back.onclick = () => load(noteFilter).catch(report);
  function openUpload() { upload.hidden = false; renderBatch(); }
  uploadToggle.onclick = () => {openUpload();file.click();};
  async function runUpload() {
    if (uploading || !batch.some(item => item.state === 'pending')) return;
    const currentEpoch = epoch; uploading = true; error.hidden = true; renderBatch();
    let completed = 0;
    try {
      let item;
      while (currentEpoch === epoch && (item = batch.find(value => value.state === 'pending'))) {
        const selected = item.file; item.state = 'uploading'; item.message = '正在读取…'; renderBatch();
        try {
          if (!/\.(pptx?|pdf)$/i.test(selected.name) || !selected.size || selected.size > 40 * 1024 * 1024) throw new Error('请选择非空、40 MB 以内的 PPT、PPTX 或 PDF。');
          const digest = await crypto.subtle.digest('SHA-256', await selected.arrayBuffer());
          if (currentEpoch !== epoch) return;
          item.sha256 = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
          if (batch.some(other => other !== item && other.state === 'complete' && other.sha256 === item.sha256)) { item.state = 'complete'; item.message = '重复文件，已保留一份'; continue; }
          const task = await api('/slides/uploads', {method: 'POST', body: {filename: selected.name, size: selected.size, sha256: item.sha256}});
          if (currentEpoch !== epoch) return;
          if (task.deck.status === 'uploading') {
            const size = task.chunkBytes, count = Math.ceil(selected.size / size), received = new Set(task.receivedChunks);
            for (let index = 0; index < count; index++) {
              if (currentEpoch !== epoch) return;
              if (!received.has(index)) {
                const contentBase64 = await base64(selected.slice(index * size, (index + 1) * size));
                if (currentEpoch !== epoch) return;
                await api(`/slides/${task.deck.id}/chunks/${index}`, {method: 'PUT', body: {contentBase64}});
              }
              if (currentEpoch !== epoch) return;
              item.message = `正在上传 ${Math.round((index + 1) / count * 100)}%`; renderBatch();
            }
            if (currentEpoch !== epoch) return;
            await api(`/slides/${task.deck.id}/uploaded`, {method: 'POST', body: {}});
          }
          if (currentEpoch !== epoch) return;
          item.state = 'complete'; item.message = '已保存，后台自动识别'; completed++;
        } catch(e) { if (currentEpoch !== epoch) return; item.state = 'error'; item.message = e.message || '上传未完成，可重新排队。'; }
        finally { if (currentEpoch === epoch) renderBatch(); }
      }
    } finally {
      if (currentEpoch === epoch) {
        uploading = false; renderBatch();
        if (completed) toast(`已保存 ${completed} 份课件。`);
        // Leave an open reader and its polling intact while the upload queue drains.
        if (!root.closest('[hidden]') && reader.hidden) load(noteFilter).catch(report);
      }
    }
  }
  upload.onsubmit = event => event.preventDefault();
  return {load, open, openForNote(note) { return load({id: note.id, title: note.title || note.sourceName}); }, openUpload, stop, reset() { epoch++; request++; stop(); deck = null; page = null; currentResult = null; imageMode = 'zh'; uploading = false; batch = []; noteFilter = null; upload.reset(); upload.hidden = true; reader.hidden = true; library.replaceChildren(); selectedFiles.replaceChildren(); pageSelect.replaceChildren(); error.hidden = true; progress.textContent = ''; translationProgress.textContent = ''; window.removeEventListener('beforeunload', preventUnload); uploadToggle.textContent = '上传 PPT'; picture.removeAttribute('src'); result.replaceChildren(); }};
}
