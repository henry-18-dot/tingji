// Optional reading aids. All content enters through text nodes; links are http(s) only.
const mounted = new WeakMap();
const STORAGE_KEY = 'tingji.reading-preferences.v1';
const COLORS = ['#467463', '#77668b', '#52778b', '#8a714a', '#946961'];
let nextId = 0;
const element = (tag, className = '', text) => {
  const result = document.createElement(tag);
  result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
};
const copyIcon = () => {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
  for (const [tag, attrs] of [['rect', { x: 8, y: 8, width: 12, height: 13, rx: 2 }], ['path', { d: 'M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3' }]]) {
    const part = document.createElementNS(svg.namespaceURI, tag);
    for (const [name, value] of Object.entries(attrs)) part.setAttribute(name, value);
    svg.append(part);
  }
  return svg;
};
const safeUrl = value => {
  try {
    if (typeof value !== 'string' || !/^https?:\/\//i.test(value)) return null;
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
};
const ignoredText = 'a,button,code,pre,svg,math,.knowledge-math,input,textarea,select,summary,h1,h2,h3,h4,h5,h6,.knowledge-toc,.knowledge-meta,.reading-settings,.reading-popover,.reading-exploration';
function textNodes(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: node => node.parentElement?.closest(ignoredText) || !node.textContent.trim() ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode); return nodes;
}
function wholeOccurrence(text, word, from = 0) {
  let index = text.indexOf(word, from);
  while (index !== -1) {
    const before = text[index - 1] || '', after = text[index + word.length] || '';
    if (!(/[\p{L}\p{N}_]/u.test(word[0]) && /[A-Za-z0-9_]/.test(before)) &&
        !(/[\p{L}\p{N}_]/u.test(word.at(-1)) && /[A-Za-z0-9_]/.test(after))) return index;
    index = text.indexOf(word, index + word.length);
  }
  return -1;
}
function preferences() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    return { terms: stored.terms !== false, symbols: stored.symbols !== false };
  } catch { return { terms: true, symbols: true }; }
}
function termColor(group) {
  const known = { position: 0, orientation: 1, rotation: 2, translation: 3 };
  const index = known[group] ?? [...group].reduce((sum, char) => sum + char.codePointAt(0), 0) % COLORS.length;
  return COLORS[index];
}

// Deliberately accept only simple full symbols with a single alphanumeric subscript.
const GREEK = new Set(['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'theta', 'lambda', 'mu', 'nu', 'xi', 'pi', 'rho', 'sigma', 'tau', 'phi', 'chi', 'psi', 'omega']);
function symbolSpec(value) {
  const match = String(value).trim().match(/^(\\?[A-Za-z]+)_\{?([A-Za-z0-9]+)\}?$/);
  if (!match) return null;
  const base = match[1].replace(/^\\/, '');
  if (base.length !== 1 && !GREEK.has(base)) return null;
  return { id: `${base}_${match[2]}`, base: base.length === 1 ? base : `\\${base}`, subscript: match[2] };
}
function readSubscript(source, start) {
  let i = start; while (/\s/.test(source[i] || '') && i < source.length) i++;
  if (source[i++] !== '_') return null;
  while (/\s/.test(source[i] || '') && i < source.length) i++;
  if (source[i] === '{') {
    const end = source.indexOf('}', i + 1); if (end === -1) return null;
    const value = source.slice(i + 1, end).trim();
    return /^[A-Za-z0-9]+$/.test(value) ? { value, end: end + 1 } : null;
  }
  return /[A-Za-z0-9]/.test(source[i] || '') ? { value: source[i], end: i + 1 } : null;
}
function skipGroup(source, start) {
  let i = start; while (/\s/.test(source[i] || '') && i < source.length) i++;
  if (source[i] !== '{') return start;
  let depth = 1; i++;
  while (i < source.length && depth) {
    if (source[i] === '\\') { i += 2; continue; }
    if (source[i] === '{') depth++;
    if (source[i] === '}') depth--;
    i++;
  }
  return i;
}
function annotateTex(source, specs, nonce) {
  // Never permit source-supplied HTML/resource commands or macro definitions through this path.
  if (/\\(?:html\w*|href|url|includegraphics|def|gdef|edef|xdef|let|futurelet|newcommand|renewcommand|providecommand|newenvironment|csname|catcode)\b/.test(source)) return null;
  const byBase = new Map();
  specs.forEach(spec => { if (!byBase.has(spec.base)) byBase.set(spec.base, []); byBase.get(spec.base).push(spec); });
  const matches = []; let i = 0;
  while (i < source.length) {
    const start = i; let token = source[i++];
    if (token === '%') { const end = source.indexOf('\n', i); i = end === -1 ? source.length : end + 1; continue; }
    if (token === '\\') {
      const command = source.slice(start).match(/^\\(?:[A-Za-z]+|.)/);
      token = command?.[0] || token; i = start + token.length;
      if (/^\\(?:text\w*|mbox|operatorname|begin|end|label|tag|color|textcolor|colorbox|fcolorbox|hspace|vspace|kern|mkern|phantom|hphantom|vphantom)$/.test(token)) { i = skipGroup(source, i); continue; }
    }
    const candidates = byBase.get(token); if (!candidates) continue;
    const sub = readSubscript(source, i); if (!sub) continue;
    const spec = candidates.find(candidate => candidate.subscript === sub.value); if (!spec) continue;
    matches.push({ start, end: sub.end, spec, className: `${nonce}-${specs.indexOf(spec)}` }); i = sub.end;
  }
  if (!matches.length) return null;
  let tex = '', cursor = 0;
  for (const match of matches) {
    tex += source.slice(cursor, match.start) + `\\htmlClass{${match.className}}{${source.slice(match.start, match.end)}}`; cursor = match.end;
  }
  return { tex: tex + source.slice(cursor), matches, classes: new Set(matches.map(match => match.className)) };
}

/** Mount after Markdown and KaTeX rendering. Returns an idempotent cleanup function. */
export function mountReadingFeatures(root, data = {}) {
  if (!root?.querySelectorAll) return () => {};
  mounted.get(root)?.();
  const id = ++nextId, prefix = `reading-${id}`, prefs = preferences();
  const controller = new AbortController(), options = { signal: controller.signal };
  const changes = [], originalMath = new Map(), symbolEntries = [], timers = new Set();
  let disposed = false, activePanel = null, activeAnchor = null, highlighted = null;
  root.classList.add('knowledge-document', 'has-reading-features');
  const later = callback => { const timer = setTimeout(() => { timers.delete(timer); callback(); }, 1600); timers.add(timer); };
  const concepts = new Map((Array.isArray(data.concepts) ? data.concepts : []).filter(item => item && typeof item.id === 'string' && typeof item.term === 'string' && typeof item.question === 'string' && item.question.trim()).map(item => [item.id, item]));

  const status = element('span', 'reading-announcement'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); root.append(status);
  async function copy(text, button, container) {
    container.querySelector('.reading-copy-fallback')?.remove();
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(text);
      if (disposed || !button.isConnected) return;
      button.classList.add('is-copied'); button.setAttribute('aria-label', '已复制');
      status.textContent = '已复制';
      later(() => { button.classList.remove('is-copied'); button.setAttribute('aria-label', '复制问题'); status.textContent = ''; });
    } catch {
      if (disposed || !button.isConnected) return;
      const fallback = element('div', 'reading-copy-fallback');
      const label = element('label', '', '请手动复制'); label.htmlFor = `${prefix}-manual-${++nextId}`;
      const field = element('textarea'); field.id = label.htmlFor; field.value = text; field.readOnly = true; field.rows = 2;
      fallback.append(label, field); container.append(fallback); positionPanel(); field.focus(); field.select();
      status.textContent = '复制未完成，请手动复制';
    }
  }
  function copyButton(text, container) {
    const button = element('button', 'reading-copy'); button.type = 'button'; button.title = '复制问题'; button.setAttribute('aria-label', '复制问题'); button.append(copyIcon());
    button.addEventListener('click', () => copy(text, button, container), options); return button;
  }
  function closePanel(restoreFocus = false) {
    if (!activePanel) return;
    activePanel.hidden = true; activeAnchor?.setAttribute('aria-expanded', 'false');
    if (restoreFocus && activeAnchor?.isConnected) activeAnchor.focus({ preventScroll: true });
    activePanel = activeAnchor = null;
  }
  function positionPanel() {
    if (!activePanel || !activeAnchor?.isConnected) return;
    const margin = 12, rect = activeAnchor.getBoundingClientRect(), viewport = globalThis.visualViewport;
    const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
    const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
    activePanel.style.maxWidth = `${Math.max(160, width - 2 * margin)}px`;
    activePanel.style.maxHeight = `${height - 2 * margin}px`;
    const size = activePanel.getBoundingClientRect();
    activePanel.style.left = `${Math.max(left + margin, Math.min(rect.left, left + width - size.width - margin))}px`;
    const below = rect.bottom + 8, above = rect.top - size.height - 8;
    activePanel.style.top = `${Math.max(top + margin, Math.min(below + size.height <= top + height - margin ? below : above, top + height - size.height - margin))}px`;
  }
  function openPanel(panel, anchor) {
    if (activePanel === panel && activeAnchor === anchor) { closePanel(); return; }
    closePanel(); activePanel = panel; activeAnchor = anchor; panel.hidden = false; anchor.setAttribute('aria-expanded', 'true'); positionPanel(); panel.focus({ preventScroll: true });
  }
  function panel(className, label) {
    const result = element('div', `reading-popover ${className}`); result.id = `${prefix}-panel-${++nextId}`; result.hidden = true; result.tabIndex = -1;
    result.setAttribute('role', 'dialog'); result.setAttribute('aria-label', label); root.append(result); return result;
  }

  // A missing explicit concept marker may be attached to its first body-text occurrence.
  for (const concept of concepts.values()) {
    if ([...root.querySelectorAll('.concept-trigger')].some(trigger => trigger.dataset.concept === concept.id)) continue;
    if (!concept.term.trim()) continue;
    for (const textNode of textNodes(root)) {
      const index = wholeOccurrence(textNode.textContent, concept.term); if (index === -1) continue;
      const after = textNode.splitText(index), remaining = after.splitText(concept.term.length);
      const trigger = element('button', 'concept-trigger', concept.term); trigger.type = 'button'; trigger.dataset.concept = concept.id; after.replaceWith(trigger);
      changes.push(() => { if (trigger.isConnected) trigger.replaceWith(document.createTextNode(concept.term)); textNode.parentNode?.normalize(); });
      void remaining; break;
    }
  }
  const conceptPanel = concepts.size ? panel('reading-concept-popover', '概念问题') : null;
  for (const trigger of root.querySelectorAll('.concept-trigger')) {
    const concept = concepts.get(trigger.dataset.concept); if (!concept) continue;
    trigger.type = 'button'; trigger.setAttribute('aria-haspopup', 'dialog'); trigger.setAttribute('aria-expanded', 'false'); trigger.setAttribute('aria-controls', conceptPanel.id);
    trigger.addEventListener('click', () => {
      if (activePanel === conceptPanel && activeAnchor === trigger) { closePanel(); return; }
      conceptPanel.replaceChildren(); conceptPanel.setAttribute('aria-label', concept.term);
      const row = element('div', 'reading-question-row'), question = element('p', 'reading-question', concept.question.trim());
      row.append(question, copyButton(concept.question.trim(), conceptPanel)); conceptPanel.append(row);
      const links = (Array.isArray(concept.links) ? concept.links : []).map(link => ({ title: String(link?.title || '').trim(), url: safeUrl(link?.url) })).filter(link => link.title && link.url).slice(0, 3);
      if (links.length) {
        const list = element('ul', 'reading-links');
        for (const link of links) { const item = element('li'), anchor = element('a', '', link.title); anchor.href = link.url; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; item.append(anchor); list.append(item); }
        conceptPanel.append(list);
      }
      openPanel(conceptPanel, trigger);
    }, options);
    changes.push(() => { trigger.removeAttribute('aria-haspopup'); trigger.removeAttribute('aria-expanded'); trigger.removeAttribute('aria-controls'); });
  }

  const terms = (Array.isArray(data.terms) ? data.terms : []).filter(term => typeof term?.text === 'string' && term.text.trim() && typeof term.group === 'string').sort((a, b) => b.text.length - a.text.length);
  if (terms.length) for (const textNode of textNodes(root)) {
    const text = textNode.textContent; let position = 0, matched = false; const fragment = document.createDocumentFragment();
    while (position < text.length) {
      let found = null;
      for (const term of terms) { const index = wholeOccurrence(text, term.text, position); if (index !== -1 && (!found || index < found.index)) found = { index, term }; }
      if (!found) break;
      fragment.append(document.createTextNode(text.slice(position, found.index)));
      const mark = element('span', 'reading-term', found.term.text); mark.style.setProperty('--term-color', termColor(found.term.group)); mark.dataset.termGroup = found.term.group;
      fragment.append(mark); position = found.index + found.term.text.length; matched = true;
    }
    if (matched) { fragment.append(document.createTextNode(text.slice(position))); textNode.replaceWith(fragment); }
  }

  function clearHighlight() {
    root.querySelectorAll('.reading-symbol.is-highlighted').forEach(mark => mark.classList.remove('is-highlighted'));
    root.querySelectorAll('.reading-symbol-control').forEach(control => control.setAttribute('aria-pressed', 'false')); highlighted = null;
  }
  function highlight(symbol, scope) {
    if (!prefs.symbols) return;
    const same = highlighted?.symbol === symbol && highlighted?.scope === scope; clearHighlight(); if (same) return;
    highlighted = { symbol, scope };
    for (const entry of symbolEntries) if (entry.scope === scope) {
      for (const mark of entry.element.querySelectorAll('.reading-symbol')) if (mark.dataset.readingSymbol === symbol) mark.classList.add('is-highlighted');
      if (entry.single === symbol) entry.element.setAttribute('aria-pressed', 'true');
    }
  }
  const specs = [...new Map((Array.isArray(data.symbols) ? data.symbols : []).map(symbolSpec).filter(Boolean).map(spec => [spec.id, spec])).values()];
  function mountMath() {
    if (!globalThis.katex?.render || disposed || !specs.length) return;
    let scope = root;
    const nonce = `${prefix}-symbol-${Math.random().toString(36).slice(2)}`;
    for (const item of root.querySelectorAll('h2,.knowledge-math')) {
      if (item.tagName === 'H2') { scope = item; continue; }
      if (originalMath.has(item)) continue;
      const source = item.dataset.sourceTex; if (!source || item.classList.contains('is-pending')) continue;
      const annotated = annotateTex(source, specs, nonce); if (!annotated) continue;
      const fresh = document.createElement('span');
      try {
        globalThis.katex.render(annotated.tex, fresh, {
          throwOnError: true, displayMode: item.dataset.display === 'true' || item.classList.contains('knowledge-math--display'), output: 'htmlAndMathml', maxExpand: 1000,
          strict: code => code === 'htmlExtension' ? 'ignore' : 'warn',
          trust: context => context.command === '\\htmlClass' && annotated.classes.has(context.class),
        });
      } catch { continue; }
      const original = [...item.childNodes]; originalMath.set(item, original); item.replaceChildren(...fresh.childNodes);
      // Keep the accessible original TeX rather than the internal class wrappers.
      item.querySelector('annotation[encoding="application/x-tex"]')?.replaceChildren(document.createTextNode(source));
      const single = annotated.matches.length === 1 && source.trim() === source.slice(annotated.matches[0].start, annotated.matches[0].end) && !item.classList.contains('knowledge-math--display') ? annotated.matches[0].spec.id : null;
      const entry = { element: item, scope: item.closest('.knowledge-chapter') || scope, single }; symbolEntries.push(entry);
      for (const match of annotated.matches) for (const mark of item.getElementsByClassName(match.className)) {
        mark.classList.add('reading-symbol'); mark.dataset.readingSymbol = match.spec.id;
      }
      if (single) {
        item.classList.add('reading-symbol-control'); item.setAttribute('role', 'button'); item.setAttribute('aria-label', `突出本节的 ${single}`); item.setAttribute('aria-pressed', 'false'); item.tabIndex = prefs.symbols ? 0 : -1;
        item.addEventListener('click', () => highlight(single, entry.scope), options);
        item.addEventListener('keydown', event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); highlight(single, entry.scope); } }, options);
      } else item.addEventListener('click', event => { const mark = event.target.closest?.('.reading-symbol'); if (mark) highlight(mark.dataset.readingSymbol, entry.scope); }, options);
    }
  }
  mountMath();
  globalThis.addEventListener('load', mountMath, { ...options, once: true });

  for (const heading of root.querySelectorAll('h2,h3')) {
    if (heading.textContent.trim() !== '继续探索') continue;
    let list = heading.closest('.knowledge-chapter')?.querySelector('.chapter-body > ul,.chapter-body > ol') || heading.nextElementSibling;
    if (!list?.matches('ul,ol')) continue;
    list.classList.add('reading-exploration');
    for (const item of [...list.children].filter(child => child.tagName === 'LI').slice(0, 3)) {
      const question = item.textContent.trim(); if (!question) continue;
      const content = element('span', 'reading-exploration-text'); content.append(...item.childNodes); item.append(content, copyButton(question, item));
      changes.push(() => { if (content.isConnected) { item.replaceChildren(...content.childNodes); } });
    }
    changes.push(() => list.classList.remove('reading-exploration')); break;
  }

  const settings = element('div', 'reading-settings'), gear = element('button', 'reading-settings-toggle'); gear.type = 'button'; gear.setAttribute('aria-label', '阅读设置'); gear.title = '阅读设置'; gear.setAttribute('aria-expanded', 'false'); gear.setAttribute('aria-haspopup', 'dialog');
  // Conventional cog icon; no permanent settings heading or toolbar.
  const cog = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); cog.setAttribute('viewBox', '0 0 24 24'); cog.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(cog.namespaceURI, 'path'); path.setAttribute('d', 'M9.5 3.5h5l.5 2.2 1.6.9 2.2-.6 2.5 4.2-1.7 1.6v1.8l1.7 1.6-2.5 4.2-2.2-.6-1.6.9-.5 2.2h-5l-.5-2.2-1.6-.9-2.2.6-2.5-4.2 1.7-1.6v-1.8L2.7 10l2.5-4.2 2.2.6L9 5.5z');
  const circle = document.createElementNS(cog.namespaceURI, 'circle'); circle.setAttribute('cx', '12'); circle.setAttribute('cy', '12.7'); circle.setAttribute('r', '3'); cog.append(path, circle); gear.append(cog); settings.append(gear); root.prepend(settings);
  const settingsPanel = panel('reading-settings-popover', '阅读设置'); gear.setAttribute('aria-controls', settingsPanel.id);
  function syncPreferences() {
    root.classList.toggle('reading-terms-on', prefs.terms); root.classList.toggle('reading-symbols-on', prefs.symbols);
    for (const entry of symbolEntries) if (entry.single) { entry.element.tabIndex = prefs.symbols ? 0 : -1; entry.element.setAttribute('aria-disabled', String(!prefs.symbols)); }
    if (!prefs.symbols) clearHighlight();
  }
  for (const [key, label] of [['terms', '术语颜色'], ['symbols', '公式变量联动']]) {
    const row = element('label', 'reading-setting'), input = element('input'); input.type = 'checkbox'; input.checked = prefs[key];
    row.append(element('span', '', label), input); settingsPanel.append(row);
    input.addEventListener('change', () => { prefs[key] = input.checked; syncPreferences(); try { localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs)); } catch { /* Settings still work when storage is disabled. */ } }, options);
  }
  gear.addEventListener('click', () => openPanel(settingsPanel, gear), options); syncPreferences();
  document.addEventListener('pointerdown', event => { if (activePanel && !activePanel.contains(event.target) && !activeAnchor?.contains(event.target)) closePanel(); }, options);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') { if (activePanel) { event.preventDefault(); closePanel(true); } else clearHighlight(); } }, options);
  document.addEventListener('focusin', event => { if (activePanel && !activePanel.contains(event.target) && !activeAnchor?.contains(event.target)) closePanel(); }, options);
  globalThis.addEventListener('resize', positionPanel, options); globalThis.addEventListener('scroll', positionPanel, { ...options, capture: true, passive: true });
  globalThis.visualViewport?.addEventListener('resize', positionPanel, options);
  const cleanup = () => {
    if (disposed) return; disposed = true; controller.abort(); timers.forEach(clearTimeout); closePanel();
    for (const undo of changes.reverse()) undo();
    for (const [math, children] of originalMath) { math.replaceChildren(...children); math.classList.remove('reading-symbol-control'); for (const name of ['role', 'aria-label', 'aria-pressed', 'aria-disabled', 'tabindex']) math.removeAttribute(name); }
    root.querySelectorAll('.reading-term').forEach(mark => mark.replaceWith(document.createTextNode(mark.textContent)));
    settings.remove(); settingsPanel.remove(); conceptPanel?.remove(); status.remove(); root.classList.remove('has-reading-features', 'reading-terms-on', 'reading-symbols-on'); mounted.delete(root);
  };
  mounted.set(root, cleanup); return cleanup;
}
