// Шестерёнка в шапке: настройки LLM (/api/settings/llm) поверх .env и выход.
(() => {
const $ = id => document.getElementById(id);
const PROVIDERS = {
  deepseek: { url: 'https://api.deepseek.com', model: 'deepseek-v4.1-flash' },
  openai: { url: 'https://api.openai.com/v1', model: 'gpt-5-mini' },
  openrouter: { url: 'https://openrouter.ai/api/v1', model: 'openai/gpt-5-mini' },
};
const FIELDS = ['base_url', 'model', 'vision', 'params', 'max_tokens', 'timeout'];
const dialog = $('settingsDialog'), form = $('settingsForm'), error = $('settingsError');
let state = null;

const input = name => form.elements[name];
const providerFor = url => Object.keys(PROVIDERS).find(k => PROVIDERS[k].url === url.replace(/\/+$/, '')) || 'custom';

async function call(path, body) {
  const response = await fetch(path, body === undefined ? {} :
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let data; try { data = await response.json(); } catch { throw Error('Не удалось прочитать ответ сервера'); }
  if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
  return data;
}

function showError(message) { error.textContent = message; error.hidden = !message; }

function render(data) {
  state = data;
  for (const name of FIELDS) {
    input(name).value = data.values[name] || '';
    const hint = form.querySelector(`[data-env="${name}"]`);
    const env = data.env[name];
    hint.textContent = data.overridden.includes(name)
      ? `Задано здесь · в .env: ${env || 'пусто'}`
      : (env ? 'Из .env' : 'В .env не задано');
  }
  $('llmProvider').value = providerFor(input('base_url').value);
  const key = data.api_key;
  $('llmApiKey').value = '';
  $('llmApiKey').placeholder = key.set ? 'Оставьте пустым, чтобы не менять' : 'Ключ не задан';
  $('llmKeyState').textContent = !key.set ? 'Не задан — агенты и чаты не работают'
    : `${key.source === 'gui' ? 'Задан здесь' : 'Из .env'}${key.hint ? ` (${key.hint})` : ''}`;
  $('llmKeyReset').checked = false;
  $('llmKeyReset').parentElement.hidden = key.source !== 'gui';
}

async function open() {
  showError('');
  dialog.showModal();
  try { render(await call('/api/settings/llm')); }
  catch (e) { showError(e.message); }
}

$('settingsOpen').addEventListener('click', open);
$('settingsClose').addEventListener('click', () => dialog.close());
$('llmProvider').addEventListener('change', event => {
  const preset = PROVIDERS[event.target.value];
  if (!preset) { input('base_url').focus(); return; }
  input('base_url').value = preset.url;
  if (!input('model').value || Object.values(PROVIDERS).some(p => p.model === input('model').value)) input('model').value = preset.model;
});
input('base_url').addEventListener('input', () => { $('llmProvider').value = providerFor(input('base_url').value); });

form.addEventListener('submit', async event => {
  event.preventDefault();
  showError('');
  const values = Object.fromEntries(FIELDS.map(name => [name, input(name).value.trim()]));
  const button = $('settingsSave'); button.disabled = true;
  try {
    render(await call('/api/settings/llm', { values, api_key: $('llmApiKey').value.trim(), reset_api_key: $('llmKeyReset').checked }));
    dialog.close();
  } catch (e) { showError(e.message); }
  finally { button.disabled = false; }
});

$('settingsReset').addEventListener('click', async () => {
  if (!confirm('Вернуть все настройки LLM к значениям из .env?')) return;
  showError('');
  try { render(await call('/api/settings/llm/reset', {})); } catch (e) { showError(e.message); }
});

$('settingsLogout').addEventListener('click', async () => {
  try { await call('/api/logout', {}); } finally { location.replace('/login'); }
});
})();
