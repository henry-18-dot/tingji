import { installWorkflows } from './workflows.js';
import { renderMarkdown } from './knowledge.js';
import { createSyncClient, getLastAccountId, registerOfflineShell } from './sync.js';
import { sessionFetch, bootstrapSession, onSession } from './session.js';

const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
const state = { token: '', accountId: null, localBrowser: true, remote: {}, syncClient: null, syncBusy: false, serverNotes: [], audioObjectUrl: null, audioObjectNoteId: null, installPrompt: null, settings: {}, automation: null, capabilities: {}, notes: [], note: null, tab: 'summary', connected: false, editing: null, loadingNote: false, chatBusy: false, uploading: false, recovery: null, recoveryReady: null, querying: false, pollTimer: null, toastTimer: null, focusReturn: null };
const busyStatuses = new Set(['transcribing', 'summarizing', 'processing', 'queued']);
const templateNames = { general: '知识笔记', meeting: '会议纪要', lecture: '知识笔记', interview: '访谈摘要' };
const statusNames = { idle: '待整理', ready: '已整理', transcribing: '正在转写', summarizing: '正在整理', error: '待处理', processing: '处理中', queued: '等待处理' };
const asrTaskStates = { queued: '排队中', processing: '正在识别', waiting_query: '等待下一次查询', submit_unknown: '提交状态待核实', submitting: '正在提交', submitted: '已提交，等待识别', paused: '查询已暂停，可继续查询', completed: '识别完成', succeeded: '识别完成', success: '识别完成', failed: '识别未完成', rejected: '任务被拒绝' };
const pendingAsrStates = new Set(['queued', 'processing', 'waiting_query', 'submit_unknown', 'submitting', 'submitted']);
let editSnapshot = null, titleSnapshot = null;
let pendingAudioDelete = null;
let workflows = null;
let autoSyncTimer = null;
let autoSyncRetryAfter = 0;
let autoSyncPaused = false;
let autoSyncFailures = 0;
let backgroundRefreshing = false;
let archiveAudioUrls = [];
let cancelSyncRequested = false;
let historyFilter = 'active';
let titleSave = Promise.resolve();
let refreshPending = false;
let finishedNotesCaching = false;
onSession(data => { state.token = data.token; });

function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}
function toast(message, error = false, automatic = false) {
  if (automatic && !error && state.settings.failureOnlyNotifications !== false) return;
  const node = $('#toast');
  clearTimeout(state.toastTimer);
  node.classList.toggle('error', error);
  $('span', node).textContent = message;
  $('use', node).setAttribute('href', error ? '#i-info' : '#i-check');
  node.hidden = false;
  state.toastTimer = setTimeout(() => { node.hidden = true; }, error ? 6500 : 3300);
}
function setBusy(button, busy, label) {
  if (!button) return;
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    if (label) button.textContent = label;
  } else {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    if (label) button.textContent = label;
  }
}
async function request(path, options = {}) {
  const headers = { 'X-App-Token': state.token, ...options.headers };
  if ((options.method || 'GET').toUpperCase() === 'POST' && (/^\/api\/notes\/[^/]+\/(transcribe|summarize)$/.test(path)) && !Object.keys(headers).some(name => name.toLowerCase() === 'x-action-id')) headers['X-Action-Id'] = crypto.randomUUID();
  let body = options.body;
  if (body && !(body instanceof Blob) && !(body instanceof ArrayBuffer) && typeof body !== 'string') {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  let response;
  try { response = await sessionFetch(path, { ...options, headers, body, accountId: state.accountId }); }
  catch (error) {
    if (error.code === 'account_changed') throw error;
    throw new Error('暂时无法连接电脑。请保持电脑开机并运行听记。');
  }
  const type = response.headers.get('content-type') || '';
  let data;
  try { data = type.includes('json') ? await response.json() : { error: await response.text() }; }
  catch { throw new Error('服务返回了无法读取的内容，请稍后重试。'); }
  if (!response.ok) {
    const message = typeof data.error === 'string' ? data.error : (data.error?.message || data.message || `请求未完成（${response.status}）`);
    throw new Error(message);
  }
  return data;
}
function dateLabel(value, withTime = false) {
  if (!value) return '';
  const d = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString('zh-CN', withTime ? { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false } : { month: 'numeric', day: 'numeric' });
}
function formatTime(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
  return `${h ? `${String(h).padStart(2, '0')}:` : ''}${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
function isBusy(note = state.note) { return !!note && busyStatuses.has(note.status); }
function compactLayout() { return matchMedia('(max-width: 760px), (pointer: coarse) and (max-width: 1100px)').matches; }
function noteTitleParts(note) {
  const title = note.title || '未命名笔记', course = note.courseName;
  const color = /^#[0-9a-f]{6}$/i.test(note.courseColor || '') ? note.courseColor : '#60796b';
  const label = el('strong');
  if ((note.summary || note.hasSummary) && course && title.startsWith(course)) {
    const prefix = el('span', 'course-name', course); prefix.style.color = color;
    label.append(prefix, document.createTextNode(title.slice(course.length)));
  } else label.textContent = title;
  return label;
}
function notePhase(note) {
  if (needsSync(note)) return '待上传';
  if (note.status === 'summarizing') return '整理中';
  if (note.asrTask?.state === 'queued') return '云端排队';
  if (note.asrTask?.state === 'processing') return '云端转写';
  if (['accepted', 'submitted'].includes(note.asrTask?.state)) return '云端已接收';
  if (note.asrTask?.state === 'submit_unknown') return '等待确认';
  if (note.asrTask?.state === 'submitting') return '正在提交';
  if (isBusy(note)) return note.stage?.includes('上传') ? '正在提交' : '正在处理';
  if (note.summary || note.hasSummary) return note.offlineReady ? '可离线读' : '已整理';
  if (note.status === 'error') return '待处理';
  return '电脑已收到';
}
function usesDeviceStorage() { return !state.localBrowser || !state.connected; }

function needsSync(note = state.note) { return !!note && !!(note.localOnly || note.dirty || note.audioDirty); }
function needsTranscription(note = state.note) { return !!note && !note.summary && !note.transcriptPurgedAt && (!note.transcript?.trim() || (note.asrComplete === false && !!(note.audioUrl || note.audioStored))); }
function hasExistingAsrTask(note = state.note) { return !!note?.asrTask?.requestId && !['failed', 'rejected', 'completed', 'succeeded', 'success'].includes(note.asrTask.state); }
function hasAutomaticWork(note = state.note) { return ['waiting', 'dispatching'].includes(note?.autoProcessState); }
function shouldPoll(note = state.note) { return (hasAutomaticWork(note) && !note.summary && !note.error && !needsSync(note) && state.automation?.enabled && !state.automation?.blockedReason) || isBusy(note) || (!!note?.asrTask?.requestId && pendingAsrStates.has(note.asrTask.state)); }
function taskTime(value) { return value ? dateLabel(value, true) : '尚无记录'; }
function renderAsrTask(note) {
  const task = note.asrTask;
  const pending = !!task?.requestId && pendingAsrStates.has(task.state);
  const accepted = ['accepted', 'submitted', 'queued', 'processing'].includes(task?.state);
  const summarizing = note.status === 'summarizing';
  $('#note-wait-hint').hidden = !(accepted || summarizing || task?.state === 'paused' || task?.state === 'submit_unknown');
  $('#note-wait-hint').textContent = summarizing ? 'iPad 可离线，电脑需保持运行。' : accepted ? 'iPad 可离线；电脑关机会暂停取回结果和整理。' : '点击继续查询。';
  if ((pending || task?.state === 'paused') && !summarizing) $('#note-status-text').textContent = task?.state === 'paused' ? '查询已暂停' : notePhase(note);
  $('#note-status').hidden = !(isBusy(note) || pending || task?.state === 'paused');
}
function updateNoteList(note) {
  const index = state.notes.findIndex(n => n.id === note.id);
  if (index >= 0) state.notes[index] = { ...state.notes[index], ...note };
  else state.notes.unshift(note);
  renderHistory();
}
function localAudioSafe(note) { return !!note?.canDeleteLocalAudio; }
function archiveItems() {
  return state.notes.filter(note => (!note.audioDeletedAt && note.audioAvailable !== false && note.audioArchivedAt) || localAudioSafe(note));
}
function audioDeleteDue(note) {
  if (note.audioDeletedAt || note.audioAvailable === false || !note.audioArchivedAt) return false;
  if (note.cleanupDue) return true;
  const deadline = note.audioDeleteDueAt || new Date(new Date(note.audioArchivedAt).getTime() + 7 * 86400000).toISOString();
  return new Date(deadline).getTime() <= Date.now();
}
function updateArchiveBadge() {
  const count = archiveItems().filter(audioDeleteDue).length;
  const badge = $('#archive-due-badge');
  badge.hidden = !count; badge.textContent = count ? `${count} 待清理` : '';
  const reminder = $('#cleanup-reminder');
  reminder.hidden = !count; reminder.textContent = count ? `${count} 份录音待清理` : '';
}
function releaseArchiveAudio() {
  $$('#archive-list audio').forEach(audio => { audio.pause(); audio.removeAttribute('src'); });
  archiveAudioUrls.forEach(url => URL.revokeObjectURL(url)); archiveAudioUrls = [];
}
async function openArchive() {
  if (workflows) { if (await leaveEditing()) return workflows.openLibrary('archived'); return; }
  if (!(await leaveEditing())) return;
  $('#archive-error').hidden = true;
  openDialog('#archive-dialog');
  await renderArchive();
}
async function renderArchive() {
  releaseArchiveAudio();
  const items = archiveItems(), due = items.filter(audioDeleteDue).length;
  $('#archive-reminder').hidden = !due;
  $('#archive-reminder').textContent = `${due} 段录音已归档 7 天，可以删除。`;
  const list = $('#archive-list'); list.replaceChildren();
  if (!items.length) { list.append(el('p', 'plain-empty', '暂无归档录音')); return; }
  for (const note of items) {
    const row = el('article', 'archive-row'), heading = el('div', 'archive-heading');
    heading.append(el('strong', '', note.title || note.sourceName || '录音'));
    if (localAudioSafe(note)) heading.append(el('span', 'small-badge', '可删除'));
    if (audioDeleteDue(note)) heading.append(el('span', 'small-badge due', '已满 7 天'));
    row.append(heading);
    const detail = el('small', 'archive-meta', note.audioArchivedAt ? `${dateLabel(note.audioArchivedAt)} 归档` : '电脑已收到录音');
    row.append(detail);
    const audio = el('audio'); audio.controls = true; audio.preload = 'none'; audio.setAttribute('aria-label', `回听 ${note.title || '录音'}`);
    let url = state.connected && !note.audioDeletedAt ? note.audioUrl || '' : '';
    if (note.audioStored && state.syncClient) {
      const blob = await state.syncClient.getAudioBlob(note.id).catch(() => null);
      if (blob) { url = URL.createObjectURL(blob); archiveAudioUrls.push(url); }
    }
    if (!$('#archive-dialog').open) { releaseArchiveAudio(); return; }
    if (url) { audio.src = url; row.append(audio); }
    const actions = el('div', 'archive-actions');
    if (note.summary || note.hasSummary) { const open = el('button', 'text-button', '查看笔记'); open.addEventListener('click', () => { closeDialog($('#archive-dialog')); openNote(note.id); }); actions.append(open); }
    if (localAudioSafe(note)) { const local = el('button', 'text-button', '删本机录音'); local.addEventListener('click', () => askDeleteAudio(note, 'device')); actions.append(local); }
    if (note.audioArchivedAt && !note.audioDeletedAt) { const remote = el('button', 'text-button', '删电脑录音'); remote.disabled = !state.connected || isBusy(note); remote.addEventListener('click', () => askDeleteAudio(note, 'computer')); actions.append(remote); }
    row.append(actions); list.append(row);
  }
}
function askDeleteAudio(note, location) {
  if (location === 'device') {
    state.syncClient.deleteLocalAudio(note.id).then(async updated => { updateNoteList(updated); if (state.note?.id === note.id) { state.note = updated; await prepareNoteAudio(updated); renderNote(); } toast('设备缓存已清理'); }).catch(error => toast(error.message, true)); return;
  }
  pendingAudioDelete = { note, location };
  $('#delete-audio-title').textContent = location === 'device' ? '删除本机录音' : '删除电脑录音';
  $('#delete-audio-description').textContent = `删除「${note.title || note.sourceName}」？笔记保留。`;
  $('#delete-audio-error').hidden = true;
  openDialog('#delete-audio-dialog');
}
async function confirmDeleteAudio() {
  if (!pendingAudioDelete) return;
  const { note, location } = pendingAudioDelete, button = $('#confirm-delete-audio');
  button.disabled = true; $('#delete-audio-error').hidden = true;
  try {
    let updated;
    if (location === 'device') updated = await state.syncClient.deleteLocalAudio(note.id);
    else {
      const data = await request(`/api/notes/${encodeURIComponent(note.id)}/audio-delete`, { method: 'POST', body: {} });
      updated = await cacheRemoteNote(data.note || data);
      const index = state.serverNotes.findIndex(item => item.id === note.id);
      if (index >= 0) state.serverNotes[index] = { ...state.serverNotes[index], ...updated };
    }
    updateNoteList(updated);
    if (state.note?.id === note.id) { state.note = updated; await prepareNoteAudio(updated); renderNote(); }
    pendingAudioDelete = null; closeDialog($('#delete-audio-dialog'));
    if ($('#archive-dialog').open) await renderArchive();
    toast('录音已删除');
  } catch (error) { $('#delete-audio-error').textContent = error.message; $('#delete-audio-error').hidden = false; }
  finally { button.disabled = false; }
}
function renderHistory() {
  const query = $('#history-search').value.trim().toLowerCase();
  const visible = state.notes.filter(n => !n.groupParentId).filter(n => historyFilter === 'trash' ? !!n.trashedAt : !n.trashedAt && (historyFilter === 'archived' ? !!n.archivedAt : !n.archivedAt));
  const filtered = visible.filter(n => `${n.title || ''}\n${n.sourceName || ''}\n${n.summary || ''}`.toLowerCase().includes(query));
  $('#history-label').textContent = { active: '我的笔记', archived: '归档笔记', trash: '回收站' }[historyFilter];
  $('#note-count').textContent = String(visible.length);
  const container = $('#history-list');
  const nodes = filtered.map(note => {
    const row = el('div', `history-row${note.summary || note.hasSummary ? '' : ' unorganized'}`);
    row.dataset.noteId = note.id;
    const button = el('button', `history-note${state.note?.id === note.id ? ' selected' : ''}`);
    button.type = 'button';
    button.setAttribute('aria-label', `打开 ${note.title || '未命名笔记'}`);
    if (state.note?.id === note.id) button.setAttribute('aria-current', 'page');
    button.append(icon(note.sourceName ? 'wave' : 'text'));
    const text = el('span');
    text.append(noteTitleParts(note), el('small', '', [dateLabel(note.recordedAt || note.createdAt), note.duration ? formatTime(note.duration) : '', notePhase(note)].filter(Boolean).join(' · ')));
    button.append(text);
    button.addEventListener('click', () => openNote(note.id));
    const more = el('button', 'icon-button history-more'); more.type = 'button'; more.append(icon('more'));
    more.setAttribute('aria-label', `${note.title || '笔记'} 的更多选项`); more.setAttribute('aria-haspopup', 'menu');
    more.addEventListener('click', event => showNoteMenu(event.currentTarget, note));
    row.append(button, more); return row;
  });
  if (!nodes.length) nodes.push(el('p', 'history-empty', query ? '无匹配笔记' : '暂无笔记'));
  container.replaceChildren(...nodes);
  updateArchiveBadge();
}
function hideNoteMenu() { $('#note-menu').hidden = true; $('#note-menu').replaceChildren(); }
function showNoteMenu(anchor, note) {
  const menu = $('#note-menu'); menu.replaceChildren();
  const option = (label, run) => {
    const button = el('button', '', label); button.type = 'button'; button.setAttribute('role', 'menuitem');
    button.addEventListener('click', () => { hideNoteMenu(); run(); }); menu.append(button);
  };
  if (note.trashedAt) option('恢复', () => changeNoteLocation(note, 'restore'));
  else {
    option('重命名', () => { $('#rename-note-id').value = note.id; $('#rename-note-title').value = note.title || ''; openDialog('#rename-dialog'); $('#rename-note-title').select(); });
    option(note.archivedAt ? '移回我的笔记' : '归档', () => changeNoteLocation(note, note.archivedAt ? 'unarchive' : 'archive'));
    option('删除', () => changeNoteLocation(note, 'delete'));
  }
  menu.hidden = false;
  const r = anchor.getBoundingClientRect(); menu.style.left = `${Math.max(8, Math.min(innerWidth - 172, r.right - 164))}px`; menu.style.top = `${Math.max(8, Math.min(innerHeight - menu.offsetHeight - 8, r.bottom + 3))}px`;
  $('button', menu).focus();
}
async function changeNoteLocation(note, action) {
  if (!(await leaveEditing())) return;
  try {
    let updated;
    if (state.syncClient && (usesDeviceStorage() || needsSync(note))) {
      updated = action === 'delete' ? await state.syncClient.deleteNote(note.id) : action === 'restore' ? await state.syncClient.restoreNote(note.id) : await state.syncClient.setArchived(note.id, action === 'archive');
      updated = await syncMetadata(updated);
    } else {
      const path = action === 'archive' || action === 'unarchive' ? 'archive' : action;
      const data = await request(`/api/notes/${encodeURIComponent(note.id)}/${path}`, { method: 'POST', body: path === 'archive' ? { archived: action === 'archive' } : {} });
      updated = await cacheRemoteNote(data.note || data);
    }
    updateNoteList(updated);
    if (state.note?.id === note.id) { state.note = updated; if (action === 'delete') await showWelcome(); else renderNote(); }
    toast(action === 'delete' ? '已移到回收站' : action === 'restore' ? '已恢复' : action === 'archive' ? '已归档' : '已移回');
  } catch (error) { toast(error.message, true); }
}
async function syncMetadata(note) {
  if (!state.connected || !state.syncClient || note.localOnly || note.audioDirty || state.syncBusy) return note;
  try { return (await state.syncClient.pushNote(note.id)).note; }
  catch { return note; }
}
function cacheFinishedNotes() {
  const client = state.syncClient;
  if (!state.connected || !client?.cacheFinishedNotes || finishedNotesCaching) return;
  finishedNotesCaching = true;
  client.cacheFinishedNotes(state.serverNotes).then(async () => {
    if (state.syncClient !== client) return;
    state.notes = await mergedNotes(); renderHistory();
    if (state.note && !hasUnsaved() && !state.editing && document.activeElement !== $('#note-title')) {
      const cached = await client.getNote(state.note.id);
      if (cached && state.note.id === cached.id && !needsSync(state.note)) { state.note = cached; renderNote(); }
    }
  }).catch(() => {}).finally(() => { finishedNotesCaching = false; });
}
async function saveTitle(note, title) {
  title = title.trim(); if (!title || title === note.title) return note;
  const patch = { title, nameSource: 'manual' };
  let updated;
  if (state.syncClient && (usesDeviceStorage() || needsSync(note))) {
    updated = await state.syncClient.updateLocalNote(note.id, patch, { baseRevision: Number(note.baseRevision ?? note.revision ?? 0) });
    updated = await syncMetadata(updated);
  }
  else {
    try {
      const data = await request(`/api/notes/${encodeURIComponent(note.id)}`, { method: 'PATCH', body: { ...patch, baseRevision: Number(note.revision ?? 0) } });
      updated = await cacheRemoteNote({ ...note, ...(data.note || data) });
    } catch (error) { if (!state.syncClient) throw error; updated = await state.syncClient.updateLocalNote(note.id, patch, { baseRevision: Number(note.baseRevision ?? note.revision ?? 0) }); }
  }
  updateNoteList(updated);
  if (state.note?.id === note.id) { state.note = updated; $('#save-status').textContent = needsSync(updated) ? '已保存，待上传' : '已保存'; renderNote(); }
  return updated;
}
async function refreshApplication() {
  if (refreshPending) return;
  refreshPending = true; $('#refresh-app').disabled = true;
  try {
    if (state.recovery || hasUnsaved() || state.uploading) {
      await bootstrap(); toast('状态已更新，保存后可刷新页面'); return;
    }
    await titleSave;
    if (state.syncBusy) {
      cancelSyncRequested = true; state.syncClient?.cancelUploads(); setSyncProgress('正在暂停上传');
      const started = Date.now();
      while (state.syncBusy && Date.now() - started < 10000) await new Promise(resolve => setTimeout(resolve, 100));
      if (state.syncBusy) { toast('上传暂停后再刷新'); return; }
    }
    if (state.recovery || hasUnsaved() || state.uploading) return;
    await navigator.serviceWorker?.getRegistration().then(registration => registration?.update()).catch(() => {});
    location.reload();
  } finally { refreshPending = false; $('#refresh-app').disabled = false; }
}
function syncConnection() {
  const configured = state.settings.asrConfigured && state.settings.deepseekConfigured && (state.settings.tosConfigured && state.settings.tosPrivateConfirmed);
  const label = $('#connection-label');
  label.classList.toggle('disconnected', !configured || !state.connected);
  $('#connection-status-text').textContent = !state.connected ? (state.syncClient ? '离线' : '待连接电脑') : configured ? '已连接电脑' : state.settings.asrConfigured && !state.settings.tosConfigured ? '待配置临时存放' : '待配置 API';
  $('#settings-dot').hidden = !!configured;
  const asr = $('#asr-badge'), deepseek = $('#deepseek-badge');
  asr.textContent = state.settings.asrConfigured ? '已配置' : '未配置';
  asr.classList.toggle('connected', !!state.settings.asrConfigured);
  deepseek.textContent = state.settings.deepseekConfigured ? '已配置' : '未配置';
  deepseek.classList.toggle('connected', !!state.settings.deepseekConfigured);
  $('#sync-connection-status').textContent = state.connected ? '已连接电脑' : '离线';
  $('#save-settings').disabled = false;
  $('#settings-connection-hint').hidden = state.connected;
  renderAutomationStatus();
}
async function bootstrap() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const data = await bootstrapSession({ signal: controller.signal, accountId: state.accountId });
    const oldAccount = state.accountId;
    state.token = data.token;
    state.settings = data.settings || {};
    state.capabilities = data.capabilities || {};
    state.localBrowser = data.localBrowser !== false;
    state.remote = data.remote || {};
    state.accountId = data.accountId || oldAccount || 'local-device';
    if (oldAccount && oldAccount !== state.accountId) { state.note = null; state.notes = []; releaseAudioObjectUrl(); }
    await attachSyncClient(state.accountId);
    state.serverNotes = data.notes || [];
    if (state.syncClient) {
      await state.syncClient.cacheNotes(state.serverNotes);
      state.notes = await mergedNotes();
    } else state.notes = state.serverNotes;
    state.connected = true;
    cacheFinishedNotes();
    $('#connection-error').hidden = true;
    renderHistory();
    syncConnection();
    updateSyncBadge();
    const linked = new URL(location.href).searchParams.get('note');
    if (linked && !state.note) await openNote(linked);
    if ($('#sync-dialog').open) renderSyncDialog();
    refreshAutomationStatus();
    scheduleAutoSync(800);
  } catch (error) {
    state.connected = false;
    if (!state.accountId) {
      try {
        const accountId = await getLastAccountId();
        if (accountId) { state.accountId = accountId; state.localBrowser = false; await attachSyncClient(accountId); }
      } catch { /* The connection error remains visible if local storage is unavailable. */ }
    }
    if (state.syncClient) {
      state.notes = await state.syncClient.listNotes();
      renderHistory();
      const linked = new URL(location.href).searchParams.get('note');
      if (linked && !state.note) await openNote(linked);
    }
    syncConnection();
    $('#connection-error').hidden = false;
    $('#connection-error-text').textContent = state.syncClient ? '暂时离线，导入文件会保存在此设备；连接恢复后自动上传。' : '首次使用请连接电脑并登录';
    if (!state.syncClient) toast(error.message, true);
    updateSyncBadge();
  } finally { clearTimeout(timeout); }
}
async function attachSyncClient(accountId) {
  if (state.syncClient?.accountId === accountId) return;
  state.syncClient?.dispose();
  const fixedAccount = accountId;
  try {
    state.syncClient = createSyncClient({ accountId, getToken: () => state.accountId === fixedAccount ? state.token : '',
      onChange: () => { updateSyncBadge(); } });
    await state.syncClient.ready;
  } catch { state.syncClient = null; toast('浏览器无法保存离线内容', true); }
}
async function mergedNotes() {
  const map = new Map(state.serverNotes.map(note => [note.id, note]));
  if (state.syncClient) for (const note of await state.syncClient.listNotes()) map.set(note.id, { ...(map.get(note.id) || {}), ...note });
  return [...map.values()].sort((a, b) => String(b.updatedAt || b.createdAt || '').localeCompare(String(a.updatedAt || a.createdAt || '')));
}
async function cacheRemoteNote(note) {
  if (!state.syncClient || !note?.id) return note;
  try { return (await state.syncClient.cacheNotes([note]))[0] || note; }
  catch { return note; }
}
async function updateSyncBadge() {
  if (!state.syncClient) return;
  try {
    const count = (await state.syncClient.listPending()).length;
    $('#sync-pending-badge').textContent = String(count);
    $('#sync-pending-badge').hidden = count === 0;
  } catch { /* The sync dialog provides actionable storage errors when opened. */ }
}
function releaseAudioObjectUrl() {
  if (state.audioObjectUrl) URL.revokeObjectURL(state.audioObjectUrl);
  state.audioObjectUrl = null; state.audioObjectNoteId = null;
}
async function prepareNoteAudio(note) {
  releaseAudioObjectUrl();
  if (state.syncClient) {
    const blob = await state.syncClient.getAudioBlob(note.id).catch(() => null);
    if (blob && state.note?.id === note.id) { state.audioObjectUrl = URL.createObjectURL(blob); state.audioObjectNoteId = note.id; }
  }
}
function progressText(progress) {
  if (progress.phase === 'uploading-audio' && progress.total) return `上传 ${Math.round(progress.loaded / progress.total * 100)}%`;
  if (progress.phase === 'downloading-audio') return '正在下载原始音频…';
  if (progress.phase === 'downloading-note') return '正在下载笔记全文…';
  if (progress.phase === 'uploading-note') return '正在保存笔记到电脑…';
  if (progress.phase === 'upload-connecting') return '正在连接电脑';
  if (progress.phase === 'upload-waiting') return '等待电脑响应，可暂停后重试';
  if (progress.phase === 'upload-retrying') return '正在重新连接';
  if (progress.phase === 'upload-completing') return '电脑正在接收文件';
  return progress.phase === 'complete' ? '已完成' : '正在同步…';
}
function setSyncProgress(text) { $('#sync-progress').textContent = text; $('#sync-progress').hidden = false; }
async function openSyncDialog() {
  if (!(await leaveEditing())) return;
  $('#sync-progress').hidden = !state.syncBusy;
  openDialog('#sync-dialog');
  await renderSyncDialog();
}
async function renderSyncDialog() {
  syncConnection();
  const remote = state.remote || {};
  let remoteUrl = '';
  try { if (remote.url && new URL(remote.url).protocol === 'https:') remoteUrl = remote.url; } catch { /* Never render non-web URL schemes. */ }
  $('#sync-remote-url').hidden = !remoteUrl;
  $('#copy-phone-url').hidden = !remoteUrl;
  if (remoteUrl) { $('#sync-remote-url').textContent = remoteUrl; $('#sync-remote-url').href = remoteUrl; }
  $('#sync-phone-help').textContent = remoteUrl ? '电脑保持开机并运行听记，在 iPad 或手机浏览器打开下方私人网址。' : state.localBrowser ? '私人网址尚未配置；配置好后会显示在这里。电脑需要保持开机并运行听记。' : '当前页面就是你的私人听记。电脑保持开机并运行服务，即可自动同步。';
  $('#install-app').hidden = !state.installPrompt;
  const pending = state.syncClient ? await state.syncClient.listPending() : [];
  $('#pending-sync-description').textContent = pending.length ? (!state.connected ? `${pending.length} 份保存在此设备，连接恢复后自动上传。` : state.syncBusy ? `正在上传 ${pending.length} 份，请保持页面打开。` : autoSyncPaused ? `${pending.length} 份已暂停，点击继续上传。` : `${pending.length} 份等待自动上传，失败文件会保留。`) : '全部已上传。电脑会自动转录并整理，可关闭此窗口。';
  $('#pending-sync-list').replaceChildren(...pending.map(note => {
    const row = el('div', 'sync-note-row');
    row.append(icon(note.audioStored ? 'wave' : 'text'), el('span', '', note.title || '未命名笔记'));
    const upload = el('button', 'text-button', '上传');
    upload.hidden = state.syncBusy;
    upload.setAttribute('aria-label', `上传 ${note.title || '笔记'}`);
    upload.addEventListener('click', () => syncPendingNotes(note.id));
    row.append(upload); return row;
  }));
  $('#push-pending-notes').disabled = false;
  $('#push-pending-notes').hidden = state.syncBusy;
  $('#push-pending-notes').textContent = !state.connected ? '重新连接' : autoSyncPaused ? '继续上传' : pending.length ? '立即重试上传' : '重新检查';
  $('#cancel-sync').hidden = !state.syncBusy;
  $('#refresh-computer-notes').disabled = false;
  const list = $('#computer-sync-list');
  const selected = new Set($$('input:checked', list).map(input => input.value));
  list.replaceChildren(...state.serverNotes.map(note => {
    const label = el('label', 'sync-select-row');
    const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.value = note.id; checkbox.checked = selected.has(note.id) || (!selected.size && state.note?.id === note.id && !state.note.localOnly);
    checkbox.disabled = false;
    checkbox.addEventListener('change', updateDownloadSelection);
    const text = el('span'); text.append(el('strong', '', note.title || '未命名笔记'), el('small', '', note.sourceName || '文字笔记'));
    label.append(checkbox, text); return label;
  }));
  if (!state.serverNotes.length) list.append(el('p', 'field-help', state.connected ? '电脑上还没有笔记。' : '联网后刷新电脑笔记列表，再选择下载。'));
  updateDownloadSelection();
}
function updateDownloadSelection() {
  const count = $$('#computer-sync-list input:checked').length;
  $('#download-selected-notes').disabled = false;
  $('#download-selected-notes').hidden = state.syncBusy;
  $('#download-selection-hint').textContent = state.syncBusy ? '当前同步完成后可下载。' : !state.connected ? '连接电脑后可下载，先点击重新连接。' : count ? `已选 ${count} 份` : '选择要下载的笔记。';
}
async function refreshComputerList() {
  const data = await request('/api/notes');
  state.serverNotes = data.notes || [];
  if (state.syncClient) await state.syncClient.cacheNotes(state.serverNotes);
  state.notes = await mergedNotes(); renderHistory();
  cacheFinishedNotes();
  await renderSyncDialog();
}
async function syncPendingNotes(onlyId = null, automatic = false) {
  if (automatic && (autoSyncPaused || Date.now() < autoSyncRetryAfter || state.editing || state.uploading || workflows?.isBusy())) return;
  if (state.syncBusy) { if (!automatic) toast('正在同步，可在上传与下载中查看进度'); return; }
  if (!state.connected) { if (!automatic) { await bootstrap(); if (!state.connected) toast('电脑未连接。文件仍保存在此设备，恢复连接后会自动上传。', true); } return; }
  if (!state.syncClient) { if (!automatic) toast('设备存储不可用，请重新打开页面后重试。', true); return; }
  if (!automatic) { autoSyncPaused = false; autoSyncRetryAfter = 0; autoSyncFailures = 0; }
  const pendingItems = (await state.syncClient.listPending()).filter(note => typeof onlyId !== 'string' || note.id === onlyId);
  if (!pendingItems.length) { if (!automatic) { await refreshComputerList(); setSyncProgress('全部已上传，电脑会自动处理。'); } return; }
  if (!automatic && !(await leaveEditing())) return;
  state.syncBusy = true; cancelSyncRequested = false; $('#sync-error').hidden = true;
  let currentId = state.note?.id;
  try {
    const pending = pendingItems;
    await renderSyncDialog();
    let conflictCount = 0, failures = [];
    for (let index = 0; index < pending.length; index++) {
      if (cancelSyncRequested) break;
      const note = pending[index];
      setSyncProgress(`正在上传 ${index + 1}/${pending.length}：${note.title || '笔记'}`);
      try {
        const result = await state.syncClient.pushNote(note.id, { onProgress: progress => setSyncProgress(`${note.title || '笔记'} · ${progressText(progress)}`) });
        if (result.conflict) conflictCount++;
        if (currentId === note.id || currentId === note.localId) currentId = result.note.id;
      } catch (error) { failures.push(`${note.title || '笔记'}：${error.message}`); }
    }
    await refreshComputerList();
    if (state.note && currentId && !state.editing) {
      const note = await state.syncClient.getNote(currentId);
      if (note) await openNote(note.id, note);
    }
    setSyncProgress(cancelSyncRequested ? '上传已暂停' : failures.length ? `${pending.length - failures.length}/${pending.length} 份已上传` : conflictCount ? `已上传；${conflictCount} 处差异已保留` : '电脑已收到，自动排队转录；可关闭此窗口。');
    if (failures.length && !cancelSyncRequested) { autoSyncFailures++; autoSyncPaused = autoSyncFailures >= 3; autoSyncRetryAfter = Date.now() + 60000; $('#sync-error').textContent = failures.join('\n') + (autoSyncPaused ? '\n自动上传连续未完成，已暂停。检查连接后点击继续上传。' : '\n文件仍保留在此设备；1 分钟后重试，也可点击立即重试上传。'); $('#sync-error').hidden = false; toast('部分文件暂未上传，已保留并等待重试。', true); }
    else { autoSyncRetryAfter = 0; autoSyncFailures = 0; if (!cancelSyncRequested) toast('电脑已收到，开始自动处理', false, automatic); }
  } catch (error) { autoSyncFailures++; autoSyncPaused = autoSyncFailures >= 3; autoSyncRetryAfter = Date.now() + 60000; $('#sync-error').textContent = error.message + ' 文件已保留，可点击立即重试上传。'; $('#sync-error').hidden = false; toast(error.message, true); }
  finally { state.syncBusy = false; await renderSyncDialog(); updateSyncBadge(); refreshAutomationStatus(); }
}
async function downloadSelectedNotes() {
  if (!state.connected) { toast('请先连接电脑，再下载笔记。', true); return; }
  if (!state.syncClient) { toast('本设备存储不可用，请重新打开页面。', true); return; }
  if (state.syncBusy) { toast('正在同步，请等待完成。'); return; }
  const ids = $$('#computer-sync-list input:checked').map(input => input.value);
  if (!ids.length) { toast('请先勾选要下载的笔记'); return; }
  state.syncBusy = true; $('#sync-error').hidden = true;
  try {
    await renderSyncDialog();
    let conflicts = 0;
    for (let index = 0; index < ids.length; index++) {
      const result = await state.syncClient.downloadNote(ids[index], { includeAudio: $('#sync-include-audio').checked, onProgress: progress => setSyncProgress(`${index + 1}/${ids.length} · ${progressText(progress)}`) });
      if (result.conflict) conflicts++;
    }
    state.notes = await mergedNotes(); renderHistory();
    if (state.note && ids.includes(state.note.id)) { const cached = await state.syncClient.getNote(state.note.id); if (cached) { state.note = cached; await prepareNoteAudio(cached); renderNote(true); } }
    setSyncProgress(conflicts ? '已下载，版本差异已保留' : '已下载');
  } catch (error) { $('#sync-error').textContent = error.message; $('#sync-error').hidden = false; }
  finally { state.syncBusy = false; await renderSyncDialog(); }
}
async function ensureAiReady() {
  if (!state.connected) { await openSyncDialog(); $('#sync-error').textContent = 'AI 处理需要连接电脑。此设备的笔记和录音会继续保留。'; $('#sync-error').hidden = false; return false; }
  if (needsSync()) { await openSyncDialog(); $('#sync-error').textContent = '此设备的修改正在等待自动上传，完成后电脑会继续处理。'; $('#sync-error').hidden = false; return false; }
  return true;
}
function closeSidebar() {
  $('#sidebar').classList.remove('mobile-open');
  $('#sidebar-backdrop').hidden = true;
  if (compactLayout()) $('#sidebar-toggle').setAttribute('aria-expanded', 'false');
}
function hasUnsaved() {
  if (state.editing && state.note) return $(`#${state.editing}-editor`).value !== ((editSnapshot?.id === state.note.id ? editSnapshot : state.note)[state.editing] || '');
  return false;
}
async function leaveEditing() {
  if (!hasUnsaved()) { state.editing = null; editSnapshot = null; return true; }
  // Saving an in-progress correction keeps navigation from discarding user text.
  try { await saveEditor(state.editing, false); return true; }
  catch { return false; }
}
async function showWelcome() {
  if (state.loadingNote) return;
  state.loadingNote = true;
  if (!(await leaveEditing())) { state.loadingNote = false; return; }
  clearTimeout(state.pollTimer);
  state.note = null;
  state.tab = 'summary';
  $('#welcome').hidden = false;
  $('#note-page').hidden = true;
  $('#topbar-label').textContent = '';
  $('#audio-player').pause();
  releaseAudioObjectUrl();
  history.replaceState(null, '', '/');
  closeSidebar();
  renderHistory();
  window.scrollTo({ top: 0, behavior: 'instant' });
  state.loadingNote = false;
}
async function openNote(id, supplied = null) {
  if (state.loadingNote) return;
  state.loadingNote = true;
  try {
    if (!(await leaveEditing())) return;
    const cached = state.syncClient ? await state.syncClient.getNote(id) : null;
    let note = supplied || ((cached && (needsSync(cached) || !state.connected)) ? cached : null);
    if (!note) {
      try { note = await request(`/api/notes/${encodeURIComponent(id)}`); note = await cacheRemoteNote(note.note || note); }
      catch (error) { if (cached?._fullCached) note = cached; else throw error; }
    }
    note = note.note || note;
    if (!state.connected && !note._fullCached && (note.hasTranscript || note.hasSummary)) throw new Error('请连接电脑下载这份笔记');
    state.note = note;
    state.tab = 'summary';
    state.editing = null;
    editSnapshot = null; titleSnapshot = null;
    $('#welcome').hidden = true;
    $('#note-page').hidden = false;
    $('#topbar-label').textContent = '';
    $('#summary-editor-wrap').hidden = true;
    $('#save-status').textContent = '';
    $('#audio-player').pause();
    await prepareNoteAudio(state.note);
    history.replaceState(null, '', `/?note=${encodeURIComponent(state.note.id)}`);
    updateNoteList(state.note);
    renderNote(true);
    closeSidebar();
    window.scrollTo({ top: 0, behavior: 'instant' });
    schedulePoll();
  } catch (error) { toast(error.message, true); }
  finally { state.loadingNote = false; }
}
function renderNote(full = false) {
  const note = state.note;
  if (!note) return;
  const busy = isBusy(note), existingTask = hasExistingAsrTask(note), editingLocked = busy || existingTask;
  $('#open-prompt-lab').href = '/prompt-lab.html?noteId=' + encodeURIComponent(note.id);
  $('#note-title').readOnly = !!note.trashedAt;
  if (document.activeElement !== $('#note-title')) $('#note-title').value = note.title || '未命名笔记';
  $('#note-date').textContent = dateLabel(note.recordedAt || note.createdAt, true);
  $('#note-source').textContent = [note.duration ? formatTime(note.duration) : '', !busy && !existingTask && !needsSync(note) ? notePhase(note) : '', note.audioArchivedAt && !note.audioDeletedAt ? '录音已归档' : ''].filter(Boolean).join(' · ');
  const audioUrl = !note.audioArchivedAt && (state.audioObjectNoteId === note.id ? state.audioObjectUrl : state.connected && note.audioAvailable !== false ? note.audioUrl || '' : '');
  $('#audio-card').hidden = !audioUrl;
  if (audioUrl && $('#audio-player').getAttribute('src') !== audioUrl) {
    $('#audio-player').src = audioUrl;
    $('#audio-download').href = audioUrl;
    $('#audio-download').download = note.sourceName || `${note.title}.webm`;
  }
  if (!audioUrl) { $('#audio-player').pause(); $('#audio-player').removeAttribute('src'); $('#audio-download').removeAttribute('href'); }
  $('#note-audio-more').hidden = !audioUrl;
  const safe = localAudioSafe(note);
  $('#device-note-state').hidden = !(needsSync(note) || safe);
  $('#device-note-state-text').textContent = needsSync(note) ? '待上传' : '电脑已收到录音';
  $('#audio-safe-badge').hidden = !safe;
  $('#delete-device-audio').hidden = !safe;
  $('#note-open-sync').hidden = !needsSync(note);
  $('#note-status').hidden = !busy;
  $('#note-status-text').textContent = note.status === 'summarizing' ? '正在整理' : '正在转写';
  renderAsrTask(note);
  workflows?.renderSources(note);
  $('#note-error').hidden = !note.error;
  $('#note-error-text').textContent = typeof note.error === 'string' ? note.error : (note.error?.message || '请重试。');
  $('#note-error-title').textContent = existingTask ? '查询中断' : '处理失败';
  $('#retry-note-process').textContent = existingTask ? '继续查询' : '重试';
  $('#retry-note-process').hidden = true;
  $('#retry-note-process').disabled = busy || state.querying;
  $('#summary-toolbar').hidden = !note.summary && !note.transcriptPurgedAt;
  $('#summary-empty').hidden = !!note.summary || !!note.transcriptPurgedAt;
  if (state.editing !== 'summary') {
    $('#summary-content').hidden = false;
    if (full || $('#summary-content').dataset.rendered !== note.summary) {
      renderMarkdown(note.summary || '', $('#summary-content'), { title: note.title });
      $('#summary-content').dataset.rendered = note.summary || '';
    }
  }
  $('#edit-summary').disabled = editingLocked;
  const process = $('#primary-process'), pendingSync = needsSync(note);
  const waitingAutomatically = hasAutomaticWork(note) && !pendingSync && !busy && !existingTask && !note.error && state.automation?.enabled && !state.automation.blockedReason;
  process.hidden = busy || state.querying || waitingAutomatically;
  process.disabled = false;
  $('span', process).textContent = pendingSync ? '上传到电脑' : existingTask ? (state.querying ? '正在查询' : '继续查询') : busy ? '处理中' : note.status === 'error' ? '重试整理' : '整理笔记';
  $('use', process).setAttribute('href', pendingSync ? '#i-upload' : '#i-spark');
  $('#summary-empty-title').textContent = busy ? '' : pendingSync ? '录音已保存' : '待整理';
  $('#summary-empty-description').textContent = pendingSync ? (!state.connected ? '文件已保存在此设备，连接恢复后自动上传。' : state.syncBusy ? '正在自动上传。电脑收到后会转录并整理，请保持页面打开。' : '等待自动上传，可在上传与下载中查看进度。') : busy ? '电脑正在处理；收到文件后，iPad 即可离开此页面。' : waitingAutomatically ? '已进入自动处理流程，无需重复提交。' : state.automation?.blockedReason || (state.automation?.enabled === false ? '自动处理已暂停，可到设置恢复。' : '');
  $('#summary-empty-description').hidden = !$('#summary-empty-description').textContent;
  $('#process-disclosure').hidden = busy || pendingSync || existingTask;
  $('#process-disclosure').textContent = '云服务按用量计费。';
  $('#configure-inline').hidden = busy || pendingSync || existingTask || (!state.automation?.blockedReason && state.automation?.enabled !== false && state.settings.deepseekConfigured && state.settings.asrConfigured && state.settings.tosConfigured && state.settings.tosPrivateConfirmed);
  updateArchiveBadge();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (!shouldPoll() || !state.connected || needsSync() || state.editing || document.activeElement === $('#note-title')) return;
  const id = state.note.id;
  state.pollTimer = setTimeout(async () => {
    try {
      const response = await request(`/api/notes/${encodeURIComponent(id)}`);
      if (state.note?.id !== id || state.editing || document.activeElement === $('#note-title')) return;
      const note = await cacheRemoteNote(response.note || response);
      if (state.note?.id !== id || state.editing || document.activeElement === $('#note-title')) return;
      const wasBusy = isBusy();
      state.note = note;
      updateNoteList(note);
      renderNote();
      if (wasBusy && !isBusy()) {
        if (note.error) toast(note.error, true);
        else toast(note.summary ? '笔记已完成' : '转写完成', false, true);
      }
      schedulePoll();
    } catch (error) {
      if (state.note?.id !== id) return;
      $('#note-status-text').textContent = '连接中断，等待恢复';
      state.pollTimer = setTimeout(schedulePoll, 3500);
    }
  }, state.note?.asrTask?.requestId ? 5000 : 1700);
}
async function beginProcess() {
  if (!state.note || state.note.summary || state.note.transcriptPurgedAt) return;
  if (needsSync()) { await syncPendingNotes(state.note.id); if (needsSync()) await openSyncDialog(); return; }
  if (!(await leaveEditing()) || !(await ensureAiReady())) return;
  if (hasExistingAsrTask()) { await refreshExistingTask(); return; }
  if (isBusy()) return;
  const hasTranscript = !needsTranscription();
  if (!state.settings.deepseekConfigured || (!hasTranscript && !state.settings.asrConfigured)) { openSettings(); toast('请先配置语音与笔记服务'); return; }
  if (!hasTranscript && (!state.settings.tosConfigured || !state.settings.tosPrivateConfirmed)) {
    openSettings(); $('#audio-storage-settings').closest('#settings-form').querySelector('details').open = true;
    $('#audio-storage-settings').open = true; $('#tos-bucket').focus(); return;
  }
  const template = 'lecture', snapshot = { ...state.note };
  await submitProcess(snapshot, template, !hasTranscript, $('#primary-process'));
}

async function submitProcess(snapshot, template, consent, button) {
  const id = snapshot.id;
  const hasTranscript = !needsTranscription(snapshot);
  button.disabled = true;
  try {
    const path = hasTranscript ? 'summarize' : 'transcribe';
    const replacesTask = !hasTranscript && consent && snapshot.asrTask?.requestId && ['failed', 'rejected', 'completed'].includes(snapshot.asrTask.state);
    const data = await request(`/api/notes/${encodeURIComponent(id)}/${path}`, { method: 'POST', body: { template, language: 'auto', ...(!hasTranscript ? { asrMode: 'standard', cloudUploadConsent: consent } : {}), ...(replacesTask ? { resubmit: true, replaceRequestId: snapshot.asrTask.requestId } : {}) } });
    const updated = await cacheRemoteNote({ ...snapshot, ...(data.note || data) });
    updateNoteList(updated);
    if (state.note?.id !== id) return true;
    state.note = updated;
    updateNoteList(state.note);
    renderNote();
    schedulePoll();
    return true;
  } catch (error) {
    if (!hasTranscript) {
      // A dropped local response must not make an accepted cloud task look new.
      try {
        const recovered = await request(`/api/notes/${encodeURIComponent(id)}`);
        const note = recovered.note || recovered;
        updateNoteList(note);
        if (hasExistingAsrTask(note)) {
          if (state.note?.id === id) { state.note = note; renderNote(); schedulePoll(); }
          toast('已恢复任务');
          return true;
        }
      } catch { /* Keep the explicit error and require a fresh user action. */ }
    }
    toast(error.message, true);
    return false;
  }
  finally { button.disabled = isBusy(); }
}
async function refreshExistingTask() {
  const id = state.note?.id;
  if (!id || state.querying) return;
  state.querying = true;
  renderNote();
  try {
    const data = await request(`/api/notes/${encodeURIComponent(id)}/query`, { method: 'POST', body: {} });
    const updated = await cacheRemoteNote(data.note || data);
    updateNoteList(updated);
    if (state.note?.id !== id) return;
    state.note = updated;
    renderNote();
    schedulePoll();
    toast('已继续查询');
  } catch (error) { toast(error.message, true); }
  finally { state.querying = false; renderNote(); }
}
function startEditor(type) {
  if (!state.note || isBusy() || hasExistingAsrTask()) return;
  if (state.editing && state.editing !== type) { toast('请先保存另一处正在编辑的内容。'); return; }
  state.editing = type;
  editSnapshot = { ...state.note };
  $(`#${type}-editor`).value = state.note[type] || '';
  $(`#${type}-content`).hidden = true;
  $(`#${type}-editor-wrap`).hidden = false;
  $(`#${type}-editor`).focus();
  $('#save-status').textContent = '正在编辑';
}
function cancelEditor(type) {
  $(`#${type}-editor-wrap`).hidden = true;
  $(`#${type}-content`).hidden = false;
  state.editing = null;
  editSnapshot = null;
  $('#save-status').textContent = '';
  renderNote();
}
async function saveEditor(type, notify = true) {
  if (!state.note) return;
  const id = state.note.id;
  const snapshot = { ...(editSnapshot?.id === id ? editSnapshot : state.note) };
  const text = $(`#${type}-editor`).value;
  const button = $(`#save-${type}-edit`);
  button.disabled = true;
  try {
    let updated;
    if (state.syncClient && (usesDeviceStorage() || needsSync(snapshot))) {
      updated = await state.syncClient.updateLocalNote(id, { [type]: text }, { baseRevision: Number(snapshot.baseRevision ?? snapshot.revision ?? 0) });
      if (type === 'transcript') { updated.transcriptEdited = true; updated.summaryStale = !!snapshot.summary; }
    } else {
      try {
        const data = await request(`/api/notes/${encodeURIComponent(id)}`, { method: 'PATCH', body: { [type]: text, baseRevision: Number(snapshot.revision ?? 0) } });
        updated = await cacheRemoteNote({ ...snapshot, ...(data.note || data) });
      } catch (error) {
        if (!state.syncClient) throw error;
        updated = await state.syncClient.updateLocalNote(id, { [type]: text }, { baseRevision: Number(snapshot.baseRevision ?? snapshot.revision ?? 0) });
        toast('修改已存本机，待上传');
      }
    }
    updateNoteList(updated);
    if (state.note?.id !== id) return;
    state.note = updated;
    state.editing = null;
    editSnapshot = null;
    $(`#${type}-editor-wrap`).hidden = true;
    $(`#${type}-content`).hidden = false;
    $('#save-status').textContent = needsSync(updated) ? '已保存，待上传' : '已保存';
    updateNoteList(state.note);
    renderNote();
    if (notify) toast(needsSync(updated) ? '已保存，待上传' : type === 'transcript' ? '已保存' : '修改已保存');
  } catch (error) { toast(error.message, true); throw error; }
  finally { button.disabled = false; }
}
function filename(label, suffix = '') { return `${(label || '听记笔记').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 100)}${suffix}.txt`; }
function downloadText(text, name) {
  const url = URL.createObjectURL(new Blob(['\uFEFF', text], { type: 'text/plain;charset=utf-8' }));
  const a = el('a'); a.href = url; a.download = name; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('已复制'); }
  catch {
    const input = el('textarea'); input.value = text; input.style.position = 'fixed'; input.style.opacity = '0'; document.body.append(input); input.select();
    const success = document.execCommand('copy'); input.remove();
    toast(success ? '已复制' : '复制未成功，请使用下载文本。', !success);
  }
}
function openDialog(id) {
  state.focusReturn = document.activeElement;
  $(id).showModal();
  closeSidebar();
}
function closeDialog(dialog) {
  dialog.close();
  if (state.focusReturn?.isConnected) state.focusReturn.focus();
}
function openSettings() {
  const s = state.settings;
  $('#asr-api-key').value = '';
  $('#asr-access-token').value = '';
  $('#deepseek-key').value = '';
  $('#tos-access-key-id').value = '';
  $('#tos-secret-access-key').value = '';
  $('#tos-session-token').value = '';
  $('#asr-api-key').placeholder = s.asrConfigured ? '已保存，填写新密钥可替换' : '填入火山引擎语音 API Key';
  $('#deepseek-key').placeholder = s.deepseekConfigured ? '已保存，填写新密钥可替换' : '把你的 DeepSeek API Key 填在这里';
  $('#asr-app-id').value = s.asrAppId || '';
  $('#deepseek-model').value = s.deepseekModel || 'deepseek-flash';
  $('#daily-budget-yuan').value = String(s.dailyBudgetYuan ?? 10);
  $('#failure-only-notifications').checked = s.failureOnlyNotifications !== false;
  $('#tos-region').value = s.tosRegion || 'cn-beijing';
  $('#tos-bucket').value = s.tosBucket || '';
  $('#tos-private-confirmed').checked = !!s.tosPrivateConfirmed;
  $('#tos-config-status').textContent = s.tosConfigured ? '已配置' : '尚未配置';
  $('#tos-config-status').classList.toggle('connected', !!s.tosConfigured);
  for (const id of ['tos-access-key-id', 'tos-secret-access-key', 'tos-session-token']) $(`#${id}`).placeholder = s.tosConfigured ? '已保存，空白会保留原配置' : id === 'tos-session-token' ? '使用临时凭据时填写' : '填写 TOS 凭据';
  $('#settings-error').hidden = true;
  renderAutomationStatus();
  syncConnection();
  openDialog('#settings-dialog');
}
function clearSecretInputs() {
  for (const id of ['asr-api-key', 'asr-access-token', 'deepseek-key', 'tos-access-key-id', 'tos-secret-access-key', 'tos-session-token']) $(`#${id}`).value = '';
}
async function saveSettings(event) {
  event.preventDefault();
  if (!state.connected) { $('#settings-error').textContent = '连接电脑后才能更改 API 设置；离线笔记仍可使用。'; $('#settings-error').hidden = false; return; }
  const button = $('#save-settings');
  button.disabled = true;
  $('#settings-error').hidden = true;
  try {
    const data = await request('/api/settings', { method: 'POST', body: {
      dailyBudgetYuan: Number($('#daily-budget-yuan').value), failureOnlyNotifications: $('#failure-only-notifications').checked,
      asrMode: 'standard', asrModeChanged: true, asrApiKey: $('#asr-api-key').value.trim(), asrAppId: $('#asr-app-id').value.trim(), asrAccessToken: $('#asr-access-token').value.trim(), asrResourceId: 'volc.seedasr.auc', deepseekApiKey: $('#deepseek-key').value.trim(), deepseekModel: $('#deepseek-model').value.trim(),
      tosRegion: $('#tos-region').value.trim() || 'cn-beijing', tosBucket: $('#tos-bucket').value.trim(), tosPrivateConfirmed: $('#tos-private-confirmed').checked, tosAccessKeyId: $('#tos-access-key-id').value.trim(), tosSecretAccessKey: $('#tos-secret-access-key').value.trim(), tosSessionToken: $('#tos-session-token').value.trim()
    } });
    state.settings = data.settings || data;
    clearSecretInputs();
    syncConnection();
    await refreshAutomationStatus();
    renderNote();
    closeDialog($('#settings-dialog'));
    toast('设置已保存在此电脑');
  } catch (error) { $('#settings-error').textContent = error.message; $('#settings-error').hidden = false; }
  finally { button.disabled = false; }
}
async function uploadFile(file, recordingMeta = file?.recordingMeta || {}, deferOpen = false) {
  if (!file || state.uploading) return;
  if (!file.size) { toast('这个文件是空的，请选择有内容的音频或视频。', true); return false; }
  if (file.size > 1024 * 1024 * 1024) { toast('文件超过 1 GB，请先拆成较小的文件再导入。', true); return false; }
  if (usesDeviceStorage() && !state.syncClient) { toast('请先连接电脑', true); return false; }
  state.uploading = true;
  const fromNote = state.note?.id || null;
  $('#upload-button').disabled = true;
  toast(`正在保存「${file.name}」到${usesDeviceStorage() ? '此设备' : '电脑'}…`, false, true);
  try {
    let note;
    if (usesDeviceStorage()) note = await state.syncClient.saveLocalRecording(file, { title: file.name.replace(/\.[^.]+$/, ''), name: file.name, language: 'auto', template: 'lecture', ...recordingMeta });
    else {
      const data = await request('/api/upload', { method: 'POST', headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-File-Name': encodeURIComponent(file.name), 'X-Language': 'auto', 'X-Template': 'lecture', ...(recordingMeta.recordedAt ? { 'X-Recorded-At': recordingMeta.recordedAt, 'X-Duration': String(recordingMeta.duration || 0), 'X-Name-Source': 'auto' } : {}) }, body: file });
      note = await cacheRemoteNote(data.note || data);
    }
    updateNoteList(note);
    if (!deferOpen && (state.note?.id || null) === fromNote) await openNote(note.id, note);
    toast(needsSync(note) ? (state.connected ? '已保存到此设备，正在自动上传' : '已保存到此设备，连接恢复后自动上传') : '电脑已收到，自动排队转录', false, true);
    if (!deferOpen) scheduleAutoSync(300);
    refreshAutomationStatus();
    return note;
  } catch (error) { toast(error.message, true); return false; }
  finally { state.uploading = false; $('#upload-button').disabled = false; $('#file-input').value = ''; }
}
let recordingDatabase;
async function recordingCache(action, value, owner = state.accountId) {
  if (!window.indexedDB || !owner) throw new Error('请先确认账号后使用录音暂存');
  if (!recordingDatabase) recordingDatabase = new Promise((resolve, reject) => {
    const opening = indexedDB.open('tingji-recording-recovery', 2);
    opening.onupgradeneeded = () => {
      const db = opening.result;
      if (!db.objectStoreNames.contains('metadata')) db.createObjectStore('metadata');
      if (!db.objectStoreNames.contains('chunks')) db.createObjectStore('chunks', { keyPath: 'index' });
      if (!db.objectStoreNames.contains('accountChunks')) { const store = db.createObjectStore('accountChunks', { keyPath: ['accountId', 'index'] }); store.createIndex('accountId', 'accountId'); }
    };
    opening.onsuccess = () => resolve(opening.result);
    opening.onerror = () => reject(opening.error);
  });
  const db = await recordingDatabase;
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(['metadata', 'chunks', 'accountChunks'], action === 'read' ? 'readonly' : 'readwrite');
    const metadata = transaction.objectStore('metadata'), chunks = transaction.objectStore('accountChunks');
    const output = {};
    const clearAccountChunks = () => { const cursor = chunks.index('accountId').openCursor(IDBKeyRange.only(owner)); cursor.onsuccess = () => { if (cursor.result) { cursor.result.delete(); cursor.result.continue(); } }; };
    if (action === 'clear') {
      metadata.delete('active:' + owner); clearAccountChunks();
      if (state.recoveryLegacy && state.localBrowser) { metadata.delete('active'); transaction.objectStore('chunks').clear(); state.recoveryLegacy = false; }
    } else if (action === 'read') {
      const meta = metadata.get('active:' + owner);
      meta.onsuccess = () => {
        output.meta = meta.result;
        if (!meta.result && state.localBrowser) {
          const legacy = metadata.get('active');
          legacy.onsuccess = () => {
            if (legacy.result && (!legacy.result.accountId || legacy.result.accountId === owner)) {
              output.meta = legacy.result; output.legacy = true;
              const pieces = transaction.objectStore('chunks').getAll(); pieces.onsuccess = () => { output.chunks = pieces.result; };
            }
          };
        }
      };
      const pieces = chunks.index('accountId').getAll(owner); pieces.onsuccess = () => { if (!output.legacy) output.chunks = pieces.result; };
    }
    transaction.oncomplete = () => resolve(output);
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error || new Error('录音暂存未完成'));
  });
}
function renderRecovery() { $('#recording-recovery').hidden = !state.recovery; }
async function restoreRecording() {
  try {
    const saved = await recordingCache('read');
    if (saved.meta && saved.chunks?.length) {
      state.recoveryLegacy = !!saved.legacy;
      state.recovery = new File(saved.chunks.sort((a, b) => a.index - b.index).map(c => c.blob), saved.meta.name, { type: saved.meta.mime });
      state.recovery.recordingMeta = { recordedAt: saved.meta.recordedAt || new Date(saved.meta.createdAt).toISOString(), duration: saved.meta.duration || 0, nameSource: 'auto' };
      renderRecovery();
    }
  } catch { /* Normal usage works even when a browser disables persistent storage. */ }
}

$('#save-recovery').addEventListener('click', async () => {
  if (!state.recovery) return;
  $('#save-recovery').disabled = true;
  try {
    if (await uploadFile(state.recovery)) { state.recovery = null; await recordingCache('clear').catch(() => {}); renderRecovery(); }
  } finally { $('#save-recovery').disabled = false; }
});
$('#download-recovery').addEventListener('click', () => {
  if (!state.recovery) return;
  const url = URL.createObjectURL(state.recovery);
  const a = el('a'); a.href = url; a.download = state.recovery.name; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
  toast('已下载备份');
});

$('#refresh-app').addEventListener('click', () => refreshApplication().catch(error => toast(error.message, true)));
$$('[data-history-filter]').forEach(button => button.addEventListener('click', () => {
  historyFilter = button.dataset.historyFilter; button.closest('details').open = false; renderHistory();
}));
$('#rename-form').addEventListener('submit', async event => {
  event.preventDefault();
  const id = $('#rename-note-id').value;
  const note = state.note?.id === id ? state.note : state.notes.find(item => item.id === id);
  if (!note) return;
  const button = $('button[type="submit"]', event.currentTarget); button.disabled = true;
  try { titleSave = saveTitle(note, $('#rename-note-title').value); await titleSave; closeDialog($('#rename-dialog')); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});
document.addEventListener('click', event => { if (!event.target.closest('.history-more, #note-menu')) hideNoteMenu(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') hideNoteMenu(); });
window.addEventListener('resize', hideNoteMenu);
$('#history-list').addEventListener('scroll', hideNoteMenu);
$('#new-note').addEventListener('click', async () => { await showWelcome(); if (!hasUnsaved()) $('#file-input').click(); });
$('#open-archive').addEventListener('click', () => workflows.openLibrary('pending').catch(error => toast(error.message, true)));
$('#cleanup-reminder').addEventListener('click', () => openArchive().catch(error => toast(error.message, true)));
$('#archive-dialog').addEventListener('close', releaseArchiveAudio);
$('#delete-device-audio').addEventListener('click', () => { if (localAudioSafe(state.note)) askDeleteAudio(state.note, 'device'); });
$('#cancel-delete-audio').addEventListener('click', () => closeDialog($('#delete-audio-dialog')));
$('#confirm-delete-audio').addEventListener('click', confirmDeleteAudio);
$('.brand').addEventListener('click', event => { event.preventDefault(); showWelcome(); });
$('#history-search').addEventListener('input', renderHistory);
$('#sidebar-toggle').addEventListener('click', () => {
  if (compactLayout()) {
    const open = $('#sidebar').classList.toggle('mobile-open');
    $('#sidebar-backdrop').hidden = !open;
    $('#sidebar-toggle').setAttribute('aria-expanded', String(open));
  } else {
    const collapsed = document.body.classList.toggle('sidebar-collapsed');
    $('#sidebar-toggle').setAttribute('aria-expanded', String(!collapsed));
  }
});
$('#sidebar-backdrop').addEventListener('click', closeSidebar);
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeSidebar(); });
$('#open-settings').addEventListener('click', openSettings);
$('#error-settings').addEventListener('click', openSettings);
$('#retry-note-process').addEventListener('click', () => beginProcess());
$('#configure-inline').addEventListener('click', openSettings);
$('#settings-form').addEventListener('submit', saveSettings);
$('#connection-label').addEventListener('click', () => openSyncDialog().catch(error => toast(error.message, true)));
$('#note-open-sync').addEventListener('click', () => openSyncDialog().catch(error => toast(error.message, true)));
$('#push-pending-notes').addEventListener('click', () => syncPendingNotes().catch(error => toast(error.message, true)));
$('#cancel-sync').addEventListener('click', () => { autoSyncPaused = true; cancelSyncRequested = true; state.syncClient?.cancelUploads(); setSyncProgress('正在暂停上传'); });
$('#download-selected-notes').addEventListener('click', downloadSelectedNotes);
$('#refresh-computer-notes').addEventListener('click', async () => { try { await refreshComputerList(); } catch (error) { $('#sync-error').textContent = error.message; $('#sync-error').hidden = false; } });
$('#copy-phone-url').addEventListener('click', () => copyText($('#sync-remote-url').href));
window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); state.installPrompt = event; $('#install-app').hidden = false; });
$('#install-app').addEventListener('click', async () => { if (state.installPrompt) { await state.installPrompt.prompt(); state.installPrompt = null; $('#install-app').hidden = true; } });
window.addEventListener('offline', () => { state.connected = false; clearTimeout(state.pollTimer); syncConnection(); $('#connection-error').hidden = false; $('#connection-error-text').textContent = '暂时离线，导入文件会保存在此设备；连接恢复后自动上传。'; renderNote(); });
window.addEventListener('online', () => {
  autoSyncRetryAfter = 0; bootstrap();
});
$('#retry-bootstrap').addEventListener('click', bootstrap);
$$('.dialog-close').forEach(button => button.addEventListener('click', () => closeDialog(button.closest('dialog'))));
$$('dialog').forEach(dialog => {
  dialog.addEventListener('click', event => { if (event.target === dialog) { const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeDialog(dialog); } });
  dialog.addEventListener('close', () => {
    if (dialog.id === 'settings-dialog') clearSecretInputs();
  });
});
$('#upload-button').addEventListener('click', () => $('#file-input').click());
$('#file-input').addEventListener('change', event => workflows.openImport(event.target.files).catch(error => toast(error.message, true)));
const zone = $('#drop-zone');
let dragDepth = 0;
zone.addEventListener('dragenter', event => { event.preventDefault(); dragDepth++; zone.classList.add('drag-over'); });
zone.addEventListener('dragover', event => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; });
zone.addEventListener('dragleave', event => { event.preventDefault(); if (--dragDepth <= 0) { dragDepth = 0; zone.classList.remove('drag-over'); } });
zone.addEventListener('drop', event => { event.preventDefault(); dragDepth = 0; zone.classList.remove('drag-over'); const files = event.dataTransfer.files; workflows.openImport(files).catch(error => toast(error.message, true)); });
window.addEventListener('dragover', event => event.preventDefault());
window.addEventListener('drop', event => event.preventDefault());
$('#note-title').addEventListener('focus', () => { titleSnapshot = state.note ? { ...state.note } : null; });
$('#note-title').addEventListener('change', () => {
  if (!state.note) return;
  const note = { ...(titleSnapshot?.id === state.note.id ? titleSnapshot : state.note) };
  titleSnapshot = null;
  const title = $('#note-title').value.trim();
  if (!title) { $('#note-title').value = note.title || '未命名笔记'; return; }
  titleSave = saveTitle(note, title).catch(error => { toast(error.message, true); $('#save-status').textContent = '标题尚未保存'; throw error; });
  titleSave.catch(() => {});
});
$('#primary-process').addEventListener('click', () => beginProcess());
for (const type of ['summary']) {
  $(`#edit-${type}`).addEventListener('click', () => startEditor(type));
  $(`#cancel-${type}-edit`).addEventListener('click', () => cancelEditor(type));
  $(`#save-${type}-edit`).addEventListener('click', () => saveEditor(type).catch(() => {}));
  $(`#${type}-editor`).addEventListener('input', () => { $('#save-status').textContent = '有未保存修改'; });
  $(`#copy-${type}`).addEventListener('click', () => copyText(state.editing === type ? $(`#${type}-editor`).value : state.note?.[type] || ''));
  $(`#download-${type}`).addEventListener('click', () => {
    const text = state.editing === type ? $(`#${type}-editor`).value : state.note?.[type] || '';
    if (!text) return;
    downloadText(text, filename(state.note.title, type === 'transcript' ? ' - 原文' : ''));
    toast('文本已下载');
  });
}
window.addEventListener('beforeunload', event => { if (state.recovery || hasUnsaved() || state.uploading || state.syncBusy || workflows?.isBusy()) { event.preventDefault(); event.returnValue = ''; } });
if (compactLayout()) $('#sidebar-toggle').setAttribute('aria-expanded', 'false');
workflows = installWorkflows({
  state, request, toast, closeSidebar, openSettings, openNote, startEditor,
  uploadFile: (file, metadata) => uploadFile(file, metadata, true),
  syncPendingNotes: () => syncPendingNotes(null, true),
  async refresh() {
    if (state.connected) {
      const data = await request('/api/notes'); state.serverNotes = data.notes || data;
      if (state.syncClient) await state.syncClient.cacheNotes(state.serverNotes);
    }
    state.notes = await mergedNotes(); renderHistory();
  },
  async accept(note) { const cached = await cacheRemoteNote(note); updateNoteList(cached); return cached; },
  async ensureRemote(note) {
    if (!needsSync(note)) return note;
    if (!state.syncClient) throw new Error('设备存储暂不可用，请重新连接');
    const result = await state.syncClient.pushNote(note.id);
    if (result.conflict || result.pendingEdits) throw new Error('这份录音有同步冲突，请先在上传与下载中处理');
    updateNoteList(result.note); return result.note;
  },
});

function renderAutomationStatus() {
  const a = state.automation;
  const message = !state.connected ? '电脑未连接。导入文件保存在此设备，连接恢复后自动上传。' : !a ? '自动处理状态暂不可用，点击刷新重新检查。' : a.blockedReason || (a.enabled === false ? '自动处理已暂停，可到设置恢复。' : a.message || (a.runningCount || a.queuedCount ? `正在处理 ${a.runningCount || 0} 份，排队 ${a.queuedCount || 0} 份。` : '自动处理已开启，等待新录音。'));
  $('#automation-status').textContent = message;
  $('#automation-settings').hidden = !state.connected || (!!a && !a.blockedReason && a.enabled !== false);
  $('#automation-enabled').checked = a?.enabled !== false;
  $('#automation-settings-status').textContent = message;
  $('#automation-inbox').textContent = a?.inboxPath ? `电脑收件文件夹：${a.inboxPath}` : '';
  const used = Number(a?.usedYuan), remaining = Number(a?.remainingYuan);
  $('#budget-usage').textContent = a && Number.isFinite(used) && Number.isFinite(remaining) ? `今日已预留 ¥${used.toFixed(2)}，剩余 ¥${remaining.toFixed(2)}。达到上限后暂停新任务。` : '达到上限后暂停新任务。';
}
async function refreshAutomationStatus() {
  if (state.connected) {
    try { state.automation = await request('/api/automation'); }
    catch { state.automation = null; }
  }
  renderAutomationStatus();
  if (state.note && !state.editing) { renderNote(); schedulePoll(); }
}
function scheduleAutoSync(delay = 1000) {
  clearTimeout(autoSyncTimer);
  if (!state.connected || autoSyncPaused) return;
  autoSyncTimer = setTimeout(() => syncPendingNotes(null, true).catch(error => toast(error.message, true)), delay);
}
async function refreshBackground() {
  if (backgroundRefreshing || document.hidden || state.uploading || state.syncBusy || workflows?.isBusy()) return;
  backgroundRefreshing = true;
  try {
    if (!state.connected) { await bootstrap(); return; }
    const data = await request('/api/notes');
    const previous = new Map(state.serverNotes.map(note => [note.id, note]));
    state.serverNotes = data.notes || [];
    for (const note of state.serverNotes) {
      const old = previous.get(note.id);
      if (!old || note.id === state.note?.id) continue;
      if (note.error && JSON.stringify(note.error) !== JSON.stringify(old.error)) toast(`${note.title || '录音'}：${typeof note.error === 'string' ? note.error : note.error.message || '处理未完成'}`, true, true);
      else if ((note.summary || note.hasSummary) && !(old.summary || old.hasSummary)) toast(`${note.title || '笔记'}已完成`, false, true);
    }
    if (state.syncClient) await state.syncClient.cacheNotes(state.serverNotes);
    state.notes = await mergedNotes(); renderHistory();
    await refreshAutomationStatus();
    if ($('#sync-dialog').open) await renderSyncDialog();
    if (!state.editing) scheduleAutoSync(500);
  } catch { /* Keep cached content; reconnect on the next refresh. */ }
  finally { backgroundRefreshing = false; }
}
$('#automation-settings').addEventListener('click', openSettings);
$('#automation-enabled').addEventListener('change', async event => {
  const desired = event.target.checked;
  if (!state.connected) { renderAutomationStatus(); toast('连接电脑后可更改自动处理。', true); return; }
  event.target.disabled = true;
  $('#automation-settings-status').textContent = '正在保存…';
  try { state.automation = await request('/api/automation', { method: 'POST', body: { enabled: desired } }); renderAutomationStatus(); renderNote(); schedulePoll(); toast(desired ? '自动处理已恢复' : '自动处理已暂停，当前任务继续完成'); }
  catch (error) { renderAutomationStatus(); $('#settings-error').textContent = error.message; $('#settings-error').hidden = false; }
  finally { event.target.disabled = false; }
});
$('#open-note-merge').addEventListener('click', event => { event.currentTarget.closest('details').open = false; });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshBackground(); });
setInterval(refreshBackground, 15000);

const bootstrapReady = bootstrap();
state.recoveryReady = bootstrapReady.then(() => restoreRecording());
registerOfflineShell().catch(() => {});
