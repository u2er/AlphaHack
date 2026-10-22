// Граф событий: выгрузка EventGraph (через /api/insights/graph) на canvas.
// Видна не вся тысяча узлов, а подграф: свежие события с соседями или узел,
// который нашли, с его связями. Раскладка — простая физика: отталкивание,
// пружины рёбер, тяга к центру.
(() => {
const $ = id => document.getElementById(id);
const el = (tag, text = '', className = '') => { const n = document.createElement(tag); if (text) n.textContent = text; if (className) n.className = className; return n; };

const TYPES = {
  event: ['События', '#ef3124'], org: ['Организации', '#0b1f35'], person: ['Люди', '#5b4fd6'],
  place: ['Места', '#13a454'], topic: ['Темы', '#c77700'], outlet: ['Издания', '#7a8794'], other: ['Прочее', '#b4bcc5'],
};
const EDGE_NAMES = { involves: 'участвует', about: 'о', reported_by: 'сообщил', in: 'где', develops: 'развитие', covers: 'освещает',
  works_at: 'работает в', contradicts: 'противоречит', part_of: 'часть', leads: 'возглавляет' };
const MAX_VISIBLE = 260;
const CELL = 90;  // ячейка сетки отталкивания: дальние узлы друг на друга не влияют
let limit = MAX_VISIBLE;

let data = null, loadedAt = 0, byId = new Map(), adjacency = new Map(), degree = new Map();
let nodes = [], links = [], hidden = new Set(['outlet']), selected = null, hover = null;
let view = { x: 0, y: 0, k: 1 }, raf = null, heat = 0, loading = null, fitPending = false, fitFocus = null;
const canvas = () => $('graphCanvas');

const typeOf = n => TYPES[n.meta.type] ? n.meta.type : 'other';
const label = n => n.name.length > 60 ? n.name.slice(0, 58) + '…' : n.name;

async function load() {
  // Граф растёт (агент новостей, базовый снимок): выгрузка старше минуты — заново.
  if (data && Date.now() - loadedAt < 60000) return data;
  if (loading) return loading;
  loading = fetch('/api/insights/graph').then(async r => {
    const body = await r.json(); if (!r.ok) throw Error(body.error || `HTTP ${r.status}`);
    data = body; loadedAt = Date.now(); byId = new Map(data.nodes.map(n => [n.id, n])); adjacency = new Map(); degree = new Map();
    for (const e of data.edges) {
      if (!byId.has(e.source) || !byId.has(e.target)) continue;
      for (const [a, b] of [[e.source, e.target], [e.target, e.source]]) {
        if (!adjacency.has(a)) adjacency.set(a, []);
        adjacency.get(a).push({ id: b, edge: e });
      }
    }
    for (const [id, list] of adjacency) degree.set(id, list.length);
    $('graphStats').textContent = `${data.nodes.length} узлов · ${data.edges.length} связей · граф «${data.graph}»`;
    return data;
  }).finally(() => loading = null);
  return loading;
}

// ── подграф ─────────────────────────────────────────────────────────────────
// base — что выбрано для показа до фильтра по типам: снятый и снова включённый
// тип возвращается из него, а не из того, что сейчас на экране.
let base = new Set();
function show(ids, focus = null, max = MAX_VISIBLE) {
  limit = max; base = new Set(ids);
  const old = new Map(nodes.map(n => [n.id, n]));
  const box = canvas().getBoundingClientRect();
  nodes = [...ids].filter(id => byId.has(id) && !hidden.has(typeOf(byId.get(id)))).slice(0, max).map(id => {
    const prev = old.get(id);
    const spread = Math.max(220, Math.sqrt(ids.size || ids.length || 1) * 22);
    const angle = Math.random() * Math.PI * 2, r = 40 + Math.random() * spread;
    return prev || { id, node: byId.get(id), x: Math.cos(angle) * r, y: Math.sin(angle) * r, vx: 0, vy: 0 };
  });
  const present = new Set(nodes.map(n => n.id)), index = new Map(nodes.map(n => [n.id, n]));
  links = data.edges.filter(e => present.has(e.source) && present.has(e.target)).map(e => ({ s: index.get(e.source), t: index.get(e.target), e }));
  if (focus && index.get(focus)) selected = index.get(focus);
  // Масштаб подгоняется, когда раскладка успокоится: видно весь подграф.
  fitPending = true; fitFocus = focus ? index.get(focus) : null;
  heat = 1; kick();
  void box;
}

function latest() {
  const events = data.nodes.filter(n => n.meta.type === 'event').sort((a, b) =>
    String(b.meta.date || '').localeCompare(String(a.meta.date || '')) || (b.meta.importance || 0) - (a.meta.importance || 0));
  const chosen = events.filter(n => (n.meta.importance || 0) >= 2).slice(0, 35);
  const ids = new Set(chosen.map(n => n.id));
  for (const n of chosen) for (const { id } of (adjacency.get(n.id) || []).slice(0, 6)) ids.add(id);
  selected = null;
  show(ids); renderSide();
}

function focusOn(id, depth = 1) {
  const ids = new Set([id]);
  let frontier = [id];
  for (let d = 0; d < depth; d++) {
    const next = [];
    for (const f of frontier) for (const { id: n } of (adjacency.get(f) || []).slice(0, d === 0 ? 60 : 12)) if (!ids.has(n)) { ids.add(n); next.push(n); }
    frontier = next;
  }
  show(ids, id); renderSide();
}

function expand(id) {
  const ids = new Set(base);
  for (const { id: n } of (adjacency.get(id) || []).slice(0, 40)) ids.add(n);
  show(ids, id); renderSide();
}

// ── физика и отрисовка ──────────────────────────────────────────────────────
// Отталкивание: пары — для небольшого подграфа, сетка соседних ячеек — для всего графа
// (2–3 тысячи узлов попарно — миллионы операций на кадр).
function push(a, b) {
  let dx = a.x - b.x, dy = a.y - b.y; const d2 = Math.max(dx * dx + dy * dy, 100);
  if (d2 > 250000) return;
  const f = 900 / d2; dx *= f; dy *= f; a.vx += dx; a.vy += dy; b.vx -= dx; b.vy -= dy;
}
function repel() {
  const n = nodes.length;
  if (n <= 400) { for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) push(nodes[i], nodes[j]); return; }
  const grid = new Map();
  for (const a of nodes) {
    const key = `${Math.floor(a.x / CELL)},${Math.floor(a.y / CELL)}`;
    if (!grid.has(key)) grid.set(key, []);
    grid.get(key).push(a);
  }
  for (const [key, cell] of grid) {
    const [cx, cy] = key.split(',').map(Number);
    for (let i = 0; i < cell.length; i++) for (let j = i + 1; j < cell.length; j++) push(cell[i], cell[j]);
    for (const [ox, oy] of [[1, 0], [1, 1], [0, 1], [-1, 1]]) {
      const other = grid.get(`${cx + ox},${cy + oy}`);
      if (other) for (const a of cell) for (const b of other) push(a, b);
    }
  }
}

function all() {
  selected = null;
  show(new Set(data.nodes.map(n => n.id)), null, Infinity); fit(); renderSide();
}

function kick() { if (!raf) raf = requestAnimationFrame(tick); }
function tick() {
  raf = null;
  if (heat > 0.02) {
    repel();
    const pull = nodes.length > 600 ? 0.004 : 0.012;
    for (const a of nodes) { a.vx -= a.x * pull; a.vy -= a.y * pull; }
    for (const { s, t } of links) {
      const dx = t.x - s.x, dy = t.y - s.y, d = Math.sqrt(dx * dx + dy * dy) || 1, f = (d - 70) * 0.012;
      s.vx += dx / d * f; s.vy += dy / d * f; t.vx -= dx / d * f; t.vy -= dy / d * f;
    }
    for (const a of nodes) {
      if (a === dragging) continue;
      a.vx *= 0.82; a.vy *= 0.82;
      const speed = Math.hypot(a.vx, a.vy);  // без потолка близкие узлы разлетаются на тысячи пикселей
      if (speed > 25) { a.vx *= 25 / speed; a.vy *= 25 / speed; }
      a.x += a.vx * heat; a.y += a.vy * heat;
    }
    heat *= 0.985;
    if (fitPending && heat < 0.25) { fitPending = false; fit(); }
  }
  draw();
  if (heat > 0.02) kick();
}

function fit() {
  if (!nodes.length) return;
  const c = canvas(), w = c.clientWidth || 800, h = c.clientHeight || 600;
  const xs = nodes.map(n => n.x), ys = nodes.map(n => n.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const k = Math.max(0.2, Math.min(2, 0.88 * Math.min(w / (maxX - minX + 80), h / (maxY - minY + 80))));
  const cx = fitFocus ? fitFocus.x : (minX + maxX) / 2, cy = fitFocus ? fitFocus.y : (minY + maxY) / 2;
  view = { x: -cx * k, y: -cy * k, k };
}

function radius(p) {
  // Во всём графе узел — постоянного размера на экране, иначе при отдалении он меньше пикселя.
  if (limit === Infinity) return (2 + Math.min(7, Math.sqrt(degree.get(p.id) || 1) * 0.8)) / Math.max(view.k, 0.05);
  return 4 + Math.min(10, Math.sqrt(degree.get(p.id) || 1) * 1.3) + (p.node.meta.importance || 0) * 0.8;
}

function draw() {
  const c = canvas(), ctx = c.getContext('2d'), dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (c.width !== w * dpr || c.height !== h * dpr) { c.width = w * dpr; c.height = h * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
  ctx.translate(w / 2 + view.x, h / 2 + view.y); ctx.scale(view.k, view.k);
  const near = selected ? new Set((adjacency.get(selected.id) || []).map(x => x.id).concat(selected.id)) : null;
  ctx.lineWidth = 1 / view.k;
  for (const { s, t } of links) {
    const lit = near && near.has(s.id) && near.has(t.id) && (s === selected || t === selected);
    ctx.strokeStyle = lit ? 'rgba(239,49,36,.65)' : near ? 'rgba(11,31,53,.06)' : 'rgba(11,31,53,.14)';
    ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(t.x, t.y); ctx.stroke();
  }
  for (const p of nodes) {
    const r = radius(p), faded = near && !near.has(p.id);
    ctx.globalAlpha = faded ? 0.25 : 1;
    ctx.fillStyle = TYPES[typeOf(p.node)][1];
    ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
    if (p === selected || p === hover) { ctx.strokeStyle = '#ef3124'; ctx.lineWidth = 3 / view.k; ctx.stroke(); ctx.lineWidth = 1 / view.k; }
  }
  ctx.globalAlpha = 1;
  ctx.font = `${12 / view.k}px system-ui, sans-serif`; ctx.fillStyle = '#0b1f35'; ctx.textBaseline = 'middle';
  for (const p of nodes) {
    const hub = (degree.get(p.id) || 0) > (limit === Infinity ? (view.k > 1.2 ? 12 : 40) : 14);
    const big = hub || p === selected || p === hover || (near && near.has(p.id) && view.k > 0.7);
    if (!big || (near && !near.has(p.id))) continue;
    ctx.fillText(label(p.node), p.x + radius(p) + 4 / view.k, p.y);
  }
}

function toWorld(event) {
  const c = canvas(), box = c.getBoundingClientRect();
  return { x: (event.clientX - box.left - box.width / 2 - view.x) / view.k, y: (event.clientY - box.top - box.height / 2 - view.y) / view.k };
}
function hit(event) {
  const p = toWorld(event);
  let best = null, dist = Infinity;
  for (const n of nodes) { const d = Math.hypot(n.x - p.x, n.y - p.y); if (d < radius(n) + 4 / view.k && d < dist) { best = n; dist = d; } }
  return best;
}

let dragging = null, panning = null, moved = false;
function bind() {
  const c = canvas();
  c.onmousedown = e => { moved = false; const h = hit(e); if (h) dragging = h; else panning = { x: e.clientX - view.x, y: e.clientY - view.y }; };
  window.addEventListener('mousemove', e => {
    if (dragging) { const p = toWorld(e); dragging.x = p.x; dragging.y = p.y; dragging.vx = dragging.vy = 0; moved = true; heat = Math.max(heat, 0.3); kick(); }
    else if (panning) { view.x = e.clientX - panning.x; view.y = e.clientY - panning.y; moved = true; draw(); }
    else if (e.target === c) { const h = hit(e); if (h !== hover) { hover = h; c.style.cursor = h ? 'pointer' : 'grab'; draw(); } }
  });
  window.addEventListener('mouseup', e => {
    if (!moved && e.target === c) { const h = hit(e); selected = h; renderSide(); draw(); }
    dragging = panning = null;
  });
  c.ondblclick = e => { const h = hit(e); if (h) expand(h.id); };
  c.addEventListener('wheel', e => {
    e.preventDefault();
    const box = c.getBoundingClientRect(), mx = e.clientX - box.left - box.width / 2, my = e.clientY - box.top - box.height / 2;
    const k = Math.max(0.2, Math.min(4, view.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
    view.x = mx - (mx - view.x) * k / view.k; view.y = my - (my - view.y) * k / view.k; view.k = k; draw();
  }, { passive: false });
  window.addEventListener('resize', () => draw());
  $('graphLatest').onclick = latest;
  $('graphAll').onclick = all;
  $('graphSearch').oninput = () => suggest(false);
  $('graphSearch').onkeydown = e => { if (e.key === 'Enter') suggest(true); if (e.key === 'Escape') $('graphResults').replaceChildren(); };
  const types = $('graphTypes');
  for (const [key, [name, color]] of Object.entries(TYPES)) {
    const chip = el('label', '', 'chip-check'); const box = el('input'); box.type = 'checkbox'; box.checked = !hidden.has(key);
    const dot = el('span', '', 'type-dot'); dot.style.background = color;
    chip.append(box, dot, el('span', name));
    box.onchange = () => {
      box.checked ? hidden.delete(key) : hidden.add(key);
      if (limit === Infinity) return all();
      show(base, selected && !hidden.has(typeOf(selected.node)) ? selected.id : null, limit);
      if (selected && hidden.has(typeOf(selected.node))) { selected = null; renderSide(); }
    };
    types.append(chip);
  }
}

async function suggest(semantic) {
  const q = $('graphSearch').value.trim(), box = $('graphResults');
  if (q.length < 2) return box.replaceChildren();
  let found = data.nodes.filter(n => n.name.toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) => (degree.get(b.id) || 0) - (degree.get(a.id) || 0)).slice(0, 8).map(n => ({ id: n.id, name: n.name, type: n.meta.type }));
  if (semantic) {
    try { const r = await fetch('/api/insights/graph-search?q=' + encodeURIComponent(q)); const body = await r.json(); if (r.ok) found = body.results.slice(0, 10); }
    catch { /* остаётся поиск по имени */ }
  }
  box.replaceChildren(...found.map(f => {
    const b = el('button', '', 'graph-result'); b.type = 'button';
    const dot = el('span', '', 'type-dot'); dot.style.background = (TYPES[f.type] || TYPES.other)[1];
    b.append(dot, el('span', f.name));
    b.onclick = () => { box.replaceChildren(); $('graphSearch').value = ''; focusOn(f.id, 1); };
    return b;
  }));
  if (!found.length) box.replaceChildren(el('div', semantic ? 'Ничего не нашлось' : 'Enter — искать по смыслу', 'muted'));
}

async function renderSide() {
  const side = $('graphSide');
  if (!selected) { side.replaceChildren(el('p', 'Выберите узел, чтобы увидеть подробности и связи. Двойной клик раскрывает его соседей.', 'muted')); return; }
  const n = selected.node, m = n.meta, type = typeOf(n);
  side.replaceChildren();
  const tag = el('span', TYPES[type][0].replace(/и$|ы$/, ''), 'chip'); tag.style.background = TYPES[type][1]; tag.style.color = '#fff';
  side.append(tag, el('h3', n.name));
  const facts = [m.date && `дата: ${m.date}`, m.importance && `важность: ${m.importance}/5`, m.role, m.about,
    m.topics?.length && `темы: ${m.topics.join(', ')}`, m.sources_count && `публикаций: ${m.sources_count}`].filter(Boolean);
  if (facts.length) side.append(el('p', facts.join(' · '), 'muted'));
  if (m.summary) side.append(el('p', m.summary));
  if (m.aliases?.length) side.append(el('p', 'Также: ' + m.aliases.join(', '), 'muted'));
  const groups = new Map();
  for (const { id, edge } of adjacency.get(n.id) || []) {
    const key = EDGE_NAMES[edge.name] || edge.name; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(byId.get(id));
  }
  const links = el('div', '', 'graph-links');
  for (const [name, list] of groups) {
    links.append(el('h4', `${name} · ${list.length}`));
    for (const other of list.slice(0, 12)) {
      const b = el('button', '', 'graph-result'); b.type = 'button';
      const dot = el('span', '', 'type-dot'); dot.style.background = TYPES[typeOf(other)][1];
      b.append(dot, el('span', label(other))); b.onclick = () => focusOn(other.id, 1); links.append(b);
    }
  }
  side.append(links);
  const sources = el('div', '', 'graph-sources'); side.append(sources);
  try {
    const r = await fetch(`/api/insights/graph/nodes/${encodeURIComponent(n.id)}`); const body = await r.json();
    const items = (body.node?.items || []).filter(i => i.type === 'source').slice(0, 6);
    if (items.length && selected?.node === n) {
      sources.append(el('h4', 'Публикации'));
      for (const item of items) {
        const d = item.data || {}, row = el('p', '', 'source-line');
        if (d.url) { const a = el('a', d.title || d.url); a.href = d.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; row.append(a); }
        else row.append(el('span', d.title || ''));
        if (d.source_name || d.published_at) row.append(el('span', ` — ${[d.source_name, String(d.published_at || '').slice(0, 10)].filter(Boolean).join(', ')}`));
        sources.append(row);
      }
    }
  } catch { /* подробности не обязательны */ }
}

async function open() {
  const before = data;
  try { await load(); } catch (error) { $('graphStats').textContent = error.message; return; }
  if (!canvas().dataset.bound) { bind(); canvas().dataset.bound = '1'; }
  if (!nodes.length || data !== before) { const focus = selected?.id; focus && byId.has(focus) ? focusOn(focus, 1) : latest(); }
  else draw();
}

window.AlfaGraph = { open, bench: (runs = 20) => { const t = performance.now(); for (let i = 0; i < runs; i++) repel(); return Math.round((performance.now() - t) / runs * 10) / 10; }, state: () => ({ nodes: nodes.length, links: links.length, view: { ...view }, heat,
  box: nodes.length ? [Math.min(...nodes.map(n => n.x)), Math.max(...nodes.map(n => n.x)), Math.min(...nodes.map(n => n.y)), Math.max(...nodes.map(n => n.y))].map(Math.round) : [] }) };
})();
