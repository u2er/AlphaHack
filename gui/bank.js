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
const DEMO_TITLES = { accounts: 'Счета', payments: 'Платежи', salary: 'Зарплатный проект', credit: 'Кредиты' };

let profiles = [], current = null, items = [], selected = null, direction = '';

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
  else if (document.querySelector(`#view-profile ${CSS.escape(hash)}`)) view = 'profile';  // шаги мастера: #draft…
  if (!['home', 'insights', 'demo', 'profile'].includes(view)) view = 'home';
  for (const v of document.querySelectorAll('.view')) v.hidden = v.id !== `view-${view}`;
  for (const a of document.querySelectorAll('.menu a')) a.classList.toggle('active', a.getAttribute('href') === (view === 'demo' ? hash : `#/${view}`));
  $('sidebar').classList.remove('open');
  if (view === 'demo') $('demoTitle').textContent = DEMO_TITLES[sub] || 'Раздел';
  if (view === 'home' || view === 'insights') loadFeed();
  if (hash.startsWith('#/')) window.scrollTo(0, 0);
}

// ── компании (профили клиентов) ─────────────────────────────────────────────
async function loadProfiles() {
  const select = $('clientSelect');
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
  await loadFeed(true);
}

function renderProfileHeader() {
  const name = current?.name || 'компании';
  $('homeTitle').textContent = name;
  const hour = new Date().getHours();
  $('homeGreeting').textContent = hour < 6 ? 'Доброй ночи' : hour < 12 ? 'Доброе утро' : hour < 18 ? 'Добрый день' : 'Добрый вечер';
  $('insightsTitle').textContent = `Лента для ${name}`;
  $('insightsRole').textContent = current?.user_role ? `Отобрано под вашу роль: ${current.user_role}` : '';
  const topics = $('topics'); topics.replaceChildren();
  for (const t of current?.topics || []) {
    const chip = el('span', '', 'topic-chip'); chip.append(el('b', t.title));
    chip.title = [t.rationale, ...t.sources.map(s => `${s.filename}, стр. ${s.page} (${s.date_hint})`)].join('\n');
    topics.append(chip);
  }
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
  foot.append(el('span', [dateOf(item), item.connector].filter(Boolean).join(' · ')));
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
    const link = el('a', 'Заполнить профиль', 'button-link'); link.href = '#/profile'; box.append(link);
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
  d.append(el('div', [dateOf(item), item.connector].filter(Boolean).join(' · '), 'muted'), el('h2', titleOf(item)), meta);

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

  const src = [];
  if (item.url) { const link = el('a', item.title || item.url); link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; const p = el('p'); p.append(link); src.push(p); }
  else src.push(el('p', item.title || '—'));
  if (item.event) src.push(el('p', `Событие в графе: ${item.event}`, 'muted'));
  d.append(block('Первоисточник', ['Источник', 'who-src'], ...src));
  d.append(el('div', `Запись ${item.uid}${a.run_id ? ` · ран аналитика #${a.run_id}` : ''} — ход рассуждений в EventGraph: GET /agent-runs/${a.run_id || '…'}`, 'trace'));
}

// ── главная ─────────────────────────────────────────────────────────────────
function renderHome() {
  const list = $('entryList'); list.replaceChildren();
  const top = items.slice(0, 3);
  $('entryTitle').textContent = top.length ? `${items.length} ${plural(items.length, 'событие важно', 'события важны', 'событий важны')} для вашего бизнеса` : 'Что важно для вашего бизнеса сегодня';
  if (!top.length) { list.append(el('div', 'Новых важных событий нет — лента обновляется по мере поступления новостей.', 'mini')); return; }
  for (const item of top) {
    const m = el('div', '', 'mini'); m.append(dots(item.analysis.importance));
    const body = el('div'); body.append(el('b', titleOf(item)), el('small', `${DIRECTION[item.analysis.direction] || ''} · ${dateOf(item)}`));
    m.append(body); list.append(m);
  }
}
const plural = (n, one, few, many) => n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many;

// ── события ─────────────────────────────────────────────────────────────────
$('clientSelect').onchange = e => selectClient(e.target.value);
$('importanceFilter').onchange = () => loadFeed(true);
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
