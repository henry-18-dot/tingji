// Course instances and ordered recording groups. All data changes use the host API.
export function installWorkflows(h) {
  const $ = (s, p = document) => p.querySelector(s);
  const all = (s, p = document) => [...p.querySelectorAll(s)];
  const node = (tag, cls = '', text = '') => { const e = document.createElement(tag); e.className = cls; e.textContent = text; return e; };
  const button = (text, fn, cls = 'button secondary') => { const e = node('button', cls, text); e.type = 'button'; e.addEventListener('click', () => Promise.resolve().then(fn).catch(error)); return e; };
  const uuid = () => crypto.randomUUID();
  const s = () => h.state;
  const busy = n => ['transcribing', 'summarizing', 'processing', 'queued'].includes(n.status);
  const hasSummary = n => !!(n.summary || n.hasSummary);
  const size = bytes => bytes >= 1024 ** 3 ? (bytes / 1024 ** 3).toFixed(2) + ' GB' : (bytes / 1024 ** 2).toFixed(1) + ' MB';
  const duration = seconds => Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds / 60) + ' 分钟' : '时长待读取';
  const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const monday = () => { const d = new Date(); d.setDate(d.getDate() - (d.getDay() + 6) % 7); return iso(d); };
  let mode = '', selected = new Set(), libraryFilter = 'pending', weekDate = monday(), lesson = null, schedule = null;
  let imports = [], mergeOrder = [], working = false, poll = null, imageUrl = null;
  const dialog = node('dialog', 'dialog workflow-dialog'); dialog.id = 'workflow-dialog'; dialog.setAttribute('aria-labelledby', 'workflow-title');
  const heading = node('div', 'dialog-heading'); const title = node('h2', '', '录音库'); title.id = 'workflow-title';
  const close = button('关闭', () => dialog.close(), 'button secondary compact'); heading.append(title, close);
  const content = node('div', 'workflow-content'); const message = node('p', 'field-error'); message.hidden = true; message.setAttribute('role', 'alert');
  dialog.append(heading, content, message); document.body.append(dialog);
  dialog.addEventListener('cancel', e => { if (working) e.preventDefault(); });
  dialog.addEventListener('close', () => { clearTimeout(poll); if (imageUrl) { URL.revokeObjectURL(imageUrl); imageUrl = null; } });
  function error(e) { const text = e?.message || String(e); if (dialog.open) { message.textContent = text; message.hidden = false; } else h.toast(text, true); }
  function show(name, nextMode) { mode = nextMode; title.textContent = name; content.replaceChildren(); message.hidden = true; clearTimeout(poll); if (!dialog.open) dialog.showModal(); h.closeSidebar(); }
  async function run(fn) { if (working) return; working = true; close.disabled = true; message.hidden = true; try { return await fn(); } finally { working = false; close.disabled = false; } }
  function requireOnline() { if (!s().connected) throw new Error('连接电脑后可处理录音；已保存的笔记仍可离线阅读。'); }
  async function refresh() { await h.refresh(); }
  async function accepted(note) { const n = note.note || note; await h.accept(n); return n; }
  function field(label, input) { const e = node('label', 'workflow-field'); e.append(node('span', '', label), input); return e; }
  function input(value = '', type = 'text') { const e = node('input', 'text-input'); e.type = type; e.value = value; return e; }
  function select(values, value) { const e = node('select', 'text-input'); values.forEach(([v, name]) => { const o = node('option', '', name); o.value = v; e.append(o); }); e.value = value; return e; }
  function toolbar(...children) { const e = node('div', 'workflow-toolbar'); e.append(...children); return e; }
  function noteLabel(n) { return n.title || n.sourceName || '未命名录音'; }
  function options() { return { audioUploadConsent: true, cloudUploadConsent: true, autoSummarize: true, retryFailed: true, asrMode: 'standard', template: 'lecture', language: 'auto' }; }
  async function ready() { requireOnline(); if (!s().settings.asrConfigured || !s().settings.deepseekConfigured || !s().settings.tosConfigured) { h.openSettings(); throw new Error('请先配置语音、笔记和私有音频存储服务。'); } }

  async function openImport(files, targetLesson = null) {
    if (working) { h.toast('正在保存上一批录音，完成后可继续导入'); return; }
    const list = Array.from(files || []); if (!list.length) return;
    if (list.length > 100) throw new Error('每次最多导入 100 个文件，请分批导入。');
    imports = list.map(file => ({ file, note: null, error: '' })); lesson = targetLesson;
    show('正在接收录音', 'import');
    const progress = node('p', 'workflow-total', '文件将逐一保存，电脑收到后自动排队。'); progress.setAttribute('role', 'status');
    const rows = node('div', 'workflow-files'), actions = toolbar();
    content.append(progress, rows, actions);
    const render = () => {
      rows.replaceChildren(); imports.forEach(item => {
        const row = node('div', 'workflow-file'), info = node('div');
        const stored = item.note && (s().notes.find(n => n.id === item.note.id) || item.note);
        const remote = stored && (!s().syncClient || (!stored.localOnly && !stored.audioDirty && !stored.dirty));
        info.append(node('strong', '', item.file.name), node('small', item.error ? 'field-error' : 'field-help', item.error || (stored ? remote ? '电脑已收到，自动排队处理中' : '已存到此设备，连接电脑后自动上传' : size(item.file.size) + ' · 等待保存')));
        row.append(info); rows.append(row);
      });
    };
    const save = async () => {
      if (working) return;
      actions.replaceChildren();
      await run(async () => {
        for (const item of imports) {
          if (item.note && (!lesson || item.associated)) continue;
          progress.textContent = '正在保存 ' + item.file.name; item.error = ''; render();
          try {
            if (!item.file.size || item.file.size > 1024 ** 3) throw new Error('文件为空或超过 1 GB，请检查原文件');
            if (!item.note) item.note = await h.uploadFile(item.file, {});
            if (!item.note?.id) throw new Error('保存未完成，请重试此文件');
            if (lesson && s().connected) {
              item.note = await h.ensureRemote(item.note);
              await accepted(await h.request('/api/notes/' + item.note.id + '/lesson', { method: 'POST', body: { lesson } }));
              item.associated = true;
            }
          } catch (e) { item.error = e.message; }
          render();
        }
      });
      const failed = imports.filter(i => i.error).length;
      progress.textContent = failed ? failed + ' 个文件保存失败，原因见下方；其余文件继续自动处理。' : '录音已保存。电脑收到后自动转录并整理，可关闭此窗口。';
      title.textContent = failed ? '部分录音需要处理' : '录音已接收';
      actions.replaceChildren();
      if (failed) actions.append(button('重试保存失败项', save, 'button primary'));
      actions.append(button('完成', () => dialog.close(), 'button secondary'));
      render();
      await h.syncPendingNotes?.();
      if (dialog.open && mode === 'import') render();
    };
    render(); await save();
  }

  async function openLibrary(filter = 'all') { if (s().connected) await refresh(); libraryFilter = filter === 'pending' ? 'all' : filter; selected.clear(); renderLibrary(); }
  function libraryItems() {
    return s().notes.filter(n => !n.trashedAt && (libraryFilter === 'notes' ? !n.groupParentId && hasSummary(n) : !n.sourceNoteIds && (n.audioStored || n.audioAvailable || n.sourceName) && (libraryFilter !== 'archived' || n.audioArchivedAt)));
  }
  function renderLibrary() {
    const merging = libraryFilter === 'notes';
    show(merging ? '合并笔记' : '录音库', 'library');
    if (!merging) {
      const filter = select([['all', '全部录音'], ['archived', '已归档录音']], libraryFilter); filter.id = 'library-filter';
      filter.addEventListener('change', () => { libraryFilter = filter.value; selected.clear(); renderLibrary(); });
      content.append(toolbar(field('显示', filter), button('刷新状态', async () => { if (s().connected) await refresh(); renderLibrary(); }, 'text-button')));
      content.append(node('p', 'field-help', '这里管理应用保存的音频。iPad“语音备忘录”和“文件”中的原文件需在对应应用删除。'));
    }
    const items = libraryItems(), selectAll = input('', 'checkbox'); selectAll.id = 'library-select-all';
    const label = node('label', 'workflow-check'); label.append(selectAll, node('span', '', '全选当前列表')); content.append(label);
    const rows = node('div', 'workflow-library'), total = node('p', 'workflow-total'); total.id = 'library-total'; content.append(rows, total);
    const updateTotal = () => { total.textContent = '已选 ' + selected.size + ' 项'; selectAll.checked = items.length > 0 && items.every(n => selected.has(n.id)); selectAll.indeterminate = selected.size > 0 && !selectAll.checked; };
    if (!items.length) rows.append(node('p', 'plain-empty', merging ? '整理完成的笔记会显示在这里。' : '暂无录音。导入文件或从 Tailscale 发到电脑后会出现在这里。'));
    items.forEach(n => {
      const check = input('', 'checkbox'); check.checked = selected.has(n.id); check.dataset.noteId = n.id;
      check.addEventListener('change', () => { check.checked ? selected.add(n.id) : selected.delete(n.id); updateTotal(); });
      const row = node('label', 'workflow-check'), text = node('span'); text.append(node('strong', '', noteLabel(n)));
      const meta = merging ? n.classDate || '尚未放入日历' : (n.audioStored ? '此设备有缓存' : '此设备无缓存') + ' · ' + (n.audioAvailable && !n.audioDeletedAt ? '电脑有原件' : '电脑无原件') + (n.audioArchivedAt ? ' · 已归档' : '') + (busy(n) ? ' · 自动处理中' : n.error ? ' · ' + n.error : '');
      text.append(node('small', 'field-help', meta)); row.append(check, text); rows.append(row);
    });
    selectAll.addEventListener('change', () => { selected.clear(); if (selectAll.checked) items.forEach(n => selected.add(n.id)); all('input[data-note-id]', rows).forEach(c => c.checked = selected.has(c.dataset.noteId)); updateTotal(); }); updateTotal();
    if (merging) {
      content.append(toolbar(button('选择顺序并拼接', () => { if (selected.size < 2) throw new Error('请至少选择两份笔记'); mergeOrder = items.filter(n => selected.has(n.id)); renderMerge(); }, 'button primary')));
      return;
    }
    const scope = select([['device', '此设备的应用音频缓存'], ['computer', '电脑原始录音'], ['cache', '电脑转换缓存'], ['cloud', '云端临时音频']], s().localBrowser ? 'computer' : 'device'); scope.id = 'delete-scope';
    content.append(toolbar(button('归档所选录音', () => archiveSelection(), 'button secondary'), field('删除位置', scope), button('删除所选录音', () => deleteSelection(scope.value), 'button secondary')));
    content.append(node('p', 'field-help', s().connected ? '删除音频保留笔记。处理中或尚未转录完成的电脑原件会保留，并显示原因。' : '电脑未连接：可清理此设备缓存；电脑原件管理需连接后使用。'));
  }
  function chosenIds() { if (!selected.size) throw new Error('请先选择记录'); return [...selected]; }
  async function archiveSelection() {
    const noteIds = chosenIds(); requireOnline();
    await run(async () => {
      const data = await h.request('/api/audio/batch-archive', { method: 'POST', body: { noteIds, archived: true } });
      await refresh(); renderLibrary();
      const failed = (data.items || []).filter(i => i.state !== 'completed');
      h.toast(failed.length ? '部分录音未归档，请查看原因' : '所选录音已归档');
      for (const item of failed) content.append(node('p', 'field-error', noteLabel(s().notes.find(n => n.id === item.noteId) || {}) + '：' + item.reason));
    });
  }
  async function retryProcessing(noteId) {
    await ready();
    const batch = await h.request('/api/batches', { method: 'POST', body: { noteIds: [noteId], options: options(), operationId: uuid() } });
    await refresh(); showBatch(batch);
  }
  async function deleteSelection(scope) {
    const list = chosenIds();
    const execute = async () => run(async () => {
      const results = [];
      if (scope === 'device') {
        for (const id of list) {
          if (!s().notes.find(n => n.id === id)?.audioStored) { results.push({ state: 'completed', noteId: id, reason: '此设备没有音频缓存' }); continue; }
          try { if (!s().syncClient) throw new Error('此浏览器没有录音缓存'); await s().syncClient.deleteLocalAudio(id); results.push({ state: 'completed', noteId: id }); }
          catch (e) { results.push({ state: 'failed', noteId: id, reason: e.message }); }
        }
      } else { requireOnline(); results.push(...(await h.request('/api/audio/batch-delete', { method: 'POST', body: { noteIds: list, scope } })).items); }
      await refresh(); show('清理结果', 'delete-result');
      content.append(node('p', '', results.filter(r => r.state === 'completed').length + ' 项完成 · ' + results.filter(r => r.state !== 'completed').length + ' 项未删除'));
      for (const item of results) { const n = s().notes.find(n => n.id === item.noteId); content.append(node('p', item.state === 'completed' ? 'field-help' : 'field-error', noteLabel(n || {}) + '：' + (item.reason || '此位置已清理'))); }
      content.append(button('返回录音库', () => openLibrary('archived')));
    });
    if (scope === 'device' || scope === 'cache') return execute();
    show('删除所选' + (scope === 'cloud' ? '云端副本' : '电脑原始录音'), 'delete-confirm');
    content.append(node('p', '', list.length + ' 个文件将从' + (scope === 'cloud' ? '云端' : '此电脑主库') + '删除。笔记保留，其他存储位置不受影响。'));
    list.forEach(id => content.append(node('p', 'field-help', noteLabel(s().notes.find(n => n.id === id) || {}))));
    content.append(button('删除这 ' + list.length + ' 个文件', execute, 'button primary'));
  }
  function renderMerge() {
    show('调整拼接顺序', 'merge');
    const name = input('合并笔记'); name.id = 'merge-title'; content.append(field('新稿名称', name));
    const rows = node('div', 'workflow-files'); content.append(rows);
    const render = () => { rows.replaceChildren(); mergeOrder.forEach((n, i) => {
      const up = button('上移', () => { [mergeOrder[i - 1], mergeOrder[i]] = [mergeOrder[i], mergeOrder[i - 1]]; render(); }, 'text-button'); up.disabled = i === 0;
      const down = button('下移', () => { [mergeOrder[i + 1], mergeOrder[i]] = [mergeOrder[i], mergeOrder[i + 1]]; render(); }, 'text-button'); down.disabled = i === mergeOrder.length - 1;
      const row = node('div', 'workflow-file'); row.append(node('strong', '', (i + 1) + '. ' + noteLabel(n)), toolbar(up, down)); rows.append(row);
    }); }; render();
    const operationId = uuid(); content.append(node('p', 'field-help', '按顺序拼接成新稿，原笔记保留；此操作不调用 AI。'), button('生成可编辑新稿', () => run(async () => {
      const n = await accepted(await h.request('/api/notes/merge', { method: 'POST', body: { noteIds: mergeOrder.map(n => n.id), title: name.value, operationId } }));
      dialog.close(); await h.openNote(n.id, n); h.startEditor('summary');
    }), 'button primary'));
  }
  async function openBatches() { requireOnline(); const data = await h.request('/api/batches'); show('处理队列', 'batches'); if (!data.batches.length) content.append(node('p', 'plain-empty', '暂无批处理')); data.batches.forEach(b => content.append(button(b.items.length + ' 条 · ' + ({ complete: '已结束', paused: '已暂停', queued: '等待处理', running: '处理中' }[b.state] || b.state), () => showBatch(b), 'workflow-batch-row'))); }
  function showBatch(batch) {
    show('批量整理进度', 'batch');
    const completed = batch.items.filter(i => i.state === 'completed').length;
    content.append(node('p', 'workflow-total', completed + ' / ' + batch.items.length + ' 项完成'));
    const labels = { waiting: '等待处理', dispatching: '正在提交', running: '处理中', completed: '笔记完成', skipped: '已跳过', failed: '待处理' };
    batch.items.forEach(i => { const row = node('div', 'workflow-file'); const info = node('div'); info.append(node('strong', '', i.title), node('small', i.state === 'failed' ? 'field-error' : 'field-help', (labels[i.state] || i.state) + (i.reason ? ' · ' + i.reason : ''))); row.append(info, button('打开', async () => { dialog.close(); await h.openNote(i.noteId); }, 'text-button')); content.append(row); });
    if (batch.state !== 'complete') content.append(button(batch.state === 'paused' ? '继续未提交项目' : '暂停未提交项目', async () => showBatch(await h.request('/api/batches/' + batch.id + '/control', { method: 'POST', body: { paused: batch.state !== 'paused' } }))));
    content.append(node('p', 'field-help', '已提交的云任务可能继续运行。失败项目保留已有结果；打开对应笔记可查看原因并继续未完成部分。'));
    if (batch.state !== 'complete') poll = setTimeout(async () => { if (dialog.open && mode === 'batch') try { showBatch(await h.request('/api/batches/' + batch.id)); } catch (e) { error(e); } }, 2000);
  }

  let calendarSelection = '', calendarRequest = 0;
  const calendarDate = () => new Date(weekDate + 'T12:00:00');
  async function assignCalendarNote(id, target) {
    requireOnline();
    await run(async () => {
      await accepted(await h.request('/api/notes/' + id + '/lesson', { method: 'POST', body: { lesson: target } }));
      calendarSelection = ''; await refresh(); await openCalendar(); h.toast('笔记已放到 ' + (target.date || target.classDate));
    });
  }
  function calendarNote(n) {
    const chip = button(noteLabel(n), () => { calendarSelection = calendarSelection === n.id ? '' : n.id; openCalendar().catch(error); }, 'calendar-note' + (calendarSelection === n.id ? ' is-selected' : ''));
    chip.draggable = true; chip.dataset.calendarNote = n.id; chip.setAttribute('aria-pressed', String(calendarSelection === n.id));
    chip.addEventListener('dragstart', event => { event.dataTransfer.setData('text/x-tingji-note', n.id); event.dataTransfer.effectAllowed = 'move'; });
    return chip;
  }
  function allowNoteDrop(element, target) {
    element.addEventListener('dragover', event => { if (event.dataTransfer.types.includes('text/x-tingji-note')) { event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = 'move'; element.classList.add('is-drop-target'); } });
    element.addEventListener('dragleave', () => element.classList.remove('is-drop-target'));
    element.addEventListener('drop', event => { const id = event.dataTransfer.getData('text/x-tingji-note'); if (!id) return; event.preventDefault(); event.stopPropagation(); element.classList.remove('is-drop-target'); assignCalendarNote(id, target).catch(error); });
  }
  async function openCalendar() {
    requireOnline(); const requestId = ++calendarRequest;
    const current = calendarDate(), monthStart = new Date(current.getFullYear(), current.getMonth(), 1, 12);
    const gridStart = new Date(monthStart); gridStart.setDate(gridStart.getDate() - (gridStart.getDay() + 6) % 7);
    const weeks = await Promise.all(Array.from({ length: 6 }, (_, i) => { const day = new Date(gridStart); day.setDate(day.getDate() + i * 7); return h.request('/api/timetable/week?start=' + iso(day)); }));
    schedule = await h.request('/api/timetable'); if (requestId !== calendarRequest) return;
    const lessons = [...new Map(weeks.flatMap(w => w.lessons || []).map(l => [l.lessonId + ':' + l.classDate, l])).values()];
    show('课程日历', 'calendar');
    const monthInput = input(iso(monthStart).slice(0, 7), 'month'); monthInput.id = 'calendar-month';
    monthInput.addEventListener('change', () => { if (monthInput.value) { weekDate = monthInput.value + '-01'; openCalendar().catch(error); } });
    const step = direction => { const next = new Date(current.getFullYear(), current.getMonth() + direction, 1, 12); weekDate = iso(next); return openCalendar(); };
    const more = node('details', 'calendar-more'), summary = node('summary', 'button secondary compact', '⋯'); summary.setAttribute('aria-label', '课程日历更多操作');
    more.append(summary, button('合并笔记', () => openLibrary('notes'), 'text-button'), button('导入 / 编辑课表', () => editSchedule(schedule), 'text-button'));
    content.append(toolbar(button('上个月', () => step(-1), 'text-button'), field('月份', monthInput), button('下个月', () => step(1), 'text-button'), button('今天', () => { weekDate = iso(new Date()); return openCalendar(); }, 'text-button'), more));
    const picked = s().notes.find(n => n.id === calendarSelection);
    const hint = node('p', 'field-help', picked ? '已选“' + noteLabel(picked) + '”：点击日期或课程放入。' : '将笔记拖到日期或课程；iPad 上先点笔记，再点日期。'); hint.setAttribute('role', 'status'); content.append(hint);
    if (picked) content.append(toolbar(button('打开所选笔记', async () => { dialog.close(); await h.openNote(picked.id); }, 'text-button'), button('取消选择', () => { calendarSelection = ''; return openCalendar(); }, 'text-button')));
    const grid = node('div', 'course-calendar'); content.append(grid);
    ['一', '二', '三', '四', '五', '六', '日'].forEach(day => grid.append(node('div', 'calendar-weekday', '周' + day)));
    const completed = s().notes.filter(n => !n.groupParentId && !n.trashedAt && hasSummary(n));
    const movedDate = n => lessons.find(l => l.lessonId === n.lessonId && l.courseId === n.courseId && l.classDate === n.classDate)?.date || n.classDate;
    for (let i = 0; i < 42; i++) {
      const date = new Date(gridStart); date.setDate(date.getDate() + i); const day = iso(date);
      const cell = node('section', 'calendar-day' + (date.getMonth() !== current.getMonth() ? ' is-outside-month' : '') + (day === iso(new Date()) ? ' is-today' : '')); cell.dataset.calendarDate = day;
      const dayButton = button(String(date.getDate()), () => calendarSelection ? assignCalendarNote(calendarSelection, { classDate: day }) : chooseAssociation({ classDate: day }), 'calendar-day-number'); dayButton.setAttribute('aria-label', day + '，放入笔记'); cell.append(dayButton); allowNoteDrop(cell, { classDate: day });
      lessons.filter(l => (l.date || l.classDate) === day).forEach(l => {
        l.courseName = l.courseName || l.name || schedule.courses.find(c => c.id === l.courseId)?.name || '课程';
        const card = button('', () => calendarSelection ? assignCalendarNote(calendarSelection, l) : openLesson(l), 'course-cell');
        card.append(node('strong', '', l.courseName), node('small', '', l.cancelled ? '停课' : (l.periods || []).join('–') + ' 节')); allowNoteDrop(card, l); cell.append(card);
      });
      completed.filter(n => movedDate(n) === day).forEach(n => cell.append(calendarNote(n))); grid.append(cell);
    }
    const unassigned = completed.filter(n => !n.classDate);
    const tray = node('section', 'calendar-note-tray'); tray.append(node('h3', '', '待放入日历'));
    if (!unassigned.length) tray.append(node('p', 'field-help', '未关联日期的整理文档会出现在这里。'));
    unassigned.forEach(n => tray.append(calendarNote(n))); content.append(tray); dialog.scrollTop = 0;
  }
  async function openLesson(l) {
    lesson = l; show(l.courseName + ' · ' + (l.date || l.classDate), 'lesson');
    const notes = s().notes.filter(n => n.lessonId === l.lessonId && n.courseId === l.courseId && n.classDate === l.classDate && !n.trashedAt && !n.groupParentId);
    const remote = (l.notes || []).map(n => typeof n === 'string' ? s().notes.find(v => v.id === n) : n).filter(Boolean);
    const joined = [...new Map([...remote, ...notes].map(n => [n.id, n])).values()];
    if (!joined.length) content.append(node('p', 'plain-empty', '此课次暂无笔记'));
    joined.forEach(n => { const row = node('div', 'workflow-file'); row.append(node('strong', '', noteLabel(n)), button('打开笔记', async () => { dialog.close(); await h.openNote(n.id); }, 'text-button')); content.append(row); });
    content.append(toolbar(button('导入这一课的录音', () => { const picker = input('', 'file'); picker.multiple = true; picker.accept = 'audio/*,video/*,.m4a,.mp3,.wav'; picker.addEventListener('change', () => openImport(picker.files, l).catch(error)); picker.click(); }, 'button primary'), button('关联已有笔记', () => chooseAssociation(l)), button('本次调课 / 停课', () => editException(l)), button('返回课表', openCalendar, 'text-button')));
    if (joined.filter(hasSummary).length >= 2) { const more = node('details', 'calendar-more'); more.append(node('summary', 'button secondary compact', '⋯'), button('合并本课笔记', () => { mergeOrder = joined.filter(hasSummary); renderMerge(); }, 'text-button')); content.append(more); }
  }
  function chooseAssociation(l) {
    show('放入 ' + (l.date || l.classDate), 'associate');
    const candidates = s().notes.filter(n => !n.groupParentId && !n.trashedAt && hasSummary(n));
    if (!candidates.length) content.append(node('p', 'plain-empty', '尚无整理完成的笔记。收到的录音会自动转录，完成后可放入日历。'));
    candidates.forEach(n => content.append(button(noteLabel(n) + (n.classDate ? ' · ' + n.classDate : ''), () => assignCalendarNote(n.id, l), 'workflow-batch-row')));
    content.append(button('返回日历', openCalendar, 'text-button'));
  }
  function editException(l) {
    show('调整本次课', 'exception');
    const action = select([['move', '调到其他日期'], ['cancel', '本次停课']], 'move'); const nextDate = input(l.date || l.classDate, 'date');
    content.append(field('操作', action), field('调整后日期', nextDate), node('p', 'field-help', '只改变本次安排；已经关联的笔记仍属于这次课。'));
    content.append(button('保存本次调整', () => run(async () => {
      const current = await h.request('/api/timetable');
      current.exceptions = [...(current.exceptions || []).filter(e => !(e.lessonId === l.lessonId && e.classDate === l.classDate)), { lessonId: l.lessonId, classDate: l.classDate, action: action.value, ...(action.value === 'move' ? { newDate: nextDate.value } : {}) }];
      await h.request('/api/timetable', { method: 'POST', body: { schedule: current, baseRevision: current.revision } }); await openCalendar();
    }), 'button primary'));
  }
  function editSchedule(draft, source = null) {
    schedule = structuredClone(draft); show('编辑课表', 'schedule-editor');
    const file = input('', 'file'); file.accept = 'image/*,.pdf'; file.id = 'schedule-file';
    const importButton = button('识别图片 / PDF', () => run(async () => {
      requireOnline(); const chosen = file.files[0]; if (!chosen) throw new Error('请先选择课表图片或 PDF');
      if (chosen.size > 20 * 1024 ** 2) throw new Error('课表文件超过 20 MB');
      const status = node('p', 'field-help', '正在读取并识别课表，请保持页面打开…'); status.setAttribute('role', 'status'); content.append(status);
      const result = await h.request('/api/timetable/import', { method: 'POST', headers: { 'Content-Type': chosen.type || 'application/octet-stream', 'X-File-Name': encodeURIComponent(chosen.name) }, body: chosen });
      if (imageUrl) URL.revokeObjectURL(imageUrl); imageUrl = chosen.type.startsWith('image/') ? URL.createObjectURL(chosen) : null;
      editSchedule({ ...result.schedule, revision: draft.revision }, result.source);
    })); importButton.id = 'schedule-recognize';
    content.append(toolbar(field('课表截图、照片或 PDF', file), importButton), node('p', 'field-help', '本机提取文字，配置了 DeepSeek 时将课表文字发送给它识别结构并按用量计费；保存前请核对。'));
    if (imageUrl) { const preview = node('img', 'schedule-preview'); preview.src = imageUrl; preview.alt = '导入的原课表'; content.append(preview); }
    if (source) {
      if (source.error || source.warnings?.length) content.append(node('p', 'field-help', [source.error, ...(source.warnings || [])].filter(Boolean).join('；')));
      const details = node('details', 'workflow-ocr'); details.append(node('summary', '', '查看提取文字')); const text = node('pre', '', source.rawText || '没有提取到文字，可手工添加课程。'); details.append(text); content.append(details);
    }
    const term = input(schedule.term || '新学期'), firstWeek = input(schedule.week1Start || '', 'date'); term.id = 'schedule-term'; firstWeek.id = 'schedule-first-week';
    content.append(toolbar(field('学期', term), field('第 1 教学周周一', firstWeek)));
    const rows = [];
    (schedule.courses || []).forEach(c => (c.lessons || []).forEach(l => rows.push({ course: c, lesson: l })));
    const list = node('div', 'schedule-editor-rows'); content.append(list);
    function addRow(c = { id: uuid(), name: '' }, l = { id: uuid(), weekday: 1, periods: [1, 2], weeks: [1, 16] }) {
      const wrapper = node('fieldset', 'schedule-edit-row'); const legend = node('legend', '', '课程'); wrapper.append(legend);
      const name = input(c.name), weekday = select([1, 2, 3, 4, 5, 6, 7].map(n => [String(n), '周' + '一二三四五六日'[n - 1]]), String(l.weekday));
      const periods = input((l.periods || []).join(',')), weeks = input((l.weeks || [1, 16]).join('-'));
      const parity = select([['', '每周'], ['odd', '单周'], ['even', '双周']], l.parity || ''); const kind = input(l.kind || '');
      const dates = l.dates?.length ? input(l.dates.join(', ')) : null;
      const remove = button('移除此行', () => wrapper.remove(), 'text-button');
      wrapper.append(field('课程名称', name), field('星期', weekday), field('节次（例如 7,8）', periods), field('周次范围（例如 1-16）', weeks), field('单双周', parity), field('类型（理论 / 实验）', kind), remove);
      if (dates) {
        [weekday, weeks, parity].forEach(e => e.closest('label').hidden = true);
        wrapper.insertBefore(field('指定日期（YYYY-MM-DD，逗号分隔）', dates), remove);
      }
      wrapper.read = () => {
        const p = periods.value.split(/[,，、\s]+/).filter(Boolean).map(Number); const w = weeks.value.split(/[-—~至,，\s]+/).filter(Boolean).map(Number);
        return { course: { ...c, name: name.value.trim() }, lesson: { ...l, ...(dates ? { dates: dates.value.split(/[,，\s]+/).filter(Boolean) } : { weekday: Number(weekday.value), weeks: w.length === 1 ? [w[0], w[0]] : w, parity: parity.value || undefined }), periods: p, kind: kind.value.trim() } };
      }; list.append(wrapper);
    }
    rows.forEach(r => addRow(r.course, r.lesson));
    content.append(button('添加课程行', () => addRow(), 'text-button'));
    const periodsText = input(Object.entries(schedule.periods || {}).map(([id, v]) => id + '=' + (v.start || '') + '-' + (v.end || '')).join('; '));
    const times = node('details', 'workflow-ocr'); times.append(node('summary', '', '节次时刻（可选）'), field('例如 7=16:20-17:10; 8=17:20-18:10', periodsText)); content.append(times);
    content.append(node('p', 'field-help', '修改后的课表需点击保存才生效。周一起始日缺失时保留“周次待确认”；识别失败的课程可手工补齐。'));
    const save = button('保存课表', () => run(async () => {
      const courses = new Map(); all('fieldset', list).forEach(e => { const r = e.read(); if (!r.course.name) throw new Error('请填写每一行的课程名称'); if (!courses.has(r.course.id)) courses.set(r.course.id, { ...r.course, lessons: [] }); courses.get(r.course.id).lessons.push(r.lesson); });
      const periods = {}; periodsText.value.split(/[;；]+/).map(s => s.trim()).filter(Boolean).forEach(text => { const match = /^(\d+)\s*=\s*(\d{1,2}:\d{2})?\s*-\s*(\d{1,2}:\d{2})?$/.exec(text); if (!match) throw new Error('节次时间格式不正确'); periods[match[1]] = { ...(match[2] ? { start: match[2] } : {}), ...(match[3] ? { end: match[3] } : {}) }; });
      const data = { ...schedule, term: term.value.trim(), week1Start: firstWeek.value || null, courses: [...courses.values()], periods };
      await h.request('/api/timetable', { method: 'POST', body: { schedule: data, baseRevision: schedule.revision || 0 } }); h.toast('课表已保存'); await openCalendar();
    }), 'button primary'); save.id = 'schedule-save'; content.append(save);
  }

  const sources = node('section', 'group-sources'); sources.hidden = true; sources.id = 'group-sources'; $('#note-source').parentElement.after(sources);
  let sourceSignature = '';
  function renderSources(note) {
    const refs = note?.sources || note?.sourceNotes || [];
    if (!note?.sourceNoteIds || note.groupKind === 'manual_merge' || note.groupTask?.state === 'merged') { sources.hidden = true; sourceSignature = ''; return; }
    sources.hidden = false;
    const signature = JSON.stringify([note.id, note.status, note.groupTask, refs]); if (signature === sourceSignature) return; sourceSignature = signature;
    sources.replaceChildren(node('strong', '', note.sourceNoteIds.length + ' 段录音 · 统一笔记'));
    const actual = refs.length ? refs : note.sourceNoteIds.map(id => s().notes.find(n => n.id === id) || { id, sourceName: '录音片段' });
    actual.forEach((n, i) => {
      const states = { completed: '转录完成', waiting_query: '已提交云端，等待查询结果', running: '正在转录', pending: '等待自动排队', failed: '转录失败', missing: '找不到源文件', waiting: '等待自动排队' };
      const row = node('div', 'group-source-row');
      row.append(node('span', '', (i + 1) + '. ' + (n.sourceName || n.title || '录音片段')), node('small', n.error || n.state === 'missing' ? 'field-error' : 'field-help', n.error || n.stage || states[n.state] || (n.asrComplete ? '转录完成' : '等待自动排队')));
      const recovery = n.recoveryAction || (n.state === 'missing' || (!n.audioAvailable && !n.asrComplete) ? '请重新导入此段原始音频，然后打开笔记继续处理。' : n.state === 'waiting_query' ? '继续处理会查询已提交任务，不会重复上传。' : n.error ? '修正上述问题后，点击下方“继续未完成部分”。' : '');
      if (recovery) row.append(node('small', 'field-help', recovery));
      if (n.audioUrl && !n.audioDeletedAt && n.audioAvailable !== false) { const player = document.createElement('audio'); player.controls = true; player.preload = 'none'; player.src = n.audioUrl; player.setAttribute('aria-label', '回听第 ' + (i + 1) + ' 段'); row.append(player); }
      sources.append(row);
    });
    if ((!hasSummary(note) || note.status === 'error' || note.missingSourceIds?.length) && !busy(note)) sources.append(button('继续未完成部分', () => retryProcessing(note.id), 'button primary'));
    if (note.missingSourceIds?.length) sources.append(node('p', 'field-error', '部分材料笔记：' + note.missingSourceIds.length + ' 段录音未纳入。'));
  }
  $('#open-course-table')?.addEventListener('click', () => { $('#settings-dialog')?.close(); openCalendar().catch(error); });
  $('#open-note-merge')?.addEventListener('click', () => openLibrary('notes').catch(error));
  return { openImport, openLibrary, openCalendar, openSchedule: async () => editSchedule(await h.request('/api/timetable')), renderSources, showBatch, isBusy: () => working };
}
