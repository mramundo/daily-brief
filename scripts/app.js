/* =========================================================
   Daily Brief — App entry point
   Wires up: theme, data fetch, hero, news, markets.
   ========================================================= */

import { renderHero, renderNews } from './news.js';
import { renderMarkets } from './prices.js';

const $ = (sel, root = document) => root.querySelector(sel);

const CONFIG = {
  newsUrl: 'data/news.json',
  pricesUrl: 'data/prices.json',
  seedNewsUrl: 'data/news.seed.json',
  seedPricesUrl: 'data/prices.seed.json',
};

const LOCALE = 'en-US';

const PALETTES = ['newsprint', 'broadsheet', 'modern-mono', 'modern-red', 'financial', 'gazette'];
const MODES    = ['light', 'dark'];

function initTheme() {
  const root = document.documentElement;

  let storedPalette = localStorage.getItem('db-palette');
  if (!PALETTES.includes(storedPalette)) storedPalette = 'newsprint';

  let storedMode = localStorage.getItem('db-mode');
  if (!MODES.includes(storedMode)) storedMode = 'light';

  applyTheme(storedPalette, storedMode);

  $('#themeToggle')?.addEventListener('click', () => {
    const next = root.dataset.mode === 'light' ? 'dark' : 'light';
    applyTheme(root.dataset.palette, next);
    localStorage.setItem('db-mode', next);
  });

  const toggle = $('#paletteToggle');
  const menu   = $('#paletteMenu');
  if (toggle && menu) {
    const options = [...menu.querySelectorAll('.palette-picker__option')];

    const markCurrent = (palette) => {
      options.forEach(o => o.setAttribute('aria-current',
        o.dataset.palette === palette ? 'true' : 'false'));
    };
    markCurrent(storedPalette);

    const closeMenu = () => { menu.hidden = true; toggle.setAttribute('aria-expanded', 'false'); };
    const openMenu  = () => { menu.hidden = false; toggle.setAttribute('aria-expanded', 'true'); };

    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      menu.hidden ? openMenu() : closeMenu();
    });

    options.forEach(opt => {
      opt.addEventListener('click', (e) => {
        e.stopPropagation();
        const palette = opt.dataset.palette;
        if (!PALETTES.includes(palette)) return;
        applyTheme(palette, root.dataset.mode);
        localStorage.setItem('db-palette', palette);
        markCurrent(palette);
        closeMenu();
      });
    });

    document.addEventListener('click', (e) => {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== toggle) closeMenu();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !menu.hidden) closeMenu();
    });
  }
}

function applyTheme(palette, mode) {
  const root = document.documentElement;
  root.dataset.palette = palette;
  root.dataset.mode = mode;
}

async function fetchJSON(url) {
  try {
    const res = await fetch(url, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`${res.status}`);
    return await res.json();
  } catch (err) {
    console.warn(`[data] fetch failed for ${url}:`, err.message);
    return null;
  }
}

async function loadData() {
  const [news, prices] = await Promise.all([
    fetchJSON(CONFIG.newsUrl).then(d => d ?? fetchJSON(CONFIG.seedNewsUrl)),
    fetchJSON(CONFIG.pricesUrl).then(d => d ?? fetchJSON(CONFIG.seedPricesUrl)),
  ]);
  return {
    news: news ?? { updated: null, hero: null, items: [] },
    prices: prices ?? { updated: null, categories: {} },
  };
}

const fmtDateTime = (iso) => {
  if (!iso) return '—';
  try {
    return new Intl.DateTimeFormat(LOCALE, {
      day: '2-digit', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    }).format(new Date(iso));
  } catch { return '—'; }
};

const fmtEdition = (iso) => {
  const d = iso ? new Date(iso) : new Date();
  try {
    return new Intl.DateTimeFormat(LOCALE, {
      weekday: 'long', day: '2-digit', month: 'long', year: 'numeric',
    }).format(d);
  } catch { return '—'; }
};

function setMeta({ news, prices }) {
  const latest = [news.updated, prices.updated]
    .filter(Boolean)
    .sort()
    .pop();
  const txt = latest ? fmtDateTime(latest) : '—';
  const lastUpd = $('#lastUpdate');
  const footUpd = $('#footerUpdate');
  const edition = $('#brandEdition');
  if (lastUpd) lastUpd.textContent = `Updated ${txt}`;
  if (footUpd) footUpd.textContent = txt;
  if (edition) edition.textContent = fmtEdition(news.updated);
  const yr = $('#year');
  if (yr) yr.textContent = new Date().getFullYear();
}

(async function boot() {
  initTheme();

  const data = await loadData();

  setMeta(data);
  renderHero(data.news);
  renderNews(data.news);
  renderMarkets(data.prices);
})();
