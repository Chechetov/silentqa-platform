// Тема оформления. Применяется ДО отрисовки body (скрипт синхронный, в <head>),
// иначе при выборе светлой темы страница мигнёт тёмным фоном.
(function () {
  const saved = localStorage.getItem('sqa_theme');
  const theme = saved === 'light' || saved === 'dark'
    ? saved
    : (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.dataset.theme = theme;
})();

// Глобальная — вызывается из onclick тумблера в .sidebar-footer.
function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('sqa_theme', next);
  const btn = document.getElementById('themeToggle');
  if (btn) btn.textContent = next === 'light' ? '🌙 Тёмная' : '☀️ Светлая';
}

// Подпись кнопки по факту готовности DOM: в разметке дефолт «☀️ Светлая»
// (предложение переключиться на светлую) — если уже светлая, показываем обратное.
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('themeToggle');
  if (btn && document.documentElement.dataset.theme === 'light') btn.textContent = '🌙 Тёмная';
});
