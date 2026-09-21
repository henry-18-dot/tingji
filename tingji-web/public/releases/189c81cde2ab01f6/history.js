import {renderMarkdown} from './knowledge.js';

const views = new WeakMap();
const busyStatuses = new Set(['uploading', 'queued', 'preparing', 'transcribing', 'summarizing']);
const versionNames = {
  summary: '整理版本', manual: '手动保存',
  'before-links': '关联前的版本', 'course-links': '课次关联',
  'links-draft': '关联生成稿', conflict: '关联生成稿',
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function versionDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

function updateButtons(view) {
  for (const button of view.root.querySelectorAll('[data-restore-version]')) {
    const version = view.note.versions.find(item => item.id === button.dataset.restoreVersion);
    const isCurrent = version?.summary === view.note.summary;
    button.disabled = view.pending || busyStatuses.has(view.note.status) || isCurrent;
    button.textContent = isCurrent ? '当前内容' : '恢复这个版本';
  }
}

function confirmRestore(view, label) {
  const dialog = element('dialog');
  const form = element('form');
  form.method = 'dialog';
  const heading = element('div', 'dialog-heading');
  const title = element('h2', '', '恢复这个版本？');
  title.id = `restore-title-${view.note.id}`;
  dialog.setAttribute('aria-labelledby', title.id);
  heading.append(title);
  const text = element('p', 'small', `${label}将成为当前笔记，现有版本保留。`);
  const actions = element('div', 'form-actions');
  const restore = element('button', 'primary', '恢复版本');
  restore.value = 'restore';
  const cancel = element('button', 'secondary', '取消');
  cancel.value = 'cancel';
  cancel.autofocus = true;
  actions.append(restore, cancel);
  form.append(heading, text, actions);
  dialog.append(form);
  document.body.append(dialog);
  view.dialog = dialog;
  return new Promise(resolve => {
    dialog.addEventListener('close', () => {
      const accepted = dialog.returnValue === 'restore';
      dialog.remove();
      if (view.dialog === dialog) view.dialog = null;
      resolve(accepted);
    }, {once: true});
    dialog.showModal();
  });
}

async function restoreVersion(view, version, label) {
  if (view.pending || busyStatuses.has(view.note.status) || version.summary === view.note.summary) return;
  view.pending = true;
  updateButtons(view);
  const noteId = view.note.id;
  try {
    if (!await confirmRestore(view, label) || view.note?.id !== noteId) return;
    const {note} = await view.api(`/notes/${encodeURIComponent(noteId)}`, {
      method: 'PATCH', body: {summary: version.summary},
    });
    if (views.get(view.root) !== view || view.note?.id !== noteId || !view.root.isConnected) return;
    view.note = note;
    await view.onNoteChanged?.(note);
    view.toast?.('笔记已恢复。');
  } catch (error) {
    view.toast?.(error?.message || '恢复没有完成，请稍后再试。');
  } finally {
    view.pending = false;
    if (view.note) updateButtons(view);
  }
}

/** Render the persisted revisions below a note. Call with null to clear private state. */
export function renderHistory(note, root, {api, toast, onNoteChanged} = {}) {
  if (!root) return;
  let view = views.get(root);
  if (!note) {
    view?.dialog?.close();
    if (view) view.note = null;
    views.delete(root);
    root.replaceChildren();
    root.hidden = true;
    return;
  }
  const versions = Array.isArray(note.versions) ? note.versions : [];
  const signature = `${note.id}|${versions.map(version => `${version.id}:${version.kind}`).join('|')}`;
  if (view?.note.id === note.id && view.signature === signature && root.firstElementChild) {
    Object.assign(view, {note, api, toast, onNoteChanged});
    updateButtons(view);
    return;
  }
  const previousOpen = new Set(view?.note.id === note.id
    ? [...root.querySelectorAll('details[open][data-version-id]')].map(node => node.dataset.versionId) : []);
  const wasOpen = view?.note.id === note.id && root.firstElementChild?.open;
  view?.dialog?.close();
  view = {note, root, api, toast, onNoteChanged, signature, pending: false, dialog: null};
  views.set(root, view);
  root.replaceChildren();
  root.hidden = !versions.length;
  if (!versions.length) return;
  const history = element('details', 'generation-details');
  history.open = Boolean(wasOpen);
  history.append(element('summary', '', `历史版本 · ${versions.length}`));
  for (const version of versions) {
    const entry = element('details');
    entry.dataset.versionId = version.id;
    const name = versionNames[version.kind] || '保存版本';
    const label = [name, versionDate(version.createdAt)].filter(Boolean).join(' · ');
    entry.append(element('summary', '', label));
    const content = element('article', 'knowledge-document reader');
    content.style.color = 'var(--ink)';
    content.style.fontSize = '15px';
    const actions = element('div', 'form-actions');
    const restore = element('button', 'secondary', '恢复这个版本');
    restore.type = 'button';
    restore.dataset.restoreVersion = version.id;
    restore.addEventListener('click', () => restoreVersion(view, version, label));
    actions.append(restore);
    entry.append(content, actions);
    let rendered = false;
    const renderContent = () => {
      if (entry.open && !rendered) {
        renderMarkdown(version.summary || '', content);
        rendered = true;
      }
    };
    entry.addEventListener('toggle', renderContent);
    entry.open = previousOpen.has(version.id);
    if (entry.open) renderContent();
    history.append(entry);
  }
  root.append(history);
  updateButtons(view);
}
