// Чат добавления компании и просмотр страничного профиля. Весь текст из данных —
// через textContent; markdown страниц разбирается в DOM, без innerHTML.
(() => {
const $ = id => document.getElementById(id);
const el = (tag, text = '', className = '') => { const n = document.createElement(tag); if (text) n.textContent = text; if (className) n.className = className; return n; };
const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} } };

async function api(path, method = 'GET', body, headers = {}) {
  const options = { method, headers: { ...headers } };
  if (body instanceof Blob) options.body = body;
  else if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  let data; try { data = await response.json(); } catch { throw Error('Не удалось прочитать ответ сервера'); }
  if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
  return data;
}
function notice(message, error = false) {
  const box = $('notice'); box.textContent = message; box.className = 'notice' + (error ? ' error' : ''); box.hidden = false;
  clearTimeout(notice.t); notice.t = setTimeout(() => box.hidden = true, 6000);
}

// ── markdown → DOM ──────────────────────────────────────────────────────────
function inline(parent, text) {
  text.split(/(\*\*[^*]+\*\*)/).forEach(part => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) parent.append(el('strong', part.slice(2, -2)));
    else if (part) parent.append(document.createTextNode(part));
  });
}
function markdown(text) {
  const root = el('div', '', 'md');
  let list = null, tableRows = null, para = [];
  let nextOrderedNumber = 1, continueOrderedList = false;
  const flushTable = () => {
    if (!tableRows?.length) return;
    const isDivider = row => row.length && row.every(cell => /^:?-{3,}:?$/.test(cell));
    const hasHeader = tableRows.length > 1 && isDivider(tableRows[1]);
    const table = el('table', '', 'md-table');
    const bodyRows = hasHeader ? tableRows.slice(2) : tableRows;
    if (hasHeader) {
      const thead = el('thead'), tr = el('tr');
      tableRows[0].forEach(cell => { const th = el('th'); inline(th, cell); tr.append(th); });
      thead.append(tr); table.append(thead);
    }
    const tbody = el('tbody');
    for (const row of bodyRows) {
      const tr = el('tr');
      row.forEach(cell => { const td = el('td'); inline(td, cell); tr.append(td); });
      tbody.append(tr);
    }
    table.append(tbody);
    const wrap = el('div', '', 'md-table-wrap'); wrap.append(table); root.append(wrap);
    tableRows = null;
  };
  const flush = () => {
    if (para.length) { const p = el('p'); inline(p, para.join(' ')); root.append(p); para = []; }
    flushTable(); list = null;
  };
  const cells = line => {
    const value = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    return value.split('|').map(cell => cell.trim().replace(/\\\|/g, '|'));
  };
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trimEnd();
    if (!line.trim()) { flush(); continue; }
    if (line.trim().startsWith('|') && line.includes('|')) {
      if (para.length || list) flush();
      continueOrderedList = false;
      (tableRows ||= []).push(cells(line));
      continue;
    }
    if (tableRows) flushTable();
    const heading = line.match(/^#{1,6}\s+(.*)/);
    const bullet = line.match(/^\s*[-*•]\s+(.*)/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (heading) { flush(); continueOrderedList = false; root.append(el('h4', heading[1])); }
    else if (bullet || numbered) {
      if (para.length) { const p = el('p'); inline(p, para.join(' ')); root.append(p); para = []; }
      const tag = bullet ? 'ul' : 'ol';
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = el(tag);
        if (numbered) {
          const writtenNumber = Number(numbered[1]);
          const startNumber = continueOrderedList && writtenNumber === 1 ? nextOrderedNumber : writtenNumber;
          if (startNumber !== 1) list.start = startNumber;
          nextOrderedNumber = startNumber + 1;
          continueOrderedList = true;
        }
        root.append(list);
      } else if (numbered) {
        nextOrderedNumber++;
        continueOrderedList = true;
      }
      const li = el('li'); inline(li, (bullet || numbered)[1]); list.append(li);
    } else { list = null; continueOrderedList = false; para.push(line.trim()); }
  }
  flush();
  return root;
}

// ── страницы профиля: оглавление слева, страница справа ─────────────────────
const ORIGIN = { agent: 'агент', user: 'исправлено вами', file: 'документ', dataset: 'набор банка', dialog: 'ваши ответы', v1: 'мастер' };
function pager(box, { pages, load, editable = false, mainLimit = 4000, onSave }) {
  let selected = pages[0]?.id, editing = false;
  const toc = el('nav', '', 'pager-toc'), body = el('article', '', 'pager-body');
  const root = el('div', '', 'pager'); root.append(toc, body); box.replaceChildren(root);
  const renderToc = () => {
    toc.replaceChildren();
    pages.forEach(page => {
      const b = el('button', '', 'toc-item' + (page.id === selected ? ' on' : '')); b.type = 'button';
      b.append(el('b', page.title));
      const meta = [page.id === 'main' ? 'видят всегда' : '', ORIGIN[page.origin] || '', page.chars ? `${Math.round(page.chars / 1000 * 10) / 10} тыс. зн.` : ''].filter(Boolean).join(' · ');
      if (meta) b.append(el('small', meta));
      b.onclick = () => { if (editing && !confirm('Есть несохранённые правки. Уйти со страницы?')) return; selected = page.id; editing = false; render(); };
      toc.append(b);
    });
  };
  const render = async () => {
    renderToc();
    const page = pages.find(p => p.id === selected); if (!page) return;
    body.replaceChildren(el('div', '', 'skeleton'));
    let content;
    try { content = await load(page); } catch (error) { body.replaceChildren(el('p', error.message, 'muted')); return; }
    const head = el('div', '', 'pager-head'), titles = el('div');
    titles.append(el('h3', page.title)); if (page.summary) titles.append(el('p', page.summary, 'muted'));
    head.append(titles);
    if (editable && !editing) { const edit = el('button', 'Редактировать', 'secondary'); edit.onclick = () => { editing = true; render(); }; head.append(edit); }
    body.replaceChildren(head);
    if (!editing) {
      body.append(markdown(content));
      if (page.sources?.length) body.append(el('p', 'Источники: ' + page.sources.map(s => s.ref).join(', '), 'source-line'));
      return;
    }
    const title = el('input'); title.value = page.title; title.setAttribute('aria-label', 'Заголовок');
    const summary = el('input'); summary.value = page.summary || ''; summary.placeholder = 'Одна строка для оглавления'; summary.setAttribute('aria-label', 'Описание для оглавления');
    const area = el('textarea', '', 'page-editor'); area.value = content;
    const counter = el('small', '', 'muted');
    const count = () => { counter.textContent = page.id === 'main' ? `${area.value.length} / ${mainLimit} символов — основную страницу агенты видят всегда` : `${area.value.length} символов`; counter.className = page.id === 'main' && area.value.length > mainLimit ? 'warning' : 'muted'; };
    area.oninput = count; count();
    const row = el('div', '', 'buttonrow'), save = el('button', 'Сохранить'), cancel = el('button', 'Отмена', 'secondary');
    cancel.onclick = () => { editing = false; render(); };
    save.onclick = async () => {
      save.disabled = true;
      try { const updated = await onSave(page.id, { title: title.value, summary: summary.value, content: area.value }); Object.assign(page, updated); editing = false; render(); }
      catch (error) { notice(error.message, true); save.disabled = false; }
    };
    row.append(save, cancel);
    body.append(el('label', 'Заголовок', 'fieldlabel'), title, el('label', 'Описание в оглавлении', 'fieldlabel'), summary,
      el('label', 'Текст страницы (markdown)', 'fieldlabel'), area, counter, row);
  };
  render();
}

// ── чат ─────────────────────────────────────────────────────────────────────
const FORM = [
  { key: 'activity', label: 'Чем занимается компания', type: 'textarea', required: true, placeholder: 'Основные продукты и услуги, дополнительные направления' },
  { key: 'industry', label: 'Отрасль', type: 'text', placeholder: 'Например, металлургия, пищевая промышленность' },
  { key: 'revenue', label: 'Выручка за год', type: 'select', options: ['', 'до 1 млрд ₽', '1–15 млрд ₽', '15–50 млрд ₽', '50–200 млрд ₽', 'больше 200 млрд ₽'] },
  { key: 'employees', label: 'Численность', type: 'select', options: ['', 'до 100', '100–500', '500–2 000', '2 000–10 000', 'больше 10 000'] },
  { key: 'regions', label: 'Регионы работы и продаж', type: 'text', placeholder: 'Где производство, где покупатели' },
  { key: 'foreign_trade', label: 'Внешняя торговля', type: 'multi', options: ['Экспорт', 'Импорт сырья и комплектующих', 'Импорт оборудования', 'Нет'] },
  { key: 'counterparties', label: 'Ключевые клиенты и поставщики', type: 'textarea', placeholder: 'Кто покупает, у кого закупаете, от кого зависите' },
  { key: 'sensitivity', label: 'Что сильнее всего влияет на бизнес', type: 'multi', options: ['Ключевая ставка и кредиты', 'Курс валют', 'Цены на сырьё', 'Логистика', 'Спрос покупателей', 'Регулирование и налоги', 'Санкции и ограничения', 'Конкуренты', 'Энергия и тарифы'] },
  { key: 'role', label: 'Ваша роль', type: 'select', required: true, options: ['', 'Собственник', 'Генеральный директор', 'Финансовый директор', 'Главный бухгалтер', 'Руководитель казначейства', 'Другой руководитель'] },
  { key: 'decisions', label: 'Какие решения вы принимаете', type: 'text', placeholder: 'Например, кредиты, закупки, валютные операции' },
  { key: 'extra', label: 'Что ещё важно знать', type: 'textarea', placeholder: 'Планы, проекты, проблемы' },
];
const FILE_HINT = 'Годовой отчёт или бухгалтерская отчётность, презентация компании, каталог продукции или прайс, список ключевых контрагентов, кредитный портфель, стратегия. PDF (в том числе сканы), Word, Excel, PowerPoint, HTML, текст, CSV, картинки — до 30 МБ, не больше 10 файлов.';

let session = null, timer = null, rendered = 0, pendingFiles = [];
const base = () => `/api/onboarding/${session.id}`;

async function open() {
  if (session) return renderAll();
  const saved = store.get('alfa-onboarding');
  if (saved) {
    try { session = await api(`/api/onboarding/${saved}`); rendered = 0; $('chat').replaceChildren(); renderAll(); return poll(); }
    catch { store.set('alfa-onboarding', null); }
  }
  await restart();
}
async function restart() {
  stopPoll(); session = await api('/api/onboarding', 'POST', {}); store.set('alfa-onboarding', session.id);
  rendered = 0; pendingFiles = []; $('chat').replaceChildren(); $('chatReview').replaceChildren(); renderAll();
}
function poll() { if (!timer) timer = setInterval(refresh, 1500); }
function stopPoll() { clearInterval(timer); timer = null; }
async function refresh() {
  try { session = await api(base()); renderAll(); } catch (error) { stopPoll(); notice(error.message, true); }
  if (['review', 'confirmed', 'error', 'form'].includes(session?.status) && !session.files.some(f => f.status === 'parsing')) stopPoll();
}

function bubble(message) {
  const row = el('div', '', `msg msg-${message.role}`);
  if (message.role === 'activity') { row.append(el('span', '', 'dot-pulse'), el('span', message.text)); return row; }
  const b = el('div', '', 'bubble');
  if (message.kind === 'form') { b.append(formCard()); row.classList.add('msg-wide'); }
  else if (message.kind === 'question') {
    b.append(el('b', message.text));
    const q = message.question; if (q?.kind !== 'text') b.append(el('small', q.kind === 'multi' ? 'Можно выбрать несколько' : 'Выберите один вариант', 'muted'));
  } else if (message.kind === 'form_answer') { b.append(el('b', 'Анкета')); b.append(el('pre', message.text, 'plain')); }
  else if (message.kind === 'error') { b.classList.add('bubble-error'); b.textContent = message.text; }
  else b.textContent = message.text;
  row.append(b); return row;
}

function formCard() {
  const box = el('div', '', 'form-card');
  if (session.status !== 'form') { box.append(el('p', 'Анкета отправлена.', 'muted')); return box; }
  box.classList.add('form-live');  // редактируемая анкета: после отправки чат перерисуется один раз
  box.append(el('h3', 'Анкета компании'));
  // компания из набора банка или название
  const company = el('div', '', 'form-row'); company.append(el('label', 'Компания', 'fieldlabel'));
  const name = el('input'); name.id = 'f-company_name'; name.placeholder = 'Название или ID в наборе банка (U_…)'; name.autocomplete = 'off';
  const hits = el('div', '', 'typeahead'); const picked = el('input'); picked.type = 'hidden'; picked.id = 'f-company_id';
  let t = null;
  name.oninput = () => {
    picked.value = ''; clearTimeout(t);
    if (name.value.trim().length < 2) return hits.replaceChildren();
    t = setTimeout(async () => {
      try {
        const data = await api('/api/companies?q=' + encodeURIComponent(name.value.trim()));
        hits.replaceChildren(...data.companies.slice(0, 6).map(c => {
          const b = el('button', '', 'typeahead-item'); b.type = 'button';
          b.append(el('b', c.id), el('small', [c.industry, c.region].filter(Boolean).join(' · ')));
          b.onclick = () => { picked.value = c.id; name.value = c.id; hits.replaceChildren(el('small', `Выбрана компания из набора банка: ${c.id} — ${c.industry}`, 'muted')); };
          return b;
        }));
      } catch { hits.replaceChildren(); }
    }, 300);
  };
  company.append(name, picked, hits, el('small', 'Если компания есть в данных банка, выберите её — подтянем данные карточки и связи.', 'muted'));
  box.append(company);
  for (const field of FORM) {
    const row = el('div', '', 'form-row');
    row.append(el('label', field.label + (field.required ? ' *' : ''), 'fieldlabel'));
    let input;
    if (field.type === 'textarea') { input = el('textarea'); input.placeholder = field.placeholder || ''; input.rows = 2; }
    else if (field.type === 'select') { input = el('select'); for (const o of field.options) { const opt = el('option', o || '—'); opt.value = o; input.append(opt); } }
    else if (field.type === 'multi') {
      input = el('div', '', 'chips');
      for (const o of field.options) { const l = el('label', '', 'chip-check'); const c = el('input'); c.type = 'checkbox'; c.value = o; l.append(c, el('span', o)); input.append(l); }
    } else { input = el('input'); input.placeholder = field.placeholder || ''; }
    input.id = 'f-' + field.key; row.append(input); box.append(row);
  }
  // файлы
  const files = el('div', '', 'form-row'); files.append(el('label', 'Документы компании (необязательно)', 'fieldlabel'));
  const drop = el('label', '', 'dropzone'); const picker = el('input'); picker.type = 'file'; picker.multiple = true; picker.hidden = true;
  drop.append(picker, el('b', 'Перетащите файлы сюда или нажмите, чтобы выбрать'), el('small', FILE_HINT));
  const list = el('div', '', 'file-list'); list.id = 'fileList';
  const upload = async fileList => { for (const file of fileList) await sendFile(file); };
  picker.onchange = () => upload(picker.files);
  drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); upload(e.dataTransfer.files); };
  files.append(drop, list); box.append(files);
  const submit = el('button', 'Отправить анкету'); submit.onclick = () => submitForm(submit);
  box.append(submit);
  setTimeout(renderFiles);
  return box;
}

async function sendFile(file) {
  if (file.size > 30 * 1024 * 1024) return notice(`${file.name}: больше 30 МБ`, true);
  pendingFiles.push(file.name); renderFiles();
  try { await api(`${base()}/files`, 'POST', file, { 'X-Filename': encodeURIComponent(file.name) }); await refresh(); poll(); }
  catch (error) { notice(`${file.name}: ${error.message}`, true); }
  finally { pendingFiles = pendingFiles.filter(n => n !== file.name); renderFiles(); }
}
function renderFiles() {
  const list = $('fileList'); if (!list) return;
  const status = { parsing: 'разбираю…', ready: 'готов', error: 'ошибка' };
  list.replaceChildren(...session.files.map(f => {
    const row = el('div', '', `file-row file-${f.status}`);
    row.append(el('b', f.filename), el('small', `${f.kind} · ${Math.max(1, Math.round(f.size / 1024))} КБ · ${status[f.status]}${f.pages ? ` · ${f.pages} стр.` : ''}${f.error ? ` — ${f.error}` : ''}`));
    return row;
  }), ...pendingFiles.map(n => el('div', `${n}: загрузка…`, 'file-row file-parsing')));
}

async function submitForm(button) {
  const form = { company_name: $('f-company_name').value, company_id: $('f-company_id').value };
  for (const field of FORM) {
    const input = $('f-' + field.key);
    form[field.key] = field.type === 'multi' ? [...input.querySelectorAll('input:checked')].map(c => c.value) : input.value;
  }
  if (form.company_id) form.company_name = '';
  button.disabled = true;
  try { session = await api(`${base()}/form`, 'POST', { form }); rendered = 0; $('chat').replaceChildren(); renderAll(); poll(); }
  catch (error) { notice(error.message, true); button.disabled = false; }
}

function questionCard(q) {
  const card = el('div', '', 'question-card-chat');
  card.dataset.ref = q.ref || '';
  let own = null, ownText = null, answerArea = null;
  if (q.kind === 'text') { answerArea = el('textarea'); answerArea.rows = 3; answerArea.id = 'qAnswer'; card.append(answerArea); }
  else {
    const name = 'q-' + (q.ref || session.messages.length), type = q.kind === 'multi' ? 'checkbox' : 'radio';
    for (const option of q.options.filter(o => o.trim())) {
      const l = el('label', '', 'choice'); const i = el('input'); i.type = type; i.name = name; i.value = option;
      l.append(i, el('span', option)); card.append(l);
    }
    // «Своё» есть всегда: и в одиночном, и в множественном выборе.
    const l = el('label', '', 'choice choice-own'); own = el('input'); own.type = type; own.name = name;
    ownText = el('input'); ownText.placeholder = 'напишите свой вариант'; ownText.id = 'qOther';
    ownText.oninput = () => { own.checked = !!ownText.value.trim() || own.checked; };
    ownText.onfocus = () => { if (type === 'radio') own.checked = true; };
    l.append(own, el('span', 'Своё:'), ownText); card.append(l);
  }
  const row = el('div', '', 'buttonrow'), send = el('button', 'Ответить'), dunno = el('button', 'Не знаю', 'secondary');
  const submit = async value => {
    send.disabled = dunno.disabled = true;
    try { session = await api(`${base()}/answer`, 'POST', { value }); renderAll(); poll(); }
    catch (error) { notice(error.message, true); send.disabled = dunno.disabled = false; }
  };
  send.onclick = () => {
    if (q.kind === 'text') return submit($('qAnswer').value);
    const chosen = [...card.querySelectorAll('input:checked')].filter(i => i !== own).map(i => i.value);
    const text = ownText.value.trim();
    if (own.checked && !text) { ownText.focus(); return notice('Напишите свой вариант или снимите отметку «Своё»', true); }
    if (own.checked) chosen.push(text);
    submit(q.kind === 'multi' ? chosen : chosen[chosen.length - 1] || '');
  };
  answerArea?.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send.click();
    }
  });
  dunno.onclick = () => submit(q.kind === 'multi' ? ['не знаю'] : 'не знаю');
  row.append(send, dunno); card.append(row);
  return card;
}

function renderAll() {
  if (!session) return;
  const chat = $('chat');
  // Открытая анкета после отправки сменяется строкой «Анкета отправлена» — один раз.
  // Эта строка тоже .form-card, поэтому проверяем только редактируемую анкету: иначе
  // каждый опрос стирал бы чат вместе с отмеченными вариантами вопроса.
  if (chat.querySelector('.form-live') && session.status !== 'form') { rendered = 0; chat.replaceChildren(); }
  for (const message of session.messages.slice(rendered)) chat.append(bubble(message));
  rendered = session.messages.length;
  // Карточку вопроса не перерисовываем, пока вопрос тот же: опрос раз в 1,5 с
  // иначе сбрасывал бы отмеченные варианты и набранный текст.
  const card = chat.querySelector('.question-card-chat');
  const same = card && session.question && card.dataset.ref === (session.question.ref || '');
  if (card && !same) card.remove();
  chat.querySelector('.typing')?.remove();
  if (session.question && !same) chat.append(questionCard(session.question));
  else if (same && chat.lastElementChild !== card) chat.append(card);  // под новыми сообщениями
  else if (session.status === 'working') { const typing = el('div', '', 'msg msg-activity typing'); typing.append(el('span', '', 'dot-pulse'), el('span', 'Агент работает…')); chat.append(typing); }
  renderFiles();
  renderReview();
  if (rendered && document.activeElement?.tagName !== 'INPUT' && document.activeElement?.tagName !== 'TEXTAREA') chat.lastElementChild?.scrollIntoView({ block: 'nearest' });
}

let reviewFor = null;
function renderReview() {
  const box = $('chatReview');
  if (!session.profile || !['review', 'confirmed'].includes(session.status)) { box.replaceChildren(); reviewFor = null; return; }
  const key = session.status + session.profile.updated_at;
  if (reviewFor === key) return;
  reviewFor = key;
  const doc = session.profile, panel = el('div', '', 'panel review');
  const head = el('div', '', 'pager-head'); const t = el('div');
  t.append(el('h3', `Профиль: ${doc.name}`), el('p', session.status === 'confirmed' ? 'Подтверждён и передан в бизнес‑аналитику.' : 'Проверьте страницы — любую можно поправить. Основную страницу аналитик видит всегда.', 'muted'));
  head.append(t);
  if (session.status === 'review') {
    const ok = el('button', 'Подтвердить профиль');
    ok.onclick = async () => {
      ok.disabled = true;
      try { const result = await api(`${base()}/confirm`, 'POST', {}); window.dispatchEvent(new Event('profile-exported')); notice(result.published ? 'Профиль передан в бизнес‑аналитику' : 'Профиль сохранён'); await refresh(); }
      catch (error) { notice(error.message, true); ok.disabled = false; }
    };
    head.append(ok);
  } else { const go = el('a', 'Открыть бизнес‑аналитику', 'button-link'); go.href = '#/insights'; head.append(go); }
  const pagerBox = el('div');
  panel.append(head);
  if (session.status === 'confirmed') panel.append(sourcesCard(doc));
  panel.append(pagerBox); box.replaceChildren(panel);
  pager(pagerBox, {
    pages: doc.pages.map(p => ({ ...p, chars: p.content.length })), editable: session.status === 'review',
    mainLimit: session.main_limit, load: async page => page.content,
    onSave: async (id, changes) => {
      session = await api(`${base()}/pages/${encodeURIComponent(id)}`, 'POST', changes);
      reviewFor = session.status + session.profile.updated_at;  // не перерисовывать поверх открытого редактора
      const page = session.profile.pages.find(p => p.id === id); return { ...page, chars: page.content.length };
    },
  });
}

function sourcesCard(doc) {
  const card = el('div', '', 'sources-card');
  if (doc.news_sources?.length) {
    const text = el('p', doc.news_sources.join(', ') + ' — изменить можно во вкладке «Бизнес‑аналитика» → «Источники».', 'muted');
    card.append(el('b', 'Источники новостей'), text);
    api('/api/insights/connectors').then(data => {
      const names = Object.fromEntries(data.connectors.map(c => [c.name, c.description.split(' — ')[0] || c.name]));
      text.textContent = doc.news_sources.map(n => names[n] || n).join(', ') + ' — изменить можно во вкладке «Бизнес‑аналитика» → «Источники».';
    }).catch(() => {});
    return card;
  }
  card.append(el('h3', 'Откуда собирать новости для компании?'), el('p', 'Выберите источники — новости из остальных для этой компании не оцениваются.', 'muted'));
  const list = el('div', '', 'chips'); card.append(list, el('div', '', 'skeleton'));
  api('/api/insights/connectors').then(data => {
    card.querySelector('.skeleton')?.remove();
    for (const c of data.connectors.filter(c => c.enabled)) {
      const l = el('label', '', 'chip-check source-choice'); const i = el('input'); i.type = 'checkbox'; i.value = c.name; i.checked = true;
      const text = el('span'); text.append(el('b', c.description.split(' — ')[0] || c.name), el('small', c.description.split(' — ')[1] || ''));
      l.append(i, text); list.append(l);
    }
    const save = el('button', 'Сохранить источники');
    save.onclick = async () => {
      const names = [...list.querySelectorAll('input:checked')].map(i => i.value);
      save.disabled = true;
      try { session = await api(`${base()}/sources`, 'POST', { sources: names }); window.dispatchEvent(new Event('profile-exported')); reviewFor = null; renderAll(); }
      catch (error) { notice(error.message, true); save.disabled = false; }
    };
    card.append(save);
  }).catch(error => { card.querySelector('.skeleton')?.remove(); card.append(el('p', error.message, 'warning')); });
  return card;
}

$('chatRestart').onclick = () => { if (confirm('Начать добавление компании заново? Текущий диалог будет закрыт.')) restart().catch(e => notice(e.message, true)); };
window.Alfa = { markdown, pager, onboarding: { open: () => open().catch(e => notice(e.message, true)) } };
})();
