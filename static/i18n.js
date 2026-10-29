/* Barrier Lens languages. The English text in index.html and app.js is the key; static/i18n/<lang>.json maps it to
   the translation (scripts/i18n_strings.py lists every key). Missing entries stay English. No build step.
   ?lang=ko opens a language by link; the menu choice is kept in this browser. Default: English. */
window.I18N = (() => {
  const LANGS = [
    ['en', 'English'], ['ko', '한국어'], ['ja', '日本語'], ['zh-CN', '简体中文'], ['zh-TW', '繁體中文'],
    ['es', 'Español'], ['fr', 'Français'], ['de', 'Deutsch'], ['vi', 'Tiếng Việt'], ['id', 'Bahasa Indonesia'],
  ];
  const KEY = 'bl-lang';
  const known = (v) => (LANGS.some(([id]) => id === v) ? v : null);
  let stored = null;
  try { stored = localStorage.getItem(KEY); } catch { /* private mode */ }
  const lang = known(new URLSearchParams(location.search).get('lang')) || known(stored) || 'en';
  document.documentElement.lang = lang;

  let dict = {};
  let templates = [];  // keys with {name} slots, as anchored regexes, for text the server wrote
  const fill = (s, v) => (v ? s.replace(/\{(\w+)\}/g, (m, k) => (k in v ? v[k] : m)) : s);
  const glue = (s) => String(s).replace(/(\d) (cm|m|km|mm|MB)(?![A-Za-z])/g, '$1\u00a0$2');  // a number never wraps away from its unit
  const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  function compile(d) {
    return Object.keys(d).filter((k) => /\{\w+\}/.test(k) && k.replace(/\{\w+\}/g, '').trim()).map((k) => {
      const names = [];
      const re = k.split(/(\{\w+\})/).map((p) => {
        const m = /^\{(\w+)\}$/.exec(p);
        if (!m) return reEsc(p);
        names.push(m[1]);
        return '(.+?)';
      }).join('');
      return { k, names, re: new RegExp(`^${re}$`, 's') };
    });
  }
  const ready = lang === 'en' ? Promise.resolve()
    : fetch(`/static/i18n/${lang}.json`).then((r) => (r.ok ? r.json() : {}))
      .then((d) => { dict = d; templates = compile(d); }).catch(() => { /* stays English */ });

  // Text this page wrote: the English source, translated, with {name} slots filled.
  function t(src, vars) { return glue(fill(dict[src] ?? src, vars)); }

  // Text the server or a model wrote: the whole text, else a {name} template, else sentence by sentence.
  function one(s) {
    if (dict[s] != null) return dict[s];
    for (const tp of templates) {
      const m = tp.re.exec(s);
      if (m) return fill(dict[tp.k], Object.fromEntries(tp.names.map((n, i) => [n, tr(m[i + 1])])));
    }
    return null;
  }
  function tr(text) {
    if (lang === 'en' || !text) return text && glue(text);
    const s = String(text).trim();
    const whole = one(s);
    if (whole != null) return glue(whole);
    const parts = s.split(/(?<=[.!?])\s+(?=[A-Z“"(])/);
    if (parts.length < 2) return glue(s);
    const join = /^(ja|zh)/.test(lang) ? '' : ' ';
    return glue(parts.map((p) => one(p) ?? p).join(join));
  }

  // Static markup: data-i18n (text), data-i18n-html (our own markup with <b>, <br>), data-i18n-attr="aria-label title".
  function apply(root = document) {
    if (lang === 'en') return;
    root.querySelectorAll('[data-i18n]').forEach((el) => {
      el.dataset.i18n ||= el.textContent.trim().replace(/\s+/g, ' ');
      el.textContent = t(el.dataset.i18n);
    });
    root.querySelectorAll('[data-i18n-html]').forEach((el) => {
      el.dataset.i18nHtml ||= el.innerHTML.trim().replace(/\s+/g, ' ');
      el.innerHTML = t(el.dataset.i18nHtml);
    });
    root.querySelectorAll('[data-i18n-attr]').forEach((el) => {
      el.dataset.i18nAttr.split(' ').forEach((a) => { if (el.hasAttribute(a)) el.setAttribute(a, t(el.getAttribute(a))); });
    });
  }

  function set(id) {
    if (!known(id) || id === lang) return;
    try { localStorage.setItem(KEY, id); sessionStorage.setItem('bl-intro-skip-once', '1'); } catch { /* private mode */ }
    const u = new URL(location.href);
    if (u.searchParams.has('lang')) u.searchParams.set('lang', id);
    if (u.href !== location.href) location.replace(u.href); else location.reload();
  }

  const num = (n, o) => Number(n).toLocaleString(lang, o);
  return { LANGS, lang, ready, t, tr, apply, set, num };
})();
