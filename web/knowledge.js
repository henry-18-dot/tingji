// Small, offline document renderer. Source text never becomes executable HTML.
import { mountReadingFeatures } from './reading-interactions.js';
import { renderRelation, renderTree } from './knowledge-relations.js';
const node = (tag, className = '', text) => {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
};
let documentId = 0;

const mathOptions = { throwOnError: false, trust: false, strict: 'warn', maxExpand: 1000, output: 'htmlAndMathml' };

function renderMathNode(element) {
  const katex = globalThis.katex;
  if (!katex?.render) return false;
  try {
    katex.render(element.dataset.tex || '', element, { ...mathOptions, displayMode: element.dataset.display === 'true' });
    element.classList.remove('is-pending');
    delete element.dataset.tex;
    return true;
  } catch {
    element.classList.add('has-error');
    return false;
  }
}

function math(tex, display, delimiters) {
  const element = node(display ? 'div' : 'span', `knowledge-math${display ? ' knowledge-math--display' : ''} is-pending`);
  element.dataset.tex = tex;
  element.dataset.sourceTex = tex;
  element.dataset.display = String(display);
  element.textContent = `${delimiters[0]}${tex}${delimiters[1]}`;
  renderMathNode(element);
  return element;
}

export function renderPendingMath(root = document) {
  root.querySelectorAll?.('.knowledge-math[data-tex]').forEach(renderMathNode);
}

if (globalThis.addEventListener) globalThis.addEventListener('load', () => renderPendingMath(document), { once: true });

export function appendInline(parent, text) {
  const tokens = /(\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<!\\)\$([^$\n]+?)(?<!\\)\$|\*\*([^*\n]+)\*\*|`([^`\n]+)`|\*([^*\n]+)\*|\[([^\]\n]+)\]\(([^\s)]+)\)|(B\s*站搜索[：:]))/g;
  let offset = 0;
  for (const match of String(text).matchAll(tokens)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    if (match[11] !== undefined) parent.append(node('span', 'bilibili-label', match[11]));
    else if (match[2] !== undefined) parent.append(math(match[2], true, ['$$', '$$']));
    else if (match[3] !== undefined) parent.append(math(match[3], true, ['\\[', '\\]']));
    else if (match[4] !== undefined) parent.append(math(match[4], false, ['\\(', '\\)']));
    else if (match[5] !== undefined) parent.append(math(match[5], false, ['$', '$']));
    else if (match[10]) {
      if (/^concept:[a-zA-Z][\w-]{0,63}$/.test(match[10])) {
        const trigger = node('button', 'concept-trigger', match[9]);
        trigger.type = 'button'; trigger.dataset.concept = match[10].slice(8);
        parent.append(trigger);
      } else if (/^https?:\/\//i.test(match[10])) {
        const link = node('a', '', match[9]);
        link.href = match[10]; link.target = '_blank'; link.rel = 'noopener noreferrer';
        parent.append(link);
      } else parent.append(document.createTextNode(match[0]));
    } else parent.append(node(match[6] ? 'strong' : match[7] ? 'code' : 'em', '', match[6] || match[7] || match[8]));
    offset = match.index + match[0].length;
  }
  parent.append(document.createTextNode(String(text).slice(offset)));
}

function cells(line) {
  const value = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  const result = []; let cell = '', code = false;
  for (let i = 0; i < value.length; i++) {
    const char = value[i];
    if (char === '\\' && value[i + 1] === '|') { cell += '|'; i++; }
    else if (char === '`') { code = !code; cell += char; }
    else if (char === '|' && !code) { result.push(cell.trim()); cell = ''; }
    else cell += char;
  }
  result.push(cell.trim());
  return result;
}

function fence(line) { return line.match(/^\s*(`{3,}|~{3,})(.*)$/); }
function closingFence(line, opening) {
  const match = fence(line);
  return match && match[1][0] === opening[1][0] && match[1].length >= opening[1].length && !match[2].trim();
}

const SVG_NS = 'http://www.w3.org/2000/svg';
const SVG_ELEMENTS = new Set(['svg', 'g', 'line', 'rect', 'circle', 'ellipse', 'polyline', 'polygon', 'path', 'text', 'tspan', 'title', 'desc', 'defs', 'marker']);
const SVG_NUMBER_ATTRIBUTES = new Set(['x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'width', 'height', 'stroke-width', 'stroke-dashoffset', 'font-size', 'markerwidth', 'markerheight', 'refx', 'refy']);
const SVG_ENUM_ATTRIBUTES = {
  'stroke-linecap': new Set(['butt', 'round', 'square']), 'stroke-linejoin': new Set(['miter', 'round', 'bevel']),
  'text-anchor': new Set(['start', 'middle', 'end']), 'dominant-baseline': new Set(['auto', 'middle', 'central', 'hanging', 'alphabetic']),
  'font-weight': new Set(['400', '700']), 'vector-effect': new Set(['non-scaling-stroke']),
};
const SVG_COLORS = new Map([
  ['black', '#000'], ['#000', '#000'], ['#000000', '#000'], ['currentcolor', '#000'],
  ['white', '#fff'], ['#fff', '#fff'], ['#ffffff', '#fff'], ['none', 'none'],
]);
const SVG_NUMBER = '[-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[eE][-+]?\\d+)?';
const svgNumberPattern = new RegExp(`^${SVG_NUMBER}$`);
const svgNumberListPattern = new RegExp(`^\\s*${SVG_NUMBER}(?:[\\s,]+${SVG_NUMBER})*\\s*$`);
const svgViewBoxPattern = new RegExp(`^\\s*${SVG_NUMBER}[\\s,]+${SVG_NUMBER}[\\s,]+${SVG_NUMBER}[\\s,]+${SVG_NUMBER}\\s*$`);
const svgTransformPattern = new RegExp(`^(?:\\s*(?:translate|scale|rotate|matrix)\\(\\s*${SVG_NUMBER}(?:[\\s,]+${SVG_NUMBER}){0,5}\\s*\\)\\s*)+$`);

function cleanSvgAttribute(name, value) {
  const lower = name.toLowerCase(), trimmed = String(value).trim();
  if (SVG_NUMBER_ATTRIBUTES.has(lower)) return svgNumberPattern.test(trimmed) ? trimmed : null;
  if (lower === 'viewbox') return svgViewBoxPattern.test(trimmed) ? trimmed.replace(/,/g, ' ') : null;
  if (lower === 'points' || lower === 'stroke-dasharray') return trimmed.length <= 2000 && svgNumberListPattern.test(trimmed) ? trimmed : null;
  if (lower === 'd') return trimmed.length <= 8000 && /^[MmZzLlHhVvCcSsQqTtAaEe0-9+\-.,\s]+$/.test(trimmed) ? trimmed : null;
  if (lower === 'transform') return trimmed.length <= 500 && svgTransformPattern.test(trimmed) ? trimmed : null;
  if (lower === 'fill' || lower === 'stroke') return SVG_COLORS.get(trimmed.toLowerCase()) ?? null;
  if (lower === 'orient') return ['auto', 'auto-start-reverse'].includes(trimmed) || svgNumberPattern.test(trimmed) ? trimmed : null;
  if (SVG_ENUM_ATTRIBUTES[lower]) return SVG_ENUM_ATTRIBUTES[lower].has(trimmed.toLowerCase()) ? trimmed.toLowerCase() : null;
  if (lower === 'preserveaspectratio') return /^(?:none|x(?:Min|Mid|Max)Y(?:Min|Mid|Max)(?:\s+(?:meet|slice))?)$/.test(trimmed) ? trimmed : null;
  return null;
}

function copySvgNode(source, state) {
  if (state.count++ > 300 || source.nodeType !== Node.ELEMENT_NODE) return null;
  const tag = source.localName?.toLowerCase();
  if (!SVG_ELEMENTS.has(tag)) return null;
  const clean = document.createElementNS(SVG_NS, tag);
  for (const attribute of source.attributes) {
    const lower = attribute.name.toLowerCase();
    if (lower === 'id' && tag === 'marker' && state.markers.has(attribute.value)) {
      clean.id = state.markers.get(attribute.value); continue;
    }
    if (['marker-start', 'marker-mid', 'marker-end'].includes(lower)) {
      const reference = attribute.value.match(/^url\(#([A-Za-z][\w-]*)\)$/);
      if (reference && state.markers.has(reference[1])) clean.setAttribute(lower, `url(#${state.markers.get(reference[1])})`);
      continue;
    }
    const value = cleanSvgAttribute(attribute.name, attribute.value);
    const names = {viewbox:'viewBox', markerwidth:'markerWidth', markerheight:'markerHeight', refx:'refX', refy:'refY', preserveaspectratio:'preserveAspectRatio'};
    if (value !== null) clean.setAttribute(names[lower] || lower, value);
  }
  for (const child of source.childNodes) {
    if (child.nodeType === Node.TEXT_NODE && ['text', 'tspan', 'title', 'desc'].includes(tag)) clean.append(document.createTextNode((child.textContent || '').slice(0, 500)));
    else if (child.nodeType === Node.ELEMENT_NODE) { const copied = copySvgNode(child, state); if (copied) clean.append(copied); }
  }
  if (['line', 'path', 'polyline'].includes(tag)) {
    if (!clean.hasAttribute('stroke')) clean.setAttribute('stroke', '#000');
    if (!clean.hasAttribute('fill')) clean.setAttribute('fill', 'none');
  } else if (['rect', 'circle', 'ellipse', 'polygon'].includes(tag)) {
    if (!clean.hasAttribute('stroke')) clean.setAttribute('stroke', '#000');
    if (!clean.hasAttribute('fill')) clean.setAttribute('fill', '#fff');
  } else if (['text', 'tspan'].includes(tag)) {
    clean.setAttribute('fill', '#000'); clean.removeAttribute('stroke');
    if (!clean.hasAttribute('font-weight')) clean.setAttribute('font-weight', '400');
  }
  return clean;
}

function svgDiagram(source, caption) {
  if (!source.trim() || source.length > 50000 || /<!DOCTYPE/i.test(source)) return null;
  const parsed = new DOMParser().parseFromString(source, 'image/svg+xml');
  if (parsed.querySelector('parsererror') || parsed.documentElement.localName?.toLowerCase() !== 'svg') return null;
  const markerPrefix = `diagram-${++documentId}-`;
  const markers = new Map([...parsed.querySelectorAll('marker[id]')].filter(item => /^[A-Za-z][\w-]*$/.test(item.id)).map(item => [item.id, markerPrefix + item.id]));
  const svg = copySvgNode(parsed.documentElement, { count: 0, markers });
  if (!svg) return null;
  svg.setAttribute('viewBox', svg.getAttribute('viewBox') || '0 0 800 420');
  svg.setAttribute('width', '100%'); svg.removeAttribute('height'); svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', (caption || svg.querySelector('title')?.textContent || '二维机械简图').slice(0, 120));
  const background = document.createElementNS(SVG_NS, 'rect');
  background.setAttribute('x', '0'); background.setAttribute('y', '0'); background.setAttribute('width', '100%'); background.setAttribute('height', '100%');
  background.setAttribute('fill', '#fff'); background.setAttribute('stroke', 'none'); svg.prepend(background);
  const figure = node('figure', 'knowledge-diagram'); figure.append(svg);
  if (caption) figure.append(node('figcaption', '', caption.slice(0, 120)));
  return figure;
}

function examples(title, lines, render, id) {
  const groups = []; let current = null, opening = null;
  for (const line of lines) {
    if (opening) { if (closingFence(line, opening)) opening = null; }
    else if (fence(line)) opening = fence(line);
    const heading = !opening && line.match(/^###\s+(.+)$/);
    if (heading) { current = { title: heading[1], lines: [] }; groups.push(current); }
    else { if (!current) { current = { title, lines: [] }; groups.push(current); } current.lines.push(line); }
  }
  const usable = groups.filter(group => group.lines.some(line => line.trim()));
  const section = node('section', 'knowledge-examples'), header = node('div', 'examples-header'), heading = node('h3', '', title);
  heading.id = `${id}-label`; section.setAttribute('aria-labelledby', heading.id); header.append(heading); section.append(header);
  const track = node('div', 'example-track'); track.id = `${id}-track`;
  if (usable.length > 1) { track.tabIndex = 0; track.setAttribute('role', 'region'); track.setAttribute('aria-label', `${title}，左右方向键切换`); }
  section.append(track);
  for (const [i, group] of usable.entries()) {
    const card = node('article', 'example-card');
    if (usable.length > 1) card.append(node('span', 'example-index', `${String(i + 1).padStart(2, '0')} / ${String(usable.length).padStart(2, '0')}`));
    const name = node('h4'); appendInline(name, group.title); card.append(name, render(group.lines, false)); track.append(card);
  }
  if (!usable.length) section.append(render(lines, false));
  if (usable.length <= 1) { section.classList.add('single-example'); return section; }
  const controls = node('div', 'example-controls'), previous = node('button', '', '←'), next = node('button', '', '→'), position = node('span', 'example-position');
  previous.type = next.type = 'button'; previous.setAttribute('aria-label', '上一个例子'); next.setAttribute('aria-label', '下一个例子');
  previous.setAttribute('aria-controls', track.id); next.setAttribute('aria-controls', track.id); position.setAttribute('role', 'status'); position.setAttribute('aria-live', 'polite');
  controls.append(previous, position, next); header.append(controls);
  let index = 0;
  const update = () => { index = Math.max(0, Math.min(usable.length - 1, Math.round(track.scrollLeft / (track.clientWidth + 16)))); previous.disabled = index === 0; next.disabled = index === usable.length - 1; position.textContent = `${index + 1} / ${usable.length}`; };
  const move = delta => { const target = Math.max(0, Math.min(usable.length - 1, index + delta)); track.scrollTo({ left: target * (track.clientWidth + 16), behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' }); };
  previous.addEventListener('click', () => move(-1)); next.addEventListener('click', () => move(1)); track.addEventListener('scroll', update, { passive: true });
  track.addEventListener('keydown', event => { if (event.target === track && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); move(event.key === 'Home' ? -usable.length : event.key === 'End' ? usable.length : event.key === 'ArrowRight' ? 1 : -1); } });
  update(); return section;
}

function detail(title, body, render, meta = false) {
  const element = node('details', `knowledge-details${meta ? ' knowledge-details--meta' : ''}`), summary = node('summary'), content = node('div', 'details-body');
  appendInline(summary, title); content.append(render(body, false)); element.append(summary, content); return element;
}

export function renderMarkdown(text, target, options = {}) {
  target.readingCleanup?.();
  const prefix = `knowledge-${++documentId}`, headings = [], metadata = []; let sectionId = 0;
  let study = null;
  const render = (lines, components = true) => {
    const fragment = document.createDocumentFragment(); let paragraph = [], list = null;
    const flush = () => { if (paragraph.length) { const p = node('p'); appendInline(p, paragraph.join('\n')); fragment.append(p); paragraph = []; } list = null; };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i], opening = fence(line), mathOpening = line.trim();
      if (mathOpening === '$$' || mathOpening === '\\[') {
        const closing = mathOpening === '$$' ? '$$' : '\\]', body = []; let end = i + 1;
        for (; end < lines.length && lines[end].trim() !== closing; end++) body.push(lines[end]);
        if (end < lines.length) { flush(); fragment.append(math(body.join('\n'), true, [mathOpening, closing])); i = end; continue; }
      }
      if (opening) {
        flush(); const body = []; let closed = false;
        while (++i < lines.length) { if (closingFence(lines[i], opening)) { closed = true; break; } body.push(lines[i]); }
        const language = opening[2].trim().toLowerCase();
        if (language === 'study' && closed) {
          try {
            const value = JSON.parse(body.join('\n'));
            if (value && typeof value === 'object' && !Array.isArray(value)) study = value;
          } catch { fragment.append(node('p', 'reading-error', '探索内容格式有误。')); }
          continue;
        }
        if (language === 'mermaid' && closed) { fragment.append(renderRelation(body.join('\n'))); continue; }
        if (language === 'tree' && closed) { fragment.append(renderTree(body.join('\n'))); continue; }
        const svg = opening[2].trim().match(/^svg(?:\s+(.+))?$/i), diagram = svg && closed ? svgDiagram(body.join('\n'), svg[1]?.trim()) : null;
        if (diagram) fragment.append(diagram); else { const pre = node('pre'); pre.append(node('code', '', body.join('\n'))); fragment.append(pre); }
        continue;
      }
      const directive = components && line.match(/^:::(details|examples)\s+(.+)$/);
      if (directive) {
        let end = i + 1, codeFence = null;
        for (; end < lines.length; end++) { if (codeFence) { if (closingFence(lines[end], codeFence)) codeFence = null; } else if (fence(lines[end])) codeFence = fence(lines[end]); else if (/^:::\s*$/.test(lines[end])) break; }
        if (end < lines.length) {
          flush(); const body = lines.slice(i + 1, end);
          if (directive[1] === 'details') {
            const isMeta = /^(?:课程信息|来源定位|课程信息[与和、/]来源定位)$/.test(directive[2].trim()), element = detail(directive[2], body, render, isMeta);
            if (isMeta) metadata.push(element); else fragment.append(element);
          } else fragment.append(examples(directive[2], body, render, `${prefix}-examples-${++sectionId}`));
          i = end; continue;
        }
      }
      if (!line.trim()) { flush(); continue; }
      const heading = line.match(/^\s*(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        flush(); const h = node(`h${heading[1].length}`); appendInline(h, heading[2]);
        if (components && heading[1] === '#' && h.textContent === options.title) continue;
        h.id = `${prefix}-section-${++sectionId}`; h.tabIndex = -1; fragment.append(h); if (components && heading[1] === '##') headings.push(h); continue;
      }
      if (line.includes('|') && i + 1 < lines.length) {
        const labels = cells(line), separators = cells(lines[i + 1]);
        if (labels.length > 1 && labels.length === separators.length && separators.every(cell => /^:?-{3,}:?$/.test(cell))) {
          flush(); const wrap = node('div', 'knowledge-table'); wrap.tabIndex = 0; wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', '知识比较表，可横向滚动');
          const table = node('table'), head = node('thead'), row = node('tr'), body = node('tbody');
          for (const label of labels) { const th = node('th'); th.scope = 'col'; appendInline(th, label); row.append(th); }
          head.append(row); i += 2;
          for (; i < lines.length && lines[i].trim() && lines[i].includes('|'); i++) {
            const values = cells(lines[i]); if (values.length !== labels.length) break;
            const tr = node('tr'); for (const [index, value] of values.entries()) { const td = node('td'); appendInline(td, value); if (separators[index].endsWith(':')) td.style.textAlign = separators[index].startsWith(':') ? 'center' : 'right'; tr.append(td); } body.append(tr);
          }
          i--; table.append(head, body); wrap.append(table); fragment.append(wrap); if (labels.length > 2) fragment.append(node('span', 'table-scroll-hint', '左右滑动查看完整表格 →')); continue;
        }
      }
      if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { flush(); fragment.append(node('hr')); continue; }
      const item = line.match(/^\s*(?:([-*+])|(\d+)[.)、])\s+(.+)$/);
      if (item) { if (paragraph.length) flush(); const tag = item[1] ? 'UL' : 'OL'; if (!list || list.tagName !== tag) { list = node(tag.toLowerCase()); if (item[2]) list.start = Number(item[2]); fragment.append(list); } const li = node('li'); appendInline(li, item[3]); list.append(li); continue; }
      if (/^>\s?/.test(line)) { flush(); const quote = node('blockquote'); appendInline(quote, line.replace(/^>\s?/, '')); fragment.append(quote); continue; }
      list = null; paragraph.push(line);
    }
    flush(); return fragment;
  };
  const fragment = render(String(text || '').replace(/\r\n?/g, '\n').split('\n'));
  if (study) {
    for (const heading of headings) {
      const chapter = node('details', 'knowledge-chapter'), summary = node('summary'), content = node('div', 'chapter-body');
      chapter.open = true; heading.before(chapter); summary.append(heading); chapter.append(summary, content);
      let sibling = chapter.nextSibling;
      while (sibling && !headings.includes(sibling)) { const next = sibling.nextSibling; content.append(sibling); sibling = next; }
    }
  }
  if (options.navigation !== false && headings.length > 1) {
    const nav = node('nav', 'knowledge-toc'); nav.setAttribute('aria-label', '本文知识点'); nav.append(node('span', 'toc-label', '本文'));
    for (const h of headings) { const link = node('a', '', h.textContent); link.href = `#${h.id}`; link.addEventListener('click', event => { event.preventDefault(); const chapter = h.closest('.knowledge-chapter'); if (chapter) chapter.open = true; h.scrollIntoView({ block: 'start' }); h.focus({ preventScroll: true }); }); nav.append(link); }
    if (study) {
      const toc = node('details', 'reading-directory'), summary = node('summary', '', '目录');
      toc.append(summary, nav); fragment.insertBefore(toc, headings[0].closest('.knowledge-chapter'));
    } else fragment.insertBefore(nav, headings[0]);
  }
  if (metadata.length) {
    const meta = node('aside', 'knowledge-meta'); meta.setAttribute('aria-label', '课程与来源信息'); meta.append(...metadata);
    const title = fragment.querySelector('h1'); if (title) title.after(meta); else fragment.insertBefore(meta, fragment.firstChild);
  }
  target.classList.add('knowledge-document'); target.replaceChildren(fragment); renderPendingMath(target);
  target.classList.toggle('knowledge-concise', Boolean(study));
  target.readingCleanup = study ? mountReadingFeatures(target, study) : null;
}
