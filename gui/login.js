// Вход по ключу: сервер ставит HttpOnly-куку, дальше — главная.
(() => {
const form = document.getElementById('loginForm');
const input = document.getElementById('loginKey');
const error = document.getElementById('loginError');
const submit = document.getElementById('loginSubmit');
form.addEventListener('submit', async event => {
  event.preventDefault();
  error.hidden = true;
  if (!input.value.trim()) { error.textContent = 'Введите ключ доступа'; error.hidden = false; return; }
  submit.disabled = true; submit.textContent = 'Проверяем…';
  try {
    const response = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: input.value }) });
    if (response.ok) { location.replace('/'); return; }
    let message = `HTTP ${response.status}`;
    try { message = (await response.json()).error || message; } catch {}
    error.textContent = message; error.hidden = false;
    input.select();
  } catch {
    error.textContent = 'Сервер недоступен'; error.hidden = false;
  } finally {
    submit.disabled = false; submit.textContent = 'Войти';
  }
});
})();
