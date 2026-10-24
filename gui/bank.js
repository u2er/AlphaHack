// Альфа-Бизнес: оболочка, главная и бизнес-аналитика. Данные — через /api/insights
// сервера GUI (он ходит в EventGraph с ключом, браузер ключа не видит).
(() => {
const $ = id => document.getElementById(id);
const el = (tag, text = '', className = '') => { const n = document.createElement(tag); if (text) n.textContent = text; if (className) n.className = className; return n; };
const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };

const DIRECTION = { risk: 'Риск', opportunity: 'Возможность', mixed: 'Риск и возможность', neutral: 'Нейтрально' };
const HORIZON = { immediate: 'сразу', weeks: 'недели', months: 'месяцы', years: 'годы' };
const RELEVANCE = { direct: 'прямое влияние', indirect: 'косвенное влияние', none: 'не касается' };
const TREND = { new: 'новое', continues: 'продолжается', escalates: 'усиливается', eases: 'ослабевает' };
const ROLES = { competitor: 'конкурент', partner: 'партнёр', client: 'клиент', supplier: 'поставщик', regulator: 'регулятор', other: 'участник' };

let profiles = [], current = null, items = [], selected = null, direction = '';
// Коннектор → название источника для людей («Lenta.ru», «Аналитика Альфа-Банка»).
let sourceNames = {}, sourceList = [];
const outlet = item => sourceNames[item.connector] || item.connector || '';
const host = url => { try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; } };
async function loadSourceNames() {
  try { setConnectors((await api('/api/insights/connectors')).connectors); }
  catch { /* без названий покажем коды коннекторов */ }
}

async function api(path) {
  const response = await fetch(path);
  let data; try { data = await response.json(); } catch { throw Error('Не удалось прочитать ответ сервера'); }
  if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
  return data;
}

function notice(message, error = false) {
  const box = $('notice'); box.textContent = message; box.className = 'notice' + (error ? ' error' : ''); box.hidden = false;
  clearTimeout(notice.t); notice.t = setTimeout(() => box.hidden = true, 6000);
}

// ── роутинг ─────────────────────────────────────────────────────────────────
function route() {
  const hash = location.hash || '#/home';
  let view = 'home', sub = '';
  if (hash.startsWith('#/')) [view, sub] = hash.slice(2).split('/');
  if (!['home', 'insights', 'profile', 'onboarding', 'graph'].includes(view)) view = 'home';
  for (const v of document.querySelectorAll('.view')) v.hidden = v.id !== `view-${view}`;
  for (const a of document.querySelectorAll('.menu a')) a.classList.toggle('active', a.getAttribute('href') === `#/${view}`);
  $('sidebar').classList.remove('open');
  if (view === 'home' || view === 'insights') loadFeed();
  if (view === 'profile') renderProfilePage();
  if (view === 'onboarding') window.Alfa.onboarding.open();
  if (view === 'graph') window.AlfaGraph.open();
  if (view === 'insights' && tab === 'sources') renderSources();
  if (hash.startsWith('#/')) window.scrollTo(0, 0);
}

// ── компании (профили клиентов) ─────────────────────────────────────────────
async function loadProfiles() {
  const select = $('clientSelect');
  await loadSourceNames();
  try {
    profiles = (await api('/api/insights/profiles')).profiles;
  } catch (error) {
    select.replaceChildren(el('option', 'Аналитика недоступна'));
    notice(error.message, true); profiles = []; renderEmpty(error.message); return;
  }
  select.replaceChildren();
  if (!profiles.length) { select.append(el('option', 'Нет профилей')); renderEmpty(); return; }
  for (const p of profiles) { const o = el('option', p.name || p.id); o.value = p.id; select.append(o); }
  const saved = store.get('alfa-client');
  select.value = profiles.some(p => p.id === saved) ? saved : profiles[0].id;
  await selectClient(select.value);
}

async function selectClient(id) {
  store.set('alfa-client', id); selected = null;
  try { current = await api(`/api/insights/profiles/${encodeURIComponent(id)}`); }
  catch (error) { notice(error.message, true); return; }
  renderProfileHeader();
  if (tab === 'sources' && connectors.length) drawSources();
  await loadFeed(true);
}

function renderProfilePage() {
  const box = $('profilePager');
  if (!current) {
    const empty = el('div', '', 'panel empty');
    empty.append(el('h3', 'Профилей пока нет'), el('p', 'Добавьте компанию — анкета, документы и пара вопросов.'));
    const link = el('a', 'Добавить компанию', 'button-link'); link.href = '#/onboarding'; empty.append(link);
    return box.replaceChildren(empty);
  }
  $('profileTitle').textContent = current.name;
  const pages = [{ id: 'main', title: 'Основное', summary: 'Агенты видят всегда', content: current.main }, ...current.toc];
  window.Alfa.pager(box, {
    pages,
    load: async page => page.content ?? (await api(`/api/insights/profiles/${encodeURIComponent(current.id)}/pages/${encodeURIComponent(page.id)}`)).content,
  });
}

function renderProfileHeader() {
  const name = current?.name || 'компании';
  $('homeProfile').textContent = current ? `${current.toc.length + 1} стр. профиля${current.updated_at ? ` · обновлён ${new Date(current.updated_at).toLocaleDateString('ru-RU')}` : ''}` : 'Добавьте компанию';
  $('homeRole').textContent = current?.user_role ? `Вы: ${current.user_role}` : '';
  if (!$('view-profile').hidden) renderProfilePage();
  $('homeTitle').textContent = name;
  const hour = new Date().getHours();
  $('homeGreeting').textContent = hour < 6 ? 'Доброй ночи' : hour < 12 ? 'Доброе утро' : hour < 18 ? 'Добрый день' : 'Добрый вечер';
  $('insightsTitle').textContent = `Лента для ${name}`;
  $('insightsRole').textContent = current?.user_role ? `Отобрано под вашу роль: ${current.user_role}` : '';
}

// ── лента ───────────────────────────────────────────────────────────────────
let loading = null;
async function loadFeed(force = false) {
  if (!current) return;
  if (loading && !force) return loading;
  const importance = $('importanceFilter').value;
  $('feed').replaceChildren(el('div', '', 'skeleton'), el('div', '', 'skeleton'));
  loading = api(`/api/insights/profiles/${encodeURIComponent(current.id)}/feed?min_importance=${importance}`)
    .then(data => { items = data.items; renderFeed(); renderHome(); })
    .catch(error => { notice(error.message, true); items = []; renderFeed(error.message); renderHome(); })
    .finally(() => loading = null);
  return loading;
}

function dots(n) {
  const box = el('span', '', 'dots'); box.title = `Важность ${n} из 5`;
  for (let i = 1; i <= 5; i++) box.append(el('i', '', i <= n ? 'f' : ''));
  return box;
}
const titleOf = item => item.analysis.headline || item.title || item.event || 'Событие';
const dateOf = item => (item.published || item.collected_at || '').slice(0, 10);

function card(item) {
  const a = item.analysis, b = el('button', '', 'card' + (selected === item.uid ? ' on' : ''));
  b.type = 'button'; b.dataset.uid = item.uid;
  const top = el('div', '', 'card-top'); top.append(dots(a.importance));
  if (a.direction) top.append(el('span', DIRECTION[a.direction] || a.direction, `chip ${a.direction}`));
  if (a.horizon) top.append(el('span', `горизонт: ${HORIZON[a.horizon] || a.horizon}`, 'chip'));
  b.append(top, el('h3', titleOf(item)), el('p', a.impact || ''));
  const foot = el('div', '', 'card-foot');
  foot.append(el('span', [dateOf(item), outlet(item)].filter(Boolean).join(' · ')));
  if (item.bank?.length) foot.append(el('span', 'Альфа‑Банк', 'who who-bank'));
  b.append(foot);
  b.onclick = () => { selected = item.uid; renderFeed(); };
  return b;
}

function visible() { return items.filter(i => !direction || i.analysis.direction === direction || (direction && i.analysis.direction === 'mixed')); }

function renderEmpty(message) {
  const box = el('div', '', 'panel empty');
  if (message) { box.append(el('h3', 'Бизнес‑аналитика недоступна'), el('p', message)); }
  else {
    box.append(el('h3', 'Расскажите о компании'), el('p', 'Лента подбирается под профиль: заполните его — и новости начнут оцениваться для вашего бизнеса.'));
    const link = el('a', 'Добавить компанию', 'button-link'); link.href = '#/onboarding'; box.append(link);
  }
  $('feed').replaceChildren(box); $('detail').replaceChildren();
  $('entryList').replaceChildren(el('div', message || 'Создайте профиль компании, чтобы получить персональную ленту.', 'mini'));
}

function renderFeed(error) {
  if (error) return renderEmpty(error);
  const list = visible(), feed = $('feed');
  if (!list.length) {
    const box = el('div', '', 'panel empty');
    box.append(el('h3', 'Пока нет важных событий'), el('p', items.length ? 'Измените фильтр направления.' : 'Новости оцениваются по мере поступления. Понизьте порог важности или загляните позже.'));
    feed.replaceChildren(box); $('detail').replaceChildren(); return;
  }
  if (!list.some(i => i.uid === selected)) selected = list[0].uid;
  feed.replaceChildren(...list.map(card));
  renderDetail(list.find(i => i.uid === selected));
}

function block(title, who, ...children) {
  const box = el('section', '', 'block'), head = el('div', '', 'block-head');
  head.append(el('h3', title)); if (who) head.append(el('span', who[0], `who ${who[1]}`));
  box.append(head, ...children); return box;
}

function renderDetail(item) {
  const a = item.analysis, d = $('detail'); d.replaceChildren(); d.scrollTop = 0;
  const meta = el('div', '', 'card-top');
  if (a.direction) meta.append(el('span', DIRECTION[a.direction] || a.direction, `chip ${a.direction}`));
  for (const [map, key] of [[RELEVANCE, 'relevance'], [HORIZON, 'horizon'], [TREND, 'trend']])
    if (a[key]) meta.append(el('span', (key === 'horizon' ? 'горизонт: ' : '') + (map[a[key]] || a[key]), 'chip'));
  for (const area of a.areas || []) meta.append(el('span', area, 'chip'));
  d.append(el('div', [dateOf(item), outlet(item)].filter(Boolean).join(' · '), 'muted'), el('h2', titleOf(item)), meta);

  const meter = el('div', '', 'meter'); meter.append(el('b', `${a.importance}/5`));
  meter.append(el('span', `Важность для ${current?.name || 'компании'}${a.confidence != null ? ` · уверенность оценки ${Math.round(a.confidence * 100)}%` : ''}`));
  d.append(meter);

  d.append(block('Как это влияет на ваш бизнес', ['AI', 'who-ai'], el('p', a.impact || '—')));
  if (a.actions?.length) { const ul = el('ul'); for (const x of a.actions) ul.append(el('li', x)); d.append(block('Что можно сделать', ['AI', 'who-ai'], ul)); }

  const bank = item.bank || [];
  const bankBody = bank.length ? bank.map(f => {
    const box = el('div', '', 'fragment'), m = el('div', '', 'fragment-meta');
    m.textContent = `${f.filename} · стр. ${f.page} · ${f.date_hint}`;
    const year = parseInt(String(f.date_hint).slice(0, 4), 10);
    if (year && year < new Date().getFullYear() - 1) m.append(el('span', ' · исторический материал', 'old'));
    box.append(m, el('q', f.text)); return box;
  }) : [el('p', 'В библиотеке аналитики банка не нашлось близких материалов.', 'muted')];
  const note = el('p', 'Фрагменты отчётов аналитиков Альфа‑Банка, близкие к событию по смыслу. Подобраны поиском — сверяйте контекст.', 'muted');
  d.append(block('Аналитика Альфа‑Банка', ['Альфа‑Банк', 'who-bank'], ...bankBody, note));

  if (a.entities?.length) {
    const wrap = el('div'); for (const e of a.entities) { const s = el('span', e.name, 'entity'); s.append(el('small', ROLES[e.role] || e.role)); wrap.append(s); }
    d.append(block('Участники', null, wrap));
  }
  if (a.related_events?.length) { const ul = el('ul'); for (const x of a.related_events) ul.append(el('li', x)); d.append(block('Связанные события', null, ul)); }

  const src = [], origin = el('div', '', 'source-origin');
  origin.append(el('b', outlet(item) || 'Источник'));
  const where = [host(item.url), item.published ? `опубликовано ${new Date(item.published).toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' })}` : ''].filter(Boolean).join(' · ');
  if (where) origin.append(el('span', where));
  src.push(origin);
  if (item.url) { const link = el('a', item.title || item.url); link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; const p = el('p'); p.append(link, el('span', ' ↗', 'muted')); src.push(p); }
  else src.push(el('p', item.title || '—'));
  if (item.event) src.push(el('p', `Событие в графе: ${item.event}`, 'muted'));
  d.append(block('Первоисточник', ['Источник', 'who-src'], ...src));
  d.append(el('div', `Запись ${item.uid}${a.run_id ? ` · ран аналитика #${a.run_id}` : ''} — ход рассуждений в EventGraph: GET /agent-runs/${a.run_id || '…'}`, 'trace'));
}

// ── главная ─────────────────────────────────────────────────────────────────
function openItem(item) { selected = item.uid; if (tab !== 'feed') switchTab('feed'); }

function renderHome() {
  const list = $('entryList'); list.replaceChildren();
  const top = items.slice(0, 3);
  $('entryTitle').textContent = top.length ? `${items.length} ${plural(items.length, 'событие важно', 'события важны', 'событий важны')} для вашего бизнеса` : 'Что важно для вашего бизнеса сегодня';
  renderKpis(); renderActions(); renderAreas(); renderHomeCards();
  if (!top.length) { list.append(el('div', 'Новых важных событий нет — лента обновляется по мере поступления новостей.', 'mini')); return; }
  for (const item of top) {
    const m = el('div', '', 'mini'); m.append(dots(item.analysis.importance));
    const body = el('div'); body.append(el('b', titleOf(item)), el('small', [DIRECTION[item.analysis.direction], outlet(item), dateOf(item)].filter(Boolean).join(' · ')));
    m.append(body); m.onclick = () => openItem(item); list.append(m);
  }
}

const isRisk = i => ['risk', 'mixed'].includes(i.analysis.direction);
const isChance = i => ['opportunity', 'mixed'].includes(i.analysis.direction);

function renderKpis() {
  const box = $('homeKpis'); box.replaceChildren();
  const level = $('importanceFilter').value, day = Date.now() - 864e5;
  const fresh = items.filter(i => Date.parse(i.published || i.collected_at || '') >= day);
  const tiles = [
    [items.length, plural(items.length, 'важное событие', 'важных события', 'важных событий'), `важность от ${level} из 5`, ''],
    [items.filter(isRisk).length, plural(items.filter(isRisk).length, 'риск', 'риска', 'рисков'), 'для вашей компании', 'risk'],
    [items.filter(isChance).length, plural(items.filter(isChance).length, 'возможность', 'возможности', 'возможностей'), 'можно использовать', 'opportunity'],
    [fresh.length, plural(fresh.length, 'новое', 'новых', 'новых') + ' за сутки', 'опубликовано за 24 часа', 'bank'],
  ];
  for (const [n, label, hint, kind] of tiles) {
    const t = el('div', '', `kpi ${kind}`); t.append(el('b', String(n)), el('span', label), el('small', hint)); box.append(t);
  }
}

function renderActions() {
  const box = $('homeActions'); box.replaceChildren();
  const picked = [...items].sort((a, b) => b.analysis.importance - a.analysis.importance).filter(i => i.analysis.actions?.length).slice(0, 4);
  if (!picked.length) { box.append(el('li', 'Рекомендации появятся с первыми важными событиями.', 'muted')); return; }
  for (const item of picked) {
    const li = el('li'); li.append(el('p', item.analysis.actions[0]));
    const link = el('a', `${DIRECTION[item.analysis.direction] || 'Событие'}: ${titleOf(item)}`, 'home-link');
    link.href = '#/insights'; link.onclick = () => openItem(item);
    li.append(link); box.append(li);
  }
}

function renderAreas() {
  const box = $('homeAreas'); box.replaceChildren();
  const counts = new Map();
  for (const item of items) for (const area of new Set(item.analysis.areas || [])) {
    const c = counts.get(area) || { n: 0, risk: 0 }; c.n++; if (isRisk(item)) c.risk++; counts.set(area, c);
  }
  const rows = [...counts].sort((a, b) => b[1].n - a[1].n).slice(0, 6);
  if (!rows.length) { box.append(el('p', 'Пока нечего показать.', 'muted')); return; }
  const max = rows[0][1].n;
  for (const [area, c] of rows) {
    const row = el('div', '', 'area-row'), bar = el('div', '', 'area-bar'), fill = el('i'), risk = el('i', '', 'risk');
    fill.style.width = `${(c.n / max) * 100}%`; risk.style.width = `${(c.risk / max) * 100}%`;
    bar.append(fill, risk);
    row.append(el('span', area[0].toUpperCase() + area.slice(1), 'area-name'), bar, el('b', `${c.n}`));
    row.title = `${c.n} ${plural(c.n, 'событие', 'события', 'событий')}, из них рисков: ${c.risk}`;
    box.append(row);
  }
  const legend = el('div', '', 'area-legend'); legend.append(el('span', 'все события', 'all'), el('span', 'из них риски', 'risk')); box.append(legend);
}

function renderHomeCards() {
  const text = (current?.main || '').replace(/^#+.*$/gm, '').replace(/[*_`>]/g, '').replace(/\s+/g, ' ').trim();
  $('homeProfileText').textContent = text ? text.slice(0, 220) + (text.length > 220 ? '…' : '') : '';
  const box = $('homeSourceList'); box.replaceChildren();
  const chosen = new Set(current?.news_sources?.length ? current.news_sources : sourceList.filter(c => c.enabled).map(c => c.name));
  const mine = sourceList.filter(c => c.enabled && chosen.has(c.name));
  $('homeSources').textContent = mine.length ? `${mine.length} ${plural(mine.length, 'источник', 'источника', 'источников')} для этой компании` : 'Откуда подбирается лента';
  for (const c of mine) {
    const row = el('div', '', 'home-source');
    const state = c.paused ? 'paused' : c.last_status === 'error' ? 'error' : c.last_status === 'ok' ? 'ok' : '';
    row.append(el('i', '', `state ${state}`), el('span', sourceNames[c.name] || c.name), el('small', when(c.last_run_at)));
    row.title = c.paused ? 'на паузе' : STATUS[c.last_status] ?? c.last_status;
    box.append(row);
  }
}
$('homeSourcesCard').onclick = () => { if (tab !== 'sources') setTimeout(() => switchTab('sources')); };
const plural = (n, one, few, many) => n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many;

// ── события ─────────────────────────────────────────────────────────────────
$('clientSelect').onchange = e => selectClient(e.target.value);
$('importanceFilter').onchange = () => loadFeed(true);
// ── источники: коннекторы и выбор компании ─────────────────────────────────
let tab = 'feed', connectors = [], sourcesTimer = null;
const STATUS = { ok: 'успешно', error: 'ошибка', timeout: 'таймаут', running: 'идёт', '': 'ещё не запускался' };
const when = value => value ? new Date(value).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—';

async function post(path, body = {}) {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json(); if (!response.ok) throw Error(data.error || `HTTP ${response.status}`); return data;
}

function switchTab(next) {
  tab = next;
  for (const b of $('insightsTabs').children) b.classList.toggle('on', b.dataset.tab === tab);
  $('insightsFeed').hidden = tab !== 'feed'; $('insightsSources').hidden = tab !== 'sources';
  clearInterval(sourcesTimer); sourcesTimer = null;
  if (tab === 'sources') { renderSources(); sourcesTimer = setInterval(() => { if (!$('view-insights').hidden && tab === 'sources' && !$('sourceDialog').open) renderSources(); }, 10000); }
}

const KIND = { telegram_channel: 'Telegram', website: 'Сайт', pdf: 'PDF' };
const nameOf = c => (c.description || c.name).split(' — ')[0] || c.name;
const whereOf = c => (c.description || '').split(' — ').slice(1).join(' — ');
const every = s => !s ? 'вручную' : s >= 86400 ? 'раз в сутки' : s >= 3600 ? `каждые ${s / 3600} ч` : `каждые ${Math.round(s / 60)} мин`;

function setConnectors(list) {
  connectors = sourceList = list;
  sourceNames = Object.fromEntries(list.map(c => [c.name, nameOf(c)]));
}

async function renderSources() {
  try { setConnectors((await api('/api/insights/connectors')).connectors); } catch (error) { notice(error.message, true); return; }
  drawSources();
}

// Пустой список у компании — «все включённые источники».
const chosenSources = () => new Set(current?.news_sources?.length ? current.news_sources : connectors.filter(c => c.enabled).map(c => c.name));

async function saveSources(names) {
  await post(`/api/insights/profiles/${encodeURIComponent(current.id)}/sources`, { sources: names });
  current.news_sources = names; renderHomeCards();
}

function drawSources() {
  $('sourcesTitle').textContent = current ? `Источники для ${current.name}` : 'Источники';
  const chosen = chosenSources();
  const order = c => (c.enabled ? 0 : 2) + (chosen.has(c.name) ? 0 : 1);
  const head = el('div', '', 'source-row source-row-head');
  for (const text of ['', 'Источник', 'Новостей', 'Проверка', 'Последний сбор', '', 'В ленте']) head.append(el('span', text));
  $('connectors').replaceChildren(head, ...[...connectors].sort((a, b) => order(a) - order(b)).map(c => sourceRow(c, chosen)));
}

function sourceRow(c, chosen) {
  const on = c.enabled && chosen.has(c.name);
  const row = el('article', '', `source-row${on ? ' on' : ''}${c.enabled ? '' : ' off'}`);
  const title = el('div', '', 'source-title');
  title.append(el('h3', nameOf(c)), el('p', c.enabled ? whereOf(c) || c.name : 'Выключен на стенде', 'muted'));
  title.title = c.description;

  // Подписи ячеек видны только на узком экране, где нет шапки списка.
  const cell = (label, value, cls = '') => { const d = el('div', '', `source-cell ${cls}`); d.append(el('small', label), value); return d; };
  const status = c.paused ? 'paused' : c.last_status === 'error' || c.last_status === 'timeout' ? 'error' : c.last_status === 'ok' ? 'ok' : '';
  const last = el('span', '', 'source-last');
  last.append(el('i', '', `state ${status}`), el('span', c.last_run_at ? when(c.last_run_at) : 'ещё не было'));
  last.title = c.paused ? 'на паузе' : STATUS[c.last_status] ?? c.last_status;
  const actions = el('div', '', 'source-actions');
  if (c.custom) { const del = el('button', 'Удалить', 'link-button'); del.onclick = () => removeSource(c, del); actions.append(del); }

  row.append(el('span', KIND[c.kind] || c.kind, `kind kind-${c.kind}`), title,
    cell('Новостей', el('b', `${c.items_count}`)), cell('Проверка', el('span', every(c.interval_seconds))),
    cell('Последний сбор', last), actions, sourceSwitch(c, on));
  if (c.last_error && status === 'error') {
    const line = c.last_error.trim().split('\n').pop();
    const err = el('p', line.length > 200 ? line.slice(0, 200) + '…' : line, 'source-error'); err.title = c.last_error; row.append(err);
  }
  return row;
}

function sourceSwitch(c, on) {
  const label = el('label', '', 'switch'), input = el('input');
  input.type = 'checkbox'; input.checked = on; input.disabled = !c.enabled || !current;
  input.setAttribute('aria-label', `${nameOf(c)} в ленте компании`);
  label.title = !c.enabled ? 'Источник выключен на стенде' : on ? 'Выключить для этой компании' : 'Включить для этой компании';
  label.append(input, el('i'));
  input.onchange = async () => {
    const names = chosenSources();
    if (input.checked) names.add(c.name); else names.delete(c.name);
    const list = connectors.filter(x => x.enabled && names.has(x.name)).map(x => x.name);
    if (!list.length) { input.checked = true; return notice('Хотя бы один источник должен остаться включённым', true); }
    input.disabled = true;
    try { await saveSources(list); notice(`${nameOf(c)}: ${input.checked ? 'включён' : 'выключен'} для ${current.name}`); drawSources(); }
    catch (error) { input.checked = !input.checked; input.disabled = false; notice(error.message, true); }
  };
  return label;
}

async function removeSource(c, button) {
  if (!confirm(`Удалить источник «${nameOf(c)}»? Уже собранные новости останутся в ленте.`)) return;
  button.disabled = true;
  try {
    await post(`/api/insights/connectors/${encodeURIComponent(c.name)}/delete`);
    const rest = (current?.news_sources || []).filter(n => n !== c.name);
    if (rest.length && rest.length !== current.news_sources.length) await saveSources(rest);
    notice(`Источник «${nameOf(c)}» удалён`); await renderSources();
  } catch (error) { notice(error.message, true); button.disabled = false; }
}

// ── форма добавления источника ──────────────────────────────────────────────
const sourceForm = $('sourceForm'), fields = sourceForm.elements;
const TARGET = {
  telegram_channel: ['Канал', '@channel или https://t.me/channel', 'Публичный канал. Закрытые каналы, группы и ссылки‑приглашения не читаются.'],
  website: ['Адрес раздела новостей', 'https://example.ru/news/', 'Страница со списком новостей; RSS найдётся сам. Сайты, которые рисуются только JavaScript, не читаются.'],
};
function pickKind() {
  const [label, placeholder, hint] = TARGET[fields.kind.value];
  $('sourceTargetLabel').textContent = label; fields.target.placeholder = placeholder; $('sourceTargetHint').textContent = hint;
}
function formError(message) { $('sourceError').textContent = message; $('sourceError').hidden = !message; }
$('sourceAdd').onclick = () => { sourceForm.reset(); pickKind(); formError(''); $('sourceDialog').showModal(); fields.target.focus(); };
$('sourceClose').onclick = $('sourceCancel').onclick = () => $('sourceDialog').close();
sourceForm.onchange = e => { if (e.target.name === 'kind') { pickKind(); formError(''); } };
sourceForm.onsubmit = async e => {
  e.preventDefault();
  const body = { kind: fields.kind.value, target: fields.target.value.trim(), title: fields.title.value.trim(), interval_seconds: +fields.interval_seconds.value };
  if (!body.target) { fields.target.focus(); return formError(`Укажите: ${TARGET[body.kind][0].toLowerCase()}`); }
  const submit = $('sourceSubmit'); submit.disabled = true; submit.textContent = 'Добавляю…'; formError('');
  try {
    const c = await post('/api/insights/connectors', body);
    // У компании со своим списком новый источник включаем сразу; пустой список и так значит «все».
    if (current?.news_sources?.length) await saveSources([...current.news_sources, c.name]);
    $('sourceDialog').close();
    notice(`Источник «${nameOf(c)}» добавлен — первый сбор начнётся в течение минуты`);
    await renderSources();
  } catch (error) { formError(error.message); }
  finally { submit.disabled = false; submit.textContent = 'Добавить'; }
};

$('insightsTabs').onclick = e => { const b = e.target.closest('button'); if (b) switchTab(b.dataset.tab); };

$('directionFilter').onclick = e => {
  const b = e.target.closest('button'); if (!b) return;
  direction = b.dataset.direction;
  for (const x of $('directionFilter').children) x.classList.toggle('on', x === b);
  renderFeed();
};
$('burger').onclick = () => $('sidebar').classList.toggle('open');
window.addEventListener('hashchange', route);
// Экспорт мастера профиля: EventGraph подхватывает файл перед разбором — обновим список компаний.
window.addEventListener('profile-exported', () => setTimeout(loadProfiles, 3000));
loadProfiles();
route();
})();
