// Сессия кончилась (ключ сменили, кука истекла) — любой 401 от /api/ ведёт на вход.
(() => {
const original = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const response = await original(input, init);
  const url = typeof input === 'string' ? input : input.url;
  if (response.status === 401 && new URL(url, location.href).pathname.startsWith('/api/')) location.replace('/login');
  return response;
};
})();
