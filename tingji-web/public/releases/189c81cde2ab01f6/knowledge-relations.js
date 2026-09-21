// Small, local-only Mermaid flowcharts and plain-text hierarchy trees.
// Dependency/source notes: docs/note-standard-v5/relations-notes.md.
const MERMAID_URL = new URL('./vendor/mermaid/mermaid.tiny-11.12.0.js', import.meta.url).href;
const SVG_NS = 'http://www.w3.org/2000/svg';
let libraryPromise;
let renderQueue = Promise.resolve();
let sequence = 0;
const pagePrefix = `kr-${globalThis.crypto?.randomUUID?.().replaceAll('-', '') || Date.now().toString(36)}`;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function complete(element, state) {
  element.dataset.renderState = state;
  element.setAttribute('aria-busy', 'false');
  element.dispatchEvent(new CustomEvent('knowledge:rendered', { bubbles: true, detail: { kind: element.dataset.kind, state } }));
  return element;
}

function fallback(element, source, message) {
  element.replaceChildren(el('p', 'knowledge-relation-message', message));
  const pre = el('pre', 'knowledge-relation-source');
  pre.append(el('code', '', source));
  element.append(pre);
  return complete(element, 'error');
}

function shell(kind) {
  const element = el('div', kind === 'tree' ? 'knowledge-tree' : 'knowledge-relation');
  element.dataset.kind = kind;
  element.dataset.renderState = 'pending';
  element.setAttribute('aria-busy', 'true');
  return element;
}

function loadMermaid() {
  if (!libraryPromise) {
    libraryPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = MERMAID_URL;
      script.async = true;
      const timeout = setTimeout(() => { script.remove(); reject(new Error('关系图组件暂时无法载入。')); }, 15000);
      script.onload = () => {
        clearTimeout(timeout);
        if (globalThis.mermaid?.render) resolve(globalThis.mermaid);
        else reject(new Error('关系图组件暂时无法载入。'));
      };
      script.onerror = () => { clearTimeout(timeout); script.remove(); reject(new Error('关系图组件暂时无法载入。')); };
      document.head.append(script);
    }).catch(error => { libraryPromise = undefined; throw error; });
  }
  return libraryPromise;
}

function sourceText(value) {
  return String(value ?? '').replace(/\r\n?/g, '\n');
}

function safeLabel(value) {
  let label = value.trim();
  if (label.startsWith('"') && label.endsWith('"')) label = label.slice(1, -1);
  if (!label || label.length > 100 || /[<>`\u0000-\u001f]/.test(label)) {
    throw new Error('关系图节点和连线只使用简短纯文本。');
  }
  // Mermaid entities protect punctuation from its grammar; HTML labels remain disabled.
  return label.replace(/[&"#]/g, character => `#${character.codePointAt(0)};`);
}

/** Parse and regenerate a deliberately small grammar, never forward arbitrary Mermaid. */
function parseFlowchart(source) {
  if (source.length > 5000) throw new Error('关系图请控制在 24 个节点以内。');
  if (/%%\s*\{|[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(source)) {
    throw new Error('关系图不支持初始化指令或控制字符。');
  }
  const clean = source.split('\n').filter(line => !/^\s*%%/.test(line)).join('\n').trim();
  const header = /^(?:flowchart|graph)\s+(TB|TD|BT|LR|RL)(?=\s|;|$)/i.exec(clean);
  if (!header) throw new Error('关系图请以 flowchart TD 或 graph LR 开头。');
  let input = clean.slice(header[0].length);
  let position = 0;
  const nodes = new Map(), edges = [];
  const shapes = [
    ['([', '])'], ['((', '))'], ['[[', ']]'], ['[(', ')]'], ['{{', '}}'],
    ['[/', '/]'], ['[\\', '\\]'], ['[', ']'], ['(', ')'], ['{', '}'],
  ];
  const skipSpace = () => { while (/[ \t]/.test(input[position] || '\0')) position++; };
  const separators = () => { while (/[\s;]/.test(input[position] || '\0')) position++; };
  function node() {
    skipSpace();
    const match = /^[\p{L}_][\p{L}\p{N}_]*(?:(?:-|\/)[\p{L}\p{N}_]+)*/u.exec(input.slice(position));
    if (!match || /^(?:click|href|callback|link|linkStyle|style|classDef|class|subgraph|end|direction)$/i.test(match[0])) {
      throw new Error('关系图只支持节点和连线；节点编号请使用文字、字母或下划线开头。');
    }
    const id = match[0];
    if (id.length > 64) throw new Error('关系图节点编号请控制在 64 字以内。');
    position += id.length;
    skipSpace();
    let definition;
    const shape = shapes.find(([open]) => input.startsWith(open, position));
    if (shape) {
      const [open, close] = shape;
      position += open.length;
      const start = position;
      let quoted = false;
      for (; position < input.length; position++) {
        if (input[position] === '"') quoted = !quoted;
        if (!quoted && input.startsWith(close, position)) break;
        if (input[position] === '\n') throw new Error('关系图每个节点标签请写在一行。');
      }
      if (position >= input.length) throw new Error('关系图节点括号未闭合，请检查原文。');
      definition = `${open}"${safeLabel(input.slice(start, position))}"${close}`;
      position += close.length;
    }
    if (!nodes.has(id)) nodes.set(id, { id: `n${nodes.size}`, definition: `["${safeLabel(id)}"]` });
    if (nodes.size > 24) throw new Error('关系图最多显示 24 个节点，请拆成几幅小图。');
    if (definition) nodes.get(id).definition = definition;
    return nodes.get(id).id;
  }
  function arrow() {
    skipSpace();
    const rest = input.slice(position);
    // Includes ordinary, undirected, dotted and thick links, plus common two-way links.
    const plain = /^(<-->|<---|-->|---|==>|===|-\.->|-\.-)/.exec(rest);
    let type, label;
    if (plain) {
      type = plain[0];
      position += type.length;
      skipSpace();
      if (input[position] === '|') {
        const end = input.indexOf('|', ++position);
        if (end < 0) throw new Error('关系图连线标签未闭合，请检查原文。');
        label = safeLabel(input.slice(position, end));
        position = end + 1;
      }
    } else {
      const labelled = /^(--|==|-\.)[ \t]*(.+?)[ \t]*(-->|==>|\.->)/.exec(rest);
      if (!labelled) return null;
      if (labelled[2].includes('\n')) return null;
      type = labelled[3] === '.->' ? '-.->' : labelled[3];
      label = safeLabel(labelled[2]);
      position += labelled[0].length;
    }
    return { type, label };
  }
  separators();
  while (position < input.length) {
    let from = node();
    let link;
    while ((link = arrow())) {
      const to = node();
      edges.push(`${from} ${link.type}${link.label ? `|"${link.label}"|` : ''} ${to}`);
      if (edges.length > 24) throw new Error('关系图连线过多，请拆成几幅小图。');
      from = to;
    }
    skipSpace();
    if (position < input.length && !/[\n;]/.test(input[position])) {
      throw new Error('关系图含有不支持的语法，请使用普通节点和箭头。');
    }
    separators();
  }
  if (!nodes.size) throw new Error('关系图中还没有节点。');
  if (!edges.length) throw new Error('关系图需要用箭头或连线说明节点之间的关系。');
  return { text: `flowchart ${header[1].toUpperCase()}\n${[...nodes.values()].map(item => item.id + item.definition).join('\n')}\n${edges.join('\n')}`, count: nodes.size };
}

const SVG_TAGS = new Set(['svg', 'g', 'defs', 'marker', 'path', 'rect', 'circle', 'ellipse', 'polygon', 'polyline', 'line', 'text', 'tspan', 'title', 'desc']);
const SVG_ATTRIBUTES = new Set(['viewBox', 'preserveAspectRatio', 'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'width', 'height', 'd', 'points', 'transform', 'dx', 'dy', 'text-anchor', 'dominant-baseline', 'alignment-baseline', 'markerWidth', 'markerHeight', 'refX', 'refY', 'orient', 'markerUnits', 'stroke-width', 'stroke-dasharray', 'stroke-linecap', 'stroke-linejoin', 'fill-rule']);

function cleanSvg(markup, prefix) {
  const parsed = new DOMParser().parseFromString(markup, 'image/svg+xml');
  const root = parsed.documentElement;
  if (root.localName !== 'svg' || parsed.querySelector('parsererror')) throw new Error('关系图暂时无法显示，请检查原文。');
  const ids = new Map();
  [root, ...root.querySelectorAll('[id]')].forEach((node, index) => {
    if (node.id) ids.set(node.id, `${prefix}-${index}`);
  });
  function copy(node) {
    if (node.nodeType === Node.TEXT_NODE) return document.createTextNode(node.textContent);
    if (node.nodeType !== Node.ELEMENT_NODE || !SVG_TAGS.has(node.localName) || node.namespaceURI !== SVG_NS) return null;
    const output = document.createElementNS(SVG_NS, node.localName);
    for (const attribute of node.attributes) {
      const { name, value } = attribute;
      if (name === 'id' && ids.has(value)) output.id = ids.get(value);
      else if (name === 'class' && /^[\w\s-]+$/.test(value)) output.setAttribute('class', value);
      else if (SVG_ATTRIBUTES.has(name) && !/(?:url\s*\(|[<>]|javascript:|data:|https?:)/i.test(value)) output.setAttribute(name, value);
      else if (/^marker-(?:start|mid|end)$/.test(name)) {
        const reference = /^url\(#([\w-]+)\)$/.exec(value);
        if (reference && ids.has(reference[1])) output.setAttribute(name, `url(#${ids.get(reference[1])})`);
      }
    }
    for (const child of node.childNodes) {
      const copied = copy(child);
      if (copied) output.append(copied);
    }
    return output;
  }
  const output = copy(root);
  // Keep the sanitized diagram legible in downloaded HTML, where application
  // stylesheets are absent. These are fixed presentation attributes, never
  // style text copied from the source diagram.
  const presentation = [
    ['text,tspan', {fill:'#252525', stroke:'none'}],
    ['.node text', {'text-anchor':'middle'}],
    ['.node rect,.node circle,.node ellipse,.node polygon,.node path', {fill:'#fff', stroke:'#4b4b4b', 'stroke-width':'1.15'}],
    ['.edgePaths path', {fill:'none', stroke:'#555', 'stroke-width':'1.3'}],
    ['.edge-pattern-dotted', {'stroke-dasharray':'3 3'}],
    ['.edge-thickness-thick', {'stroke-width':'2.5'}],
    ['marker path,marker polygon,marker circle', {fill:'#555', stroke:'#555'}],
    ['.labelBkg,.edgeLabel rect', {fill:'#fff', stroke:'none'}],
  ];
  for (const [selector, attributes] of presentation) for (const node of output.querySelectorAll(selector)) {
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  }
  output.setAttribute('font-family', 'Segoe UI, Microsoft YaHei, sans-serif');
  output.setAttribute('font-size', '15');
  const dimensions = output.getAttribute('viewBox')?.trim().split(/[\s,]+/).map(Number);
  if (!dimensions || dimensions.length !== 4 || !dimensions.every(Number.isFinite) || dimensions[2] <= 0 || dimensions[3] <= 0) {
    throw new Error('关系图尺寸无效，请检查原文。');
  }
  output.style.width = `${Math.ceil(dimensions[2])}px`;
  output.style.height = 'auto';
  output.removeAttribute('width');
  output.removeAttribute('height');
  output.setAttribute('role', 'img');
  output.setAttribute('aria-label', '关系图');
  return output;
}

/** Returns immediately. Await element.renderComplete or listen for knowledge:rendered. */
export function renderRelation(value) {
  const source = sourceText(value);
  const element = shell('relation');
  element.append(el('p', 'knowledge-relation-message', '关系图载入中…'));
  let parsed;
  try { parsed = parseFlowchart(source); }
  catch (error) {
    element.renderComplete = Promise.resolve(fallback(element, source, error.message));
    return element;
  }
  element.dataset.nodeCount = String(parsed.count);
  const id = `${pagePrefix}-${++sequence}`;
  const task = async () => {
    let container;
    try {
      const mermaid = await loadMermaid();
      mermaid.initialize({
        startOnLoad: false, securityLevel: 'strict', htmlLabels: false,
        suppressErrorRendering: true, maxTextSize: 8000, maxEdges: 24,
        theme: 'base', fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif',
        themeVariables: { fontSize: '15px', primaryColor: '#ffffff', primaryTextColor: '#252525', primaryBorderColor: '#444444', lineColor: '#555555', secondaryColor: '#ffffff', tertiaryColor: '#ffffff', edgeLabelBackground: '#ffffff' },
        flowchart: { htmlLabels: false, useMaxWidth: false, curve: 'linear', nodeSpacing: 24, rankSpacing: 36, padding: 10 },
      });
      container = el('div');
      container.setAttribute('aria-hidden', 'true');
      container.style.cssText = 'position:absolute;left:-10000px;top:0;width:2000px;visibility:hidden;pointer-events:none';
      document.body.append(container);
      const result = await mermaid.render(id, parsed.text, container);
      const svg = cleanSvg(result.svg, id);
      const scroll = el('div', 'knowledge-relation-scroll');
      scroll.tabIndex = 0;
      scroll.setAttribute('role', 'region');
      scroll.setAttribute('aria-label', '关系图，可横向滚动');
      scroll.append(svg);
      element.replaceChildren(scroll);
      return complete(element, 'ready');
    } catch (error) {
      return fallback(element, source, error.message?.startsWith('关系图') ? error.message : '关系图暂时无法显示，请检查原文。');
    } finally { container?.remove(); }
  };
  element.renderComplete = renderQueue.then(task, task);
  renderQueue = element.renderComplete.then(() => undefined, () => undefined);
  return element;
}

function parseTree(source) {
  if (source.length > 5000) throw new Error('从属树最多显示 24 项。');
  const lines = source.split('\n').filter(line => line.trim());
  if (!lines.length) throw new Error('从属树中还没有内容。');
  if (lines.length > 24) throw new Error('从属树最多显示 24 项，请按主题拆分。');
  const roots = [], stack = [];
  let indentUnit;
  let childCount = 0;
  for (const line of lines) {
    const whitespace = /^\s*/.exec(line)[0];
    if (/[^ ]/.test(whitespace)) throw new Error('从属树请使用空格缩进，每层保持一致。');
    const label = line.slice(whitespace.length).trim();
    if (!label || label.length > 160 || /[\u0000-\u001f]/.test(label)) throw new Error('从属树每项请使用一行简短文字。');
    const indent = whitespace.length;
    if (!indentUnit && indent) indentUnit = indent;
    if (indent && (!indentUnit || indent % indentUnit)) throw new Error('从属树的缩进层级不一致。');
    const depth = indent ? indent / indentUnit : 0;
    if (depth > 3) throw new Error('从属树最多显示 4 层，请拆分更深的内容。');
    if (depth > stack.length) throw new Error('从属树缺少上一级，请检查缩进。');
    const item = { label, children: [] };
    if (depth === 0) roots.push(item);
    else { stack[depth - 1].children.push(item); childCount++; }
    stack.length = depth;
    stack.push(item);
  }
  if (!childCount) throw new Error('从属树需要上下级关系；并列内容请使用普通列表。');
  return roots;
}

/** Plain-text indentation becomes a compact native details hierarchy. */
export function renderTree(value) {
  const source = sourceText(value), element = shell('tree');
  try {
    const roots = parseTree(source);
    const list = items => {
      const ul = el('ul', 'knowledge-tree-list');
      for (const item of items) {
        const li = el('li', 'knowledge-tree-item');
        if (item.children.length) {
          const details = el('details', 'knowledge-tree-branch');
          details.open = true;
          details.append(el('summary', '', item.label), list(item.children));
          li.append(details);
        } else li.append(el('span', 'knowledge-tree-leaf', item.label));
        ul.append(li);
      }
      return ul;
    };
    element.append(list(roots));
    complete(element, 'ready');
  } catch (error) { fallback(element, source, error.message); }
  element.renderComplete = Promise.resolve(element);
  return element;
}
