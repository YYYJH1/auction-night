// Reads ?theme=light|dark and ?lang=en|zh from the address and puts them on <html>.
// The build script waits for window.__ready before it takes the picture. Open a figure in a
// browser with the same query string to preview it.
(() => {
  const query = new URLSearchParams(window.location.search);
  const pick = (name, allowed, fallback) => (allowed.includes(query.get(name)) ? query.get(name) : fallback);
  const root = document.documentElement;
  root.dataset.theme = pick('theme', ['light', 'dark'], 'light');
  root.dataset.lang = pick('lang', ['en', 'zh'], 'en');
  root.lang = root.dataset.lang === 'zh' ? 'zh-CN' : 'en';
  window.__ready = false;
  window.addEventListener('load', () => {
    document.fonts.ready.then(() => {
      window.__ready = true;
    });
  });
})();
