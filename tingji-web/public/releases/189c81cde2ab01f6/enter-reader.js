// Reveal the existing, safely rendered document in source order. No source code runs.
const STORAGE = 'tingji.enter-reader.v1';
const OMIT = '.reading-settings,.reading-popover,.reading-announcement,.reading-directory,.knowledge-toc,.table-scroll-hint,.example-controls,.example-index';
const ATOMIC = 'figure,.knowledge-math--display,.knowledge-relation,.knowledge-tree';
const INTERACTIVE = 'input,textarea,select,button,a,summary,audio,video,[contenteditable]:not([contenteditable="false"]),[role="button"],[role="textbox"]';

function fingerprint(value) {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i++) hash = Math.imul(hash ^ value.charCodeAt(i), 16777619);
  return `${value.length}-${(hash >>> 0).toString(36)}`;
}
function read(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } }
function write(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} }

// Operate on a download clone, leaving the reader and its saved progress alone.
export function restoreFullDocument(root) {
  root.classList.remove('enter-reading');
  root.querySelectorAll('[data-enter-hidden]').forEach(node => node.removeAttribute('data-enter-hidden'));
  root.querySelectorAll('.enter-current').forEach(node => node.classList.remove('enter-current'));
  root.querySelectorAll('.enter-line,.enter-code-line').forEach(node => node.replaceWith(...node.childNodes));
  root.querySelectorAll('details').forEach(node => { node.open = true; });
  return root;
}

export function createEnterReader({root, modes, controls, previous, next, restart, progress, hint}) {
  let context = null, available = false, mode = 'continuous', count = 0;
  let steps = [], groups = [], omitted = [], opened = [], lines = [], paragraphs = [], codeBlocks = [];
  const buttons = [...modes.querySelectorAll('[data-reading-mode]')];
  const focusReader = () => root.focus({preventScroll:true});
  const modeKey = () => `${STORAGE}.${encodeURIComponent(context.userId)}.mode`;
  const progressKey = () => `${STORAGE}.${encodeURIComponent(context.userId)}.${encodeURIComponent(context.noteId)}`;
  const save = () => { if (context) write(progressKey(), {revision:context.revision, count}); };

  function resetDocument() {
    for (const node of [...steps, ...groups.map(item => item.node), ...omitted]) {
      node.removeAttribute('data-enter-hidden'); node.classList.remove('enter-current');
    }
    for (const [node, wasOpen] of opened) node.open = wasOpen;
    for (const [node, children] of paragraphs) node.replaceChildren(...children);
    for (const [node, children] of codeBlocks) node.replaceChildren(...children);
    root.classList.remove('enter-reading');
    steps = []; groups = []; omitted = []; opened = []; lines = []; paragraphs = []; codeBlocks = [];
  }

  function splitParagraph(node) {
    // Keep existing inline elements (and their event listeners) intact. Newlines
    // and sentence endings in text become reading steps; formulas stay whole.
    let line = null;
    const start = () => {
      if (!line) { line = document.createElement('span'); line.className = 'enter-line'; lines.push(line); node.append(line); }
      return line;
    };
    const finish = () => { if (line?.textContent.trim() || line?.querySelector('img,svg,math')) steps.push(line); else line?.remove(); line = null; };
    const children = [...node.childNodes];
    paragraphs.push([node, children]);
    for (const child of children) {
      if (child.nodeType === Node.TEXT_NODE) {
        const pieces = child.textContent.split(/(\n+|(?<=[。！？])(?:[”’」』])?|(?<=[.!?])\s+(?=[A-Z]))/u);
        child.remove();
        for (const piece of pieces) {
          if (!piece) { if (line && /[。！？][”’」』]?$/.test(line.textContent)) finish(); continue; }
          if (/^\n+$/.test(piece)) { start().append(document.createTextNode(piece)); finish(); continue; }
          if (/^\s+$/.test(piece) && /[.!?]$/.test(line?.textContent || '')) { start().append(document.createTextNode(piece)); finish(); continue; }
          start().append(document.createTextNode(piece));
          if (/[。！？][”’」』]?$/.test(piece) || /[.!?]\s+$/.test(piece)) finish();
        }
      } else if (child.nodeType === Node.ELEMENT_NODE && child.tagName === 'BR') {
        child.remove(); start().append(child); finish();
      } else { start().append(child); if (/[。！？][”’」』]?$/.test(child.textContent || '')) finish(); }
    }
    finish();
  }

  function splitCode(pre) {
    const code = pre.querySelector(':scope > code') || pre;
    const children = [...code.childNodes], source = code.textContent;
    codeBlocks.push([code, children]); code.replaceChildren();
    let current = null, leading = '';
    for (const text of source.split(/(?<=\n)/u)) {
      if (!text.trim()) {
        if (current) current.append(document.createTextNode(text)); else leading += text;
        continue;
      }
      current = document.createElement('span'); current.className = 'enter-code-line';
      current.textContent = leading + text; leading = '';
      code.append(current); steps.push(current);
    }
    if (!current) { code.textContent = leading; omitted.push(pre); }
  }

  function collect(node) {
    if (!(node instanceof Element)) return;
    if (node.matches(OMIT)) {
      if (!node.matches('.reading-settings,.reading-popover,.reading-announcement')) omitted.push(node);
      return;
    }
    if (node.hidden) return;
    if (node.matches(ATOMIC) || /^H[1-6]$/.test(node.tagName) || ['LI','TR','HR'].includes(node.tagName)) {
      steps.push(node); return;
    }
    if (node.tagName === 'DETAILS') { opened.push([node, node.open]); node.open = true; }
    const first = steps.length;
    if (node.tagName === 'PRE') splitCode(node);
    else if (node.tagName === 'P' || node.tagName === 'BLOCKQUOTE') splitParagraph(node);
    else if (node.tagName === 'SUMMARY' && !node.querySelector('h1,h2,h3,h4,h5,h6')) steps.push(node);
    else for (const child of [...node.children]) collect(child);
    if (steps.length > first && !steps.includes(node)) groups.push({node, first});
    else if (steps.length === first && node.textContent.trim() && !node.children.length) steps.push(node);
  }

  function apply({scroll = false} = {}) {
    steps.forEach((node, index) => {
      node.dataset.enterHidden = String(index >= count);
      node.classList.toggle('enter-current', index === count - 1);
    });
    for (const {node, first} of groups) node.dataset.enterHidden = String(first >= count);
    for (const node of omitted) node.dataset.enterHidden = 'true';
    previous.disabled = count === 0;
    next.disabled = count >= steps.length;
    restart.disabled = count === 0;
    progress.textContent = `${count} / ${steps.length}`;
    next.textContent = count >= steps.length && steps.length ? '已读完' : '下一行';
    hint.textContent = count === 0 ? '按 Enter 逐行阅读' : count >= steps.length ? '本篇已读完' : 'Enter 下一行 · Shift + Enter 上一行';
    if (scroll && count) {
      const node = steps[count - 1], bounds = node.getBoundingClientRect(), bar = controls.getBoundingClientRect();
      if (bounds.bottom > innerHeight - 28 || bounds.top < Math.max(16, bar.bottom + 12)) node.scrollIntoView({block:'center', behavior:'instant'});
    }
  }

  function build() {
    resetDocument();
    for (const node of [...root.children]) collect(node);
    root.classList.add('enter-reading');
    count = Math.max(0, Math.min(steps.length, Number.isInteger(count) ? count : 0));
    apply();
  }

  function updateControls() {
    modes.hidden = !available;
    controls.hidden = !available || mode !== 'enter';
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.readingMode === mode)));
  }

  function setMode(value) {
    if (!context || !['continuous','enter'].includes(value)) return;
    if (value !== mode) {
      mode = value;
      if (mode === 'enter') build(); else resetDocument();
      write(modeKey(), mode);
    }
    updateControls(); focusReader();
  }

  function move(delta) {
    if (!available || mode !== 'enter') return;
    const target = Math.max(0, Math.min(steps.length, count + delta));
    if (target === count) return;
    count = target; apply({scroll:true}); save();
  }

  buttons.forEach(button => button.addEventListener('click', () => setMode(button.dataset.readingMode)));
  previous.addEventListener('click', () => { move(-1); focusReader(); });
  next.addEventListener('click', () => { move(1); focusReader(); });
  restart.addEventListener('click', () => { count = 0; apply(); save(); focusReader(); root.scrollIntoView({block:'start',behavior:'instant'}); });
  root.addEventListener('click', event => {
    if (mode === 'enter' && event.target.closest('summary') && !event.target.closest('a,button')) event.preventDefault();
  });
  document.addEventListener('keydown', event => {
    if (!available || mode !== 'enter' || event.key !== 'Enter' || event.defaultPrevented || event.repeat || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    if (root.closest('[hidden]') || event.target.closest?.(INTERACTIVE)) return;
    if ([...document.querySelectorAll('dialog[open],[role="dialog"]:not([hidden])')].some(node => node.getClientRects().length)) return;
    event.preventDefault(); move(event.shiftKey ? -1 : 1);
  });

  return {
    mount({userId, noteId, source}) {
      const revision = fingerprint(source || '');
      if (context?.userId === userId && context?.noteId === noteId && context?.revision === revision) return;
      resetDocument(); context = {userId:String(userId || 'local'), noteId, revision};
      mode = read(modeKey()) === 'enter' ? 'enter' : 'continuous';
      const saved = read(progressKey()); count = saved?.revision === revision && Number.isInteger(saved.count) ? saved.count : 0;
      if (source && mode === 'enter') build();
      updateControls();
    },
    beforeRender() { resetDocument(); context = null; },
    setVisible(value) { available = Boolean(value); updateControls(); },
    reset() { resetDocument(); context = null; mode = 'continuous'; count = 0; available = false; updateControls(); },
  };
}
